# ImReconstruct 2.0 → ImProcess

**Status:** Design — Milestone 12 audit complete, refactor plan proposed
**Date:** 2026-05-29
**Scope:** `imswitch/imreconstruct/` → `imswitch/improcess/`
**Audit appendices:**
[model](imreconstruct-2-0.model.md) · [controller](imreconstruct-2-0.controller.md) · [view](imreconstruct-2-0.view.md)

---

## 1. Rename: ImReconstruct → ImProcess

The module name `imreconstruct` is a leftover from the MoNaLISA-only era and
actively misleads users: most modalities (STED, FLIM, confocal, widefield,
WidefieldSTARSS) don't *reconstruct* anything in the SIM sense — they want
viewing, drift correction, averaging, deskew, lifetime overlay, etc.

To draw a clean line under the legacy app and signal that the generalized
module covers **all post-acquisition processing**, rename:

| Old | New |
|---|---|
| `imswitch/imreconstruct/` | `imswitch/improcess/` |
| `ImRecMainView` / `ImRecMainController` | `ImProcessMainView` / `ImProcessMainController` |
| `ImRecWidgetController` (base) | `ImProcessWidgetController` |
| Window title "ImReconstruct" | "ImProcess" |
| Roadmap section "ImReconstruct Generalization" | "ImProcess (post-processing module)" |

The legacy `imreconstruct` package is **not** kept as a compatibility shim —
external scripts that imported from it are essentially nonexistent, and the
MoNaLISA reconstruction pipeline continues to be available *as a plugin* under
`improcess.reconstructors.monalisa`.

Filename of *this* design doc stays `imreconstruct-2-0.md` so the existing
roadmap link (Milestone 12) keeps resolving; the doc itself talks about
ImProcess throughout.

---

## 2. Audit summary

The audit covered all 28 source files (~4,000 LOC) across `model/`,
`controller/`, `view/`. Detailed classifications live in the appendix docs.
Headline numbers:

| Layer | Files | LOC | Generic | Mixed | MoNaLISA-only |
|---|---:|---:|---:|---:|---:|
| model | 7 | 1,373 | 4 (DataObj, Denoiser, UNet, UNetRCAN) | 1 (ReconObj) | 2 (PatternFinder, SignalExtractor) |
| controller | 10 | 1,309 | 5 | 3 (DataFrame, Watcher, ReconstructionView) | 2 (ImRecMainView, ScanParams) |
| view | 7 | 1,252 | 4 | 1 (ImRecMainView — shell generic, params MoNaLISA) | 2 (ReconstructionView, ScanParamsDialog) |

**~70% of the codebase is already modality-agnostic.** The MoNaLISA coupling
is concentrated in a small number of well-identified call sites — this is a
refactor, not a rewrite.

### What stays as shared infrastructure

- **I/O:** `DataObj` (HDF5 / TIFF / Zarr loader with lazy-load + file locks)
- **Denoising:** `Denoiser`, `UNet`, `UNetRCAN` (generic PyTorch models)
- **Shell:** `ImProcessMainController`, `basecontrollers`, `CommunicationChannel`
- **Data management:** `MultiDataFrame` + controller, `DataEditDialog` + controller
- **Per-frame viewer:** `DataFrame` + controller (pattern scatter becomes optional hook)
- **File watcher:** `WatcherFrame` + controller (reconstruction dispatch + save go through plugin)
- **Main window layout:** `ImRecMainView` shell — docks, menus, dialogs

### What moves behind the MoNaLISA plugin

- `PatternFinder` (FFT-based SIM-pattern detection)
- `SignalExtractor` (Windows-only `GPU_acc_recon.dll` wrapper)
- `ReconObj.coeffsToImage` (MoNaLISA raster reassignment, bidirectional scan
  correction, dimension-label permutation)
- `ScanParamsDialog` + `ScanParamsController` (MoNaLISA 4D scan metadata)
- `ReconParTree` contents (pattern offsets/periods, PSF FWHM, BG modelling,
  hard-coded denoiser name `Vimentin_UNet_RCAN_lowSNR`)
- MoNaLISA save-as-TIFF assumptions (6D `[Dataset, Base, Time, Slice, Y, X]`
  with `TZCYX` ImageJ axes)

### Implicit data contracts that need to become explicit

The audit found that controllers pass raw arrays between MoNaLISA-specific
components and assume their shape:

- `PatternFinder` → `[offsetVert, offsetHori, periodVert, periodHori]`
- `SignalExtractor` → 4D coefficients `(bases, frames, gridRows, gridCols)`
- `ReconObj.reconstructed` → 6D `[Dataset, Base, Timepoint, Slice, X, Y]`
- `scanParDict` → MoNaLISA dimension labels + directions + steps

These are MoNaLISA contracts dressed up as generic ones. They get moved
*inside* the MoNaLISA plugin, behind the new interfaces.

---

## 3. The three new contracts

### 3.1 `Reconstructor` (plugin)

```python
class Reconstructor(ABC):
    """A single processing pipeline (MoNaLISA SIM, deskew, drift-correct, …)."""

    name: str                       # human-readable, shown in plugin picker
    id: str                         # stable, e.g. "monalisa", "view-only", "snouty-deskew"
    file_extensions: list[str]      # for watcher dispatch + dataset filtering

    @abstractmethod
    def make_param_widget(self, parent) -> QWidget:
        """Return the parameter editor embedded in the left panel
        (replaces the hard-coded ReconParTree)."""

    @abstractmethod
    def make_metadata_dialog(self, parent) -> QDialog | None:
        """Return the metadata/scan-params dialog (replaces ScanParamsDialog).
        Return None if the plugin needs no acquisition metadata."""

    @abstractmethod
    def process(self, data_obj: DataObj, params: dict) -> ProcessingResult:
        """Run the pipeline. Pure function over (data, params)."""

    def make_overlay(self, data_obj: DataObj, params: dict) -> Overlay | None:
        """Optional: provide a viewer overlay (e.g. MoNaLISA pattern scatter,
        STED depletion ROI). Default: no overlay."""
```

### 3.2 `ProcessingResult`

```python
class ProcessingResult(ABC):
    """What a Reconstructor returns. Replaces direct ReconObj exposure."""

    name: str
    data: np.ndarray | zarr.Array   # n-dim, no fixed shape
    axis_labels: list[str]          # e.g. ["T", "Z", "Y", "X"] or
                                    # ["Dataset", "Base", "T", "Z", "X", "Y"]
    view_modes: list[ViewMode]      # named axis permutations for the viewer
    display_levels: tuple[float, float] | None

    @abstractmethod
    def save(self, path: Path, fmt: str) -> None: ...
```

`ViewMode` is `(name: str, transpose: tuple[int, ...])` — replaces the
hard-coded standard/bottom/left radio buttons in `ReconstructionView`. The
view dynamically generates one radio button per `view_mode`.

### 3.3 `ReconstructorRegistry`

```python
class ReconstructorRegistry:
    def register(self, plugin: type[Reconstructor]) -> None: ...
    def list(self) -> list[Reconstructor]: ...
    def get(self, plugin_id: str) -> Reconstructor: ...
    def auto_select(self, data_obj: DataObj) -> Reconstructor:
        """Pick a default plugin based on data_obj.attrs (modality tag,
        shape, file format). Fall back to view-only."""
```

Registration is explicit at module load — entry-points / dynamic discovery
can come later. For now, `improcess/reconstructors/__init__.py` imports
each built-in plugin and calls `registry.register(...)`.

---

## 4. Refactor plan (4 phases)

Each phase ends with a working app. No big-bang switch.

### Phase A — Rename + structural extraction

1. `git mv imswitch/imreconstruct imswitch/improcess`.
2. Update all imports (`from imswitch.imreconstruct...` → `improcess`).
3. Rename `ImRec*` symbols → `ImProcess*` (controllers, view, window title).
4. Update `__main__.py`, top-level launcher, docs references, roadmap.
5. **No behaviour change.** App still runs the MoNaLISA pipeline exactly as
   today; this is pure relocation.

**Out-of-scope cleanups to flag as separate tasks** (not blockers):
- Extract `unet_layers.py` to deduplicate the 140 lines shared between
  `UNet.py` and `UNetRCAN.py`.
- Fix `PatternFinder.findBestPeak` L91 likely-bug `(h1-h2)/(h1-h2)`.

### Phase B — Define interfaces + move MoNaLISA behind the registry

1. Add `improcess/reconstructors/{__init__.py, base.py, registry.py}` with
   the three contracts above.
