# Task: Recording→scan arming-readiness handshake (Phase 3)

You are working in the ImSwitch2 repository on branch `feat/live-reconstruction`.
This is Phase 3 from `docs/advanced_scan_triggered_recording_audit.md`
(Finding 4). Phases 0–2 already landed in the working tree (uncommitted) and the
suite is green. Build on that. Do NOT commit.

## Problem

For scan-driven recordings, `RecordingController` starts the recording manager,
sleeps a FIXED `0.3 s`, then starts the scan — with no confirmation that the
detectors are actually armed before scan TTL output begins. Two call sites:

- `RecordingController.toggleREC()` (ScanOnce path), the
  `self._master.recordingManager.startRecording(**self.recordingArgs)` followed
  by `time.sleep(0.3)` then `run_scan(...)`
  (`imswitch/imcontrol/controller/controllers/RecordingController.py`, ~lines
  200–203).
- `RecordingController.nextLapse()` (ScanLapse path), `startRecording(...)` then
  `time.sleep(0.3)` then `run_scan(...)` (~lines 269–272).

The fixed sleep is a timing race: on a slow-arming camera the scan can start
before the camera is ready (lost first frames / wrong frame count); on a
fast machine the sleep just wastes 300 ms.

## Design — replace the sleep with a readiness Event (bounded wait)

Tie readiness to the moment `detectorsManager.startAcquisition()` returns inside
the recording worker — that call is what arms/starts every acquisition detector
(for ThorCam TSI, Phase 2 made it ensure-armed). Use a `threading.Event` so the
controller can wait deterministically WITHOUT depending on Qt signal delivery
(the controller wait blocks the GUI thread, so a queued Qt signal would never be
delivered — an Event set directly by the worker thread avoids that deadlock).

### Model changes — `imswitch/imcontrol/model/managers/RecordingManager.py`

1. In `RecordingManager.__init__`, add `self.__acqStartedEvent = threading.Event()`
   (`threading` is already imported).
2. In `RecordingManager.startRecording(...)`, `self.__acqStartedEvent.clear()`
   BEFORE `self.__thread.start()`, so each recording starts with a fresh event.
3. Add a method to set it (called by the worker) and a public wait method:

   ```python
   def _signalAcquisitionStarted(self):
       """Called by the worker once detector acquisition has started (armed)."""
       self.__acqStartedEvent.set()

   def waitForAcquisitionStarted(self, timeout=None):
       """Block until the recording worker has started detector acquisition
       (i.e. all acquisition detectors are armed), or until timeout.

       Returns True if acquisition started within the timeout, False otherwise.
       Used by scan-driven recordings to gate scan TTL output on detector
       readiness instead of a fixed sleep. Safe to call from the GUI thread:
       waits on a threading.Event set directly by the worker thread, so it does
       not depend on the Qt event loop.
       """
       return self.__acqStartedEvent.wait(timeout)
   ```

4. In `RecordingWorker.run()`, set the event right after
   `detectorsManager.startAcquisition()` returns and before `self._record()`:

   ```python
   def run(self):
       acqHandle = self.__recordingManager.detectorsManager.startAcquisition()
       self.__recordingManager._signalAcquisitionStarted()
       try:
           self._record()
       finally:
           self.__recordingManager.detectorsManager.stopAcquisition(acqHandle)
   ```

   (Name-mangling: the worker already reaches the manager via
   `self.__recordingManager` / `self._RecordingWorker__recordingManager`; call
   the new public-ish `_signalAcquisitionStarted` on it. If the private-name
   access is awkward, add a thin method on RecordingManager that the worker
   already can call, mirroring how it calls `detectorsManager`.)

   Leave the existing `sigRecordingStarted.emit()` inside `_record()` UNCHANGED
   (it still drives the GUI). The new Event is a separate, earlier "armed"
   signal.

Add a module constant near the other recording constants:
`RECORDING_ARM_TIMEOUT = 5.0  # seconds; max wait for detectors to arm before starting a scan`.

### Controller changes — `RecordingController.py`

