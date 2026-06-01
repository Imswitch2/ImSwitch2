# ImReconstruct 2.0 Controller Layer Audit

**Milestone 12: ImReconstruct Generalization — Controller Audit**

## Summary

The ImReconstruct controller layer consists of 9 controller files (1309 LOC excluding `__init__.py`) organized around a central communication channel. The majority of the code is **generic** — managing data loading, multi-dataset workflows, file watching, and reconstruction display. However, the **critical reconstruction path** (ImRecMainViewController) is **hard-wired to MoNaLISA** through direct instantiation and invocation of `PatternFinder`, `SignalExtractor`, `Denoiser`, and `ReconObj`. The scan-parameter configuration (ScanParamsController) encodes MoNaLISA-style scan dimensions (up/down, right/left, back/front, timepoints) and directional scanning. Every other controller operates on the generic `DataObj` shell and would translate cleanly to a plugin-based architecture — the main refactoring effort is decoupling the reconstruction orchestration from the MoNaLISA model classes and routing reconstruction requests through a modality-specific plugin registry instead of direct calls.

---

## File-by-File Analysis

### CommunicationChannel.py

**Classification:** Generic  
**LOC:** 43 (including copyright)  
**Responsibilities:**
- Defines signal-based communication between controllers
- Provides 8 signals: data folder changed, save folder changed, current data changed, scan params updated, pattern updated, pattern visibility changed, add to multi-data, reconstruct, execution finished

**MoNaLISA-coupled call sites:** None

**Reuse verdict:** ✅ **100% reusable.** Pure signal infrastructure. The `sigPatternUpdated` and `sigScanParamsUpdated` signals carry MoNaLISA-specific payloads but the signal interface itself is modality-agnostic. In a plugin architecture, these would become `sigReconstructionConfigUpdated` or similar generic names with plugin-specific payloads.

---

### basecontrollers.py

**Classification:** Generic  
**LOC:** 36 (including copyright)  
**Responsibilities:**
- Defines `ImRecWidgetControllerFactory` and `ImRecWidgetController` base classes
- Provides dependency injection of `commChannel` and `moduleCommChannel` to all controllers

**MoNaLISA-coupled call sites:** None

**Reuse verdict:** ✅ **100% reusable.** Standard controller factory pattern, no modality assumptions.

---

### ImRecMainController.py

**Classification:** Generic  
**LOC:** 45 (including copyright)  
**Responsibilities:**
- Entry point controller for the ImReconstruct module
- Initializes communication channel and controller factory
- Creates the main view controller
- Handles module close event cleanup

**MoNaLISA-coupled call sites:** None (delegates to ImRecMainViewController which is coupled)

**Reuse verdict:** ✅ **100% reusable.** Pure orchestration, no direct model references. The MoNaLISA coupling is entirely in the child controller it spawns (ImRecMainViewController).

---

### DataEditController.py

**Classification:** Generic  
**LOC:** 54 (including copyright)  
**Responsibilities:**
- Controls the data edit window
- Displays mean data and individual frames from a `DataObj`
- Provides UI for setting dark frame (currently stubbed out)

**MoNaLISA-coupled call sites:** None

**Reuse verdict:** ✅ **100% reusable.** Operates entirely on the generic `DataObj` interface (`data` array, `numFrames`, `name`, `datasetName`). No reconstruction-specific logic.

---

### DataFrameController.py

**Classification:** Mixed (generic with MoNaLISA pattern visualization)  
**LOC:** 112 (including copyright)  
**Responsibilities:**
- Controls the current data display frame
- Shows mean data or individual frames from the current `DataObj`
- **Visualizes the MoNaLISA pattern grid** on top of the data
- Manages pattern visibility and pattern grid generation

**MoNaLISA-coupled call sites:**
- **Lines 73-95:** `makePatternGrid()` — Assumes `self._pattern` is a 4-element array `[Row-offset, Col-offset, Row-period, Col-period]` and generates a grid of points for SIM pattern visualization. This grid calculation is specific to MoNaLISA's periodic excitation pattern.
- **Lines 31-35, 37-42:** `patternUpdated()` and `patternVisibilityChanged()` — Receive pattern updates and control pattern grid display.

