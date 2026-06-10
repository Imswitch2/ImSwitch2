# BeadRec audit & rework plan

Audit target: `imswitch/imcontrol/controller/controllers/BeadRecController.py`
and related scan / analysis modules.

---

## 1. Audit findings

### Bug A — axis selection with extra / line-step dims
`BeadRecController.updateParameters` filters out zero-valued dims and then
takes the first two:

```python
self.stepSizes = stepSizes[dims != 0]
self.dims      = self.dims[self.dims != 0]
if len(self.dims) > 2:
    self.dims = self.dims[:2]
```

`ScanControllerAdvanced.getDimsScan()` already returns `(x, y, z)` in physical
order. Filtering by zero is fragile:

- If `Y == 0` (1D scan) and `Z != 0`, BeadRec ends up with `(X, Z)`.
- Non-scan axes padded as `length=1, step=1` produce dim `= 1` (not `0`),
  which survives the filter and can displace `Y`.
- `n_linesteps` (`S`) is not in `getDimsScan()` today, but the digital schema
  emits `Nx · Ny · S` frames. BeadRec writes into a flat buffer of size
  `Nx · Ny` and wraps, so only the last linestep survives the reconstruction.

**Fix**

- Drop the zero filter. Treat index `0` as fast (X) and index `1` as slow (Y)
  by contract. Treat a `0` at index 0 or 1 as an invalid scan (warn + skip).
- Add `getNumLineSteps()` to scan controllers (`ScanControllerAdvanced` has
  the value internally) and expose it through `CommunicationChannel`.
- In `BeadWorker`, keep only every `S`-th detector frame (with a UI selector
  for which linestep), or allocate the buffer as `(Nx, Ny · S)` and let the
  user pick a sub-stripe. The first option is simpler and matches typical
  use ("show the bead reconstruction for the readout laser linestep").

### Bug B — `parametersChanged` comparison
`(prior_dims != self.dims).any()` would raise on ragged arrays; it is gated
by an `or` length check so it is safe today, but the length check becomes
dead once dims are a fixed `(X, Y)` tuple. Tighten when rewriting.

### Rework 1 — TriggerScope / Snouty compatibility

`CommunicationChannel.getDimsScan / getScanStepSizes` resolve against the
controller registered under widget key `"Scan"`. TriggerScope setups
register their own controllers
(`TriggerScopeScanController`, `TriggerScopeRasterController`,
`TriggerScopePLSRController`, `TriggerScopeLSXYRController`,
`TriggerScopeGalvoDetectionController`, `TriggerScopePLSRMulticolorController`,
`EtSnoutyController`) and none implement the BeadRec accessors. BeadRec
falls through its one-shot `RuntimeError` branch and silently does nothing.

Frame-to-pixel mapping also differs across modes:

- NI-DAQ: one trigger per dwell pixel ⇒ chunk-of-frames maps directly to
  contiguous flat-buffer indices.
- TriggerScope raster: same mapping holds (`_runRasterScan`).
- TriggerScope pLS-RESOLFT / multicolor / GalvoDetection: camera frame count
  ≠ `Nx · Ny` (cycle / readout axes); BeadRec's assumption does not hold.

**Plan**

1. Define a small `BeadRecScanSource` protocol (or ABC mixin) any scan
   controller can implement:
   - `getScanDims() -> tuple[int, int]` — physical `(Nx, Ny)`
   - `getScanStepSizes() -> tuple[float, float]` — `(x_step, y_step)`
   - `getNumLineSteps() -> int` — default `1`
   - `getFramesPerScanPixel() -> int` — default `1`; `> 1` for cycle modes
   - `isBeadRecCompatible() -> bool` — `False` for modes whose frame stream
     does not map to a 2D raster
2. Implement it on `TriggerScopeRasterController` (its
   `_analogParameterDict` already carries `target_device`, `axis_length`,
   `axis_step_size`). Stub the others as `isBeadRecCompatible = False`
   until their mapping is agreed.
3. Replace the hardcoded `"Scan"` widget-key lookup in
   `CommunicationChannel` with `getBeadRecScanSource()` that iterates
   registered controllers and returns the first one implementing the
   protocol. Keep `getDimsScan` for back-compat, rewritten on top of the
   protocol.
4. `BeadRecController` consumes the protocol explicitly. `BeadWorker`
   honors `framesPerScanPixel` by averaging or sub-sampling frames in
   `append_roi_means`.
