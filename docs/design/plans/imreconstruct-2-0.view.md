# ImReconstruct 2.0: View Layer Audit

**Milestone 12 scoping document — view layer reusability analysis**

The ImReconstruct view layer (1,252 LOC across 7 files) is **largely reusable** with targeted refactoring. The main application shell (`ImRecMainView`, `DataFrame`, `MultiDataFrame`, `WatcherFrame`, `DataEditDialog`) provides generic data-management and viewing infrastructure that is agnostic to reconstruction algorithm. Two components are **MoNaLISA-bound**: `ReconstructionView` (hard-codes 3D orthogonal views specific to MoNaLISA's point-scanning output) and `ScanParamsDialog` (encodes 4D scan-pattern metadata for MoNaLISA acquisitions). The critical parameter tree embedded in `ImRecMainView` (`ReconParTree`, lines 246–278) is **heavily MoNaLISA-specific** (pattern offsets, PSF FWHM, background modeling) and must be externalized to a per-modality reconstructor plugin to achieve generalization. Bottom line: **~70% of the view layer is reusable**, 30% requires modality-aware refactoring.

---

## File-by-file audit

### ImRecMainView.py

**Classification:** Mixed (generic shell + MoNaLISA-specific parameters)  
**LOC:** 338  
**Widgets owned:**
- Menu bar with file actions (save reconstruction, save coefficients, set folders)
- Three dock widgets: `DataFrame` (current data viewer), `MultiDataFrame` (batch data manager), `WatcherFrame` (file watcher)
- `ReconParTree` (parameter tree — **MoNaLISA-specific**, see below)
- `BtnFrame` (action buttons for reconstruction)
- `ReconstructionView` (napari-based 3D viewer)
- Two modal dialogs: `ScanParamsDialog`, `PickDatasetsDialog`

**MoNaLISA-bound UI elements:**

1. **`ReconParTree` parameter definitions (lines 251–274):**
   - **Line 252:** `'Pixel size'` with default value `77 nm` — MoNaLISA-specific camera/scan calibration
   - **Lines 254–259:** `'Pattern'` group with `Row-offset`, `Col-offset`, `Row-period`, `Col-period` (defaults 9.89, 10.4, 11.05, 11.05) — encodes MoNaLISA SIM pattern geometry; `Find pattern` action button triggers pattern-finding algorithm
   - **Lines 260–265:** `'Reconstruction options'` group:
     - `'PSF FWHM'` (default 220 nm) — point-spread-function deconvolution parameter for MoNaLISA
     - `'BG modelling'` with values `['Constant', 'Gaussian', 'No background']` and nested `'BG Gaussian size'` (500 nm) — MoNaLISA background-subtraction modes
   - **Line 266:** `'Scanning parameters'` action button — opens `ScanParamsDialog` (MoNaLISA scan metadata)
   - **Line 267:** `'Show pattern'` checkbox — overlays pattern grid on raw data (MoNaLISA-specific visualization)
   - **Line 268:** `'Bleaching correction'` — photobleaching correction for MoNaLISA time-series
   - **Lines 270–273:** `'Denoising options'` group with `'Model name'` defaulting to `'Vimentin_UNet_RCAN_lowSNR'` — **hardcoded MoNaLISA-trained denoiser**

2. **Dimension naming (lines 43–48):**
   - `r_l_text = 'Right/Left'`, `u_d_text = 'Up/Down'`, `b_f_text = 'Back/Forth'`, `timepoints_text = 'Timepoints'`, `p_text = 'pos'`, `n_text = 'neg'` — these dimension labels are passed to `ScanParamsDialog` (line 116–117) and encode MoNaLISA's 4D acquisition axes (XYZ + time) with directional metadata

3. **Pattern-related methods and signals (lines 31–34, 103–111, 200–214):**
   - **Line 31:** `sigShowPatternChanged` — toggles pattern overlay
   - **Line 32:** `sigFindPattern` — triggers pattern-finding algorithm (MoNaLISA SIM)
   - **Line 34:** `sigPatternParamsChanged` — reacts to pattern parameter edits
   - **Lines 200–207:** `getPatternParams()` — retrieves row/col offsets and periods (MoNaLISA pattern)
   - **Lines 209–214:** `setPatternParams()` — updates pattern tree values

**Generic/reusable elements:**
- File menu actions (lines 50–86): save/load data, set default folders — **fully generic**
- Data management docks (`DataFrame`, `MultiDataFrame`, `WatcherFrame`) — **fully generic**
- Reconstruction buttons (`reconCurrBtn`, `reconMultiBtn`, `updateBtn`, `denoiseBtn` in `BtnFrame`) — **generic actions**, though `denoiseBtn` currently wired to MoNaLISA denoiser
- Dialog infrastructure (`requestFilePathFromUser`, `requestFolderPathFromUser`, `showScanParamsDialog`, `showPickDatasetsDialog`) — **fully generic**
- Layout (lines 141–158): left panel = parameters + buttons + data management, right panel = reconstruction viewer — **fully generic**

**Reuse verdict:**  
**70% reusable.** The application shell, menu, docks, and layout are modality-agnostic. The parameter tree must be **externalized** to a per-modality reconstructor class that provides its own parameter schema. Pattern-related methods would become optional hooks invoked only by modality plugins that need them (e.g., SIM reconstructors). The dimension-naming strings should come from the reconstructor's metadata, not be hardcoded.

---

### DataFrame.py

**Classification:** Generic  
**LOC:** 138  
**Widgets owned:**
- `pg.ImageItem` with histogram LUT for raw data display
- Frame slider and frame number input
- Three action buttons: "Show mean image", "Adjust/compl. data", "Unload data"
- File name and dataset name labels, frame count label
- `patternScatter` (ScatterPlotItem for pattern overlay, lines 65–71)
- Embedded `DataEditDialog` (lines 73, 100–101)

**MoNaLISA-bound UI elements:**

1. **Pattern scatter overlay (lines 65–71, 92–98):**
   - **Lines 65–71:** Creates `patternScatter` (red scatter points) for visualizing MoNaLISA SIM pattern
   - **Lines 92–98:** `setShowPattern()` adds/removes scatter from view
   - **Line 106:** `setPatternGridData(x, y)` updates scatter positions

**Generic/reusable elements:**
- Image viewer, histogram, frame navigation (slider + text input) — **fully generic**
- "Show mean image" / "Adjust/compl. data" / "Unload data" buttons — **fully generic**
- File/dataset metadata display — **fully generic**

**Reuse verdict:**  
**95% reusable.** Only the pattern scatter overlay is MoNaLISA-specific. This should become an **optional modality plugin feature** — the base `DataFrame` provides a `setShowPattern()` / `setPatternGridData()` interface, and the reconstructor decides whether to use it. For non-SIM modalities, these methods are no-ops.

---

### MultiDataFrame.py

**Classification:** Generic  
**LOC:** 213  
**Widgets owned:**
- `QListWidget` for multi-dataset batch management
- Eight action buttons: "Add data", "Load selected/all", "Set as current", "Remove/all", "Unload/all", "Save selected/all"
- Data-loaded status label

**MoNaLISA-bound UI elements:**  
**None.** All functionality is dataset-agnostic: load files, track in-memory status, highlight current item, delete/save. The internal data object storage (`.data(1)`) is opaque — works with any `DataObj` subclass.

**Reuse verdict:**  
**100% reusable.** No changes required.

---

### WatcherFrame.py

**Classification:** Generic  
**LOC:** 67  
**Widgets owned:**
- Folder path text input and "Browse" button
- `QListWidget` for displaying files in watched folder
- "Watch and run" checkbox

**MoNaLISA-bound UI elements:**  
**None.** The file-extension filter (line 40: `file.endswith('.'+extension)`) is parameterized via `updateFileList(extension)` — the extension comes from the main view's parameter tree (`'File extension'`, line 269 in `ImRecMainView.py`, values `['hdf5', 'zarr']`). The watcher logic itself is generic.

**Reuse verdict:**  
**100% reusable.** No changes required. The extension is passed in dynamically, so it adapts to any modality's file format.

---

### DataEditDialog.py

**Classification:** Generic  
**LOC:** 134  
**Widgets owned:**
- Modal dialog with two `pg.ImageItem` + histogram widgets (data view + dark frame view)
- Frame slider and frame number input
- "Show mean image" button
- `DataEditActions` sub-widget with "Set Dark/Offset frame" button (lines 105–117)

**MoNaLISA-bound UI elements:**  
**None.** All actions are dataset-level operations: view frames, set dark frame for subtraction, view mean. No reconstruction-algorithm logic.

**Reuse verdict:**  
**100% reusable.** This is a generic data-preprocessing dialog. Works for any frame stack.

---

### ReconstructionView.py

**Classification:** MoNaLISA-specific (but generalizable with minor refactoring)  
**LOC:** 163  
**Widgets owned:**
- Embedded napari viewer with one `Image` layer (`'Reconstruction'`)
- `QButtonGroup` with three radio buttons: "Standard view", "Bottom side view", "Left side view" (lines 31–51)
- `QListWidget` for managing multiple reconstruction results
- Two buttons: "Remove current", "Remove all"

**MoNaLISA-bound UI elements:**

1. **Orthogonal view radio buttons (lines 31–51):**
   - **Lines 35–48:** Three fixed views (`'standard'`, `'bottom'`, `'left'`) — these are tailored to MoNaLISA's 3D point-scanning output (XYZ volume). For 2D modalities (e.g., widefield), only "Standard view" is meaningful. For multi-channel or time-series data, different view modes might be needed (e.g., "Channel merge", "Time projection").
   - The view names (`'standard'`, `'bottom'`, `'left'`) are hardcoded string literals retrieved via `getViewName()` (line 112). The controller (`ReconstructionController`) interprets these to permute axes.

**Generic/reusable elements:**
- Napari viewer with dynamic image layer — **fully generic**
- Multi-reconstruction list management — **fully generic**
- `setImage()` / `getImage()` / `clearImage()` / `setImageDisplayLevels()` — **fully generic**

**Reuse verdict:**  
**80% reusable.** The napari viewer and reconstruction-list management are generic. The **view radio buttons must become modality-configurable**: a per-modality reconstructor should declare which view modes it supports (e.g., `['standard']` for 2D, `['standard', 'bottom', 'left']` for 3D, `['standard', 'channel1', 'channel2', 'merge']` for multi-channel). The view group should be **dynamically generated** from this list at runtime.

---

### ScanParamsDialog.py

**Classification:** MoNaLISA-specific  
**LOC:** 198  
**Widgets owned:**
- Modal dialog with 4×4 grid of combo boxes and text inputs for scan dimensions
- Four dimension rows: Dimension 0/1/2 (combo-selectable from `Right/Left`, `Up/Down`, `Back/Forth`) + Dimension 3 (fixed label "Timepoints")
- Each dimension has: direction (`pos`/`neg`), steps, step size (nm)
- "Unidirectional scan" checkbox

**MoNaLISA-bound UI elements:**

1. **Hardcoded 4D scan geometry (lines 29–56):**
   - **Lines 29–36:** Dimension 0 can be `Right/Left`, `Up/Down`, or `Back/Forth` (passed as `r_l_text`, `u_d_text`, `b_f_text` from main view, line 13)
   - **Lines 38–51:** Dimensions 1 and 2 are populated from the remaining two axes (enforced via `dim0Changed()` / `dim1Changed()` logic, lines 120–150)
   - **Lines 53–56:** Dimension 3 is **hardcoded as "Timepoints"** (line 54: `self.dim3DimLabel = QtWidgets.QLabel(self.timepoints_text)`)
   - This encodes MoNaLISA's acquisition structure: **3 spatial dimensions + 1 time dimension**, with each spatial axis scanned in a chosen direction (`pos`/`neg`)

2. **Unidirectional scan flag (lines 58, 176–177):**
   - **Line 58:** `unidirCheck` checkbox — relevant for MoNaLISA's galvo-scanning (bidirectional vs. unidirectional line scans)
   - Not universally applicable (e.g., tiled widefield has no concept of scan direction)

3. **Step size in nanometers (lines 27, 110–113):**
   - Assumes physical scanning with a calibrated step size — not relevant for camera-based acquisitions with fixed pixel sizes

**Generic/reusable elements:**  
**None beyond Qt boilerplate.** The entire dialog is a MoNaLISA scan-metadata editor.

**Reuse verdict:**  
**0% reusable as-is.** This dialog must be **replaced with a per-modality metadata editor**. Each reconstructor plugin should provide its own parameter schema (e.g., STED: pixel dwell time + depletion power; widefield deconvolution: wavelength + NA + immersion RI; FLIM: time bins + IRF). The main view should invoke `reconstructor.showMetadataDialog()` instead of hardcoding `ScanParamsDialog`.

---

## Main-view composition

The following widgets are composed in `ImRecMainView` (lines 88–158):

| Widget | Dock/Frame | Generic | MoNaLISA-specific | Notes |
|--------|-----------|---------|-------------------|-------|
| `DataFrame` | `currentDataDock` | ✅ | Pattern overlay | Data viewer; pattern scatter is optional |
| `MultiDataFrame` | `multiDataDock` | ✅ | — | Batch data manager |
| `WatcherFrame` | `watcherDock` | ✅ | — | File watcher; extension is parameterized |
| `ReconParTree` | Left panel (embedded) | — | ✅ | **Must externalize to reconstructor plugin** |
| `BtnFrame` | Left panel (embedded) | ✅ | Denoise button name | Action buttons; denoise model is MoNaLISA-trained |
| `ReconstructionView` | Right panel (embedded) | ✅ | View radio buttons | Napari viewer; view modes must be modality-configurable |
| `ScanParamsDialog` | Modal | — | ✅ | **Replace with per-modality metadata dialog** |
| `PickDatasetsDialog` | Modal | ✅ | — | Generic dataset picker from `imcommon` |

**Summary:** 6 out of 8 widgets are reusable. The two non-reusable components (`ReconParTree`, `ScanParamsDialog`) must be **provided by the reconstructor plugin**, not baked into the view.

---

## Open questions

1. **Parameter tree externalization strategy:**
   - Should each reconstructor plugin return a `pyqtgraph.parametertree.Parameter` tree that the main view embeds as-is?
   - Or should we define a declarative JSON-like schema that the main view converts to a parameter tree?
   - How do we handle parameter persistence (save/load reconstruction presets)?

2. **View mode configuration:**
   - Should `ReconstructionView` accept a list of `(view_name, view_label)` tuples at construction time?
   - Or should the reconstructor inject view-mode buttons dynamically after the viewer is created?

3. **Metadata dialog lifecycle:**
   - Should `ScanParamsDialog` be replaced with a base class (`ReconstructorMetadataDialog`) that each modality subclasses?
   - Or should reconstructors provide Qt widgets that the main view embeds in a generic container dialog?

4. **Pattern overlay generalization:**
   - Is the pattern scatter overlay (`DataFrame.patternScatter`) worth preserving as an optional feature?
   - Or should modality-specific overlays be handled entirely by the reconstructor (e.g., injecting custom `pg.GraphicsItem` into the image view)?

5. **Denoising integration:**
   - Should the "Denoise current" button (line 298–299 in `ImRecMainView.py`) invoke a per-modality denoiser?
   - Or is denoising better handled as a post-reconstruction step in a separate processing pipeline?

6. **Reconstruction result polymorphism:**
   - Does `ReconstructionView` need to handle more than `np.ndarray` images?
   - E.g., multi-channel stacks, hyperspectral cubes, FLIM decay histograms, point clouds (localization microscopy)?

7. **File format abstraction:**
   - `WatcherFrame` currently watches for `.hdf5` or `.zarr` files. Should the file-extension list come from the reconstructor?
   - What if a modality uses proprietary formats (e.g., `.nd2`, `.czi`, `.oir`)?

---

**Next steps (for unified `imreconstruct-2-0.md`):**  
Audit `model/` and `controller/` layers, then synthesize a refactoring plan that preserves the 70% reusable view infrastructure while externalizing modality-specific logic to reconstructor plugins.