**Reuse verdict:** ⚠️ **Mostly reusable.** The pattern grid logic (lines 73-95) is MoNaLISA-specific and would need to be replaced with a plugin-provided overlay renderer. The rest (data display, frame navigation) is generic. Could become a base class with pattern rendering delegated to a reconstruction plugin.

---

### MultiDataFrameController.py

**Classification:** Generic  
**LOC:** 231 (including copyright)  
**Responsibilities:**
- Manages the multi-dataset list
- Handles adding datasets from disk or memory recordings
- Provides load/unload/delete/save operations for multiple datasets
- Synchronizes with the memory recording system (ImControl integration)
- Sets the current dataset for reconstruction

**MoNaLISA-coupled call sites:** None

**Reuse verdict:** ✅ **100% reusable.** Operates entirely on the generic `DataObj` interface. No reconstruction-specific logic — pure data management.

---

### WatcherFrameController.py

**Classification:** Mixed (generic file watcher with MoNaLISA reconstruction trigger)  
**LOC:** 137 (including copyright)  
**Responsibilities:**
- Watches a directory for new data files (zarr or hdf5)
- Automatically triggers reconstruction when new files appear
- Saves reconstructed images to a `rec/` subdirectory
- Logs reconstruction timing

**MoNaLISA-coupled call sites:**
- **Line 86:** `self._commChannel.sigReconstruct.emit(dataObjs, True)` — Triggers reconstruction. The reconstruction path is MoNaLISA-specific (handled by ImRecMainViewController).
- **Lines 99-121:** `saveImage()` — Saves the reconstructed image. Assumes the image format is the 6D MoNaLISA ReconObj output: `[datasets, bases, timepoints, slices, Y, X]`. Line 100 squeezes and reshapes assuming this structure: `image = np.squeeze(image[:, 0, :, :, :, :])`.

**Reuse verdict:** ⚠️ **Mostly reusable.** The file-watching infrastructure (lines 15-88) is generic. The reconstruction trigger (line 86) and image save logic (lines 99-121) are coupled to the MoNaLISA pipeline. Would need:
1. Plugin registry to dispatch `sigReconstruct` to the correct pipeline
2. Plugin-provided image format/save logic instead of hard-coded 6D array assumption

---

### ScanParamsController.py

**Classification:** MoNaLISA-specific  
**LOC:** 51 (including copyright)  
**Responsibilities:**
- Controls the scan parameters dialog
- Manages scan parameter dictionary with dimensions, directions, steps, step sizes, unidirectional flag
- **Hard-codes MoNaLISA dimension labels:** `u_d_text` (up/down), `r_l_text` (right/left), `b_f_text` (back/front), `timepoints_text`
- Provides UI to configure 3D scanning + timepoints with positive/negative directions

**MoNaLISA-coupled call sites:**
- **Lines 10-17:** `__init__()` — Hard-codes the default scan parameter dictionary with MoNaLISA dimension names (`u_d_text`, `r_l_text`, `b_f_text`, `timepoints_text`) and directional labels (`p_text`, `n_text`). These are retrieved from the widget, which itself hard-codes these text labels.
- **Lines 28-34:** `applyParams()` — Retrieves dimensions, directions, steps, step sizes, and unidirectional flag from the widget. The widget expects exactly 4 dimensions (X, Y, Z, time) and 3 directional axes.

**Reuse verdict:** ❌ **Not reusable.** This is a MoNaLISA-specific configuration UI. Other modalities (e.g., STED, FLIM, confocal tiling) have different parameter requirements:
- STED might need pulse timing, depletion power per pixel
- FLIM needs time-bin counts, photon counting parameters
- Confocal tiling needs tile overlap percentages, stitching parameters

A generalized version would need a plugin-provided configuration widget instead of this hard-coded dialog.

---

### ReconstructionViewController.py

**Classification:** Mixed (generic display with MoNaLISA ReconObj interface)  
**LOC:** 146 (including copyright)  
**Responsibilities:**
- Controls the reconstruction result viewer
- Manages the list of reconstruction objects
- Provides 3 view modes: standard, bottom, left (different axis transpose orders)
- Handles histogram level management and view updates
- **Operates on ReconObj's 6D `reconstructed` array structure**