5. When no compatible source is found, disable the Run button in the
   widget with a tooltip explaining why (replaces today's silent
   one-shot warning).

### Rework 2 — generalize bead / donut analysis

`analyze_donut` is hard-wired to a specific donut protocol: threshold →
largest connected component → erosion → minimum → two profile peaks per
axis. Pad / disk sizes are magic numbers; the fill metric is bespoke; and
`run_donut_analysis` calls `plt.show()` directly, blocking the GUI loop.

**Plan**

1. Introduce a `FitModel` registry in `bead_recognition.py`. Each entry
   exposes `name`, `param_names`, `initial_guess(image, mask) -> p0`,
   `model(xy, *p) -> values`, `summary(p) -> dict` (metrics in physical
   units). Initial set:
   - `gaussian2d` — rotated, with offset and amplitude
   - `donut_r2_gaussian` — `A · r² · exp(-r² / 2σ²) + B`, optionally
     elliptical and rotated
   - `legacy_donut` — keep existing `analyze_donut` behind this entry
2. Single fitter entry point
   `fit_bead(image, params, model_name, roi=None) -> FitResult` using
   `scipy.optimize.curve_fit` with bounds. ROI defaults to the bead ROI or
   the largest-CC bounding box from `_prepare_component_mask`. Returns
   pixel and physical-unit coordinates (using step sizes from the scan
   source).
3. Widget: replace the single "Donuts analysis" button with a model
   dropdown + Run. Result panel shows fit overlay (center cross, contour
   at FWHM / r-max), residual image, and a metrics table
   (σx, σy, θ, amplitude, offset, R²; or fillX/Y/min for the donut).
4. Route `find_bead_center` through fits — `Maxima` ⇒ centroid from
   `gaussian2d`, `Minima` ⇒ centroid from `donut_r2_gaussian`. Preserves
   existing callers (e.g. EtSTED's
   `commChannel.beadRecWorkflow.on_query_center_coord`).
5. Move plotting out of `BeadRecController.py`. Render into a Qt figure
   widget owned by the BeadRec widget, or emit results via a signal.

### Implementation order

1. Axis-selection fix + drop zero filter (smallest, isolated).
2. `n_linesteps` plumbing + sub-sampling in `BeadWorker`.
3. `BeadRecScanSource` protocol + TriggerScope raster implementation +
   `CommunicationChannel` capability lookup.
4. `FitModel` framework (Gaussian + donut-r² + legacy) and widget dropdown.
5. (Optional) pLS-RESOLFT / Snouty cycle-aware reconstruction once the
   frame→pixel mapping is agreed for those modes.

### Open questions

1. For `S > 1`: UI picker for the reconstructed linestep, or always the
   first?
2. For TriggerScope pLS-RESOLFT: is there a meaningful 2D bead
   reconstruction (e.g. one sub-frame per cycle), or disable BeadRec?
3. Fit framework: real-time during scan, or post-scan only?

---

## 2. Parallel agent task prompts

The four tasks below touch disjoint files and can run in parallel.
Each agent must produce a single commit on its own branch. We will
sanity-check after each major step.

---

### Task 1 — Axis-selection + line-step buffer fix

**File:** `imswitch/imcontrol/controller/controllers/BeadRecController.py`
(also touches
`imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py`
and `imswitch/imcontrol/controller/CommunicationChannel.py`
for the `getNumLineSteps` accessor.)

**Task summary**

Fix `BeadRecController.updateParameters` so the scan dims used for the
reconstruction buffer are always `(Nx, Ny)` (axes 0 and 1 of
`getDimsScan()`), not the result of "filter zeros + take first two".
Also plumb `n_linesteps` through `CommunicationChannel` and make
`BeadWorker` sub-sample every `S`-th detector frame so the buffer is no
longer overwritten when `S > 1`.

**Todo list**

- Add `getNumLineSteps()` to `ScanControllerAdvanced` (returns
  `int(self._digitalParameterDict.get("n_linesteps", 1))`, default `1`
  if not populated yet). Add a permissive default `getNumLineSteps`
  returning `1` on `ScanControllerBase`.
- Add `getNumLineSteps()` to `CommunicationChannel`, mirroring
  `getDimsScan` — return `1` if no scan controller is registered.
