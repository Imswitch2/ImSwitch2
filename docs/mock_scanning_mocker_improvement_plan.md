# Mock Scanning And Mocker Improvement Plan

## Goal

Make mock scanning a first-class, hardware-free acquisition path that behaves like
the real DAQ-triggered flow:

- simulated scans can run with no NI-DAQ hardware and no physical AO/DO channels,
- mock Hamamatsu cameras receive frame triggers during scans,
- mock APD and PMT detectors produce scan-shaped synthetic data,
- mixed detector setups such as Hamamatsu + APD work without lifecycle races,
- ScanOnce and ScanLapse recordings finish from frame-count contracts instead of
  hanging or relying on manual intervention.

The main design direction is to keep NI-DAQ simulation logic out of the already
large `NidaqManager` by moving scan-simulation orchestration into a small
separate component.

## Current Status

The mock-related implementation now has the first structural split in place.

- `MockHamamatsu.mockTrigger(n)` queues externally triggered frames.
- `MockHamamatsu.getFrames()` drains queued frames when `trigger_source != 1`,
  and free-runs in internal trigger mode.
- `NidaqManager` delegates software-only scan timing and virtual frame-trigger
  dispatch to `ScanSimulationCoordinator`.
- `SimulatedScanPlan` computes scan duration, scan positions, and per-detector
  frame trigger counts outside `NidaqManager`.
- No-channel simulated scans can now emit `sigScanBuilt`, `sigScanStarted`, and
  `sigScanDone`; real DAQ mode still raises `No signals to send`.
- Legacy `registerExternalScanDriver()` no longer suppresses simulated camera
  trigger generation.
- APD and PMT force synthetic data generation when the NI-DAQ is simulated.
- APD and PMT `getChunk()` now return an explicit frame batch so the
  `RecordingManager` no longer splits one scan image into row-shaped frames.
- APD and PMT synthetic scan workers no longer call the legacy
  `finishExternalMock()` completion hook while NI-DAQ simulation is active.
- Hamamatsu-only and mixed Hamamatsu + APD ScanOnce paths now have
  recording-manager-level regressions.
- Hardware-free Hamamatsu and mixed Hamamatsu + APD setup templates are
  available under `imswitch/_data/user_defaults/imcontrol_setups/`.
- `ScanInfoContract` now validates the core shared metadata invariants:
  physical dimensions stay separate from linesteps, `scan_samples` is
  level-based with `len(img_dims) + 1` entries, and fast-axis sample helpers
  match the scan samples.
- `SimulatedScanPlan` now counts a TTL that starts high at sample zero as a
  rising edge, which matches real scan-start trigger semantics.
- Hamamatsu, APD, and PMT managers now expose explicit mock-protocol lifecycle
  methods (`mockStartScan`, `mockTrigger`, `mockStopScan`, `mockScanDone` as
  applicable), and simulated APD/PMT scans enter through `mockStartScan`.
- APD mock scans now generate monotonic cumulative counter readings, reconstruct
  bounded integer photon counts, and clip before writing the `uint16` image
  buffer instead of wrapping overflow values.
- PMT mock scans now generate bounded `float32` voltage samples in the
  configured range (default `-5.0..5.0` V) and average dwell samples per pixel
  instead of summing voltages.
- APD/PMT synthetic scan workers now mark themselves stopped, request their
  scan thread to quit at run completion, and clear finished worker/thread
  references during cleanup so repeated stop calls do not touch deleted Qt
  wrappers.

Known issues:

- ScanLapse is not yet covered by a recording-manager-level mock regression.
  An attempted two-cycle regression exposed Qt/thread teardown instability in
  the test process, so this should be handled as a focused follow-up rather
  than hidden inside the broader mock scanner patch.
