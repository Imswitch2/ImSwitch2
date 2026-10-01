# Magic numbers and shortcuts: what was fixed, and how

**Status:** Review — companion to [magic-number-audit.md](magic-number-audit.md), 2026-09-12.
**Branch:** `codex/acquisition-layout-schema` (PR #29). Every finding the audit verified,
and every lead it left unchallenged, is fixed here. Each entry: the defect in one line,
the fix in one line, where it lives, and the test that pins it.

Two conventions carried through. Where a number was a *unit* problem, the fix names the
unit and derives what depends on it (bytes not frames, hertz not milliseconds, a period
not a bin count). Where a number stood in for a *declaration*, the fix either declares it
in the setup file with a documented default, or refuses at start with a message naming
the setting — never silently substitutes.

## Behaviour changes to look at on a rig

These change what an operator sees. Everything else is invisible unless it was already
going wrong.

| Change | Why it will be noticed |
|---|---|
| **Point-detector scans refuse to start without `nidaq.timerCounterChannel`** (six of seven shipped setups leave it `null`). | Previously the APD/PMT armed against `ctr2InternalOutput`, which nothing drove; the scan produced zeros or a driver timeout. Set the field to a counter no detector uses (`"Dev1/ctr2"`). |
| **`scan.sampleRate` must be `100000` on real NI-DAQ hardware.** Construction refuses otherwise. | The board timebase is 100 kHz; another rate silently ran the scan slower by the ratio with the metadata claiming the requested timing. All shipped setups already say 100000. |
| **Stage (Beta) scans refuse a dwell of 4 ms or less**; the stage panels open at 10 ms instead of 1 ms. | Below 4 ms the ramp+settle folded back over previous pixels and the stage visited the wrong places. A fast stage declares `move_time`/`settle_time` in `scanDesignerParams`. |
| **TriggerScope firmware scans refuse a DAC excursion outside the axis's `minVolt`/`maxVolt`.** | The firmware ramps from the parked position; a scan longer than the travel remaining above it used to be uploaded unchecked. |
| **Photometrics `External "frame-trigger"` now programs `EXT_TRIG_EDGE_RISING` (2304)**, one exposure per rising edge; it used to program `EXT_TRIG_LEVEL` (2560), exposure-while-high. | Needs confirming on a Photometrics camera; no shipped setup uses one. |
| **Point Scan panel's phase delay opens at 0 µs** (was 100); both panels' delay fields are labelled in µs; a setup file can seed them via `scanDesignerParams.phase_delay` / `d3step_delay`. | A calibrated lag belongs in the setup file. Persisted widget state still wins. |
| **Focus lock: `updateFreq` is documented as hertz; a non-positive value is refused; the reacquire deadline is `reacquireTimeoutS + reacquireSamples/updateFreq`.** | Deadline gets longer (1.5 s instead of 1.0 s at the shipped 10 Hz), so a tiling run that used to give up now reacquires. |
| **Every camera without `cameraPixelSizeUm` warns at startup**, and files carry a new `Camera pixel size source` parameter. | The 0.15 µm placeholder is still used; it is now labelled as assumed. |
| **Four camera managers removed** (`BaslerManager`, `GXPIPYManager`, `ESP32CamManager`, `JetsonCamManager`); `AVManager` relabelled as the synthetic camera. | Their drivers were never in the tree; they could only ever run the mock. A setup naming one fails to load with an unknown manager name. |
| **FLIM histogram window defaults to one laser period (391 bins)**, not 64 bins. | Lifetimes above ~2 ns were reported as 0.84–0.95 ns with the old window. A declared short window warns. |
| **Recordings' on-disk chunks are ≤ 4 MiB** instead of 32 frames. | Files are laid out differently; readers see per-frame reads ~30× faster on big cameras. |

## The fixes

### Recording and writer path (branch-introduced or in its path)

| # | Finding | Fix | Where | Test |
|---|---|---|---|---|
| 1 | Chunk queue capped at 16/1000 **frames** by detector category. | Byte budget `MAX_QUEUED_CONSUMER_BYTES` (256 MiB); frame count follows from the frame. | `DetectorManager.py` | `test_chunk_contract.py`, `test_detector_chunk_consumers.py` |
| 2 | Writer queue bounded at 64 *items*. | `WRITER_QUEUE_MAX_BYTES` (512 MiB) applied by the producer. | `RecordingManager.py` | `test_recording.py` |
| 3 | Live view fanned backlog into a stalled recorder. | Display reads reuse the latch instead of draining. | `DetectorManager.py` | `test_chunk_contract.py` |
| 4 | Overflow trim was `list.pop(0)` (O(n²)). | Overflowed queue released once. | `DetectorManager.py` | `test_chunk_contract.py` |
| 5 | Finalize join was a flat 30 s against a 512 MiB queue. | Progress-based join: stuck only if no byte drained for 30 s. | `RecordingManager.py` | `test_recording.py` |
| 6 | `recorded_frames_per_time_point` walked every frame with spans. | Counts the first run and checks divisibility. | `acquisition_layout.py` | `test_layout_review_findings.py` |
| 7 | Stall watchdog a flat 10 s. | `max(10 s, 3 × declared frame interval)`. | `RecordingManager.py` | `test_recording.py` |
| 8 | Batch flushed on count only (empty file until 32 frames). | Time-based flush at 0.5 s. | `RecordingManager.py` | `test_recording.py` |
| 9 | On-disk chunk depth = write batch (32 frames → 4 GiB cliff, 30× read amplification). | `TARGET_CHUNK_BYTES` (4 MiB) sizes the depth. | `RecordingManager.py` | `test_recording_chunk_depth.py` |
| 10 | Live `poll()` returned every unread frame. | `LIVE_POLL_MAX_BYTES` (64 MiB) per poll, cursor stays put. | `improcess/live/sources.py` | four source test files |
| 11 | `getNumCamTTL` counted one cycle's edges as per-position for point scans. | Point-scan layouts cross-checked against the tiled TTL, refused on mismatch. | `_acquisition_layout_source.py` | `test_acquisition_layout_recording_seam.py` |
| 12 | Arm wait 5 s contained a 30 s writer-open budget. | `RECORDING_ARM_TIMEOUT = WRITER_OPEN_TIMEOUT_S + DETECTOR_ARM_TIMEOUT_S`. | `RecordingManager.py`, `RecordingController.py` | `test_recording_chunk_depth.py` |
| 13 | `completion_outcome` HDF5 attr was a literal `S13`. | Width from `VALID_COMPLETION_OUTCOMES`; too-narrow attrs recreated, never truncated. | `RecordingManager.py` | `test_hdf5_outcome_attr.py` |

### Detector managers

| # | Finding | Fix | Where | Test |
|---|---|---|---|---|
| 14 | TimeTagger scan-driven but no `rawFrameIsDeferred`/`drainChunk`: FLIM recording saved the 1 s preview as complete. | Both declared; raw frame delivered once at `is_final`, never after an abort. | `SwabianTimeTaggerManager.py` | `test_swabian_time_resolved_contract.py` |
| 15 | FLIM window 64 × 32 ps = 2 ns against a 12.5 ns period. | `n_bins` defaults to one laser period; short declared windows warn. | same | same |
| 16 | APD clipped real counts to `mockPhotonCountMax`. | Clip only in simulation. | `APDManager.py` | `test_dtype_silent_casts.py` |
| 17 | Four camera managers could only ever run the mock; two returned 4-D chunks. | Removed; `AVManager` relabelled synthetic in catalog/docs/README. | `detectors/`, `builtins.py`, docs | catalog/coverage tests updated |
| 18 | Photometrics mock fallback was a Hamamatsu mock (crashed on the next line). | `MockPhotometrics` answers the PVCAM surface; `"mock"` honoured. | `photometrics_mock.py`, `PhotometricsManager.py` | `test_photometrics_manager.py` |
| 19 | Photometrics trigger write/read maps disagreed on two of three codes. | One table of PVCAM codes; read-back derived; unknown codes reported. | `PhotometricsManager.py` | same |
| 20 | ThorCam re-arm used frames-per-trigger (1) as ring depth. | Depth remembered at arm; `frameBufferDepth` property (4). | `thorcamera_tsi.py`, `ThorCamTSIManager.py` | `test_thorcam_tsi_recording_contract.py` |
| 21 | APD/PMT hardcoded `ctr2InternalOutput` and 1 MHz. | Terminal and rate from `NidaqManager.getTimerClockTerminal()`/`timerRateHz`; refusal without a timer counter. | `NidaqManager.py`, `APDManager.py`, `PMTManager.py` | `test_nidaq_clock_contract.py` |
| 22 | PMT AI range accepted and dropped (±5 V always). | Passed through; `aiVoltageMin`/`aiVoltageMax` (default ±5 V). | `NidaqManager.py`, `PMTManager.py` | same |
| 23 | Camera pixel size 0.15 µm assumed silently and written as calibration. | Startup warning; `Camera pixel size source` parameter (`setup file`/`assumed default`/`user`). | `DetectorManager.py` | `test_camera_pixel_size_parameter.py` |

### Scan and DAQ timing

| # | Finding | Fix | Where | Test |
|---|---|---|---|---|
| 24 | Scan clock 100 kHz literal at eight sites vs. `scan.sampleRate`. | `SCAN_CLOCK_RATE_HZ`/`TIMER_COUNTER_RATE_HZ` once; hardware refuses another rate. | `NidaqManager.py` | `test_nidaq_clock_contract.py` |
| 25 | Reads inherited nidaqmx's 10 s timeout (lines > 10 s failed). | Timeout derived from the task's clock: 2 × expected + 10 s floor. | `NidaqManager.py` | same |
| 26 | TriggerScope DAC excursions unchecked against `minVolt`/`maxVolt`. | `check_dac_range`/`check_firmware_scan_dac_ranges` before upload in all five modes. | `_triggerscope_scan_geometry.py`, five controllers | `test_triggerscope_dac_range.py` |
| 27 | Beta stage 2 ms move + 2 ms settle hardcoded inside each dwell. | `move_time`/`settle_time` designer params; dwell ≤ sum refused; mostly-motion dwell warned; panels open at 10 ms. | `BetaScanDesigner.py`, `ScanWidgetBase.py`, `ScanWidgetMoNaLISA.py` | `test_beta_stage_timing.py` |
| 28 | `maxScanTimeMin` honoured by Galvo only, declared by Beta setups only. | Beta implements `checkSignalLength`; Base and MoNaLISA managers call it. | `BetaScanDesigner.py`, `ScanManagerBase.py`, `ScanManagerMoNaLISA.py` | same |
| 29 | "D3 step delay (samples)" consumed as µs. | Both delay fields labelled and documented in µs; detectors convert phase delay at the timer rate. | widgets, `APDManager.py`, `PMTManager.py` | `test_scan_delay_units.py` |
| 30 | Galvo phase delay hardcoded 100 vs 0 in two panels; Advanced never restored it; garbled entry → silent 0. | Both open at 0, seeded from `scanDesignerParams`; serializer restores both delays; only a missing accessor falls back. | `scan_parameters.py`, both scan controllers | same |

### Timeouts and waits

| # | Finding | Fix | Where | Test |
|---|---|---|---|---|
| 31 | `focusLock.updateFreq` documented ms, consumed Hz. | Documented as hertz; validated > 0; timer floored at 1 ms. | `SetupInfo.py`, `FocusLockController.py`, docs | `test_focus_lock_setup_validation.py` |
| 32 | Reacquire deadline in seconds vs. a window in samples. | Deadline = settle allowance + `reacquireSamples/updateFreq`. | same | same |
| 33 | Autofocus slept 150 ms then read the newest (stale) frame. | `autofocus.settleTimeMs` + shared fresh-frame handshake (`_fresh_frame.py`). | `AutofocusController.py`, `TilingController.py` | `test_fresh_frame.py` |
| 34 | Pulse-generator `stop()` never checked the join. | `TimeoutError` when the worker outlives the join (three sites). | `PulseStreamerManager.py`, `TeensyPulseManager.py`, `teensypulse.py` | `test_pulse_generator_integration.py` |

### ImProcess readers and limits

| # | Finding | Fix | Where | Test |
|---|---|---|---|---|
| 35 | `getMeanData()` read every plane on the GUI thread. | Preview averages ≤ 256 planes at an even stride. | `DataObj.py` | (bounded by `MEAN_PREVIEW_MAX_PLANES`) |
| 36 | Contrast gate in elements (64 Mi), cost in bytes ×17. | Working-set byte budget (256 MiB at 17 B/element). | `contrast.py` | `test_display_budgets.py` |
| 37 | Layout preflight opt-in; the calibrated-loop refusal was dead code. | BeadRec requires calibrated `scan_x`/`scan_y`. | `beadrec/reconstructor.py` | same |
| 38 | `Processor.id` defaulted to `"unnamed"`. | Concrete subclasses without an `id` fail at definition. | `processors/base.py` | same |
| 39 | Live stall watchdog switched off by a per-source flag authors had to remember. | Opt-in per continuous source (`idles_between_stacks` defaults True). | `live/sources.py` | same |

## Not changed, on purpose

- The verifier refuted 10 findings; they are not listed and nothing was done.
- `mockPhotonCountMax` keeps its name and its job (the synthetic generator's ceiling).
- The Beta refusal threshold is `dwell ≤ move + settle`; the 4–20 ms band the verifier called
  "the genuinely dangerous one" gets a warning, not a refusal, because whether it bites depends
  on the camera's exposure within the dwell, which the designer does not know.
