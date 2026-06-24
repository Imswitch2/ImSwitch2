# Advanced Scan Triggered Recording Audit

Date: 2026-06-24 (revised 2026-06-24 after source verification)

Scope: recording-widget behavior when using Advanced Scan with an externally
triggered camera, with focus on the `ThorCamTSIManager` path. This note
records which behaviors are confirmed bugs or design risks (with `file:line`
evidence), which are intentional, and a fix plan structured for sequential
OpenHands runs.

## Current conclusion

Live view should **not** be required to record with a triggered camera. The
recording worker starts detector acquisition itself
(`RecordingWorker.run()` -> `detectorsManager.startAcquisition()`,
[RecordingManager.py:1439](../imswitch/imcontrol/model/managers/RecordingManager.py:1439)),
using the same acquisition-handle model as live view
([DetectorsManager.py:93](../imswitch/imcontrol/model/managers/DetectorsManager.py:93)).
If a setup only records correctly when live view is already running, that is a
symptom of the detector-manager bugs below, not of a missing live-view
requirement.

The behavior where `ScanOnce` does not auto-start standalone scan controllers
unless a generic `Scan` controller is registered is **intentional**
(`toggleREC` guards `run_scan` behind `hasScanWidget()`,
[RecordingController.py:202](../imswitch/imcontrol/controller/controllers/RecordingController.py:202)).
It prevents one recording button from broadcasting `sigRunScan` to multiple
standalone scan controllers. Not a bug.

### What changed in this revision

- **Finding 2 is the critical issue and is *not* triggered-specific.** The
  `getChunk()` shape violation affects `ThorCamTSIManager` recording in every
  mode (SpecFrames, SpecTime, ScanOnce, ScanLapse), with software *or* hardware
  trigger. Frame-targeted modes can complete almost instantly with mangled data;
  SpecTime also gets corrupted frame accounting and batching. The previous
  draft scoped this too narrowly to triggered mode.
- **Finding 1 (no-op `startAcquisition`) is downgraded** to a latent
  robustness issue. The camera is armed in `__init__` and re-armed on every
  ROI/crop change, so in the common path it is armed when recording starts.
- Every finding now carries a `file:line` reference and either a confirmed
  mechanism or an explicit design-risk classification.

## Confirmed mechanism: how a 2D chunk corrupts recording

This is the chain that makes Finding 2 catastrophic. Trace it once:

1. `ThorCamTSIManager.getChunk()` returns `getLatestFrame()` — a single 2D
   `(H, W)` array
   ([ThorCamTSIManager.py:244](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:244)).
   The base-class contract documents `(numFrames, height, width)`
   ([DetectorManager.py:310](../imswitch/imcontrol/model/managers/detectors/DetectorManager.py:310)).
2. `readChunk()` does `consumerQueue.extend(newFrames)`
   ([DetectorManager.py:348](../imswitch/imcontrol/model/managers/detectors/DetectorManager.py:348)).
   `list.extend` over a 2D ndarray iterates the **first axis**, so one `(H, W)`
   image is appended as `H` separate 1D rows of shape `(W,)`.
3. `_getNewFrames()` does `np.stack(newFrames)`
   ([RecordingManager.py:1769](../imswitch/imcontrol/model/managers/RecordingManager.py:1769)),
   reassembling an `(H, W)` array, and the loop reads `n = len(newFrames) = H`
   ([RecordingManager.py:1661](../imswitch/imcontrol/model/managers/RecordingManager.py:1661)).
4. The frame counter advances by `H` per poll. For a target of `recFrames`
   frames the loop clips to `newFrames[:remaining]`
   ([RecordingManager.py:1667](../imswitch/imcontrol/model/managers/RecordingManager.py:1667)),
   enqueues a single `(remaining, W)` 2D array, which the storer writes as
   **one** frame of shape `(remaining, W)`
   (`writeFrames` promotes 2D -> `(1, Y, X)`,
   e.g. [RecordingManager.py:426](../imswitch/imcontrol/model/managers/RecordingManager.py:426)).