- A standalone two-cycle Hamamatsu ScanLapse reproduction now completes,
  including per-cycle in-memory HDF5 reads. The pytest version still aborts if
  the wait path fails: the writer thread is blocked on its queue while the main
  thread waits in `RecordingManager.endRecording()`. That points to pytest Qt
  signal delivery/cleanup around an active recording worker. The recording
  readiness race below has been fixed, but the Hamamatsu ScanLapse pytest case
  still aborts and remains out of the stable suite.
- `waitForAcquisitionStarted()` now waits until the recording stream is open and
  the recording chunk consumer is registered, not only until detector
  acquisition starts. This removes a real race where scan TTL output could begin
  before the recorder was ready to consume frames.
- RAM HDF5 recordings now emit `sigMemoryRecordingAvailable` after closing the
  in-memory HDF5 handle, so consumers do not see a half-finalized BytesIO.
- Recording writer shutdown is idempotent, and `abortRecording()` now asks an
  already-open writer to abort immediately. This covers the no-frames cleanup
  path without relying only on the Qt worker reaching its own teardown.
- Controller-side `getNumCamTTL()` now uses the same rising-edge rule as the
  simulation plan, including TTLs that start high at sample zero. This keeps
  ScanOnce frame targets aligned with simulated camera trigger counts.
- `RecordingManager` now prepares a fresh recording worker/thread for each
  recording start, which avoids reusing a deleted Qt thread wrapper between
  sequential recording cycles.

Progress log:

- 2026-06-29: Added `imswitch/imcontrol/model/managers/mockscan/` with
  `ScanSimulationCoordinator` and `SimulatedScanPlan`.
- 2026-06-29: Updated `NidaqManager` to delegate simulation, support no-channel
  simulated scans, and stop treating external mock drivers as a global trigger
  suppression mechanism.
- 2026-06-29: Added focused tests:
  - `test_mock_scan_simulation_coordinator.py`
  - `test_hamamatsu_mock_triggering.py`