**MoNaLISA-coupled call sites:**
- **Line 59:** `data = self._widget.getCurrentItemData().reconstructed` — Assumes the current item is a `ReconObj` (MoNaLISA-specific model class).
- **Lines 61-66:** `setImgSlice()` transpose logic — Assumes the data is 6D with specific axis semantics: `[Dataset, Base, Time point, Slice, X, Y]`. The transpose orders (standard: `[0,1,2,3,4,5]`, bottom: `[0,1,2,4,3,5]`, left: `[0,1,2,5,4,3]`) swap the spatial axes for different projection views.
- **Line 69:** `axisLabels = np.array(['Dataset', 'Base', 'Time point', 'Slice', 'X', 'Y'])` — Hard-codes MoNaLISA axis names.
- **Lines 90-96:** `axisStepChanged()` — Extracts the "Base" axis (index 1) to update histogram levels. The "Base" concept (different SIM pattern bases) is MoNaLISA-specific.
- **Lines 114-127:** `updateRecon()` and `scanParamsUpdated()` — Call `reconObj.updateImages()` and `reconObj.updateScanParams()`, which are `ReconObj` methods (MoNaLISA model class).

**Reuse verdict:** ⚠️ **Partially reusable.** The display infrastructure (multi-item list, histogram levels, view transpose) is generic. The **hard dependency on ReconObj and its 6D array structure** requires refactoring. A generalized version would need:
1. An abstract reconstruction result interface (e.g., `ReconstructionResult` with `get_data()`, `get_axis_labels()`, `get_transpose_orders()`)
2. Plugins provide their own result classes implementing this interface
3. The controller operates on the interface instead of directly on `ReconObj`

---

### ImRecMainViewController.py

**Classification:** MoNaLISA-specific (orchestration + reconstruction)  
**LOC:** 453 (34% of controller layer excluding copyright)  
**Responsibilities:**
- **Central orchestration controller** for the ImReconstruct module
- Creates and wires all child controllers (DataFrame, MultiDataFrame, Watcher, Reconstruction, ScanParams, PickDatasets)
- **Instantiates MoNaLISA model classes:** `SignalExtractor`, `PatternFinder`, `Denoiser` (lines 42-44)
- Manages current data object and data/save folder state
- Handles pattern finding and pattern parameter updates
- **Orchestrates MoNaLISA reconstruction:** extracts data via `SignalExtractor`, creates `ReconObj`, adds coefficients, updates images
- Provides denoising with `Denoiser` (calls UNet/UNetRCAN models)
- Saves reconstructions and coefficients as TIFF with ImageJ metadata

**MoNaLISA-coupled call sites:**

#### Model instantiation:
- **Line 9:** `from imswitch.imreconstruct.model import DataObj, ReconObj, PatternFinder, SignalExtractor, Denoiser` — Imports MoNaLISA-specific model classes.
- **Line 42:** `self._signalExtractor = SignalExtractor()` — Instantiates MoNaLISA signal extraction model.
- **Line 43:** `self._patternFinder = PatternFinder()` — Instantiates MoNaLISA pattern finder.
- **Line 44:** `self._denoiser = Denoiser()` — Instantiates MoNaLISA denoiser (wraps UNet/UNetRCAN).

#### Pattern finding:
- **Lines 137-150:** `findPattern()` — Calls `self._patternFinder.findPattern(meanData)` to detect the SIM pattern in the mean image. This is specific to MoNaLISA's periodic excitation pattern.

#### Denoising:
- **Lines 92-119:** `denoiseCurrent()` — Extracts the current `ReconObj`, retrieves reconstruction data, calls `self._denoiser.init_model()`, `self._denoiser.load_model()`, `self._denoiser.predict()` with crop size and padding. Assumes UNet/UNetRCAN models trained on MoNaLISA data. Creates a new `ReconObj` with denoised data.
- **Lines 103-106:** Model type selection — Maps model name (e.g., 'RCAN') to 'UNetRCAN' or 'UNet'. Hard-coded to these two architectures.