- In `BeadRecController.updateParameters`:
  - Read `dims` and `stepSizes` as before but stop filtering by
    `dims != 0`.
  - Use `self.dims = (int(dims[0]), int(dims[1]))` and
    `self.stepSizes = (float(stepSizes[0]), float(stepSizes[1]))`.
  - If `dims[0] <= 0` or `dims[1] <= 0`, log a warning, leave previous
    `dims` untouched, set `parametersChanged = False`, return.
  - Read `self.linesteps = self._commChannel.getNumLineSteps()` and
    include it in the `parametersChanged` comparison.
- In `BeadAcquisitionConfig` (`model/bead_recognition.py`) add a
  `linesteps: int = 1` field and pass it through
  `from_scan_dims(..., linesteps=...)`. `total_pixels` stays
  `dims[0] * dims[1]`.
- In `BeadWorker.run`, after `n = len(newImages)`, drop frames so only
  every `S`-th frame survives. Concretely: maintain a
  `self._lineStepPhase` (default `0`) counter; iterate the chunk,
  append to a `kept` list when `(self._lineStepPhase + i) % S == 0`,
  update phase. Pass `kept` to `append_roi_means`. Reset phase on
  `configure()`.
- Update `_createAcquisitionConfig` to pass `linesteps=self.linesteps`.

**Do NOTs**

- Do NOT change `BeadAcquisitionConfig.scan_dims` semantics — it stays
  `(Nx, Ny)`.
- Do NOT touch the donut / analysis code paths.
- Do NOT introduce a UI picker for "which linestep" yet — phase `0` is
  fine for this step.
- Do NOT change `CommunicationChannel.getDimsScan` signature.

**Implementation**

1. `ScanControllerBase.getNumLineSteps` default returning `1`.
2. `ScanControllerAdvanced.getNumLineSteps` reading from
   `_digitalParameterDict`.
3. `CommunicationChannel.getNumLineSteps` resolving against the
   `"Scan"` controller; mirror the existing pattern.
4. Edit `BeadRecController.updateParameters` per todo. Keep the
   `_warnedNoScanWidget` one-shot RuntimeError branch.
5. Add `linesteps` to `BeadAcquisitionConfig` and the
   `_createAcquisitionConfig` call site.
6. Add `_lineStepPhase` state + sub-sampling loop in `BeadWorker.run`.
   Reset in `configure`.

**Sanity checks**

- `python -m compileall imswitch/imcontrol/controller/controllers/BeadRecController.py imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py imswitch/imcontrol/controller/controllers/ScanControllerBase.py imswitch/imcontrol/controller/CommunicationChannel.py imswitch/imcontrol/model/bead_recognition.py`
- Existing tests: `pytest imswitch/imcontrol -k bead -x` if they exist.
- Manual smoke: launch with an Advanced setup, start a scan with
  `n_linesteps=3`, confirm the reconstruction is `(Ny, Nx)` and not
  visibly torn / wrapped.

**Commit instructions**

Single commit on a new branch `fix/beadrec-axis-linesteps`. Message:

```
BeadRec: use explicit (X, Y) axes and subsample linesteps

- getDimsScan() axes 0/1 are now used directly; the zero-filter that
  could shift in Z or a linestep axis is removed.
- New getNumLineSteps() accessor on the scan controller chain so
  BeadRec knows the linestep count S.
- BeadWorker keeps every S-th detector frame, fixing the buffer wrap
  that previously discarded all but the last linestep.
```

Do NOT push.

---

### Task 2 — `BeadRecScanSource` protocol + capability lookup

**File:**
- `imswitch/imcontrol/controller/controllers/_beadrec_scan_source.py` (new)
- `imswitch/imcontrol/controller/CommunicationChannel.py`
- `imswitch/imcontrol/controller/controllers/ScanControllerBase.py`
- `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py`
- `imswitch/imcontrol/controller/controllers/TriggerScopeRasterController.py`

**Task summary**

Introduce a small protocol any scan controller can implement to expose
the metadata BeadRec needs. Wire `CommunicationChannel` to look up by
capability instead of by the hardcoded `"Scan"` widget key, so
TriggerScope / Snouty setups can participate. Keep the existing
`getDimsScan` / `getScanStepSizes` accessors working for back-compat.

**Todo list**

- New module `_beadrec_scan_source.py` defining a
  `typing.Protocol` (runtime-checkable) `BeadRecScanSource` with
  methods: `getScanDims`, `getScanStepSizes`, `getNumLineSteps`,
  `getFramesPerScanPixel`, `isBeadRecCompatible`.
- Default mixin `BeadRecScanSourceMixin` providing sensible defaults
  (`getNumLineSteps -> 1`, `getFramesPerScanPixel -> 1`,
  `isBeadRecCompatible -> True`).
