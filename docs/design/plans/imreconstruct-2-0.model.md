# ImReconstruct 2.0 — Model Layer Audit

**Date:** 2026-05-29  
**Scope:** `imswitch/imreconstruct/model/`  
**Purpose:** Classify every model file as generic, MoNaLISA-specific, or mixed to inform the upcoming Reconstructor plugin interface design for Milestone 12.

## Summary

The ImReconstruct model layer contains **7 implementation files** (1,373 LOC total, excluding `__init__.py`). Of these:

- **3 files are generic** (DataObj, Denoiser, UNet/UNetRCAN): reusable data I/O, denoising infrastructure, and PyTorch models.
- **2 files are MoNaLISA-specific** (PatternFinder, SignalExtractor): tightly coupled to periodic hexagonal pattern detection and Windows-only CUDA reconstruction DLL.
- **1 file is mixed** (ReconObj): generic coefficient storage with hardcoded MoNaLISA dimension semantics (`Right/Left`, `Up/Down`, `Back/Forth`).
- **1 file has duplicated code** (UNet.py and UNetRCAN.py share 140+ lines of identical helper functions and layer definitions).

**Critical coupling:**  
SignalExtractor is **Windows-only** (L19-21, L26-31) and depends on a proprietary `GPU_acc_recon.dll` that is MoNaLISA reconstruction-specific. PatternFinder assumes a periodic 2D pattern with FFT-based peak detection suitable for structured illumination but not modality-agnostic. ReconObj's `coeffsToImage` method (L85-172) hardcodes MoNaLISA scan dimension labels and raster reassignment logic, preventing reuse for other modalities without interface changes.

---

## File-by-File Classification

### 1. DataObj.py

**Classification:** 🟢 **GENERIC**  
**LOC:** 187  
**Public API:**
- `class DataObj` (L11-170)
  - `__init__(name, datasetName, *, path=None, file=None)` (L12)
  - Properties: `data`, `attrs`, `dataLoaded`, `datasetName`, `numFrames` (L25-63)
  - Methods: `checkAndLoadData()`, `checkAndUnloadData()`, `getMeanData()` (L65-93)
  - Static: `getDatasetNames(path)`, `_open(path, datasetName, allowMultipleDatasets)` (L95-137)
  - Utilities: `describesSameAs(other)`, `checkLock()`, `checkModifTime(minDiffTime)` (L139-169)

**MoNaLISA-coupled symbols:** None

**Reuse verdict:** **Keep as-is (shared infrastructure)**

**Notes:**  
Pure I/O abstraction supporting HDF5 (`h5py`), TIFF (`tifffile`), and Zarr formats (L30-36, L112-135). No assumptions about array dimensionality, scan pattern, or modality. The `checkLock()` and `checkModifTime()` methods (L150-169) provide file synchronization guards suitable for any multi-step reconstruction workflow. Lazy-loading pattern (L26-36) is reusable. No refactoring needed.

---

### 2. Denoiser.py

**Classification:** 🟢 **GENERIC**  
**LOC:** 128  
**Public API:**
- `class Denoiser` (L9-128)
  - `__init__()` (L10)
  - `init_model(model_name: str, model_type: str)` (L33)
  - `load_model(model_name: str)` (L64)
  - `predict(data, crop_size, pad=True, clip_neg=True)` (L86)

**MoNaLISA-coupled symbols:** None

**Reuse verdict:** **Keep as-is (shared infrastructure)**

**Notes:**  
Generic denoising service supporting UNet and UNetRCAN architectures (L53-61). Model configuration loaded from JSON (L40-51), state dict from `.pt` file (L68-83). The `predict()` method (L86-128) operates on arbitrary `(frames, height, width)` data with configurable center-cropping and padding. PyTorch optional (L13-28, graceful fallback). No scan pattern or modality assumptions. Imports `UNet.UNet_PosEncod` and `UNetRCAN.UNetRCAN` (L16-17) but only as generic architecture choices. Model storage path uses `getSystemUserDir()` (L11), making it user-agnostic. Fully reusable.

---

### 3. PatternFinder.py

**Classification:** 🔴 **MONALISA-SPECIFIC**  
**LOC:** 117  
**Public API:**
- `class PatternFinder` (L6-100)
  - `findPattern(image)` → `[offsetVert, offsetHori, optPerVertPx, optPerHoriPx]` (L7-81)
  - `findBestPeak(peaks)` (L83-99)