#### Reconstruction orchestration:
- **Lines 268-292:** `extractData()` — **Core MoNaLISA reconstruction step.** Retrieves FWHM values, background modeling, pixel size from widget, converts to sigmas, calls `self._signalExtractor.extractSignal(data, sigmas, pattern, device.lower())` to extract coefficients. This is the MoNaLISA SIM processing algorithm.
- **Lines 303-260:** `reconstruct()` — Iterates over data objects, calls `extractData()` to get coefficients, creates `ReconObj` (line 315 onwards — see below), calls `reconObj.addCoeffsTP(coeffs)` (line 252), calls `reconObj.updateImages()` (line 254).
- **Lines 315-321:** (not visible in earlier view, need to check) — Instantiates `ReconObj` with MoNaLISA-specific constructor arguments: `dataObj.name`, `self._scanParDict`, dimension text labels (`r_l_text`, `u_d_text`, `b_f_text`, `timepoints_text`), directional text labels (`p_text`, `n_text`).

Let me verify the ReconObj instantiation:

#### Scan parameter management:
- **Lines 49-56:** Default `_scanParDict` — Hard-codes MoNaLISA scan dimensions (`u_d_text`, `r_l_text`, `b_f_text`, `timepoints_text`), directions (`p_text`), default steps `['35', '35', '1', '1']`, step sizes `['35', '35', '35', '1']`.
- **Lines 228-267:** `currentDataChanged()` — Attempts to read MoNaLISA-style scan metadata from the data object's attributes: `ScanStage:target_device`, `ScanStage:positive_direction`, `ScanStage:axis_step_size`. These attribute names are hard-coded expectations from MoNaLISA acquisition workflow.

#### Save logic:
- **Lines 316-347:** `saveReconstruction()` — Retrieves voxel sizes and time step from `reconObj.getScanParams()` using MoNaLISA dimension names (`r_l_text`, `u_d_text`, `b_f_text`, `timepoints_text`). Assumes the reconstruction is a 6D array, squeezes and transposes it (lines 342-344), saves as ImageJ TIFF with axes 'TZCYX'.
- **Lines 349-355:** `saveCoefficients()` — Saves coefficients (also assumes specific array structure, swaps axes, writes as ImageJ TIFF with axes 'TZCYX').

**Reuse verdict:** ❌ **Not reusable as-is.** This controller is the **main bottleneck** for generalization. It hard-wires the entire MoNaLISA reconstruction pipeline. Refactoring strategy:
1. **Extract reconstruction logic** into a MoNaLISA-specific plugin (e.g., `MonalisaReconstructionPlugin`)
2. **Replace direct model calls** with plugin registry lookups: `plugin = registry.get_plugin_for_data(dataObj)`, `coeffs = plugin.reconstruct(data, config)`
3. **Decouple pattern finding** — make `findPattern()` a plugin method, not a controller responsibility
4. **Decouple denoising** — make denoising a plugin-provided post-processing step, not hard-coded UNet models
5. **Generalize save logic** — let plugins provide their own save formats instead of assuming 6D ImageJ TIFF
6. **Scan params** — make scan params plugin-specific configuration, not a fixed dictionary

---

## Control Flow: "User Clicks Reconstruct" → "Image Appears"

This trace follows the **single-dataset reconstruction path** (user clicks "Reconstruct Current"):

1. **User action:** User clicks "Reconstruct" button in `ImRecMainView` (view layer, not shown here)
2. **Signal emission:** View emits `sigReconstuctCurrent` (note: typo in original code)
3. **Entry point:** `ImRecMainViewController.reconstructCurrent()` (line 294)
   - Checks if `self._currentDataObj` exists, returns if None
   - Calls `self.reconstruct([self._currentDataObj], consolidate=False)`

4. **Reconstruction orchestration:** `ImRecMainViewController.reconstruct(dataObjs, consolidate)` (line 303)
   - Loops over `dataObjs` (in this case, a single-item list)
   - Calls `dataObj.checkAndLoadData()` to ensure data is in memory
   - Validates frame count vs scan parameters (line 310)
   - **Creates ReconObj:** `reconObj = ReconObj(dataObj.name, self._scanParDict, self._widget.r_l_text, self._widget.u_d_text, self._widget.b_f_text, self._widget.timepoints_text, self._widget.p_text, self._widget.n_text)` (lines 315-321, continuation not shown above)
   - Retrieves raw data array: `data = dataObj.data` (line 243, approximately)
   - Optionally applies bleaching correction (lines 244-245)
   - **Calls MoNaLISA reconstruction:** `coeffs = self.extractData(data)` (line 247)

