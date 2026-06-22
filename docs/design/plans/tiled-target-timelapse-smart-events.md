# Tiled Target Timelapse And Event-Gated Acquisition Audit

## Question

Can ImSwitch support a workflow that:

1. acquires a tiled overview;
2. selects targets manually or by segmentation;
3. revisits each target every X seconds and records data;
4. switches setup modes between overview and target acquisition; and
5. eventually gates the target acquisition through event-trigger detection?

Short answer: yes, but the clean implementation should be a new composable
workflow layer. Tiling, target detection, recording, setup modes, and
event-trigger logic already exist, but they are not yet exposed through one
workflow-safe target scheduler.

## Implementation Status

Updated 2026-06-21.

- **T1 model foundation implemented:** `Target`, `TargetList`, segmentation
  conversion, JSON serialization, pixel-to-stage target conversion, and optional
  stitched overview construction in `TilingWorkflow` are in place. Manual
  target editing in the tiling UI is still pending.
- **T2 implemented:** `TargetTimelapseWorkflow` visits enabled targets in
  A -> B -> C -> A order, moves/settles/acquires through a callback, and records
  per-target metadata and failure policy behavior.
- **T3 workflow foundation implemented:** `SmartModeWorkflowAdapter` lets target
  workflows preflight and apply role-based setup modes, including event,
  optional resume, and optional idle transitions. Overview/scouting-mode
  controller wiring remains pending.
- **T4 model foundation implemented:** `EventProbe` and
  `EventGatedAcquisitionCallback` provide bounded event probing, no-event
  skipped acquisitions, probe-error policy, and event-positive acquisition
  triggering. The real camera/pipeline/recording controller adapters still need
  to be wired.
- **T5 pending:** operator UI for target lists, manual target editing, workflow
  controls, and event-gated toggles is not implemented yet.

## Existing Building Blocks

### Tiling And Overview

Current usable pieces:

- `TilingController` already supports spiral tiling, live stitched overview,
  click-to-navigate, segmentation, and explicit cell iteration through
  `runCellTargeting(feature_callback=...)`.
- `TilingWorkflow` supports model-level spiral tiling and per-tile callbacks.
- `detect_cell_targets(...)` is model-level segmentation and can run without a
  GUI.
- `StitchedImage` provides pixel-to-stage conversion for target centroids.

Important gap:

- the model-level stitched overview path exists now, but the operator-facing
  flow still needs target-list save/load and manual target editing in the
  tiling UI.

### Target Selection

Segmentation targets are close:

- `TilingController.detectCellTargets()` detects targets and overlays markers
  without moving hardware.
- `TilingController.runCellTargeting(...)` can move through targets and call a
  per-feature callback.
- `TilingWorkflow.run_cell_targeting(...)` can iterate segmented targets when a
  `StitchedImage` and canvas origin are supplied.

Manual targets need a small UI/API addition:

- click-to-navigate currently moves the stage immediately;
- it does not maintain a persistent target list with labels, stage positions,
  source metadata, or saved overview coordinates.

### Recording And Acquisition

Current usable pieces:

- `RecordingController` exposes API calls for snapshots, finite-frame
  recordings, finite-time recordings, scan-once recordings, and scan timelapse.
- `RecordingManager` can be called directly for workflow-owned finite
  recordings.
- WFS-derived workflows show the preferred pattern for workflow acquisitions:
  call camera/trigger/laser facades directly and write outputs with explicit
  filenames.

Important gap:

- existing scan timelapse is scan-centric, not target-list-centric. It repeats
  the current scan, but does not move through arbitrary targets first.
- `RecordingController` is UI-oriented and uses widget state. A robust workflow
  should use a small `RecordingFacade` or a target-acquisition callback rather
  than driving the GUI controller from a background target loop.

### Setup Modes

Current usable pieces from the smart-microscopy work:

- `SmartMicroscopyModeService` maps workflow roles to setup modes.
- `applyRole(...)` is synchronous and returns `ApplyResult`.
- `preflight(...)` can block unsafe automated transitions.
- EtSnouty already uses the service behind
  `smartMicroscopyModeSwitchingEnabled["EtSnouty"]`.

Remaining gap:

- `SmartModeWorkflowAdapter` is available for model workflows, but overview
  acquisition and target-timelapse UI wiring still need to use it explicitly.

Recommended role mapping for Snouty:

- `scouting`: widefield overview / target search mode;
- `event`: tilted or light-sheet acquisition mode;
- `resume`: optional return-to-search mode between targets;
- `idle`: safe state at the end or after failure.

### Event Triggering

Current usable pieces:

- `EventTriggeredControllerBase` has the core loop:
  fast-frame pipeline -> coordinate transform -> slow scan trigger -> resume.
- `EtSTEDPipelineRunner` already isolates pipeline loading/execution.
- `EventTriggeredSessionState` tracks runtime state.
- EtSnouty now has mode switching but is still a standalone controller.

Remaining gap:

- a model-level frame-by-frame `EventProbe` exists, but the real
  event-triggered camera frame source, pipeline runner, coordinate transform,
  and scan/recording facade still need adapters.
- target iteration must not run a normal event controller at the same time as a
  workflow moves the stage, because both would own acquisition, stage motion,
  lasers, and scan triggering.

## Feasibility

### A. Tiled Target Timelapse

Feasible with moderate work.

The minimal production shape is:

1. run an overview tile scan;
2. build a persistent `TargetList`;
3. loop timepoints;
4. for each target, move XY stage, settle, optionally autofocus, acquire a
   finite recording/snap/scan;
5. save metadata with target id, stage coordinates, timepoint, source overview,
   and segmentation/manual-selection provenance.

Existing tiling and segmentation cover steps 1-2 for segmented targets. The new
work is the persistent target model and timepoint scheduler.

### B. Tiled Target Timelapse With Modes

Feasible after adding a workflow-facing mode adapter.

For Snouty:

1. apply `scouting` before overview acquisition;
2. acquire overview and targets in widefield mode;
3. before each target acquisition, apply `event`;
4. acquire target recording;
5. apply `resume` or `scouting` before moving/searching again;
6. apply `idle` at completion/failure if configured.

This should reuse `SmartMicroscopyModeService`, not direct setup-mode names in
the timelapse workflow. The workflow should preflight all required roles before
any stage movement or recording.

### C. Target Timelapse With Event-Gated Acquisition

Feasible, but this is the larger step.

The clean architecture is a reusable event probe:

```text
Target scheduler
  -> move to target
  -> apply scouting mode
  -> acquire fast frames for probe window
  -> run event-detection pipeline
  -> if event found:
       apply event mode
       trigger scan or recording
     else:
       skip acquisition or save negative probe metadata
  -> resume / next target
```

This should not be implemented by trying to drive an existing event-triggered
controller while a workflow owns the target loop. Instead, extract a
model-level event-detection/probe service from the base controller pieces.

## Proposed New Components

### `Target`

Small dataclass:

- `id`;
- `stage_xy`;
- optional `row_col` in overview pixels;
- `source`: `manual`, `segmentation`, `imported`;
- `props`: segmentation/manual metadata;
- `enabled`;
- optional `z_um`.

### `TargetList`

Container with:

- target ordering;
- enable/disable;
- serialization to JSON next to the overview;
- conversion from segmentation output;
- conversion from manual overview clicks.

### `TargetTimelapseWorkflow`

Workflow parameters:

- `targets`;
- `interval_s`;
- `n_timepoints` or `duration_s`;
- `settle_s`;
- `acquisition_callback`;
- optional autofocus callback;
- failure policy: continue, retry, abort;
- metadata root.

Execution contract:

- interval is measured per cycle, not per target, unless explicitly configured
  otherwise;
- one cycle visits every enabled target once;
- every acquisition produces metadata even on failure.

### `ModeTransitionFacade`

Thin wrapper around `SmartMicroscopyModeService`:

- `preflight(workflow_name, roles)`;
- `apply(role, required=True)`;
- safe `idle` fallback;
- logs role transitions into workflow metadata.

### `TargetAcquisition`

A callback/protocol with implementations for:

- snap image;
- finite-frame/time recording;
- scan-once recording;
- custom workflow, e.g. WFS, z-stack, Snouty scan.

This avoids coupling the scheduler to the Recording widget.

### `EventProbe`

Model-level service extracted from event-triggered controllers:

- load pipeline and parameters;
- maintain previous-frame context;
- run detection on fast frames;
- return detected coordinates plus optional analysis image/exinfo;
- optional coordinate transform.

The target scheduler can then decide whether to trigger acquisition.

## Recommended Implementation Sequence

### Phase T1: Target List And Overview Foundation — Model Foundation Implemented

- Done: `TilingWorkflow.run(...)` can optionally build and expose
  `StitchedImage`.