**MoNaLISA-coupled symbols:**
- **Entire `findPattern` method** (L7-81): assumes a **periodic 2D grid pattern** detectable via FFT peak analysis in both horizontal and vertical directions. Specific to structured illumination microscopy (SIM) or parallelized scanning geometries.
  - Thresholding logic (L9-11) and mean projection (L16-17) are generic but the FFT-based period detection (L19-34) assumes regular spacing.
  - Gaussian fitting of FFT peaks (L46-67) to sub-pixel precision is specific to periodic patterns.
  - Phase extraction via complex FFT values (L69-79) assumes sinusoidal modulation.

**Reuse verdict:** **Move to `reconstructors/monalisa/` or extract interface**

**Notes:**  
This is a **pattern recognition algorithm tailored to MoNaLISA's hexagonal or grid illumination pattern**. While the FFT approach could generalize to other SIM modalities, the current implementation has no configuration for non-periodic patterns, sparse patterns, or random access scanning. The `findBestPeak` logic (L83-99) has a potential division-by-zero bug (L91: `(height1 - height2) / (height1 - height2)` should be `(height1 + height2)`). For a generic reconstructor framework, this should either:
1. Move to a MoNaLISA-specific reconstructor plugin, OR
2. Be abstracted behind a `PatternRecognizer` interface with modality-specific implementations.

---

### 4. ReconObj.py

**Classification:** 🟡 **MIXED**  
**LOC:** 189  
**Public API:**
- `class ReconObj` (L6-173)
  - `__init__(name, scanParDict, r_l_text, u_d_text, b_f_text, timepoints_text, p_text, n_text)` (L7)
  - Getters: `getDispLevels()`, `getReconstruction()`, `getCoeffs()`, `getScanParams()` (L29-39)
  - Setters: `setDispLevels(levels)`, `updateScanParams(scanParDict)`, `updateReconstructed(new_img)` (L26, L54, L72)
  - Core: `addCoeffsTP(inCoeffs)`, `updateImages()`, `coeffsToImage(coeffs, scanParDict)` (L41, L57, L85)

**MoNaLISA-coupled symbols:**
- **Constructor parameters** `r_l_text, u_d_text, b_f_text` (L7): hardcoded to `'Right/Left'`, `'Up/Down'`, `'Back/Forth'` in `ImRecMainView.py`. These are **MoNaLISA-specific dimension labels** for raster scanning (L12-17).
- **`coeffsToImage` method** (L85-172): entire method is **MoNaLISA scan geometry hardcoded**:
  - Expects `scanParDict['dimensions']` to be a 4-element list containing the three spatial labels plus timepoints (L88-103).
  - Hardcoded dimension index lookups (L98-102) using the text labels.
  - Bidirectional scan correction (L116-118) assumes galvo mirror raster scanning.
  - Direction inversion logic (L120-127) assumes positive/negative scan directions.
  - Complex dimension permutation logic (L130-169) to map `(fast, mid, slow, time)` indices into `(timepoint, slice, row, col)` based on which label is assigned to which axis — **this is a MoNaLISA raster reassignment algorithm**.

**Generic parts:**
- `addCoeffsTP` (L41-52): generic coefficient accumulation into 4D numpy array.
- `updateImages` (L57-70): loops over datasets and bases, calling `coeffsToImage` — loop structure is generic.
- `addGridOfCoeffs` (L75-83): numpy array assignment — generic.
- Display level storage (L24, L26-30): generic metadata.

**Reuse verdict:** **Extract interface, implement MoNaLISA-specific subclass**

**Notes:**  
The coefficient storage and management is **generic** and reusable. The `coeffsToImage` reconstruction logic is **entirely MoNaLISA-specific**. Recommended approach:
1. Define a `ReconstructorBase` class with generic coefficient storage, `addCoeffsTP`, display levels.
2. Make `coeffsToImage` an abstract method.
3. Implement `MoNaLISAReconstructor(ReconstructorBase)` with the current `coeffsToImage` logic.
4. Controller passes dimension labels and scan parameters via the reconstructor-specific API.

---

### 5. SignalExtractor.py

**Classification:** 🔴 **MONALISA-SPECIFIC**  
**LOC:** 132  
**Public API:**
- `class SignalExtractor` (L10-115)
  - `__init__()` (L16)
  - `extractSignal(data, sigmas, pattern, dev)` (L69)
  - Helper: `make3dPtrArray(inData)`, `make4dPtrArray(inData)` (L33, L48)