- 2026-06-29: Verified targeted suite:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
  imswitch/imcontrol/_test/unit/test_mock_scan_simulation_coordinator.py
  imswitch/imcontrol/_test/unit/test_hamamatsu_mock_triggering.py
  imswitch/imcontrol/_test/unit/test_nidaq_simulation_startup.py
  imswitch/imcontrol/_test/unit/test_dtype_silent_casts.py::TestAPDManagerExplicitCasts -q`
  -> 14 passed.
- 2026-06-29: Added a mixed Hamamatsu + APD simulated scan test and prevented
  APD/PMT simulation close paths from entering hardware input-task cleanup.
  Updated targeted suite -> 15 passed.
- 2026-06-29: Added Hamamatsu internal-trigger free-run coverage plus APD-only
  and PMT-only simulated scan completion tests. Updated targeted suite ->
  18 passed.
- 2026-06-29: Fixed APD/PMT chunk shape to return a frame batch, added
  Hamamatsu-only and mixed Hamamatsu + APD ScanOnce RAM-recording regressions,
  and added Hamamatsu mock setup templates/docs. Updated targeted suite ->
  20 passed.
- 2026-06-29: Guarded APD/PMT synthetic workers so coordinator-owned simulated
  scans no longer call the legacy `finishExternalMock()` path. Updated
  targeted suite -> 20 passed.
- 2026-06-29: Tried to add a two-cycle Hamamatsu ScanLapse regression. A
  standalone reproduction completed, but the pytest version could abort during
  teardown while a writer thread was still waiting, so ScanLapse remains a
  documented follow-up.
- 2026-06-29: Tried a deterministic fake-detector ScanLapse restart regression
  to isolate the test from Hamamatsu polling. It also completed standalone but
  aborted under pytest while the writer thread waited for finalization, so the
  remaining ScanLapse item is now narrowed to Qt/test-harness lifecycle rather
  than mock detector trigger delivery.
- 2026-06-29: Added `validate_scan_info_contract()`, enabled validation in
  `ScanInfoContract`, added Beta 2D/3D linestep and advanced TTL trigger-count
  tests, and fixed simulated TTL edge counting at sample zero. Updated targeted
  suite -> 25 passed.
- 2026-06-29: Added manager-level mock lifecycle methods for Hamamatsu, APD,
  and PMT. Simulated APD/PMT scan-build handling now enters through
  `mockStartScan`, and tests assert the protocol path. Updated targeted suite
  -> 26 passed.
- 2026-06-29: Retried exact two-cycle Hamamatsu ScanLapse. Standalone execution
  passed, including opening each in-memory HDF5 dataset after each lapse cycle.
  Pytest still aborts with the writer thread waiting on its queue and the main
  thread waiting in `endRecording()`, so no process-aborting ScanLapse test was
  kept in-tree.
- 2026-06-29: Moved the recording-readiness event to after writer stream open
  and chunk-consumer registration, delayed RAM HDF5 memory-available emission
  until after close, and added a non-crashing readiness regression. Updated
  targeted suite -> 28 passed. The process-aborting Hamamatsu ScanLapse pytest
  regression was retried after this change and still failed, so it was removed
  again.
- 2026-06-29: Made writer stop/abort idempotent, allowed `abortRecording()` to
  wake an open writer with no frames, and added a non-crashing abort regression.
  Retried the Hamamatsu ScanLapse pytest case with abort-safe cleanup; it still
  aborted while waiting for the Qt recording worker thread, so the remaining
  issue is below the writer queue level. Stable targeted suite -> 29 passed.
- 2026-06-29: Updated `SuperScanController.getNumCamTTL()` to count sample-zero
  high TTLs and avoid rebuilding TTL signals once per detector. Added a
  scan-once source contract regression. Expanded targeted suite -> 35 passed.
- 2026-06-30: Fixed the user-reported APD mock overflow-looking values by
  switching the synthetic APD source to bounded cumulative counter readings and
  adding explicit uint16 clipping. Fixed PMT mock values to stay in a voltage
  range and average dwell samples. Tightened APD/PMT worker teardown so scan
  completion no longer depends on a later UI event to clear a finished scan
  thread. Expanded targeted suite -> 38 passed.
- 2026-07-01: Diagnosed the ScanLapse pytest-abort report as a real
  `threading.Thread` (`WriterThread`) join/teardown interaction with pytest's
  Qt event loop, not a cycling-logic bug: `RecordingWorker._record()`'s
  `finally` block already joins the writer thread (`WriterThread.finish()`)
  and clears `RecordingManager.record` *before* `RecordingController`
  advances the lapse, and `nextLapse()` already re-arms a drain-retry timer
  while `record` is still true (see the 2026-06-26 `nextLapse` fix above), so
  the two-completion-path race between `sigScanDone` and worker frame-count
  completion was already closed at the logic level. Added
  `test_scanlapse_two_cycle_drain_and_progression` in `test_recording.py`: a
  controller-level regression driving two full lapse cycles
  (`nextLapse`/`scanDone`/`recordingCycleEnded`) against a fake
  `RecordingManager`, with no real `QThread`/`WriterThread` involved, so it
  cannot hit the documented teardown abort. This gives the previously-bare
  multi-cycle cycling contract (lapse progression, per-cycle savename/
  `recLapseIndex`, drain-retry, final-cycle reset) real regression coverage.
  Did not attempt another real-`RecordingManager`+`WriterThread` two-cycle
  test - every prior attempt at that exact shape reproduced standalone and
  aborted under pytest, and no new hypothesis for *why* pytest's Qt event
  loop and the plain-Python writer thread deadlock/abort during teardown was
  found this session. That diagnosis is the remaining `[todo]` in Phase 6.

## Target Architecture

Introduce a dedicated scan simulation coordinator, tentatively:

```text
imswitch/imcontrol/model/managers/mockscan/
  __init__.py
  ScanSimulationCoordinator.py
  SimulatedScanPlan.py