- Done: `Target` / `TargetList` model classes.
- Done: conversion from segmentation output to `TargetList`.
- Done: target-list JSON serialization.
- Extend `TilingController` with manual "add target" behavior separate from
  click-to-navigate.
- Save overview + target list metadata.

Acceptance:

- tiled overview can produce a stable target list from segmentation;
- manual targets can be added, removed, reordered, and saved;
- no target iteration occurs during passive detection.

Status:

- model/test coverage is in place for segmented target conversion, stitched
  overview construction, and target-list serialization;
- manual UI target editing and end-user metadata save flow remain pending.

### Phase T2: Target Timelapse Without Modes — Implemented

- Done: `TargetTimelapseWorkflow`.
- Done: move/settle/acquire loop with callback acquisition.
- Done: metadata for target id, timepoint, stage position, callback result,
  timing, and errors.
- Done: fake tests for order, timing, disabled targets, and failure policy.

Acceptance:

- workflow visits A -> B -> C -> A over multiple timepoints;
- per-target acquisition callback is invoked exactly once per enabled target per
  timepoint;
- failed target acquisition can continue to the next target.

### Phase T3: Setup-Mode Integration — Workflow Foundation Implemented

- Done: workflow-facing mode adapter/facade.
- Done: preflight of configured roles before target motion.
- Done: `event` role before target acquisition, optional `resume` after each
  acquisition, optional `idle` at workflow completion/failure.
- Pending: overview/probe `scouting` controller wiring.
- Pending: production Snouty target-timelapse setup mapping.

Acceptance:

- Snouty overview can run in widefield/scouting mode;
- target acquisition can run in tilted/event mode;
- failed mode application stops the target workflow and applies `idle`.

Status:

- target acquisition mode transitions are implemented and covered by unit
  tests;
- overview/scouting-mode integration still belongs in the controller/UI layer.

### Phase T4: Event-Gated Target Acquisition — Model Foundation Implemented

- Done: add model-level `EventProbe` shaped for reuse by the event-triggered
  pipeline, without importing controllers or Qt.
- Done: add probe-window parameters: max frames and minimum warmup frames.
- Done: add event-gated acquisition callback:
  if no event, save negative metadata and continue;
  if event, apply `event` and trigger scan/recording.
- Done: add fake tests for A -> B -> C -> A cycling with events only on selected
  targets/timepoints.
- Pending: wire `EventProbe` to the real camera frame source, ET pipeline
  runner, coordinate transforms, and scan/recording facade.
- Pending: dwell-time-based probe windows.

Acceptance:

- target loop can probe without triggering acquisition when no event is found;
- event-positive targets trigger exactly one acquisition;
- event-negative targets do not trigger scan/recording;
- probe errors obey the target workflow failure policy by default;
- mode transitions are logged for every target when mode integration is enabled.

### Phase T5: UI And Operator Workflow

- Add a target table next to the tiling overview.
- Add manual target creation/edit/delete.
- Add a "target timelapse" panel: interval, cycles, acquisition preset,
  mode workflow, event-gated toggle.
- Add progress and stop controls that are safe across stage motion, recording,
  and scan execution.

## Main Risks

- Coordinate drift between overview and later target visits. Mitigation:
  optional periodic overview refresh, autofocus, or fiducial correction.
- Concurrency conflicts with live view, recording, event controllers, and stage
  motion. Mitigation: one workflow owns acquisition/stage during the run.
- Recording from a background callback through the GUI controller is fragile.
  Mitigation: use a workflow-safe recording facade or direct manager call.
- Setup modes may contain laser state. For Snouty, keep laser emission owned by
  the acquisition workflow unless deliberately changed.
- Segmentation on downsampled overviews can shift centroids. Store pixel size,
  overview scale, and canvas origin; validate with manual target click tests.
- Event-triggered logic currently assumes a continuous field of view. Target
  probing needs a bounded single-target API rather than full controller arming.

## Recommendation

Build this as a workflow stack, not as more special cases in EtSnouty or
TilingController.

The model-level foundation for **T1-T4** is now in place. The next useful
deliverable should be **T5 plus controller adapters**: expose target lists and
target timelapse in the tiling UI, wire overview/scouting modes, and connect
the event probe to the real camera/pipeline/recording stack.

Keep the integration incremental: first make tiled target timelapse usable with
a simple snap/finite-recording callback, then enable mode-backed Snouty
overview/acquisition switching, and only then turn on real event-gated
recording for selected setups.