**MoNaLISA-coupled symbols:**
- **Entire class is Windows-only** (L19-21): `if os.name != 'nt': raise RuntimeError`
- **Hardcoded CUDA DLL dependency** (L26-31):
  - Loads `cudart64_90.dll` (CUDA 9.0 runtime) from `dirtools.DataFileDirs.Libs` (L26-28).
  - Loads `GPU_acc_recon.dll` (L29-31), a **proprietary MoNaLISA reconstruction library** containing CUDA kernels for pattern-based signal extraction.
- **`extractSignal` method** (L69-115):
  - Takes `pattern` as a 4-element float array (L76-78) — specific to MoNaLISA's pattern parameters (offsets and periods from PatternFinder).
  - Takes `sigmas` (basis function widths) as input (L79-82) — MoNaLISA Gaussian basis assumption.
  - Calls DLL functions `calc_coeff_grid_size` (L89-93) and `extract_signal_GPU/CPU` (L102-111) — **black-box MoNaLISA reconstruction**.
  - Returns 4D coefficient array (bases, frames, gridRows, gridCols) (L96-97) — specific to MoNaLISA's multi-basis decomposition.

**Reuse verdict:** **Move to `reconstructors/monalisa/`**

**Notes:**  
This is the **most tightly coupled** component. It cannot be reused for any other modality without replacing the DLL. The Windows-only restriction (L19-21) is acceptable for a modality-specific plugin but blocks cross-platform use of the entire imreconstruct module today. **Action for Milestone 12:** Move this class into a `reconstructors.monalisa` plugin and make it **optional** so ImReconstruct can load without it. Provide a fallback or raise a clear error if MoNaLISA reconstruction is attempted on non-Windows or without the DLL.

---

### 6. UNet.py

**Classification:** 🟢 **GENERIC**  
**LOC:** 297  
**Public API:**
- **Functions:** `swish(x)`, `getDivisors(n)`, `find_closest_divisor(a, b)`, `normalize(norm_type, channels, groups)`, `get_timestep_embedding(timesteps, embedding_dim)` (L7-64)
- **Layers:** `conv_block`, `downsamp_block`, `upsamp_block` (L67-149)
- **Model:** `class UNet_PosEncod(nn.Module)` (L153-296)

**MoNaLISA-coupled symbols:** None

**Reuse verdict:** **Keep as-is (shared infrastructure)** — but note code duplication with UNetRCAN.py

**Notes:**  
Pure PyTorch U-Net implementation with optional positional encoding (timestep embedding L46-64, L252-267) and configurable upsampling (before/after, convolutional/interpolation, L235-246). Accepts a `UNet_prm` dictionary with hyperparameters (L163-179), making it fully reusable for any image-to-image task. No scan pattern or modality assumptions. **Code smell:** Functions `swish`, `getDivisors`, `find_closest_divisor`, `normalize` (L7-43) and layer classes `conv_block`, `downsamp_block`, `upsamp_block` (L67-149) are **duplicated verbatim** in `UNetRCAN.py` (L4-118). Recommend extracting these into a `unet_layers.py` helper module.

---

### 7. UNetRCAN.py

**Classification:** 🟢 **GENERIC**  
**LOC:** 323  
**Public API:**
- **Duplicate helpers:** `swish`, `getDivisors`, `find_closest_divisor`, `normalize` (L4-39) — **same as UNet.py L7-43**
- **Duplicate layers:** `conv_block`, `downsamp_block`, `upsamp_block` (L41-118) — **same as UNet.py L67-149**
- **RCAN components:** `ChannelAttention`, `RCAB`, `RG`, `RCAN` (L121-179)
- **Models:** `class UNet(nn.Module)` (L182-298), `class UNetRCAN(nn.Module)` (L301-323)

**MoNaLISA-coupled symbols:** None

**Reuse verdict:** **Keep as-is (shared infrastructure)** — but consolidate duplicated code

**Notes:**  
Generic PyTorch implementation of RCAN (Residual Channel Attention Network) and a UNet variant with optional channel attention in skip connections (L233-251, L289-292). The `UNetRCAN` class (L301-323) combines both architectures for denoising/super-resolution tasks. Fully reusable. **Code duplication issue:** Lines 4-118 are **140+ lines of exact duplication** from `UNet.py`. This violates DRY and creates maintenance burden. **Recommended action:**
1. Create `imswitch/imreconstruct/model/unet_layers.py` with shared helpers and layer classes.
2. Import in both `UNet.py` and `UNetRCAN.py`.
3. Document that these are generic denoising architectures available for any reconstructor.