- `ScanControllerBase` inherits the mixin; concrete `getScanDims` /
  `getScanStepSizes` implementations call the existing
  `getDimsScan` / `getScanStepSizes` and return `(dims[0], dims[1])`
  and `(step[0], step[1])` respectively.
- `ScanControllerAdvanced.getNumLineSteps` (already added in Task 1 if
  merged — coordinate or no-op here).
- `TriggerScopeRasterController` implements `BeadRecScanSourceMixin`:
  `getScanDims` returns
  `(int(axis_length[0] / axis_step_size[0]), int(axis_length[1] / axis_step_size[1]))`
  (guard against zero step), `getScanStepSizes` returns the first two
  step sizes.
- `CommunicationChannel`:
  - Add `getBeadRecScanSource()` that iterates registered controllers
    and returns the first instance of `BeadRecScanSource` whose
    `isBeadRecCompatible()` returns `True`. Cache the lookup.
  - Rewrite `getDimsScan` / `getScanStepSizes` to use that source when
    the `"Scan"` widget key is absent; keep the old path as the first
    attempt for back-compat.

**Do NOTs**

- Do NOT implement the protocol on pLS-RESOLFT / multicolor /
  GalvoDetection / Snouty controllers in this task — leave them
  unmodified.
- Do NOT change `BeadRecController` in this task (Task 4 wires it).
- Do NOT change widget code.
- Do NOT remove `getDimsScan` / `getScanStepSizes`.

**Implementation**

1. Write `_beadrec_scan_source.py` with the `Protocol` and the mixin.
2. Make `ScanControllerBase` inherit the mixin and provide
   `getScanDims` / `getScanStepSizes` via the existing accessors.
3. Add the same on `TriggerScopeRasterController`.
4. Add `getBeadRecScanSource` + capability lookup in
   `CommunicationChannel`. Rewrite `getDimsScan` /
   `getScanStepSizes` to fall through to the source.

**Sanity checks**

- `python -m compileall` on every touched file.
- Manual: load a TriggerScope raster setup; confirm
  `commChannel.getBeadRecScanSource()` returns the raster controller
  and that `getScanDims()` matches the widget's pixel counts.

**Commit instructions**

Single commit on branch `feat/beadrec-scan-source-protocol`. Message:

```
BeadRec: capability-based scan source lookup

- New BeadRecScanSource protocol + mixin exposing scan dims, step
  sizes, linestep count and frames-per-pixel.
- ScanControllerBase and TriggerScopeRasterController implement it.
- CommunicationChannel falls through to the capability lookup when
  the "Scan" widget key is not registered, unblocking TriggerScope
  and Snouty setups.
```

Do NOT push.

---

### Task 3 — Fit framework in `bead_recognition.py`

**File:** `imswitch/imcontrol/model/bead_recognition.py`
(plus a new `imswitch/imcontrol/model/bead_fits.py` for the models.)

**Task summary**

Add a fit framework that can fit a 2D Gaussian or a `r² · Gaussian`
donut to a bead image, alongside the existing legacy donut analysis.
Public API only — no widget changes here.

**Todo list**

- New module `bead_fits.py` with:
  - `@dataclass(frozen=True) FitResult` (`model`, `params`, `param_std`,
    `r_squared`, `center_px`, `summary: dict`).
  - `FitModel` interface (`name`, `param_names`,
    `initial_guess(image, mask)`, `model(xy, *p)`, `summary(p)`,
    `bounds(image, mask)`).
  - Implementations: `Gaussian2D` (rotated, with offset),
    `DonutR2Gaussian` (`A · r² · exp(-r² / 2σ²) + B`, isotropic to
    start; document where ellipticity / rotation would extend it).
  - Registry `FIT_MODELS: dict[str, FitModel]`.
- In `bead_recognition.py`:
  - `fit_bead(image, model_name, params=None, roi=None) -> FitResult`
    using `scipy.optimize.curve_fit` with bounds from the model.
    Default ROI: largest-CC bounding box from `_prepare_component_mask`
    expanded by a small margin; fall back to the whole image.
  - Keep `analyze_donut` and friends as-is.
- Unit tests `tests/test_bead_fits.py` covering:
  - Gaussian recovers known `(x0, y0, σx, σy, θ)` within tolerance on
    synthetic data.
  - DonutR2Gaussian recovers known `(x0, y0, σ)` and amplitude.
  - `fit_bead` raises a clear error on a flat image.