Net result for frame-targeted modes: the recording can "complete" almost
instantly, the saved file can contain one mangled image (the first `remaining`
rows of a single frame), and the frame counter reports success. `SpecTime` does
not have the same target-frame clipping, but it still gets corrupted frame
accounting and batching because rows are treated as frames.

Several known managers avoid this by returning 3D `(n, H, W)` arrays or a list
of frames — see `TISManager`/`BaslerManager`/`JetsonCamManager`
(`np.expand_dims(..., 0)`), `HamamatsuManager.getChunk` (`getFrames()[0]`, a
list), and `PMTManager` (3D when ready, empty otherwise). `APDManager` and
`SwabianTimeTaggerManager` also appear intended to return `(1, Ny, Nx)` when
ready, but their idle return shapes are looser. Some camera managers delegate
to SDK wrapper methods such as `getLastChunk()`, so their actual runtime shapes
should be audited separately. `ThorCamTSIManager` is the confirmed bare-2D
offender in this audit.

## Findings (severity-ranked)

### 1 (CRITICAL). `getChunk()` returns a 2D frame, violating the chunk contract

`ThorCamTSIManager.getChunk()` returns a single 2D frame
([ThorCamTSIManager.py:244](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:244)).
Via the `readChunk` -> `extend` -> `np.stack` chain above, this corrupts all
recordings with this camera, independent of trigger mode. This is the root
cause to fix first.

Expected: `getChunk()` must return a 3D `(n, H, W)` chunk of *real* acquired
frames, or an empty chunk when none are pending. Prefer an empty chunk shaped
`(0, H, W)` with the detector dtype when the spatial shape is known; use
`(0, 0, 0)` only as a fallback. The important contract is `len(chunk) == 0`
for no frame and `chunk.ndim == 3` for real frames.

### 2 (CRITICAL). No-frame path fabricates a black frame for recording

In hardware-trigger mode, `getLatestFrame()` polls ~10 ms and then returns a
zero image when no frame is pending
([ThorCamTSIManager.py:171-173](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:171)).
Because `getChunk()` delegates to `getLatestFrame()`, a recording that is
waiting for hardware triggers receives fabricated black frames instead of an
empty chunk. Combined with Finding 1 the black frame is then row-expanded too.

A zero-fill fallback is reasonable for *live-view display* but wrong for a
destructive recording read. The recording path must distinguish:

- no frame available yet (return empty chunk, keep waiting),
- a real frame arrived (return it),
- camera timed out / not armed (surface, let the stall watchdog fire —
  [RecordingManager.py:1699](../imswitch/imcontrol/model/managers/RecordingManager.py:1699)).

Fix direction: split the display fallback (`getLatestFrame`, may return cached
or zero frame) from the recording read (`getChunk`, real frames or empty,
never fabricated).

### 3 (MEDIUM). `startAcquisition()` is a no-op (latent robustness gap)

`ThorCamTSIManager.startAcquisition()` is `pass`
([ThorCamTSIManager.py:282-284](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:282));
it assumes the camera stays armed from `__init__`
([ThorCamTSIManager.py:115](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:115)).
Every other camera manager actively (re)starts acquisition here (Basler/AV/
Hamamatsu/TIS `start_live`). In the common path the camera *is* armed (init,
plus re-arm on every ROI/crop change in `_updateROI`,
[ThorCamTSIManager.py:230-238](../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:230)),
so this is not the active failure — but it is fragile: if the camera is ever
disarmed (SDK error, external disarm, trigger-mode change), recording cannot
recover, and live view appears to "fix" it by exercising the path first.

Fix direction: make `startAcquisition()` idempotently ensure the camera is
armed (arm if not armed), rather than assuming it.

### 4 (MEDIUM). Recording-to-scan start uses a fixed sleep, not an arming handshake