---

## Cross-File Coupling Analysis

### Call Graph (Controller → Model)

From `ImRecMainViewController.py`:
```
ImRecMainViewController.__init__():
    self._signalExtractor = SignalExtractor()  # MoNaLISA-specific, Windows-only
    self._patternFinder = PatternFinder()      # MoNaLISA-specific FFT pattern detection
    self._denoiser = Denoiser()                # Generic

ImRecMainViewController.reconstructDataset():
    dataObj = DataObj(...)                     # Generic I/O
    pattern = self._patternFinder.findPattern(dataObj.getMeanData())  # MoNaLISA pattern
    coeffs = self._signalExtractor.extractSignal(data, sigmas, pattern, dev)  # MoNaLISA DLL
    reconObj = ReconObj(name, scanParDict, r_l_text, u_d_text, b_f_text, ...)  # Mixed
    reconObj.addCoeffsTP(coeffs)               # Generic storage
    reconObj.updateImages()                    # Calls MoNaLISA-specific coeffsToImage
    denoised = self._denoiser.predict(...)     # Generic denoising (optional)
    reconObj.updateReconstructed(denoised)     # Generic setter
```

### Dependency Chains

1. **MoNaLISA reconstruction pipeline** (current hardcoded flow):
   - `DataObj` (generic) → `PatternFinder.findPattern()` (MoNaLISA) → `SignalExtractor.extractSignal()` (MoNaLISA, Windows-only) → `ReconObj.addCoeffsTP()` (generic) → `ReconObj.coeffsToImage()` (MoNaLISA) → `Denoiser.predict()` (generic, optional)

2. **Shared infrastructure** (no coupling):
   - `DataObj` ↔ no dependencies within model layer
   - `Denoiser` → imports `UNet.UNet_PosEncod` and `UNetRCAN.UNetRCAN` (both generic)
   - `UNet.py` ↔ no internal dependencies (pure PyTorch)
   - `UNetRCAN.py` ↔ no internal dependencies (pure PyTorch)

3. **Circular/tight coupling** (design smell):
   - `ReconObj` does **not** import `PatternFinder` or `SignalExtractor` but **assumes their output formats** (4-element pattern array, 4D coefficient array with specific axis order). This is **implicit coupling via data shape contracts** rather than explicit interface.

### Assumptions Leaked Across Files

| File | Leaked Assumption | Source/Evidence |
|---|---|---|
| `ReconObj.py` | Coefficients are 4D: `(frames, bases, gridRows, gridCols)` where frames must equal `steps[0] * steps[1] * steps[2] * steps[3]` | L88-95 |
| `ReconObj.py` | Scan has exactly 4 dimensions: 3 spatial + 1 temporal | L88-92, L98-102 |
| `ReconObj.py` | Dimension labels are `r_l_text`, `u_d_text`, `b_f_text`, `timepoints_text` | L98-102, L130-169 |
| `ReconObj.py` | Scan may be bidirectional (galvo raster) | L116-118 |
| `SignalExtractor.py` | Pattern is 4 floats: `[offsetVert, offsetHori, periodVert, periodHori]` | L76-78 |
| `SignalExtractor.py` | Data is 3D: `(frames, rows, cols)` | L33-46, L85-87 |
| `SignalExtractor.py` | Sigmas are Gaussian basis widths for multi-basis decomposition | L79-82 |
| `PatternFinder.py` | Input is a 2D periodic pattern with detectable FFT peaks | L7-81 |

**Critical insight:** The **4-element pattern array** from `PatternFinder` and **4D coefficient array** from `SignalExtractor` are **MoNaLISA data contracts** that `ReconObj` implicitly depends on. No abstract interface exists today — the controller simply passes raw arrays between components.

---

## Open Questions

1. **What is the full MoNaLISA pattern contract?**  
   - `PatternFinder.findPattern()` returns `[offsetVert, offsetHori, periodVert, periodHori]` but what are the units (pixels? microns?)?  
   - What happens if the pattern is not detected (peaks too weak, no periodicity)? Current code has no error handling for this case.

