# WFS Workflows → ImSwitch Experiments

Port the eight WFS workflow modules
(`/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/workflows/`) into
`imswitch/imcontrol/model/workflows/` so they can be driven from ImSwitch's
scripting module.

## Goal

A user opens the Console / Scripting widget, runs:

```python
from imswitch.imcontrol.model.workflows import RecordingWorkflow, RecordingParams
from imswitch.imcontrol.model.workflows.facade import build_facade_from_master

facade = build_facade_from_master(api._master)   # one-liner per script
wf = RecordingWorkflow(facade, RecordingParams(...))
wf.run()
```

…and gets a polarisation-resolved acquisition saved to disk.

## Key design decisions

1. **Adapter layer.** WFS workflows talk to a custom `Microscope`
   aggregator (`om.laser_con.set_constant_power`, `om.cam.get_data`,
   `om.trig.snap_trigger`, …). Building a `MicroscopeFacade` that exposes
   the WFS-shaped interface and delegates to ImSwitch managers underneath
   lets each workflow port nearly 1:1, makes them easy to review against
   the WFS originals, and keeps a clean seam for headless tests.
2. **Model-only.** Workflows live under `model/workflows/` and depend on
   nothing in `controller/` or `view/`. No PyQt5/qtpy. No tkinter dialogs;
   all parameters arrive via typed dataclasses passed to `__init__`.
3. **No napari display from workflows.** The WFS recording workflow
   pokes napari directly; we drop that from the ports and defer
   visualization to a future widget task.
4. **No widget reads.** WFS reads spin-box values like
   `self.widget.con_view.SB_488_power.value()` from inside workflows.
   For the ImSwitch port, the parameter dataclass carries these values.

## Phasing

### Phase 0 — Facade and pure utilities (this phase)

- Plan persisted: this document.
- Confirm `spiral.py` and `stitched_image.py` already ported under
  `imswitch/imcontrol/model/workflows/`.
- Add `imswitch/imcontrol/model/workflows/facade.py` with sub-facade
  classes (`LaserConFacade`, `CamFacade`, `TrigFacade`, `StageConFacade`,
  `ZStageConFacade`, `RotatorFacade`) and a `MicroscopeFacade` aggregator.
- Add `imswitch/imcontrol/model/workflows/mock_facade.py` —
  `MockMicroscopeFacade` for headless tests; records every call for
  assertions and returns canned `np.ndarray` from `cam.get_data()`.
- Add `imswitch/imcontrol/_test/unit/test_microscope_facade.py` covering
  the mock's call-recording contract.

### Phase 1 — Single-device workflows (parallel agents)

Four independent ports — run as parallel subagents (see prompts at end
of this doc):

- `RecordingWorkflow` — H/V polarimetric acquisition.
- `ZStackWorkflow` (+ `run_autofocus`).
- `CWSTARSSWorkflow` — photoselection sequence.
- `CalibrationWorkflow` — QWP/HWP sweep + segmentation check.

Each delivers: implementation + tests in one commit; no push.

### Phase 2 — Composite workflows (sequential)

- `TilingWorkflow` (depends on `RecordingWorkflow`, `StitchedImage`).
- `DefocusScanWorkflow` (depends on `RecordingWorkflow` + `ZStackWorkflow`).
- `SerialCWSTARSSWorkflow` (depends on `CWSTARSSWorkflow`).
- `MultiWellTilingWorkflow` (depends on `TilingWorkflow` + autofocus).

### Phase 3 — Example scripts and docs

- One example script per workflow under
  `imswitch/_data/user_defaults/scripts/wfs/`.
- Short scripting-cookbook page in `docs/`.

## Open questions / deferred

- Visualization (napari layer updates) — handled in a later widget task,
  not in workflows themselves.
- Per-laser modulation-power semantics for `set_triggered_mode`/
  `set_constant_power` — the facade currently passes through to
  `LaserManager.setValue`/`setModulationPower`. May need refinement
  once Phase 1 surfaces concrete needs.
- `TrigFacade.Sendsignal` shape — the WFS pulse-scheme arrays
  (`tWindowM`, `laserMod_1..3`) translate to `PulseStep` lists in
  ImSwitch. Phase 1 prompts include the mapping detail.

## Parallel subagent prompts

The four Phase-1 prompts live in the conversation that produced this
plan. Each is self-contained and follows the project's standard
`File / Task summary / Todo / Do NOTs / Implementation / Sanity checks
/ Commit instructions` format.
