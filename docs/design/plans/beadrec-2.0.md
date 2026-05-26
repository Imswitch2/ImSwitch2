# BeadRec 2.0 Plan

## Goal

Make bead reconstruction testable, robust during scan lifecycle changes, and
usable as a normal ImControl tool without mixing detector polling, GUI state,
analysis plotting, and reconstruction state in one controller.

## Current Problems

- `BeadRecController` owns too many responsibilities: scan lifecycle,
  detector chunk polling, ROI handling, reconstruction buffers, file I/O,
  result-list bookkeeping, center finding, donut analysis, and matplotlib
  plotting.
- `BeadWorker` reads widget state and mutates controller state directly from a
  worker thread. That makes thread ownership unclear and hard to test.
- ROI bounds are consumed directly from the visual without normalization,
  clipping, integer conversion, or empty-ROI handling.
- Reconstruction writes wrap silently when detector chunks exceed the expected
  scan-pixel count.
- Display scaling currently uses image shape, while physical scan step sizes
  are available and should define anisotropic scaling.
- Center finding and donut analysis mix algorithm output with plotting side
  effects, so failures are hard to report cleanly and hard to unit-test.
- Widget/result-list state is duplicated between `listRecs` in the controller
  and `QListWidgetItem` metadata in the widget.

## Phase 1: Pure Model Baseline

Status: **implemented**.

Added `imswitch/imcontrol/model/bead_recognition.py` with no Qt, detector, or
hardware dependencies:

- `BeadAnalysisParameters`
  - Typed passive analysis defaults matching the legacy widget dictionary.
  - Provides `from_mapping()` and `as_dict()` for backward-compatible widget
    integration.
- `RoiBounds`
  - Integer, clipped ROI representation with NumPy row/column slices.
- `normalize_roi_bounds()`
  - Converts visual `(x0, y0, x1, y1)` coordinates into valid image bounds.
  - Handles reversed coordinates, float coordinates, image clipping, and empty
    ROI rejection.
- `mean_intensity_in_roi()`
  - Computes the per-frame ROI mean used by bead reconstruction.
- `create_reconstruction_buffer()`, `append_roi_means()`,
  `reconstruction_image()`
  - Testable reconstruction-buffer helpers that preserve current display
    orientation and make wrap/stop behavior explicit.
- `rescale_reconstruction_to_pixel_size()`
  - Uses physical scan step sizes instead of image shape to rescale
    anisotropic reconstructions.

Unit coverage was added in
`imswitch/imcontrol/_test/unit/test_bead_recognition_model.py`.

## Phase 2: Safe Controller Integration

Status: **implemented**.

- Replace ad hoc ROI slicing in `BeadWorker` with `normalize_roi_bounds()` and
  `mean_intensity_in_roi()`.
- Replace `np.resize()` display reshaping with `reconstruction_image()`.
- Replace shape-based scaling with `rescale_reconstruction_to_pixel_size()`.
- Keep existing UI behavior and signal contracts intact.
- Add controller-level tests with fake comm channel, fake widget, and fake
  detector manager. No hardware execution.

Phase 2 integration now routes the live controller through the tested model
helpers for ROI clipping, ROI means, reconstruction buffers, display reshaping,
and physical step-size scaling. Worker ownership is intentionally unchanged
until Phase 3.

## Phase 3: Thread Ownership Cleanup

Status: **implemented**.

- Stop letting `BeadWorker` access `_widget`, `_master`, and mutable controller
  attributes directly.
- Introduce an immutable acquisition configuration object for dims, ROI, and
  scaling options.
- Emit typed worker results: image/progress/error/completion.
- Let the controller remain the only owner of widget updates.

Phase 3 now removes broad controller access from `BeadWorker`. The worker
receives narrow callables for scan-running state, detector chunks, and ROI
bounds, owns its reconstruction buffer/index internally, and emits reconstructed
buffers back through `sigNewChunk(object)`. Controller/widget updates remain on
the controller side. Phase 7 completed the later acquisition-config and
structured-update hardening that was identified here.

## Phase 4: Analysis Extraction

