# Smart Microscopy Pipeline Designer

Status: **Accepted 2026-11-02; revised 2026-11-03 after an external review** (§11: execution and action contracts tightened, the coordinate contract pulled into the offline phase, v1 control flow made explicit). Phases 0, E, 1 and 2 implemented on `smart-pipeline` (one branch, which also carries workflow schema 2); next: Phase 3 (the designer).
Branch: `smart-pipeline`. Related: `improcess-workflow-editor-2.md`,
`smart-microscopy-mode-switching.md`, `tiled-target-timelapse-smart-events.md`,
`etsted-2-0.md`, `etmonalisa-2-0.md`, `scanner-calibration-widget.md`.

## 1. Goal

A graph that ImControl loads and runs as a **live control loop**:

```
live detector ──▶ process (threshold, segment, …) ──▶ numbers (a coordinate)
      ▲                                                       │
      │                                               transform (slot)
      │                                                       ▼
   resume ◀──────── action (move a positioner, run a scan, snap, …)
```

designed in the ImProcess **workflow editor** (boxes and cables, typed ports),
saved as a file, and chosen in a small ImControl panel with Arm / Stop — the
Script Runner ergonomics, for graphs instead of scripts.

Today the three event-triggered controllers (`EtSTED`, `EtMonalisa`,
`EtSnouty`) each *are* one such loop, hard-wired: a fast camera, a user Python
function, a polynomial transform, a triggered scan. The pipeline designer is
the general version of that loop. Those controllers are not touched until the
general loop has run on a rig (§8, Phase 5).

## 2. What exists, and what is missing

### ImProcess workflows (`imswitch/improcess/workflows`, `model/workfloweditor`)

Reusable as they are:

- the **document model** — steps, `step.port` references, *kinds* as port
  types (`model/result.py:28-46`: image, labels, table, curve, localization,
  rgb, composite, recording, any), `WorkflowDocument`, `nodes`, `graph`,
  `RunSession`; all Qt-free;
- the **editor window** (`view/workfloweditor/editor.py:82 MainWindow`),
  which depends on a `Catalog` and a duck-typed `host`
  (`runInteractive`, `on_run_progress/step/ended`, optional
  `currentRecording`, `showPreview`, …); it imports nothing from ImProcess's
  hardware-free analysis beyond the catalog;
- **processors** (`processors/base.py:172 Processor`) and their declarative
  `param_spec()`; the **Python step** (`processors/python_step`) with its
  `# ports:` header and worker-thread run with cancel;
- `Save` with placeholder paths.

Missing for a control loop:

- the runner is **one pass** per call (`workflows/runner.py:214 run`), a new
  `QThread` per run (`controller/WorkflowController.py`); nothing drives it per
  frame and there is no live source — the ImControl→ImProcess "live" path is
  the zarr folder watcher (`live/discovery.py`), results flow back only as a
  display image (`sigLiveReconResult`);
- the **step kinds are a closed set** (`workflows/steps.py:185 _STEP_TYPES`:
  source, reconstruct, consolidate, process, save); there is no *action* or
  *gate* step, and `Save` is the only sink;
- a Python step **cannot output a number**: a 0-d array is refused
  (`python_step/context.py:397`), a 1-D array becomes a curve; there is no
  `points` or `value` kind;
- no threshold/centroid processor yields a coordinate (`regionprops` lives
  in `analysis/roi_measurements.py` but is not a step).

### ImControl (`imswitch/imcontrol`)

Reusable as they are:

- **frames**: `detectorsManager.acquire([det], LeasePurpose.EVENT_STREAM)`
  keeps a detector polled without live view; frames arrive on
  `commChannel.sigUpdateImage(detectorName, image, init, scale, isCurrent)`
  (`CommunicationChannel.py:83`, producer `DetectorsManager.LVWorker`);
  `controller/_fresh_frame.grab_fresh_frame` gives a frame exposed *after*
  a move;