Replace BOTH `time.sleep(0.3)` calls (ScanOnce in `toggleREC`, and `nextLapse`)
with a bounded wait on the new readiness Event:

```python
from imswitch.imcontrol.model.managers.RecordingManager import RECORDING_ARM_TIMEOUT
...
self._master.recordingManager.startRecording(**self.recordingArgs)
armed = self._master.recordingManager.waitForAcquisitionStarted(RECORDING_ARM_TIMEOUT)
if not armed:
    self.__logger.error(
        'Detectors did not report armed within %.1fs; starting scan anyway '
        '(check camera arming/triggering).', RECORDING_ARM_TIMEOUT
    )
# ... then the existing run_scan / hasScanWidget logic, unchanged ...
```

Behavior on timeout: log a prominent error and PROCEED best-effort (start the
scan anyway). Do NOT introduce a new abort path — the existing stall watchdog
(`RecordingManager._record`) already handles "frames never arrive". The goal is
strictly: proceed as soon as armed (usually faster than 300 ms), wait longer if
arming is slow, and surface a loud error if arming never completes.

Keep the ScanOnce `hasScanWidget()` guard and the ScanLapse flow otherwise
exactly as they are. Only the `time.sleep(0.3)` -> readiness-wait substitution
changes.

## Tests

Add a focused, DETERMINISTIC test (avoid timing-flaky sleeps where possible).
Suggested approach in `imswitch/imcontrol/_test/unit/test_recording.py` (it
already builds a real `DetectorsManager` + `RecordingManager` from
`detectorInfosBasic`; reuse that scaffolding):

1. **Wait blocks until arming completes.** Wrap/monkeypatch one managed
   detector's `startAcquisition` so it blocks on a `threading.Event` the test
   controls. Start a recording, assert
   `recordingManager.waitForAcquisitionStarted(timeout=0.2)` returns `False`
   while the detector is still blocked; release the detector; assert
   `waitForAcquisitionStarted(timeout=2.0)` returns `True`. Then end/abort the
   recording cleanly.
2. **Wait returns True in the normal (fast-arming) path.** A plain recording
   reaches armed state and `waitForAcquisitionStarted(timeout=2.0)` returns
   `True`.
3. **Timeout path returns False.** Either with a detector that never finishes
   arming within a short timeout, assert `waitForAcquisitionStarted(0.1)` is
   `False`. Clean up so the test does not hang (release the block in teardown).

Keep tests robust: always release any blocking Event and end the recording in a
`finally`/teardown so a failure cannot wedge the suite.

## Acceptance criteria

1. `RecordingManager` exposes `waitForAcquisitionStarted(timeout)` backed by a
   `threading.Event` that is cleared in `startRecording` and set by the worker
   immediately after `detectorsManager.startAcquisition()` returns.
2. Both `time.sleep(0.3)` scan-start delays in `RecordingController`
   (`toggleREC` ScanOnce + `nextLapse`) are replaced by the bounded readiness
   wait; timeout logs an error and proceeds best-effort (no new abort path).
3. `sigRecordingStarted` and all existing scan/lapse control flow are otherwise
   unchanged.
4. New tests pass and the full suite is green:

   ```
   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit/test_recording.py \
     imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py \
     imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py \
     imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py -q
   ```

   (Use the `python` on PATH — conda 3.12. `-p pytestqt.plugin` is REQUIRED.)

5. Changed files limited to `RecordingManager.py`, `RecordingController.py`, and
   `test_recording.py`. Do not commit.

## Notes / guardrails

- Sequential, single-checkout workflow: edit directly in this checkout.
- Do NOT change `DetectorsManager` for this phase — readiness is observed at the
  RecordingManager/worker boundary (after `startAcquisition()` returns), which
  already covers all acquisition detectors. Keep the change minimal.
- Do not alter the live-view `sleep(0.3)` inside `DetectorsManager.startAcquisition`
  (that is the LV-thread startup path, unrelated to this handshake).
- When done, print a concise summary: files changed, the tests added, and the
  final test result line.