2. **Can SignalExtractor be made cross-platform?**  
   - Is the `GPU_acc_recon.dll` source available? Could it be reimplemented in CUDA/PyTorch/Numba for portability?  
   - If not, should there be a CPU-only fallback using NumPy? The DLL has `extract_signal_CPU` (L102) — does it have the same Windows-only limitation?

3. **What other reconstruction modalities are planned?**  
   - Does widefield/deconvolution need pattern detection? (No → PatternFinder not needed)  
   - Does STORM/PALM need grid reassignment? (No → `coeffsToImage` logic not needed)  
   - Does confocal just need denoising? (Yes → only `Denoiser` + `DataObj` needed)

4. **Should dimension labels be modality config or hard-coded?**  
   - Current approach: `r_l_text`, `u_d_text`, `b_f_text` are set in `ImRecMainView.py` (L12-14) and passed through constructor.  
   - Alternative: make them part of a `MoNaLISAConfig` dataclass and let other modalities define their own dimension semantics.

5. **Is the `scanParDict` format stable?**  
   - `ReconObj.coeffsToImage` expects keys: `steps`, `dimensions`, `directions`, `unidirectional` (L88-127).  
   - Is this a MoNaLISA-specific scan parameter schema or a general ImSwitch scan format?  
   - If modality-agnostic, document it. If MoNaLISA-specific, make it explicit.

6. **Code duplication: consolidate now or later?**  
   - `UNet.py` and `UNetRCAN.py` share 140+ lines. Fix during Milestone 12 or defer?  
   - Low risk to consolidate (purely internal, no API change) but adds work to this milestone.

---

## Recommendations for Milestone 12

### Immediate Actions (Required for Plugin Interface Design)

1. **Move MoNaLISA-specific code to a plugin:**
   - Create `imswitch/imreconstruct/reconstructors/monalisa/` package.
   - Move `PatternFinder`, `SignalExtractor`, and the MoNaLISA-specific parts of `ReconObj` (`coeffsToImage` logic).
   - Define a `MoNaLISAReconstructor` class that encapsulates the full pipeline.

2. **Define abstract reconstructor interface:**
   ```python
   class ReconstructorBase(ABC):
       @abstractmethod
       def reconstruct(self, data_obj: DataObj, params: dict) -> ReconstructionResult:
           """Reconstruct images from raw data."""
   
       @abstractmethod
       def get_required_params(self) -> dict:
           """Return parameter schema for this reconstructor."""
   ```

3. **Keep shared infrastructure in `model/`:**
   - `DataObj` (I/O)
   - `Denoiser` (optional denoising step, usable by any reconstructor)
   - `UNet`, `UNetRCAN` (model architectures for denoising)

4. **Consolidate duplicated code:**
   - Extract `swish`, `getDivisors`, `find_closest_divisor`, `normalize`, `conv_block`, `downsamp_block`, `upsamp_block` into `unet_layers.py`.

### Future Work (Post-Milestone 12)

5. **Make SignalExtractor optional:**
   - Catch import failure gracefully in `MoNaLISAReconstructor`.
   - Raise a descriptive error if reconstruction is attempted without the DLL: `"MoNaLISA reconstruction requires GPU_acc_recon.dll (Windows-only)"`

6. **Cross-platform reconstruction:**
   - Investigate PyTorch/Numba reimplementation of the signal extraction kernel.
   - If DLL source is unavailable, document the Windows-only limitation prominently.

7. **Add other reconstructors:**
   - Widefield deconvolution (Richardson-Lucy, Wiener filter)
   - STORM/PALM localization + rendering
   - Confocal denoising-only (just `DataObj` → `Denoiser`)

---

## Appendix: File Statistics

| File | LOC | Classes | Functions | Classification |
|---|---:|---:|---:|---|
| `DataObj.py` | 187 | 1 | 2 static, 6 instance | 🟢 Generic |
| `Denoiser.py` | 128 | 1 | 3 | 🟢 Generic |
| `PatternFinder.py` | 117 | 1 | 2 | 🔴 MoNaLISA |
| `ReconObj.py` | 189 | 1 | 7 | 🟡 Mixed |
| `SignalExtractor.py` | 132 | 1 | 3 | 🔴 MoNaLISA |
| `UNet.py` | 297 | 4 classes | 5 functions | 🟢 Generic |
| `UNetRCAN.py` | 323 | 8 classes | 3 functions | 🟢 Generic |
| **Total** | **1,373** | **14** | **23** | **3 generic / 2 specific / 1 mixed** |

---

**End of audit.**