2. Create `improcess/reconstructors/monalisa/` and move into it:
   - `PatternFinder`, `SignalExtractor`
   - `ReconObj.coeffsToImage` logic (the rest of `ReconObj` becomes the
     generic `ProcessingResult` base in `improcess/model/`)
   - `ScanParamsDialog`, `ScanParamsController` (rewritten as
     `MonalisaParamsDialog` / `MonalisaParamWidget`)
   - The MoNaLISA-specific section of `ReconParTree` (pattern, FWHM, BG
     modelling, denoiser name) becomes `MonalisaReconstructor.make_param_widget`.
3. Rewrite `ImProcessMainViewController` to dispatch through the registry:
   ```python
   plugin = self._registry.get(self._current_plugin_id)
   params = self._param_widget.get_values()
   result = plugin.process(data_obj, params)
   self._widget.addNewResult(result)
   ```
4. Rewrite `ReconstructionViewController` to operate on
   `ProcessingResult.data` + `axis_labels` + `view_modes` (no `ReconObj` import).
5. Rewrite `WatcherFrameController.saveImage` to call `result.save(...)`.

**Exit criterion:** existing MoNaLISA workflow works end-to-end through the
registry. MoNaLISA is one entry in `registry.list()`.

### Phase C — View-only fallback

1. Add `improcess/reconstructors/view_only/` — minimal `Reconstructor` whose
   `process()` returns the raw `DataObj.data` wrapped as a `ProcessingResult`.
2. `registry.auto_select()` returns view-only for any dataset without a
   modality tag.
3. **Outcome:** loading any STED / FLIM / confocal / widefield / SNOUTY
   dataset shows frames immediately, with the existing data-edit, multi-data,
   and watcher tooling — no reconstruction algorithm needed.

### Phase D — Per-modality plugins

Surface-level targets (owners + design TBD per plugin):

- **WidefieldSTARSS:** polarization-channel demux + per-cell metrics from the
  tiling workflow output (depends on outputs from M11's WidefieldStarss port).
- **STED / confocal:** frame averaging, drift correction, FLIM lifetime
  overlay when paired FLIM data is present.
- **SNOUTY lightsheet:** deskew + optional deconvolution hooks.
- **SIM / MoNaLISA:** already done in Phase B — reference implementation.

Each plugin lands as its own PR. Plugins added after Phase B do not require
any further core changes.

### Phase E (post-M12) — M10 live-pipeline hook

Once the Zarr streaming reconstruction from Milestone 10 lands, route its
output through `registry` so live reconstruction is no longer limited to the
MoNaLISA pipeline. Out of scope for M12 itself; called out in the roadmap.

---

## 5. Risks and open questions

1. **Plugin discovery.** Built-in registration is fine for now; revisit
   when third parties want to ship out-of-tree reconstructors.
2. **Cross-platform SignalExtractor.** Still Windows-only (proprietary
   `GPU_acc_recon.dll`). Acceptable as a *plugin* limitation. The rest of
   ImProcess becomes loadable on macOS/Linux.
3. **Parameter persistence.** Today's `ReconParTree` doesn't persist values
   across sessions. Generalizing to per-plugin widgets is the right time to
   add presets; design TBD — likely a JSON file per plugin under the user
   config dir.
4. **Multi-channel / hyperspectral / point-cloud results.** Phase B
   interface assumes `np.ndarray | zarr.Array`. Localization microscopy
   (PALM/STORM) would need point-cloud results — out of scope for M12, but
   the `ProcessingResult` abstract base should not be made array-only in a
   way that closes that door.
5. **Watcher dispatch.** With multiple plugins, the watcher needs to know
   which plugin handles which file. First cut: use the *currently selected*
   plugin. Smarter dispatch (per-folder, per-file-attr) is a follow-up.
6. **Docs.** No dedicated ImReconstruct user/dev page exists today. A new
   `docs/imswitch/improcess.rst` lands with Phase B.

---

## 6. Out-of-tree consequences

- **Roadmap.md:** rename Milestone 12 from "ImReconstruct Generalization" to
  "ImProcess (post-processing module)". Update other roadmap references
  (Milestones 10, 11, 13) that say `imreconstruct` to `improcess`.
- **`imswitch/__init__.py` / module launcher:** swap the import.
- **README + docs `gui.rst`:** rename mentions.
- **CLAUDE.md / agent prompts:** mention the rename for future agents.
- **Tests:** rename `tests/imreconstruct/` if it exists; update import paths.

These belong in Phase A so the rename is atomic.
