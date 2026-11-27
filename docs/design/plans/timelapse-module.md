# Timelapse module (widget + controller)

Status: implemented 2026-11-18 on `feat/timelapse` (worktree `../Imswitch2-timelapse`, off origin/main 8c749cceb); see "Outcome" at the end. Rig validation owed.

## Goal

A `Timelapse` dock, similar to `Tiling`. It runs N timepoints with a delay between them. One acquisition kind is chosen per run:

| Kind | Per timepoint | Lasers | Saved as |
|---|---|---|---|
| Camera (non-scanned) | F frames from the chosen free-running detectors | the ticked lasers, each at its own power, are switched on only for the point's frames and off otherwise | ordinary camera timelapse (`<name>_time####` per file, or one grouped HDF5/Zarr lapse file) |
| Scan | one scan of the chosen scan source | **read-only**: the list shows what the scan program fires (it arms and pauses its own lasers per point); every other laser stays off for the whole run | ordinary scan timelapse (`<name>_scan####` or grouped file) |
| Tiling (only if the Tiling controller exists) | one complete tiling run with the Tiling widget's settings | free-running tiling: the ticked lasers are gated per tile; triggered tiling: read-only, as for Scan | tiling's own dataset format, one folder per timepoint under one timelapse folder (the agreed exception) |