Status: **implemented**.

- Move foci/donut center finding and donut fill analysis into pure functions
  returning structured result objects.
- Remove direct `matplotlib.pyplot.show()` calls from analysis functions.
- Add deterministic failure reasons for no blob, invalid area, insufficient
  peaks, empty background, and invalid parameter ranges.

Phase 4 now adds pure `CenterDetectionResult` and `DonutAnalysisResult`
model objects. `find_bead_center()`, `find_center_foci()`,
`find_center_donut()`, and `analyze_donut()` live in
`bead_recognition.py` without plotting or GUI side effects. The controller's
center-query path uses the pure center search, and the existing manual
`run_donut_analysis()` button is reduced to a plotting compatibility wrapper
around `analyze_donut()`.

## Phase 5: Widget 2.0

Status: **implemented**.

- Make the widget internally scrollable/responsive without changing dock
  placement semantics.
- Add status/progress feedback without changing existing BeadRec signals.
- Remove the restrictive `setMaximumWidth(100)` result-list behavior.
- Keep deeper acquisition-control UX changes for follow-up work.

Phase 5 now adds a first UI containment/status pass without changing BeadRec
signals: the image viewer can shrink, the result list no longer has a hard
100 px maximum, horizontal scrolling is enabled for long result names, and the
widget exposes `setStatusText()` plus `updateProgress()` for controller/worker
feedback. The run control is still the legacy checkbox; replacing it with a
proper Start/Stop control is left for a later UX-specific pass.

## Phase 6: Result Model and Persistence

Status: **implemented**.

- Replace parallel controller/widget result lists with a typed result record:
  name, image, axial name, current/final state, source path, timestamp, scaling
  state.
- Persist passive settings only: analysis parameters, scale toggle, last folder,
  and optional default ROI size.
- Never persist active running/acquisition state.

Phase 6 now introduces `BeadRecResultRecord` for saved reconstructions and
stores passive metadata alongside image data. `BeadRecController` owns typed
records and keeps the legacy `listRecs` image list synchronized for
compatibility. The controller also registers with widget-state persistence and
saves/restores passive settings only: analysis parameters, scale toggle, ROI
visibility, last directory, and result metadata. Image pixel data and running
state are intentionally not restored.

## Phase 7: Acquisition Config and Worker Updates

Status: **implemented**.

- Replace raw scan-dim worker configuration with an immutable acquisition
  config object.
- Emit structured worker updates containing the reconstruction buffer,
  filled-pixel count, total-pixel count, written-frame count, and wrap state.
- Keep the controller responsible for widget progress/status updates.
- Preserve existing detector polling and scan lifecycle behavior.

Phase 7 now adds `BeadAcquisitionConfig` and `BeadWorkerUpdate` to the pure
model layer. `BeadWorker` is configured with a validated immutable config and
emits structured updates instead of raw buffers. The controller still accepts
legacy raw-buffer updates defensively, but the normal path now carries explicit
progress metadata. This keeps worker ownership clearer without changing scan
timing, detector flushing, TTL behavior, or hardware control paths.

## Remaining Work

The core seven-phase architecture pass is implemented. The following optional
improvements remain scoped but not yet scheduled:

- **Start/Stop UX**: Replace the legacy run checkbox with explicit Start/Stop
  button controls for clearer acquisition state management.
- **Richer worker error reporting**: Extend `BeadWorkerUpdate` to carry typed
  error conditions (detector timeout, ROI out of bounds, buffer overflow) instead
  of relying on status text only.
- **Optional ROI/default persistence**: Add configurable default ROI bounds to
  widget-state persistence for repeated acquisitions with the same region.
- **Controller fake tests**: Expand no-hardware controller tests with fake scan
  lifecycle and detector chunk injection to cover edge cases (wrap behavior,
  empty ROI, mid-scan parameter changes).

## Safety Notes

BeadRec observes scan and detector data, but phase 1 does not touch live scan
execution, detector acquisition, hardware timing, movement, TTL generation, or
laser control. Later controller phases must remain no-hardware-testable first
and need explicit review because they interact with scan/detector lifecycle.