```

or a similarly small module under `imswitch/imcontrol/model/simulation/`.

The coordinator should be the single owner of simulated scan timing and virtual
trigger dispatch. `NidaqManager` should delegate to it when `nidaq.simulation`
is true.

Responsibilities:

- build a per-scan simulation plan from `signalDict`, `scanInfoDict`, and
  `setupInfo`,
- allow simulated scans even when no AO/DO tasks exist,
- emit virtual per-detector frame triggers based on TTL waveforms and recording
  frame-count contracts,
- notify detector mocks that a scan is starting or stopping,
- complete the simulated scan exactly once.

Non-responsibilities:

- detector-specific frame/sample synthesis,
- detector display buffering,
- recording file writing.

Detector mocks should own their data generation. The coordinator should only
provide scan timing and trigger events.

## Mock Detector Contract

Add a small optional protocol for detector managers or detector interfaces:

```python
def mockStartScan(scanInfoDict: dict, signalDict: dict) -> None: ...
def mockTrigger(n: int = 1) -> None: ...
def mockStopScan() -> None: ...
def mockScanDone() -> bool: ...
```

Not every detector needs every method. The coordinator can use `hasattr` for
incremental adoption.

Expected behavior:

- Cameras use `mockTrigger(n)` to queue exactly `n` frames.
- APD/PMT use `mockStartScan(...)` to initialize synthetic scan-shaped buffers
  or worker state.
- `mockStopScan()` cancels pending simulated work.
- Completion is reported to the coordinator rather than directly calling
  `NidaqManager.finishExternalMock()` as the primary lifecycle mechanism.

## Important Design Rule

Separate these responsibilities:

1. Trigger generation: coordinator / simulated DAQ.
2. Detector data production: detector mocks.
3. Scan completion: coordinator, exactly once.

APD/PMT synthetic acquisition must not suppress camera trigger generation.

## Implementation Phases

### Phase 1: Lock Down The Scan Contract

- [done] Clarify `ScanInfoContract` documentation:
  - [done] `img_dims` contains physical scan dimensions only.
  - [done] `n_linesteps` is separate.
  - [done] `scan_samples` is level-based: `[per_pixel, per_line, per_frame, ...]`.
  - [done] the length of `scan_samples` is `len(img_dims) + 1`.
- [done] Add validation helpers for the above invariants.
- [done] Add unit tests for `BetaScanDesigner` and advanced/linestep scan outputs.

### Phase 2: Extract The Simulation Coordinator

- [done] Move the current `SimulatedScanWorker` concept out of `NidaqManager`.
- [done] Add a `SimulatedScanPlan` with:
  - nominal scan duration,
  - capped wall-clock duration,
  - per-detector trigger counts,
  - per-detector expected frame counts,
  - detector participation metadata.
- [partial] Keep `NidaqManager.runScan()` responsible only for:
  - building real hardware tasks in real mode,
  - delegating to the coordinator in simulation mode,
  - emitting existing public signals for compatibility.

### Phase 3: Support No-Channel Simulated Scans

- [done] In simulation mode, do not raise `No signals to send` when no AO/DO hardware
  channels exist.
- [done] Still emit `sigScanBuilt`, `sigScanStarted`, and `sigScanDone`.
- [done] Use scan metadata and TTL dictionaries to drive virtual scan behavior.
- [done] Add a unit test using the existing `mock_scan_setup.json`.

### Phase 4: Hamamatsu Mock Trigger Integration

- [done] Keep the external-trigger queue in `MockHamamatsu`.
- [done] Route simulated camera triggers through the coordinator.
- [done] Ensure `trigger_source == 1` remains free-run and external modes are
  trigger-gated.
- [done] Add tests:
  - [done] external mode returns exactly queued frames,
  - [done] internal mode free-runs,
  - [done] flush resets pending external frames,
  - [done] a simulated scan sends the expected camera trigger count.

### Phase 5: APD/PMT Integration

- [done] Replace global `registerExternalScanDriver()` suppression with per-scan
  detector participation.
- [done] Keep APD/PMT synthetic data generation in their managers for now, but report
  scan completion to the coordinator.
- [done] Do not let APD/PMT block mock camera triggers in mixed setups.
- [done] APD/PMT return frame-batch chunks compatible with `RecordingManager`.
- [done] APD mock signal values are cumulative counter samples that reconstruct
  to bounded integer counts.
- [done] PMT mock signal values are bounded float voltages and are averaged per
  pixel.
- [done] APD/PMT synthetic workers request thread shutdown and clear finished
  worker/thread references after scan completion.
- [done] Add tests for:
  - [done] APD-only mock scan completion,
  - [done] PMT-only mock scan completion,
  - [done] mixed Hamamatsu + APD scan completion and frame production.

### Phase 6: Recording-Level Regression Tests

Add tests that prove ScanOnce/ScanLapse behavior, not only low-level signals:

- [done] no-channel mock setup records without stalling,
- [done] Hamamatsu mock ScanOnce reaches `recFrames`,
- [done] mixed Hamamatsu + APD ScanOnce reaches both detector frame targets,
- [partial] linestep trigger counts match the simulator/controller TTL edge
  contract, including sample-zero high TTLs,
- [done] scan completion is emitted once at manager level.
- [partial] ScanLapse mock recording completes without stalling. Controller-
  level two-cycle regression added (`test_scanlapse_two_cycle_drain_and_progression`
  in `test_recording.py`) covering the cycling state machine against a fake
  RecordingManager. Still `[todo]`: a real `RecordingManager`/`WriterThread`
  two-cycle regression - every attempt at that (including this one)
  reproduces standalone but aborts under pytest during writer thread
  teardown; see the 2026-07-01 progress log entry below.

### Phase 7: Setup Templates

Update or add no-hardware setups:

- [done] true no-channel MoNaLISA mock setup,
- [done] Hamamatsu mock scan setup with `cameraListIndex: "mock"` and external trigger
  mode,
- [done] mixed Hamamatsu + APD mock scan setup.

Each setup should be documented as either:

- [done] no-channel pure software simulation, or
- [done] simulated NI-DAQ with fake `Dev1/...` channels.

Those are distinct modes and should not be confused.

## Test Commands

Useful targeted commands while iterating:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  imswitch/imcontrol/_test/unit/test_scan_info_contract.py \
  imswitch/imcontrol/_test/unit/test_mock_scan_simulation_coordinator.py \
  imswitch/imcontrol/_test/unit/test_hamamatsu_mock_triggering.py \
  imswitch/imcontrol/_test/unit/test_nidaq_simulation_startup.py \
  imswitch/imcontrol/_test/unit/test_dtype_silent_casts.py::TestAPDManagerExplicitCasts \
  imswitch/imcontrol/_test/unit/test_recording.py::test_worker_lapse_metadata_augmentation \
  imswitch/imcontrol/_test/unit/test_recording.py::test_waitForAcquisitionStarted_waits_for_stream_open \
  imswitch/imcontrol/_test/unit/test_recording.py::test_abortRecording_releases_open_writer_without_frames \
  imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py -q
```

## Commit Plan

Keep commits small and reviewable:

1. Document and validate scan sample contract.
2. Add scan simulation coordinator skeleton and tests.
3. Support no-channel simulated scans.
4. Route mock Hamamatsu triggers through the coordinator.
5. Integrate APD/PMT synthetic scan production without suppressing cameras.
6. Add no-hardware and mixed-detector recording regressions.
7. Update mock setup templates and docs.

Avoid mixing unrelated UI scaling/import fixes into these commits.

## Definition Of Done

The mocker work is complete when:

- `mock_scan_setup.json` can run a scan without physical channels,
- a mock Hamamatsu camera in external trigger mode records the expected frames,
- APD/PMT simulation works without real NI-DAQ input tasks,
- mixed mock camera + APD/PMT scans do not race or suppress triggers,
- ScanOnce and ScanLapse complete without stalls,
- tests cover the lifecycle and frame-count contracts,
- `NidaqManager` delegates simulation details to a separate coordinator instead
  of growing more mock-specific logic.