- the **Et loop pieces** behind `EventTriggeredControllerBase`
  (`controllers/EventTriggeredBaseController.py:107`): `EtSTEDPipelineRunner`
  (user function `f(img, prev_frames, binary_mask, testmode, exinfo,
  *params) -> (coords, exinfo)`), `EtSTEDTransformService` (swap/invert flags
  + 20-coefficient polynomial from `transform_pipelines/*.csv`),
  `EtSTEDTriggeredScanRunner` (axis centers → `scanCoordinator.arm` or
  `scanWorkflow.start_external_recording`), `EventTriggeredSession`
  (Experiment / Validate / Visualize run modes);
- **actions with a completion**: `ScanRunHandle` (`model/scan_request.py:35`:
  `wait`, `done`, `add_done_callback`), `ScanWorkflowService`
  (`WorkflowServices.py:189`, marshals to the UI thread),
  `api.imcontrol.reserve(...)` handles whose `.positioner(name, axis).apply(v,
  deadline_s)` return a `ControlResult`, `PositioningRequest`
  (`model/workflows/positioning_request.py` — the "request, don't call" seam
  that keeps the GUI event loop alive);
- **modes**: `SmartMicroscopyModeService` roles scouting / event / resume /
  idle / validation with `preflight` hazards and the per-setup policy;
- the **scripting API**: the flat `@APIExport` surface on `api.imcontrol`
  (detectors, positioners, lasers, scans, recording, modes, reserve,
  `signals()`), with type hints and docstrings `generateAPI` already reads;
- the Qt-free `model/workflows` layer: `EventProbe`, `SmartModeWorkflowAdapter`,
  the WFS-shaped hardware `facade.py`.

Missing:

- a **persisted camera↔stage / camera↔scanner calibration** with a known
  frame of reference; the Et polynomial is per-controller and file-named by
  timestamp; tiling has only a µm/px scale plus orientation
  (`TilingController._canvasPixelToStage`). **Left as a slot** (§5) — the
  transforms themselves are out of scope here;
- a **positioner-moved** signal (only `sigSetXYPosition` exists, unconsumed);
- the Et loop runs as **UI-thread slots**; the 2026-06 workflow-readiness
  audit asks for a runtime context (`should_stop`, sleep, progress, cleanup).

## 3. Shape

Three parts, each on its own side of the module boundary.

| Part | Lives in | Qt | What it is |
|---|---|---|---|
| **Pipeline document** | `imcommon/model/smartpipeline/` | no | step types, kinds, file format, validation. Shares the step/ref/kind vocabulary and the editor model with ImProcess workflows. |
| **Engine** | `imcontrol/model/smartpipeline/` | no | runs a document tick-by-tick against *ports*: a frame source, a step runner, an action backend, a transform registry. Mock backends for tests and for offline replay. |
| **Designer + panel** | editor window (shared) + `imcontrol` widget | yes | the workflow editor opened with the pipeline catalog and a pipeline host; a `SmartPipeline` dock with Choose / Arm / Stop / mode / status / event log. |

Naming: the thing is a **smart pipeline**, not a third "workflow"
(`imcontrol/model/workflows` and `improcess/workflows` already collide).

### 3.1 Step types