**Do NOTs**

- Do NOT touch `BeadRecController` or the widget.
- Do NOT remove or rename existing public functions in
  `bead_recognition.py` (`analyze_donut`, `find_bead_center`,
  `find_center_foci`, `find_center_donut`).
- Do NOT add matplotlib imports to `bead_fits.py` — pure numpy /
  scipy.
- Do NOT introduce real-time / streaming fits.

**Implementation**

1. Write `bead_fits.py` with the dataclasses, the two models and the
   registry.
2. Add `fit_bead` in `bead_recognition.py` re-exporting `FitResult` and
   the registry for callers.
3. Tests under `tests/test_bead_fits.py` using `numpy` only; mark with
   `pytest.importorskip("scipy")`.

**Sanity checks**

- `pytest tests/test_bead_fits.py -x`.
- `python -c "from imswitch.imcontrol.model.bead_recognition import fit_bead, FIT_MODELS; print(list(FIT_MODELS))"`.

**Commit instructions**

Single commit on branch `feat/beadrec-fit-framework`. Message:

```
BeadRec: generic 2D Gaussian / donut fit framework

- New bead_fits module with FitModel registry and Gaussian2D /
  DonutR2Gaussian implementations.
- fit_bead() entry point in bead_recognition.py, ROI-aware, with
  bounded curve_fit and R² reporting.
- Unit tests covering parameter recovery on synthetic data.
```

Do NOT push.

---

### Task 4 — BeadRec widget & controller wiring for the fit framework

> Run **after** Tasks 1–3 land; depends on the new `fit_bead` API and
> the `BeadRecScanSource`.

**File:**
- `imswitch/imcontrol/view/widgets/BeadRecWidget.py`
- `imswitch/imcontrol/controller/controllers/BeadRecController.py`

**Task summary**

Replace the single "Donuts analysis" button with a model dropdown
(Gaussian / Donut r²·Gaussian / Legacy donut) and route the
controller's analysis pipeline through `fit_bead`. Move
`plt.show()`-style plotting out of the controller into a Qt figure
panel owned by the widget.

**Todo list**

- Widget: replace the analysis button with a `QComboBox` populated
  from `FIT_MODELS` plus a `Run fit` button. Add a `QLabel`
  metrics-table area and a small matplotlib `FigureCanvas` for the
  fit overlay + residual.
- Signal `sigFitRequested(modelName: str)` from widget.
- Controller: handle `sigFitRequested` by calling `fit_bead` on
  `self.imDisplay` with `self.stepSizes`, then push the
  `FitResult` and overlay arrays back to the widget via a new
  `displayFitResult(result, image)` method.
- `find_bead_center` (controller-side `centerCoordQuery`) routes
  `Maxima` ⇒ `gaussian2d` centroid and `Minima` ⇒
  `donut_r2_gaussian` centroid via `fit_bead`. Keep the legacy
  `analyze_donut` path reachable via the dropdown.
- Delete the blocking `plt.show()` calls in
  `run_donut_analysis` / `findCenterFoci`; route diagnostics through
  the widget instead.

**Do NOTs**

- Do NOT change `BeadAcquisitionConfig` or `BeadWorker`.
- Do NOT remove `analyze_donut` from `bead_recognition.py`.
- Do NOT change the `commChannel.beadRecWorkflow` signatures
  consumed by EtSTED.

**Implementation**

1. Widget UI changes + new signal.
2. Controller: replace `donutsAnalysis` and the matplotlib paths with
   `fit_bead` calls; emit results to the widget.
3. Centroid pipeline (`centerCoordQuery`) re-routed through
   `fit_bead`; preserve return types.

**Sanity checks**

- `python -m compileall` on the touched files.
- Manual: load a saved donut tiff, run each model from the dropdown,
  confirm metrics + overlay render and that the EtSTED auto-center
  workflow still receives a `(y, x)` tuple.

**Commit instructions**

Single commit on branch `feat/beadrec-fit-ui`. Message:

```
BeadRec: model-selectable fit UI

- Dropdown chooses Gaussian / donut-r² / legacy donut; results render
  in an embedded canvas instead of a blocking plt.show().
- find_bead_center centroids route through fit_bead.
```

Do NOT push.

---

## 3. Sequencing

- Tasks 1, 2 and 3 are independent — start them in parallel.
- Task 4 depends on Tasks 1, 2 and 3 — start it after they pass
  review.
- After each task lands, we sanity-check together before moving on.