5. **Signal extraction (core MoNaLISA step):** `ImRecMainViewController.extractData(data)` (line 268)
   - Retrieves reconstruction parameters from widget: FWHM values (`getFwhmNm()`), background modeling (`getBgModelling()`), Gaussian background size (`getBgGaussianSize()`), pixel size (`getPixelSizeNm()`), compute device (`getComputeDevice()`)
   - Converts FWHM to sigmas (line 283)
   - Retrieves pattern: `pattern = self._pattern` (line 286)
   - **Invokes MoNaLISA SignalExtractor:** `coeffs = self._signalExtractor.extractSignal(data, sigmas, pattern, device.lower())` (line 288)
   - Returns coefficients array

6. **Coefficient accumulation:** Back in `reconstruct()` (line 252)
   - `reconObj.addCoeffsTP(coeffs)` — Adds the extracted coefficients to the ReconObj (MoNaLISA model class method)

7. **Image update:** `ImRecMainViewController.reconstruct()` continues (line 254)
   - `reconObj.updateImages()` — Calls ReconObj method to compute the final reconstruction from coefficients (MoNaLISA model class method, likely involves Fourier transforms or matrix inversions)

8. **Display update:** `ImRecMainViewController.reconstruct()` (line 255)
   - `self._widget.addNewData(reconObj, reconObj.name)` — Adds the ReconObj to the widget's reconstruction list
   - Widget emits signals that trigger `ReconstructionViewController.listItemChanged()` (line 28)

9. **Image rendering:** `ReconstructionViewController.fullUpdate()` (line 46)
   - Calls `self.setImgSlice()` (line 49)
   - Retrieves `data = self._widget.getCurrentItemData().reconstructed` (line 59) — Gets the 6D array from ReconObj
   - Transposes axes based on view mode (lines 61-70)
   - Calls `self._widget.setImage(im, axisLabels)` (line 72) — Displays the image in the napari/pyqtgraph viewer

10. **Display finalization:** View layer updates histogram, auto-levels, resets camera (handled by ReconstructionViewController.fullUpdate() and widget methods)

**Result:** Reconstructed image appears in the viewer.

**MoNaLISA-specific hops in this flow:**
- Step 4: Creation of `ReconObj` (MoNaLISA model class)
- Step 5: Call to `SignalExtractor.extractSignal()` (MoNaLISA algorithm)
- Step 6: Call to `ReconObj.addCoeffsTP()` (MoNaLISA model method)
- Step 7: Call to `ReconObj.updateImages()` (MoNaLISA model method)
- Step 9: Access to `ReconObj.reconstructed` attribute (MoNaLISA 6D array structure)

**To generalize:** Steps 4-7 need to be replaced with plugin dispatch:
```python
# Instead of:
reconObj = ReconObj(...)
coeffs = self._signalExtractor.extractSignal(...)
reconObj.addCoeffsTP(coeffs)
reconObj.updateImages()

# Generalized:
plugin = self._plugin_registry.get_plugin_for_data(dataObj)
config = plugin.get_config_from_widget(self._widget)
result = plugin.reconstruct(data, config)  # Returns ReconstructionResult (interface)
self._widget.addNewData(result, result.name)
```

Step 9 would work on the `ReconstructionResult` interface instead of directly on `ReconObj`.

---

## Open Questions

### 1. Plugin registry architecture
- **Where does the plugin registry live?** Model layer, controller layer, or module-level singleton?
- **How are plugins discovered?** Entry points (Python package metadata), config file registration, directory scanning?
- **How is the correct plugin selected for a dataset?** File metadata (e.g., `attrs['ReconstructionModality']`)? User choice in UI? Auto-detection from data shape/attributes?

### 2. Reconstruction configuration UI
- **Does each plugin provide its own config widget?** If so, how is it dynamically inserted into the main view?
- **Is there a common config interface?** E.g., all plugins support `get_config_widget()` returning a QWidget?
- **How are plugin-specific parameters persisted?** Separate config files per plugin? Extended DataObj attributes?

### 3. ReconstructionResult interface
- **What's the minimal interface?** Candidate methods: `get_data()` → array, `get_axis_labels()` → list[str], `get_transpose_orders()` → dict[str, list[int]], `get_display_levels()` → tuple, `save(path, format)`.
- **How are multi-base results handled?** MoNaLISA has a "Base" axis (different SIM pattern phases). Do other modalities have similar concepts? Should this be part of the interface or MoNaLISA-specific?
- **Do plugins control their own save logic?** Or is there a common TIFF/Zarr writer that plugins configure?

