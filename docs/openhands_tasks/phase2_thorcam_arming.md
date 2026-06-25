# Task: ThorCam TSI — ensure-armed startAcquisition + Bulb chunk fix (Phase 2)

You are working in the ImSwitch2 repository on branch `feat/live-reconstruction`.
This is Phase 2 from `docs/advanced_scan_triggered_recording_audit.md`. Phase
0+1 already landed in the working tree (uncommitted): `getChunk()` returns 3-D
real frames or an empty `(0, H, W)` chunk and never fabricates, the mock is
trigger-gated, and `test_thorcam_tsi_recording_contract.py` exists. Build on
that green tree. Keep this change tightly scoped to the ThorCam TSI path + its
tests. Do not commit.

## Goal A — `startAcquisition()` must ensure the camera is armed (Finding 3)

Today `ThorCamTSIManager.startAcquisition()` is a bare `pass`
(`imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py`). It assumes
the camera stays armed from `__init__`. If the camera is ever disarmed (SDK
error, external disarm, a trigger-mode/ROI change that didn't re-arm), recording
cannot recover, and only running live view first "fixes" it. Make
`startAcquisition()` idempotently ensure the camera is armed.

### A1. Add a symmetric `is_armed` property

In `imswitch/imcontrol/model/interfaces/thorcamera_tsi.py`:

- `ThorTSICamera` (the REAL wrapper): add an `is_armed` property returning
  `bool(getattr(self._camera, 'is_armed', False))`. (Its `arm()`/`disarm()`
  already guard on `self._camera.is_armed`; this just exposes it publicly so the
  manager can query it.)
- `MockThorTSICamera`: add an `is_armed` property returning `self._armed`.

### A2. Implement ensure-armed

In `ThorCamTSIManager.startAcquisition()`:

```python
def startAcquisition(self):
    """Ensure the camera is armed for acquisition.

    Idempotent: arms only if not already armed, so recording can recover if the
    camera was disarmed after __init__ (SDK error, external disarm, mode change).
    Other camera managers actively (re)start acquisition here; this one keeps the
    camera continuously armed but must not silently no-op when it is disarmed.
    """
    if not self._camera.is_armed:
        self._camera.arm(buffer_size=4)
```

Use the same `buffer_size=4` already used in `__init__`. Leave
`stopAcquisition()` as a no-op (the camera is intentionally kept continuously
armed; do not disarm on stop). Leave `getLatestFrame()` and the Phase 1
`getChunk()` structure otherwise unchanged.

## Goal B — Bulb mode should drain like Hardware in `getChunk()`

Phase 1 grouped `Bulb` with `Software` in `getChunk()` (issues a software
trigger). Bulb is actually a hardware-triggered mode, and the mock already gates
Bulb frames behind hardware triggers (`get_pending_frame` treats
`_trigger_mode != 0` as hardware). Fix the manager to match.

In `getChunk()`, change the mode branch so ONLY `'Software'` issues a software
trigger and polls; `'Hardware'` AND `'Bulb'` take the drain path (no trigger,
no fabrication):

- `if mode == 'Software':` -> issue one software trigger, poll briefly, return
  `(1, H, W)` or empty `(0, H, W)`.
- `else:  # Hardware or Bulb` -> drain all pending frames via
  `get_pending_frame()` until `None`, return `(n, H, W)` or empty `(0, H, W)`.

Update the `getChunk()` docstring accordingly (it currently says
"Software/Bulb modes: issue trigger").

## Tests

Extend `imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py`
(reuse its `_make_manager` helper). Add:

1. **startAcquisition arms a disarmed camera.**
   `mgr._camera.disarm()`; assert `not mgr._camera.is_armed`;
   `mgr.startAcquisition()`; assert `mgr._camera.is_armed`.
2. **startAcquisition is idempotent when already armed.** Camera armed from
   `__init__`; calling `startAcquisition()` twice does not raise and leaves it
   armed (`mgr._camera.is_armed` stays True).
3. **Bulb mode drains hardware triggers, no software trigger.** Operation Mode
   `'Bulb'`; `mgr._camera.simulate_hardware_trigger(2)`; `getChunk()` returns
   `ndim == 3`, `shape[0] == 2`; assert no pending software trigger was created
   (e.g. `mgr._camera._pending_software_triggers == 0`). A second `getChunk()`
   with no new triggers returns an empty `(0, H, W)` chunk.
4. **Bulb mode, no trigger → empty 3-D chunk** (never fabricated zeros).

## Acceptance criteria

1. `startAcquisition()` arms a disarmed camera and is idempotent when armed;
   `is_armed` exists on both `ThorTSICamera` and `MockThorTSICamera`.
2. `getChunk()` Bulb mode uses the hardware drain path (no software trigger, no
   fabrication).
3. New tests above pass; the full suite is green:

   ```
   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit/test_recording.py \
     imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py \
     imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py \
     imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py -q
   ```

   (Use the `python` on PATH — conda 3.12. The `-p pytestqt.plugin` is REQUIRED;
   without it the qtbot tests error.)

4. Only `ThorCamTSIManager.py`, `thorcamera_tsi.py`, and the test module
   change. Do not commit; leave changes in the working tree for review.

## Notes / guardrails

- Sequential, single-checkout workflow: edit directly in this checkout, no
  parallel work.
- Do not touch `_updateROI`, the recording loop, or other managers.
- When done, print a concise summary: files changed, the new tests added, and
  the final test result line.