Decisions taken with Lenny on 2026-11-17:
- Scanned timepoints: laser list read-only (the scan's lasers); the timelapse keeps the rest off.
- One illumination state per timepoint, not a channel sequence.

Decisions I made (open to change):
- Interval mode is chosen per run: *start to start* (fixed cadence; an overrunning point starts the next one immediately and logs the slip) or *delay after each point ends*.
- At run start every laser is switched off (Live). They stay off at the end. Power setpoints the run changed are restored.
- Folder, file name and save format come from the Recording panel, as `recordScanSeries` already does.

## Architecture: the module owns *when*, Recording owns *what is written*

Recording already writes the lapse format: one manager session per point with `recLapseTotal/Index/IntervalS/ScheduledTime` and `singleLapseFile`. It also has a per-point gate, `setPositioningProvider(provider, partitionKind=...)` plus `setCycleTerminalCallback(cb)`, which Tiling uses to hold a point until the stage arrives (`RecordingController._awaitPositioning`, `TilingController._RecordingDispatcher`).

The Timelapse module reuses that gate as an **illumination/cadence gate**:
1. Recording runs its lapse with interval 0 and the module's provider installed (`partitionKind='time'`).
2. `provider(i)` returns a `PositioningRequest`. A module worker thread waits for the deadline, switches the lasers on, waits for the frame boundary (below), then resolves it. The request's `attributes` carry per-point metadata (`timelapse:lasers`, `timelapse:scheduled_s`, `timelapse:interval_mode`).
3. Recording arms and writes point i.
4. The cycle-terminal callback (writer drained *and* scan finished) switches the lasers off and records the end time. The next deadline is computed from it in *delay* mode.

Because the gate decides when each point starts, both interval modes work the same way for every kind.

### Frame boundary for laser gating (camera kind, free-running tiles)

Same proof as `_fresh_frame.grab_fresh_frame`. Switch the laser on at T, open a chunk-consumer boundary on every recorded detector, and wait for ≥1 frame past it. The recording's own boundary opens after that, so its first frame started exposing after T. The laser goes off only after the last recorded frame arrived, which means its exposure has ended. An extra *illumination settle* (ms) covers slow sources (shutters, AOTF rise). It comes from the user or the setup. Absent means "none declared" and is recorded as such in the metadata, never guessed (no-magic-numbers rule). Hardware TTL gating via the camera's fire output is out of scope.

The detectors are held for the whole run with `detectorsManager.acquire(names, LeasePurpose.WORKFLOW)`, so the cameras free-run through the delays (as tiling does).

## Work packages (one branch, one commit each)

**T1. RecordingController: camera series and gate on camera lapse** (`controller/controllers/RecordingController.py`, `controller/WorkflowServices.py`)
- `recordCameraSeries(detectorNames, timepoints, framesPerPoint, singleFile)` mirrors `recordScanSeries`: caller's detectors, the widget left untouched, the same refusals. Generalize `_ScanSeriesRequest` (add `kind`, `framesPerPoint`; `source=None` for camera) rather than adding a second request type.
- `nextCameraLapse` consults `_awaitPositioning()` exactly as `nextLapse` does. `recFrames = framesPerPoint` instead of the hard 1.
- The cycle-terminal callback must also fire for CameraLapse points. Check the `recordingCycleEnded` ~2608 path.
- `RecordingWorkflowService.record_camera_series(...)` as the module's entry point; `stop_camera_series`.
- Tests: the gate defers arming until resolved; framesPerPoint honoured; the lapse attrs and savename are identical to a widget-started camera lapse; refusals.

**T2. TilingController: run hook for workflows** (`TilingController.py`)
- Split `startTiling` into a settings snapshot and a launch. Add `runTilingForWorkflow(saveFolder, illuminationGate=None) -> result`, which runs synchronously on the caller's thread with save forced on and the dataset written to `saveFolder`.
- `illuminationGate` has `before_tile()`/`after_tile()` around `_FreeRunningTileSource.acquire`. It is not used in triggered mode (the scan arms its own lasers).
- Busy/stop/close semantics unchanged. `stopTiling` also stops a workflow run.

**T3. Model + setup** (`model/workflows/timelapse.py` new, `model/SetupInfo.py`)
- Pure, Qt-free `TimelapsePlan` (kind, timepoints, interval_s, interval_mode, frames_per_point, lasers {name: power|None}, detectors, single_file, settle_ms|None, scan_source) with validation, and a deadline scheduler (`next_start(i, prev_start, prev_end)`). Unit-test it heavily.
- Optional `TimelapseInfo` setup section with UI defaults only (no hardware numbers): defaultTimepoints, defaultIntervalS, defaultIntervalMode, illuminationSettleMs (optional), defaultKind.

**T4. TimelapseController** (`controller/controllers/TimelapseController.py`)
- Worker thread + gate provider + terminal callback. Lasers go through `LiveScreeningWorkflowService` / the Laser controller via `commChannel.controllerRegistry()` (gate → powerDevice expansion, the widget stays in sync). Scan kind: `record_scan_series(source, detectors, frames=N, interval 0)`. Tiling kind: loop of `runTilingForWorkflow(<root>/t####)` with the module's own cadence.
- APIExport: `startTimelapse`, `stopTimelapse` (finish current point, then stop), `getTimelapseStatus`.
- Read-only scan lasers via `scanWorkflow.laser_acquisition_states()` (`active_for_scan`).

**T5. TimelapseWidget + registration** (`view/widgets/TimelapseWidget.py`)
- Kind combo (Tiling entry only when available), scan-source combo, timepoints, interval + mode, frames/point, laser table (checkbox + power; read-only in scan kind), detector checkboxes (non-scan-driven only for camera kind), single-file checkbox, settle ms, Start/Stop, progress "t i/N, next in x s".
- Register in `controllers/__init__.py`, `widgets/__init__.py`, `ImConMainView` (`'Timelapse'` title + default dock), `ViewSetupInfo` availableWidgets docstring, config editor (`editor.py` category + description, `builtin_templates/sections/timelapse.json`), and the mock setups.

**T6. Docs + e2e on the mock setup**: `.rst` page (docs are Sphinx rst), and a headless mock run of all three kinds checking the files on disk.

Implementation workflow: Sonnet agents from structured briefs, one at a time in this worktree, reviewed between packages. Order: T3, T1, T2, T4, T5, T6.

## Things to verify while building
- How `_ScanSeriesRequest` is read at RecordingController lines ~437, 540, 561, 691, 1192, 3252, 3287, 3415 before generalizing it.
- That `recordingCycleEnded` (~2597) calls `_cycleTerminalCallback` for CameraLapse, not only ScanLapse.
- `_validateCameraLapse` rejects externally triggered or scan-driven detectors; the widget should filter on the same rule.
- Rig validation owed (PR review = rig test): laser gating against real exposure timing.

## Outcome (2026-11-18)

Built in the order T3, T1, T2, T5 (widget before controller, so the controller was written against the real widget), T4, T6. User docs: `docs/timelapse.rst`.

Verified end to end on the hardware-free app (`simple_mock_setup.json` + a mock XY stage + Tiling + Timelapse, booted offscreen): camera 3/3 timepoints into one grouped HDF5 file, 488 on ~0.25 s per timepoint and off between, `metadata/timelapse` and `Laser:488:Enabled = True` in every point; scan 2/2 APD scans grouped, slip recorded for an overrunning point; tiling 2/2 timepoints of 2x2 tiles in `t0/`, `t1/` with `timelapse.json`, the laser switched once per tile.

The end-to-end run found two defects the unit tests had not:
- Recording: a scan series whose first point was held by the gate was reported as not started while its retry timer ran on (fixed, regression test).
- Timelapse: start-to-start was anchored at the release, adding the switch-on + frame-proof time to every period (fixed, regression test).

Found outside this work, not fixed here: the mock Marzhauser stage (`example_mock.json`) cannot answer `?pos`, marks its position unknown and refuses every move, so Tiling cannot step on that mock at all.

Still owed: rig validation of the light timing against real exposures (PR review = rig test).