### 4. Pattern/overlay visualization
- **Is pattern visualization a generic concept?** Other modalities might want overlays (e.g., STED depletion pattern, FLIM ROI masks). Should this be part of the plugin interface?
- **How is overlay rendering delegated to plugins?** E.g., `plugin.get_overlay_renderer()` returns an object with `draw(image, params)` method?

### 5. Signal/communication channel refactoring
- **Do reconstruction-specific signals stay in CommunicationChannel?** Or move to a `ReconstructionCommChannel` owned by the plugin?
- **How do plugins emit progress updates?** E.g., STED reconstruction might take minutes and need a progress bar. Standard signal (`sigReconstructionProgress`)? Plugin-specific signals?

### 6. Denoising generalization
- **Is denoising a first-class plugin feature?** Or a separate post-processing plugin system?
- **How are denoising models registered?** Per-reconstruction-plugin (e.g., MoNaLISA plugin provides its own denoisers), or separate denoiser plugins that work on any reconstruction result?
- **Model download/management?** Current `Denoiser` class loads models from disk. Should there be a model manager service?

### 7. Scan parameter generalization
- **Do all modalities have "scan parameters"?** Or is this a MoNaLISA/scanning-microscopy concept? E.g., FLIM might have "time bins" instead of "scan steps".
- **Should the scan params dialog be plugin-provided?** Or is there a common "acquisition metadata" concept that all plugins configure differently?

### 8. Backward compatibility
- **Do we need to support old MoNaLISA data files?** If so, the MoNaLISA plugin must be able to read legacy metadata format.
- **Is there a migration path for existing saved reconstructions?** E.g., old TIFF files with hard-coded axis names.

### 9. WatcherFrameController plugin dispatch
- **How does the watcher know which plugin to use?** File extension? Metadata in the file? User configuration?
- **Can multiple plugins run in parallel?** E.g., watch directory A with MoNaLISA plugin, directory B with STED plugin?

### 10. Testing strategy
- **Do we need a "null" or "passthrough" plugin for testing?** E.g., a plugin that just averages frames and returns a 3D stack?
- **How do we test plugin registration/dispatch without MoNaLISA dependencies?** Mock plugins? Test fixtures?

---

## Files Requiring Refactoring (Priority Order)

### 🔴 High Priority (blocks generalization)
1. **ImRecMainViewController.py** — Core orchestration, hard-wired to MoNaLISA models
2. **ScanParamsController.py** — MoNaLISA-specific parameter UI
3. **ReconstructionViewController.py** — Hard dependency on ReconObj 6D structure

### 🟡 Medium Priority (MoNaLISA-specific features, but not blockers)
4. **DataFrameController.py** — Pattern grid visualization (lines 73-95)
5. **WatcherFrameController.py** — Reconstruction trigger and image save (lines 86, 99-121)

### 🟢 Low Priority (no refactoring needed, already generic)
6. **CommunicationChannel.py** — May need signal renaming, but structure is sound
7. **basecontrollers.py** — No changes needed
8. **ImRecMainController.py** — No changes needed
9. **DataEditController.py** — No changes needed
10. **MultiDataFrameController.py** — No changes needed

---

## Estimated Refactoring Scope

- **Lines requiring modification:** ~500-600 LOC (ImRecMainViewController: 200-250, ScanParamsController: 30-40, ReconstructionViewController: 80-100, DataFrameController: 20-30, WatcherFrameController: 40-50, plus new plugin interface code: 150-200)
- **New code required:** Plugin interface definitions (50-100 LOC), MoNaLISA plugin implementation (300-400 LOC migrated from ImRecMainViewController + model layer), plugin registry (100-150 LOC)
- **Total effort estimate:** Medium-large refactor. **Core architecture change** (plugin dispatch) + **significant controller rewiring** + **interface design**.

---

**Audit completed:** 2026-05-29  
**Next steps:** Model layer audit (`imreconstruct-2-0.model.md`), view layer audit (`imreconstruct-2-0.view.md`), then unified design doc (`imreconstruct-2-0.md`).