| Step | Ports in → out | Notes |
|---|---|---|
| `source` | → `image` (+ `frame` meta: detector, index, t, scale) | *live*: a detector under an `EVENT_STREAM` lease; *clock*: periodic snap (EtSnouty's clock mode); *replay*: an ImProcess `Source` (a recording, frame by frame) — the same file runs offline. |
| `process` | kinds as today | **ImProcess processors unchanged.** Plus new small ones that ImProcess wants anyway: `threshold` (image→labels), `regionprops` (labels→`points` table with area/intensity), `pick` (points→points: largest / brightest / nearest / first), `points-to-value`. |
| `python` | declared `# ports:` | the ImProcess Python step with **`points` and `value` outputs allowed**, a per-step `state` dict that survives across ticks (prev frames, exinfo), and the Et function contract offered as a recipe so existing `analysis_pipelines/*.py` run unchanged through an adapter. |
| `transform` | `points@A` → `points@B` | the **calibration slot** (§5). The engine refuses a pipeline whose action gets points in the wrong frame. |
| `gate` | any → same (+ `event`) | `condition` (non-empty / value in range), `min-interval`, `once`, `count-limit`, `busy` (drop ticks while an action runs). Pure logic, no hardware. |
| `action` | `points` / `value` / `event` → `event` (done, result) | hardware. Generated from the scripting API (§4) plus a few composite ones: `move-to` (positioner, axis map), `run-scan` (axis centers → `EtSTEDTriggeredScanRunner` / `ScanRunHandle`), `snap`, `record` (finite), `apply-role` (smart mode), `run-script` (a script file with `scriptInput()` values fed from ports — the Script Runner bridge). |
| `sink` | any | `event-log` (CSV/JSON per tick, like the Et event logs), `overlay` (points onto the live view layer), `save` (ImProcess `Save`). |

New kinds, added to `PORT_KINDS` in ImProcess (not a schema bump; kinds are
open strings checked against the registry): `points` (N×2/3 float table with
a `frame` tag and the source frame index), `value` (scalar with unit),
`event` (bool + payload dict).

### 3.2 Runtime semantics (the part worth agreeing on first)

- **Tick-driven.** A source emission is a tick. The engine runs the steps in
  list order (topological, as the editor keeps them) on **one worker thread**
  per armed pipeline, with a `CancelToken` like `WorkflowController`. Frames
  arrive by queued signal into a bounded queue (size 1, *latest wins*); a
  frame that arrives mid-tick is dropped, as `runPipeline` drops them today.
- **Actions are asynchronous.** An action returns a completion (`ScanRunHandle`,
  `PositioningRequest`, a `ControlResult` future). The tick ends there; the
  pipeline enters **busy** until the completion fires, then runs the `resume`
  part. Busy + resume map one-to-one onto the Et pause/resume modality and the
  smart-mode roles `event` → `resume`. An action is never called from the
  worker thread directly: it goes through the services that already marshal
  (`ScanWorkflowService`, reservation handles, `PositioningRequest`).
- **State.** Steps may keep state per step id (`StepState`), reset on arm.
  The Python step exposes it as `state`.
- **Run modes**, as `EventRunMode` today: *Visualize* (actions are no-ops,
  overlays and logs on), *Validate* (actions logged with their arguments,
  roles applied, nothing moves), *Experiment*.
- **Arm = preflight, then take.** Validate the document; resolve every device
  name against `setupInfo`; `reserve()` the devices the actions name
  (owner = pipeline, so the GUI and scripts are refused while armed);
  `SmartMicroscopyModeService.preflight` on the roles the pipeline declares;
  acquire the `EVENT_STREAM` lease; apply `scouting`. Any refusal arms
  nothing.
- **Stop / failure / close = idempotent cleanup**, in this order: stop the
  source, cancel the running action's completion if it can be, apply `idle`,
  release leases and reservations, write the event log footer. Application
  close drains like `ScriptExecutor.shutdown`, bounded.
- **Never per frame:** mode roles, laser power, reservations. Those are arm /
  action / stop events — the smart-mode plan's rule, kept.

### 3.2a v1 control flow, as implemented in Phase 1

The review (§11, finding 4) asked for the graph's semantics to be written
down before anything moved. v1 is: **one source, a processing DAG, one
sequential action chain, sinks anywhere.**

- A tick runs the steps in file order. A step whose input is missing — a
  gate upstream did not open, or the step that made it failed — is
  *suppressed* for the tick; a processing failure is logged and the tick
  goes on (overlays keep drawing). A gate always emits its `fired` event,
  so a sink on it logs the "no" ticks too.
- The **action chain** is the action steps in file order. The first one whose
  inputs are there starts and the tick ends; the pipeline is *busy* until its
  completion settles, then the next action starts with the values the tick
  had when the chain started (an action may read the `done` event of the
  one before it), and so on. While busy, ticks still run their processing and
  sinks, a `busy` gate closes, and an action that would start is counted as
  *dropped-busy*. A failed, cancelled, timed-out or refused action **stops the
  pipeline**: failure enters cleanup, never the next tick.
- **Completion contract** (finding 2): a started action returns a completion
  with `done` (outcome final: ok / failed / cancelled / timeout), `ended`
  (nothing still executing on the hardware), `wait`, `abort()` (asks, and
  says whether it can) and the run *epoch* it was started in. Stop aborts the
  pending completion and drains, bounded by a cleanup budget; an action that
  did not end is reported in the summary as `unfinished_action` (Phase 2
  keeps its devices reserved, the control executor's quarantine). A completion
  from an earlier arm is ignored. Per-step `timeout_s` is a timeout.
- **Freshness** (finding 6): a `Frame` envelope carries the detector, a
  sequence number, a timestamp, the exposure and the pixel grid
  (`FrameIdentity`: shape, binning, ROI offset, axis order). Ingress is a
  one-slot *coalescing inbox* — latest wins, duplicates and late sequences
  refused, a maximum age, drops counted — that the producer never blocks on
  and never runs a step in. After a chain completes, frames with a sequence
  not above the last one seen are stale and dropped. A frame whose grid
  differs from the one the pipeline was armed on stops the pipeline.
- **Run modes** (finding 5), one effects table (`MODE_EFFECTS`): *Visualize*
  never dispatches (actions logged as skipped, the chain continues at once),
  applies no roles, reserves nothing; *Validate* checks every action — its
  catalogue entry, parameters, the coordinate contract — and logs it as
  "would …", dispatching nothing; *Experiment* dispatches. In Experiment a pipeline
  with hardware actions must name its `scouting` and `idle` roles or it
  does not arm (Validate applies no role, so it needs none). The generic `call-api` action is **dropped from
  v1**; `run-script` needs an explicit resource and lifecycle contract before
  it exists.
- **Clock and state** (finding 7): gates keep their clocks on the engine's
  injectable clock; arming resets every step's state, the gate counters and
  the chain; a replay source stamps `t = index × interval`, so a replay is
  deterministic and the ImProcess editor's cached partial runs are not what a
  pipeline replay is. An empty set of points is a normal tick.

### 3.3 Offline first

Because `source: replay` is an ImProcess `Source`, a pipeline opens in the
ImProcess editor on a recording and runs there with the action backend stubbed
(actions become log lines, overlays still draw). That gives the designer the
editor's Run / Run-to-selected / previews for free and makes the detection
part of every pipeline a plain, testable workflow. The live run in ImControl is
the same document with real ports.

## 4. Actions from the scripting API

"It needs to know the standard hardware control functions." The scripting
API is already the catalogue: `generateAPI` sees every `@APIExport` function
with its signature and docstring. An `ActionCatalog` turns a **curated
allow-list** of them into action step types:

- parameter fields from the signature (the way `EtSTEDPipelineRunner` and the
  config editor build forms); device-name parameters become dropdowns filled
  from `setupInfo` (`getDetectorNames`, `getPositionerNames` + axes,
  `getLaserNames`, scan sources, setup-mode names);
- each entry declares which parameters may be **bound to a port** (`points` →
  `(x, y)` of `setPositioner`, `value` → `setLaserValue`), what it
  **reserves**, whether it is **hardware-critical** (laser power, scan start:
  shown with a warning in the inspector, refused in *Validate* unless
  whitelisted), and what **completion** it returns;
- a generic `call-api` action for anything not curated, parameters typed in,
  no port binding, always logged.

First curated set: `setPositioner` / `movePositioner`, `setLaserActive` /
`setLaserValue`, `runScan` (+ axis centers), `snapImage`, `startRecording` /
`stopRecording`, `applySetupMode` by role, `autoFocus`, `runScript` with
inputs. Everything in this list exists today; nothing new on the hardware side.

## 5. The transform slot

Defined here, implemented by you. **The coordinate contract landed in Phase 1
(finding 3)**, not Phase 4: `imcommon.model.smartpipeline.frames` has the
frame tags (`detector:Camera`, `stage:XYStage`, `scanner:galvo`), the
`FrameIdentity` that makes a detector tag a coordinate system (shape,
binning, offset, axis order), and `check_points` — the check every action
runs at dispatch, on Python-produced values too: coordinates present, 2 or 3
columns, finite, the frame tag *exactly* the one the action takes, bounds
when known, the grid the one armed on. `transforms.py` has the protocol,
`identity` and `scale-flip` (pixel size, origin, swap, invert, offset, and a
`relative` flag that makes the result a displacement rather than a
position); the EtSTED polynomial wrapper and calibration files remain Phase
4. The protocol as planned:

```python
class CoordinateTransform(Protocol):
    id: str                      # e.g. "etsted-poly", "affine-2d", "identity"
    source_frame: str            # "detector:Camera" (pixels)
    target_frame: str            # "stage:XYStage" (µm) | "scanner:galvo" (V)
    def apply(self, points, context) -> points: ...
    def to_dict(self) / from_dict(cls, d)       # persisted calibration
```

Calibrations live in `<ImSwitchConfig>/calibrations/<id>.json`, picked by name
in the `transform` step. Shipped: `identity`, `scale-flip` (pixel size,
`swapXY`/`invertX`/`invertY` — what `EtSTEDInfo` has), and a wrapper over
`EtSTEDTransformService` for the existing polynomial files. Frame tags on
`points` are what lets validation refuse *camera pixels into a stage move*
before anything is armed. Calibration *procedures* (click-calibration,
`EtSTEDAutoCalibration`) stay where they are and gain a "save as calibration
`<id>`" at the end — later.

## 6. Where the editor lives

The designer is the ImProcess editor window with another catalog and host.
**Decided 2026-11-02: A.** `view/workfloweditor` and the Qt-free
`model/workfloweditor` move to `imcommon`, ImProcess re-exports the old
names, and ImControl never imports ImProcess. (The alternative, importing
lazily from ImProcess and greying out "Design…" when the module is off, was
smaller but kept a cross-module import.)

What the move has to separate:

- **Generic** (moves): `WorkflowDocument`, `nodes`, `graph`, `recipes`,
  `Catalog` / `PluginEntry` as *types*, `MainWindow` and the canvas, palette,
  inspector, `ParamForm`, start page, output and issue panels. These know
  steps, ports and kinds, not processors.
- **ImProcess-specific** (stays): `build_catalog` over `PluginRegistry`,
  `RunSession` if it keeps importing `runner`/`sources`, `WorkflowController`
  as the host, the "From recording" / "Pin" / "Show in viewer" host methods.
  The window must keep working with a host that lacks them (it already treats
  them as optional).
- **Shared vocabulary** the document needs too: `PORT_KINDS` and `result_kind`
  (`model/result.py`) — the kind *names* move to `imcommon`, the result
  classes stay.

**Inventory 2026-11-02 — the cut is wider than the two packages.** The
editor model is Qt-free in its own files but not transitively: `nodes.py`
and `document.py` import `workflows/steps.py`, which imports
`sources.SourceSpec` at top level and, lazily, `processors/base.py` (which
imports `qtpy` at import time), `save_protocol` and `plugin_contract`;
`catalog.PluginEntry.output_spec` also reaches `processors/base`; `session`
reaches `runner` and `sources`; `recipes`, `preferences`, `folders` and the
start page reach ImProcess snippets, options file and folder names. So the
move is:

- to `imcommon/model/`: `kinds.py` (the `*_KIND` constants, `RESULT_KINDS`,
  `PORT_KINDS`, `result_kind`), `param_spec.py` whole, `workflows/steps.py`
  and the pure part of `sources.py` (`SourceSpec`, `SOURCE_KINDS`,
  `SourceError`, `parse_binding`, `spec_mismatches`), with the four lazy
  ImProcess hooks replaced by a pure signature helper (`call_output_spec`
  without Qt) and by catalog-supplied `contract_problem`, `normalize_format`
  / `save_formats`;
- to `imcommon/model/workfloweditor/`: `graph`, `commands`, `nodes`,
  `previews`, `document` (minus `from_result`, which becomes an ImProcess
  helper), `catalog` (types only), `session` (`RunSession` only; `execute`,
  `close_source`, `source_stem_of` become host hooks), `folders` and
  `preferences` with the folder and options path passed in;
- to `imcommon/view/workfloweditor/`: everything, with recipes, the source
  file filter, the Python-step defaults/snippets and the window title
  supplied by the host or catalog;
- staying in ImProcess: `build_catalog` + `_entry`, `recipes`,
  `session.execute`, `document.from_result`, `runner`/`replay`/`batch`/
  `runtime`, `WorkflowController`, and re-export shims at every old path
  (the controller, ten test modules and two `tools/` scripts import them).

There are no resource files. A guard test like
`imcommon/_test/test_acquisition_metadata.py:171` (forbidden-import set)
is the acceptance check. Phase E in §8. Phases 0 and 1 do not wait for it.

## 7. Safety boundaries

Same as the Et 2.0 plans: no hardware timing constants, waveforms, laser
powers, TTL settings or calibration constants change. The engine adds no
hardware call that a script cannot make today — every action is an API
function or an existing service. Hazard policy comes from
`smartMicroscopyModePolicies`. A pipeline must name its roles explicitly;
without a `scouting`/`idle` pair it arms only in Visualize.

## 8. Phases

| Phase | Deliverable | Where | Done when |
|---|---|---|---|
| **0 — Open the workflow model** (implemented 2026-11-02, `feat/workflow-model-open` 067889bc) | step-type registry instead of `_STEP_TYPES`; kinds `points` / `value` / `event`; Python step emits them + `state`; `threshold`, `regionprops`, `pick` processors | ImProcess | an ImProcess workflow thresholds a recording and shows a points table; existing workflow files load unchanged |
| **1 — Document + engine, offline** (implemented 2026-11-03, `feat/smart-pipeline-phase1`) | `imcommon.model.smartpipeline` (step kinds `live`/`transform`/`gate`/`action`/`sink` in the registry, `SmartPipelineDocument`, `validate_pipeline`, frames and the coordinate contract, `identity`/`scale-flip`, `ActionSpec`/`ActionCatalog`); `imcontrol.model.smartpipeline` (`Engine`, ports, `CoalescingInbox`, `SimpleCompletion`, `EventLog`, mocks); `improcess.workflows.smartpipeline` (`ProcessorStepRunner`, `RecordingFrameSource`) | imcommon / imcontrol model / improcess | `test_smartpipeline_replay.py`: a recording replays through threshold → regionprops → pick → gate → scale-flip → `move-to` and logs "would move-to XYStage to (y, x)" per detected cell; Experiment mode moves the mock stage |
| **2 — Live run in ImControl** (implemented 2026-11-03, `smart-pipeline`) | `model/smartpipeline/live_source.py` (`LiveFrameSource`: the `EVENT_STREAM` lease, the envelope with the detector's grid and exposure, duplicate frames recognised, `CoalescingInbox` to the worker), `live_actions.py` (**owner-aware adapters**, finding 1: `move-to` drives one positioner axis per coordinate column with the pipeline's reservation token on `setPosition(..., owner=)`, through the control executor with a deadline — a stage that does not return is quarantined and stays reserved; `snap` through the Recording panel, marshalled; `log`), `catalog.py` (the v1 specs, shared with the mock), `run.py` (`PipelineRun`: arm = check, reserve, `scouting`, lease; one worker; stop = idle, release all but quarantined, lease), the `SmartPipeline` panel (Choose / Reload / mode / Arm / Stop / status with ticks and drops / event log / Save log). Processing steps reach ImProcess's processors by a lazy import when that module is loaded. Shipped: `smart_pipelines/threshold_move_demo.json` and `calibrations/camera_to_mock_xy.json` for `example_no_hardware.json` (its `example_mock.json` stage cannot move: an `MHXYStageManager` without a port) | imcontrol | `test_smartpipeline_live.py`: a frame through the lease → threshold-like detection → `move-to` moves the mock stages with the token; a device another owner holds refuses the arm; a missing setup mode releases what was reserved; a quarantined stage stays reserved until it returns; stop is idempotent from any thread. **Deferred to Phase 3+:** the mode service's hazard `preflight` for the pipeline's roles (the service maps `(workflow, role)` through `setupInfo`, the pipeline maps roles itself; the run applies the mode by name and refuses a missing one), a time-based fresh-exposure boundary after a move (the detector poll carries no sequence or timestamp; v1 is sequence-based), forms generated from the catalogue, actions for scans, recording and autofocus (each needs its own completion adapter: `ScanRunHandle` cannot cancel, `autoFocus` returns `None`). |
| **E — Editor to imcommon** (implemented 2026-11-02, `feat/workflow-editor-imcommon`, stacked on Phase 0) | the split in §6; ImProcess re-exports; no behaviour change | imcommon / ImProcess | every ImProcess workflow-editor test passes unchanged against the new paths; `imswitch.improcess` is not imported by `imcommon` |
| **3 — Designer** | editor opened from the panel with the pipeline catalog and host; overlay sink on the live view; recipes "threshold → move", "Et-style triggered scan" | shared / imcontrol | design, dry-run on a recording, arm live, without leaving the GUI |
| **4 — Transform slot** | Et polynomial wrapper, calibration files under `<ImSwitchConfig>/calibrations/`, the detector's real `FrameIdentity` from the camera (ROI, binning), "save as calibration" at the end of the calibration procedures (the protocol, `identity`, `scale-flip` and frame-tag validation landed in Phase 1) | imcommon / imcontrol | a pipeline with a camera→stage transform arms; one without is refused with the reason |
| **5 — Et on the engine** | EtSTED recipe = source → Et-function adapter → polynomial → busy gate → `run-scan` with roles; behind a rollout flag like `smartMicroscopyModeSwitchingEnabled` | imcontrol | EtSTED's Validate mode produces the same event log as the legacy controller on the same recording; rig validation pending |

Phases 0, 1 and E are independent of each other; 3 needs all three.
EtSnouty's duplicated loop and the positioner-moved signal are noted, not in
scope.

## 9. Open decisions

1. ~~§6 — editor to `imcommon` (A) or lazy import (B).~~ Decided: A.
2. **Decided: worker thread.** Engine thread: one worker per armed pipeline (proposed) vs. UI-thread slots
   as the Et base does today. The worker needs every action to go through a
   marshalling service; all listed ones already do.
3. **Decided: generated.** Action catalogue: generated from the API with an allow-list (proposed) vs.
   a hand-written set of six. Generated is more code once, then free for every
   new `@APIExport`.
4. **Decided: strict.** Should the `transform` step be mandatory between a `points` producer and
   any hardware action (strict), or may `identity` be implicit when source and
   target frames match (convenient)? Proposed: strict, with `identity` one
   click away.
5. **Decided: run EtSTED on the engine behind the flag.** Scope of Phase 5: recipe only, or actually run EtSTED on the engine behind
   the flag. The review suggests keeping the recipe/parity work apart from
   enabling the engine on a rig: two steps inside Phase 5.
6. **Decided (review): hand-written action adapters, generated forms.** The
   catalogue's *forms* come from the `@APIExport` signatures; the *execution*
   of each action is a curated adapter with an explicit resource and
   lifecycle contract (3.2a). No generic `call-api` in v1.
7. **Kept strict after the review.** The reviewer favours an implicit
   identity when the complete coordinate frames match; the frames now *are*
   complete (tag plus `FrameIdentity`), so the engine could allow it. Kept
   strict for v1: a `transform` step is the one place a reader of a pipeline
   sees that a coordinate changes hands. Revisit when the designer (Phase 3)
   can add the identity step itself.

## 10. Main risks

- **Rate.** A processor that takes longer than the frame interval makes the
  loop drop most frames; fine for event detection, wrong for tracking. The
  panel shows ticks/s and drops/s from day one.
- **Host contract.** The editor's `host` duck type was written for one caller;
  a second caller will find the implicit bits. Phase 3 is where that cost lands.
- **Two vocabularies.** ImProcess "workflow" and ImControl "workflows" are
  different things; the pipeline must not become a third set of step classes
  that drifts. Hence Phase 0 opens the *existing* model instead of copying it.
- **Safety of generated actions.** Allow-list plus hardware-critical flags plus
  Validate mode; still, the first rig run of any new pipeline is Validate.

## 11. Review record

External review of the accepted plan, 2026-11-03 (seven findings, in the
reviewer's priority order), and what Phase 1 did about each:

| # | Finding | Resolution |
| --- | --- | --- |
| 1 | Reservations would block the pipeline's own actions: `setPositioner` reaches the manager without a token, `reserve()` takes waveform outputs by default, scan admission carries no token | Phase 2's scope is now *owner-aware action adapters* (phase table), designed before the catalogue is generated; each adapter carries the reservation and declares what it reserves. Nothing in Phase 1 calls hardware. |
| 2 | Completion, cancellation and hardware stopping are different contracts; `autoFocus` returns `None`, `ScanRunHandle` cannot cancel | The completion contract in 3.2a: `done`, `ended`, `abort()`, timeout, epoch; stop drains bounded and reports an action that did not end. Tested with a completion that is cancelled but not ended. |
| 3 | Coordinate validation must precede the first live movement; `detector:Camera` is not a coordinate identity; displacement vs. position | Frames and `check_points` in Phase 1 (§5); `FrameIdentity` on every frame, refused at tick when it changes; `scale-flip` has `relative`; every dispatch checks Python-produced values too. Phase 2 moves nothing that did not pass through this. |
| 4 | Control-flow semantics undefined | 3.2a: one source, a DAG, one sequential action chain; suppression; failure enters cleanup; the engine owns the loop. |
| 5 | Validate mode and unrestricted actions weaken the guarantees; `applyRole` changes hardware; `call-api` and `run-script` have unknown resource use | `MODE_EFFECTS` table; Validate applies no roles and dispatches nothing; `call-api` dropped from v1; `run-script` deferred until it has a contract (decision 6). |
| 6 | The frame queue needs a freshness contract; `sigUpdateImage` has no sequence or exposure; `grab_fresh_frame` is not unconditional | `Frame` envelope and `CoalescingInbox` in Phase 1 (duplicates, late sequences, max age, replaced frames all counted); stale-after-chain rule. Phase 2 stamps sequence and exposure at the lease and refines the boundary with the completion time. |
| 7 | Editor reuse needs step descriptors; replay needs a clock and a reset policy; empty point sets were refused | Step kinds declare `ports` and `declared_kind(kinds)` and the schema's `declared_kinds` asks them (the inspector's class dispatch is Phase 3's descriptor work); the engine clock is injectable, arm resets, the replay source stamps simulated time; `PointsResult` accepts an empty set and `regionprops`/`pick` return one. |

The reviewer's preferences on the open decisions are in §9 (6 and 7).