`toggleREC` starts the recording manager, sleeps `0.3 s`, then starts the scan
([RecordingController.py:200-203](../imswitch/imcontrol/controller/controllers/RecordingController.py:200));
`nextLapse` does the same
([RecordingController.py:269-272](../imswitch/imcontrol/controller/controllers/RecordingController.py:269)).
There is no confirmation that externally triggered detectors are armed before
scan TTL output begins. This is a confirmed design risk rather than a confirmed
root cause. It may present differently per camera manager and per machine, and
after Findings 1 and 2 are fixed it should be hardware-validated to decide how
urgent the handshake is.

Fix direction: replace the fixed sleep with a readiness signal — the worker
starts detector acquisition and reports ready (or error/timeout); the
controller starts the scan only after all selected detectors report armed.

### 5 (LOW). `ScanLapse` bypasses the `ScanOnce` standalone-scan guard

`ScanOnce` only calls `run_scan` when `hasScanWidget()` is true
([RecordingController.py:202](../imswitch/imcontrol/controller/controllers/RecordingController.py:202)).
`ScanLapse` calls `scanWorkflow.run_scan(...)` unconditionally from
`nextLapse()`
([RecordingController.py:272](../imswitch/imcontrol/controller/controllers/RecordingController.py:272)),
bypassing the safety rule deliberately added for `ScanOnce`. On a setup with
multiple standalone scan controllers listening to `sigRunScan`, a lapse
recording could start all of them at once.

Fix direction: decide whether `ScanLapse` should mirror `ScanOnce` (guard on
`hasScanWidget()`, otherwise arm-only) or adopt an explicit active-scan-source
selection. Verify against standalone scan controllers first; may be a no-op for
the intended setups.

### 6 (LOW). Missing TTL configuration is masked by a default of 1

`getNumCamTTL()` only includes detectors that have a TTL cycle signal in the
digital parameter dict
([basecontrollers.py:378-384](../imswitch/imcontrol/controller/basecontrollers.py:378)).
`_record()` then defaults absent detectors to one TTL per scan position:
`recFrames * numCamTTL.get(detectorName, 1)`
([RecordingManager.py:1587-1591](../imswitch/imcontrol/model/managers/RecordingManager.py:1587)).
If a camera is not actually a scan TTL target, recording silently expects
`recFrames` frames that will never arrive.

Note the interaction: once Findings 1 & 2 are fixed (no fabricated frames,
empty chunk when idle), this misconfiguration surfaces *cleanly* as a stall
([RecordingManager.py:1699](../imswitch/imcontrol/model/managers/RecordingManager.py:1699))
instead of silent corruption. Fixing the data path is therefore a prerequisite
for this to be diagnosable. Optional follow-up: warn when a recorded detector
is absent from `numCamTTL` in a scan mode.

## Test-double gap

`MockThorTSICamera.get_pending_frame()` returns a synthetic frame whenever the
camera is armed, ignoring trigger mode entirely
([thorcamera_tsi.py:342-358](../imswitch/imcontrol/model/interfaces/thorcamera_tsi.py:342)).
It cannot reproduce the hardware-trigger no-frame path, so today's tests cannot
catch Findings 1, 2, or 6. Any fix must extend the mock (or add a dedicated
triggered fake) that only yields frames after an explicit simulated trigger and
returns "no frame" otherwise.

## Fix plan (sequential OpenHands runs)

All agent work shares one checkout and runs sequentially (see project memory).
Run each phase as its own headless task: `openhands -f <taskfile> --headless`,
review the diff and run the suite between phases, then proceed. Phases are
ordered so each builds on a green tree.

Baseline test command for every phase:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest imswitch/imcontrol/_test/unit/test_recording.py
imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py
imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py -q`

After Phase 0, add the new ThorCam/triggered-recording contract test module to
that command so later phases cannot pass without exercising the new regression
coverage.

### Phase 0 — Pin current behavior with a triggered fake (tests first)

- Extend `MockThorTSICamera` (or add a `TriggeredFakeDetector` test double) so
  it only returns a frame after a simulated trigger and otherwise reports no
  frame; expose whether `startAcquisition()` armed it.
- Add failing tests asserting the *target* contract:
  `getChunk()` returns 3D or empty, never a 2D frame; no fabricated frames
  reach recording; recording alone arms the detector; no frames recorded
  before triggers. These should fail against current `main`.
- Files: `imswitch/imcontrol/model/interfaces/thorcamera_tsi.py`,
  `imswitch/imcontrol/_test/unit/` (new test module),
  pattern reference `test_detector_chunk_consumers.py`.

### Phase 1 — Fix `getChunk()` chunk semantics (Findings 1 & 2)

- `getChunk()` polls for *real* pending frames and returns a 3D `(n, H, W)`
  stack, or an empty chunk when none are pending. Prefer `(0, H, W)` with the
  detector dtype when the ROI shape is known; use `(0, 0, 0)` only as a fallback.
  Never fabricate.
- Keep `getLatestFrame()` as the display path (its zero-fill fallback stays for
  live view only).
- Confirm `len(chunk) == 0` is the agreed empty signal end-to-end through
  `readChunk`/`_getNewFrames`.
- Phase 0 tests must now pass. File:
  `imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py`.

### Phase 2 — Make `startAcquisition()` ensure-armed (Finding 3)

- `startAcquisition()` arms the camera if not already armed (idempotent);
  `stopAcquisition()` keeps the documented continuous-arming behavior unless a
  reason to disarm is established.
- Add tests: arm-if-disarmed; no double-arm if already armed.
- File: `ThorCamTSIManager.py`.

### Phase 3 — Arming-readiness handshake (Finding 4)

- Replace the `time.sleep(0.3)` before `run_scan` with a readiness signal:
  worker starts acquisition, detectors report armed (or error/timeout), and the
  controller starts the scan only once all selected detectors are ready.
- Test with a fake detector that arms slowly.
- Files: `RecordingManager.py` (worker/manager signal),
  `DetectorsManager.py` (acquisition-started status),
  `RecordingController.py:200-203` and `:269-272`.
- Larger, cross-cutting change — keep it isolated in its own phase.

### Phase 4 — `ScanLapse` scan-source guard (Finding 5)

- Decide and implement: mirror `ScanOnce`'s `hasScanWidget()` guard in
  `nextLapse()`, or document why lapse intentionally differs.
- Verify against standalone scan controllers (TriggerScope family) that listen
  to `sigRunScan`. File: `RecordingController.py:272`.

### Phase 5 (optional) — Surface missing TTL config (Finding 6)

- Warn (or require opt-in) when a recorded detector is absent from `numCamTTL`
  in `ScanOnce`/`ScanLapse`, so the `* 1` default cannot silently hide a scan
  misconfiguration. File: `RecordingManager.py:1587` (or controller-side).

### Likely fix order (summary)

1. Phase 0 — triggered fake + failing contract tests.
2. Phase 1 — `getChunk()` 3D/empty semantics (the critical data-corruption fix).
3. Phase 2 — `startAcquisition()` ensure-armed.
4. Phase 3 — arming-readiness handshake (broad robustness fix).
5. Phase 4 — `ScanLapse` guard decision.
6. Phase 5 — optional TTL-config warning.

Phases 1-2 are the correctness fixes. Phase 3 is the robustness fix that makes
hardware-triggered recording less dependent on machine timing and SDK behavior.

## Hardware validation matrix (post-fix)

Run each scenario with and without live view; expect identical frame count and
timing in both columns:

| Setup | Live view | Expected |
| --- | --- | --- |
| Advanced Scan + triggered ThorCam TSI | off | Records all triggered frames |
| Advanced Scan + triggered ThorCam TSI | on | Same frame count and timing |
| Advanced Scan + triggered non-ThorCam camera | off | Records all triggered frames |
| Advanced Scan + triggered non-ThorCam camera | on | Same frame count and timing |
| Recording armed, no scan trigger | off | No fake frames, then clean stall/timeout |

For each run compare: expected vs written frame count; first-frame timestamp
relative to scan start; whether any black/fallback frame was written before
triggers; whether the camera was armed before scan TTL output.
