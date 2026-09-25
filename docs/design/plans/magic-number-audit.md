# Magic numbers and shortcuts: an audit

**Status:** Reference — audit of `codex/acquisition-layout-schema`, 2026-09-10/11.
**Why:** two bugs on this branch shared one shape, and the question was whether
the tree holds more of them.

The shape. A detector's chunk queue was capped at sixteen **frames**. Sixteen is
generous for a point detector, whose one raw frame is a whole assembled volume
published once per completed scan. Applied to a camera at 9338 fps it is a stall
tolerance of 1.7 **milliseconds**, and any disk hiccup aborted the recording. The
number was not wrong; the *unit* was. A queue bounds memory, and a frame count
bounds no memory, because a frame is 3 kB on one detector and 8 MB on another.
Separately, pulses per position defaulted to one when a scan declared none — on
both sides of the check meant to catch that, so the check compared a default
with itself.

What the two have in common: a fact that is right for the case it was written
for, silently wrong for its neighbour, and nothing fails when it is wrong.

Eight agents searched for more, each measuring the consequence rather than
listing constants. Every serious finding was then handed to a second agent told
to refute it. That second pass ran across four sessions, because it kept hitting
the account's usage limit; the last sessions ran the verifiers on a smaller
model, which was enough for the job.

## Tally

- **32 verified** — reproduced by one agent and survived another's attempt to refute it.
- **10 refuted** — not listed.
- **12 never challenged by a second agent** — listed at the end as leads, and fixed all the same.
- **Every verified finding and every lead is fixed on this branch** (32 verified + 12 leads), each with its own test. What each fix did, and which ones change behaviour a rig operator will notice, is in [magic-number-audit-fixes.md](magic-number-audit-fixes.md).

Only the first twelve were consequences of this branch's own changes or sat in its path; the rest are older defects of the same shape, fixed here because they were found here.

## Verified

### Detector managers

**FLIM TCSPC window defaults to 2.048 ns against the 80 MHz laser period the same constructor declares** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:88` · *bites someone today*

n_bins default 64 (line 88) x binwidth_ps default 32 ps (line 89) = a 2.048 ns TCSPC histogram window. Five lines further down, laser_rep_rate_mhz defaults to 80.0 MHz (line 93) = a 12.5 ns excitation period. The window covers 16% of the period it is meant to sample.

Anyone running the Swabian TimeTagger FLIM detector on the shipped defaults, which docs/devices/detectors.rst:709-715 reproduces verbatim as the copy-paste example config. Every fluorophore with a lifetime above ~2 ns reports 0.84-0.95 ns, and the FLIM image loses essentially all lifetime contrast: a 2 ns and a 4 ns species differ by 0.08 ns in the reported map where the truth is a factor of two. There is no warning, the intensity image looks correct, min_counts_per_pixel and the 5x outlier clamp behave normally, and the numbers are physically plausible -- so it reads as a real (short) lifetime, not as a truncated histogram.

> Reported vs true tau, moment fit, n_bins=64 binwidth=32ps (window 2.048 ns): 0.5->0.45 (-10%), 1.0->0.68 (-32%), 2.0->0.84 (-58%), 2.5->0.87 (-65%), 3.0->0.89 (-70%), 4.0->0.92 (-77%), 6.0->0.95 ns (-84%). Closed form: measured = tau - T/(exp(T/tau)-1), which saturates at ~T/2 as tau grows, so the whole 2-6 ns range collapses into 0.84-0.95 ns. With n_bins=390 at the same 32 ps (window 12.48 ns ~ one 80 MHz period) the same code returns 0.98/1.96/2.40/2.79/3.41 ns -- errors of 2-15% instead of 32-84%. Secondary: only 1-exp(-2.048/2.5) = 56% of a 2.5 ns emitter's photons land inside the window at all, so the intensity image and the min_counts_per_pixel=20 validity gate are both short by ~44%.

*Verifier's correction:* The finding stands and is, if anything, understated; four corrections to scope, severity and the proposed fix. (a) Which fitters: the compression bites the default 'moment' AND 'phasor' (phasor at 64x32: 0.45/0.70/0.89/0.93/0.96/0.99/1.03 ns for 0.5-6 ns, i.e. 56-83% low for 2-6 ns), but NOT 'exp1', which is truncation-invariant on clean decays (0-1% error at the default window in my run, only noisier because 29-56% of photons are lost). A user who follows the docs' advice that exp1 is 'most accurate' escapes the bias; the docs' phasor statement 'close to the true tau when tau << T_rep' (detectors.rst:807-810) is false at the shipped window. (b) Severity with a real IRF is worse than the claim's numbers, which assume the IRF peak at bin 0: with a 200 ps FWHM IRF the argmax peak lands at bin 10-12, leaving ~1.7 ns of post-peak window, and moment/phasor report 0.51/0.61/0.60/0.65 ns for tr

*The right shape:* Do not default the window independently of the rate. Derive it: n_bins = ceil((1/laser_rep_rate) / binwidth_ps) for the declared rep rate, or at minimum refuse to start (or warn loudly, once) when n_bins*binwidth_ps < 0.8 * (1/laser_rep_rate_mhz). The two numbers are one physical fact expressed twice; only one of them should be a free default.


**SwabianTimeTagger is scan-driven but does not declare rawFrameIsDeferred, so a FLIM recording saves the one-second live-preview snapshot as the finished measurement** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:962` · *bites someone today*

Two per-detector declarations that default to the camera answer and that a scan-driven detector must remember to override: rawFrameIsDeferred (DetectorManager.py:538, default False) and drainChunk (DetectorManager.py:561, default raw = display). Swabian overrides isScanDriven, pixelSizeUm, scale, dtype, finishScan -- and neither of these two. Combined with FlimWorker.LIVE_PREVIEW_S = 1.0 s (line 1149), the raw stream becomes 'whatever the scan had reached one second in'.

Anyone doing a ScanOnce or ScanLapse recording of a FLIM scan. RecordingManager.py:3831 registers the recording as ChunkKind.RAW; RecordingManager.py:3625-3627 plans exactly ONE frame for a scan-driven detector. The FLIM worker publishes a non-final preview frame every LIVE_PREVIEW_S and sets _newFrameReady unconditionally (line 783), so the recording's plan is satisfied by the first preview tick, should_stop() fires, and the file is finalised as a clean, complete scan. The saved lifetime image contains only the lines acquired in the first second; the rest is zeros. Nothing downstream can tell -- no warning, no stall, no short-frame message. APDManager (:760, :806) and PMTManager (:819, :860) both implement the deferral correctly, so this is the one scan-driven detector where the contract silently reverts to the camera behaviour it was written to replace.

> A 256x256 FLIM scan at 1 ms/pixel takes 65.5 s. The first preview tick lands at t = LIVE_PREVIEW_S = 1.0 s, i.e. ~1000 of 65536 pixels = 1.5% of the measurement. The recording's plan for a scan-driven detector is 1 frame, so it stops there: 98.5% of the acquisition is discarded and the remaining frames are counted as 'delivered beyond the plan'. The error scales with scan length -- every FLIM scan longer than one second is affected, and the longer (i.e. better) the scan, the smaller the fraction that gets saved.

*Verifier's correction:* The finding stands and is reachable, but three details are wrong. (1) WRONG MECHANISM IN THE HEADLINE. The recording victim comes from the missing drainChunk override (DetectorManager.py:561, default ChunkPayload(display=frames, raw=frames)), not from rawFrameIsDeferred. rawFrameIsDeferred is read in exactly one place, getLatestFrameShared(is_save=True) at DetectorManager.py:669, so its absence is a second, separate consequence: a Snap/save-hint read during a running FLIM scan returns a half-filled frame instead of raising RawFrameUnavailableError (verified: my check1.py got the 1.6%-filled frame back). Retitle as "SwabianTimeTaggerManager overrides neither drainChunk nor rawFrameIsDeferred"; the recording bug is drainChunk's. (2) "the remaining frames are counted as 'delivered beyond the plan'" is wrong, and the truth is quieter: should_stop sets shouldStopNext, the loop exits and the f

*The right shape:* Give SwabianTimeTaggerManager the same latch APD and PMT have: rawFrameIsDeferred -> True, and a drainChunk that publishes the volume exactly once, when _on_frame_ready arrives with is_final=True (and never for a generation that aborted). Better still, make the base class refuse the default: a manager whose isScanDriven is True should not be allowed to inherit rawFrameIsDeferred=False silently -- assert it, or derive the raw contract from isScanDriven rather than from a second flag that must agree with it.


**APDManager clips real photon counts to mockPhotonCountMax on the live NI-DAQ path** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:1212` · *bites someone today*

_mock_photon_count_max, default 5000 counts (APDManager.py:82-92, config key mockPhotonCountMax). It is a synthetic-data bound -- docs/mock-infrastructure.rst:78 lists it alongside mockPhotonCountMean/mockRandomSeed as parameters of the mock generator -- and randomInput() at :1388 is its intended consumer. samples_to_pixels() applies it to every pixel, gated only on _ttlmultiplying, never on _simulation_mode.

Any real APD confocal/STED acquisition with a per-pixel count above 5000. The clip is a flat top at exactly 5000 on bright features, which is indistinguishable from APD/counter saturation, so the user blames the detector or the sample. PMTManager, written by the same hand, keeps its mock bounds strictly inside randomInput() (:1150-1162) -- the APD is the one that leaks. Nobody will find it by reading the config either: the only way to raise the ceiling is to set a key called 'mockPhotonCountMax' on a real rig.

> The clip threshold in count-rate terms is 5000/dwell: dwell 100 us -> 50 Mcps (above APD saturation, safe); dwell 500 us -> 10 Mcps; dwell 1 ms -> 5 Mcps; dwell 10 ms -> 0.5 Mcps. Measured: at dwell 10 ms and 1 Mcps the true 10000 counts/pixel is reported as 5000 -- a factor of 2 low, silently. A bright bead at 5 Mcps with a 1 ms dwell sits exactly on the threshold. Note also that the ONLY clip warning in the manager (_warned_mock_count_clip, :645-651) guards the uint16 dtype range 0..65535, which this 5000 clip makes permanently unreachable -- so the clip that fires is the one that is never reported, and the reporting machinery guards a clip that can no longer happen.

*Verifier's correction:* The finding stands; the diagnosis needs two corrections, one tightening and one widening.

WIDEN THE MECHANISM: the report describes the clip as "gated only on _ttlmultiplying". On APDManager that gate is dead -- _ttlmultiplying is assigned only at APDManager.py:125 (False) and never from scanInfoDict, unlike PMTManager.py:951. So the clip applies to EVERY real APD scan including TTL-multiplying (MoNaLISA/STED sequence) setups, which the report implies would escape it. (Side effect worth a separate finding, not this one: APD's float32/NaN TTL image path at :732 and :949 is therefore also dead in production.)

TIGHTEN THE VICTIM: "Any real APD confocal/STED acquisition with a per-pixel count above 5000" overstates it. At the shipped default dwell of 0.02 ms (ScanWidgetPointScan.py:14, ScanWidgetAdvanced.py:32) the clip engages only above 250 Mcps, and at 100 us only above 50 Mcps -- both 

*The right shape:* Gate the clip on self._manager._simulation_mode, or move it into randomInput() where the other mock bounds live. If a real-data ceiling is genuinely wanted it should be a separate, differently named property expressed in counts-per-second (a detector property) rather than counts-per-pixel (which silently depends on the dwell time).


**A camera with no cameraPixelSizeUm silently gets 0.15 um/px, and no shipped setup declares it** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/DetectorManager.py:201` · *bites someone today*

default: float = 0.15 um -- 'a typical high-mag value' per the docstring. It is the sample-plane pixel size used by tiling, stitching, scale bars, napari layer scale and the saved OME PhysicalSizeX/Y.

Every camera detector whose setup file omits cameraPixelSizeUm -- which is all 15 shipped setups. The function goes to real trouble to catch a MISSPELLED key (DetectorManager.py:228-239 warns about near-misses) and to catch an unparseable value (:246-252 warns about decimal commas), but the plain-absent case at :240-243 returns 0.15 with no log line at all. Because configuredCameraPixelSize then returns None, the parameter is also left out of configOwnedParameters (:325-330), so the fallback becomes ordinary persisted widget state rather than a calibration the config owns. The user sees a scale bar and an OME PhysicalSize that look like real calibration and cannot be told from one. Separately, a camera manager that forgets makeCameraPixelSizeParameter entirely gets a different silent default: pixelSizeUm returns [1.0, 1.0, 1.0] (:485-489), i.e. 1 um/px.

> A common real configuration -- 6.5 um sensor pitch at 20x -- is 0.325 um/px. Against the silent 0.15 that is a 2.17x scale error in every stitched mosaic, every measured distance and every saved PhysicalSize. At 60x on the same sensor the true value is 0.108 um/px and the default is 1.39x too large in the other direction, so the sign of the error is not even consistent.

*Verifier's correction:* Four corrections; the finding survives all of them.

1. "all 15 shipped setups" is wrong. Six of the fifteen declare no detectors at all (example_coolLED has only lasers/positioners/nidaq/rs232/scan; fiji_processor, general_image_processing, monalisa_processor, snouty_processor, widefieldstarss_processor are improcess setups). The accurate statement is: 9 setups define detectors, holding 12 camera-detector entries, and 12 of 12 get the 0.15 default. The three scan-driven entries (2 APD, 1 more APD in the mixed setup) are correctly exempt.

2. The missing log line is not an oversight, and framing it as one points at the wrong fix. imswitch/imcontrol/_test/unit/test_camera_pixel_size_parameter.py:56 is `test_omitted_key_uses_default_silently`, docstring "Leaving the property out is a legitimate choice, not a mistake", asserting messages == []. docs/devices/detectors.rst:70 documents "Omitt

*The right shape:* Treat 'absent' the same way the function already treats 'misspelled': warn once, naming the detector and the value being assumed. Better, make the fallback self-identifying -- record in the parameter (and hence in the saved metadata) that the pixel size is an assumed default rather than a declared calibration, so a file written with it cannot be mistaken for a calibrated one.


**APD and PMT hardcode the NI-DAQ sample clock terminal to counter 2 while the setup file chooses which counter generates it** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:69` · *bites on a different rig*

_nidaq_clock_source = 'ctr2InternalOutput' (APDManager.py:69, PMTManager.py:62) and _detection_samplerate = 1e6 (APDManager.py:68, PMTManager.py:61). Both are hardcoded in the detector; the pulse train they name is created by NidaqManager from setupInfo.nidaq.timerCounterChannel (NidaqManager.py:128, :1240-1250) at rate=1e6. The detector hardcodes the counter INDEX; the config chooses it.

A point-scanning rig whose timer counter is anything but ctr2 -- e.g. an X-series board where ctr0/ctr1 are the free ones, or a second device. SetupInfo.py:504-519 accepts any string or integer and the config-editor template (utility_scripts/builtin_templates/sections/nidaq.json:8) only suggests 'Dev1/ctr2' as an example. Set timerCounterChannel to 0 and the APD/PMT input task is clocked from ctr2InternalOutput, a terminal driven by nothing: the finite CI/AI read never receives its sample clock, so the scan produces no data and the read blocks until teardown. Nothing cross-checks the two, and the failure names neither counter. Worse, timerCounterChannel defaults to None (SetupInfo.py:504), in which case NidaqManager.py:1240 skips creating the timer task entirely while the detector still arms an input task clocked from it. Also note the terminal string carries no device prefix, while the APD's own counter line does honour a configurable deviceName (APDManager.py:71-74).

> Related and measurable: the ratio between the two clocks is derived from config on the detector side and hardcoded on the NI-DAQ side. APDManager.py:1010 computes _frac_scan_det_rate = round(1e6 * scan_time_step) where scan_time_step = 1/setupInfo.scan.sampleRate, while NidaqManager.py:1244 computes the timer's finite sample count as outputSamples * (1e6/100e3) and clocks the AO from a hardcoded '100kHzTimebase' at 100000 Hz (NidaqManager.py:1264-1272). Every shipped setup uses sampleRate 100000, so both give 10. Set scan.sampleRate to 200000 (a natural thing to try for smoother galvo waveforms) and the APD arms for N*5 detection samples while the hardware still runs the scan for N*10 -- the APD stops reading halfway and the second half of every image is blank. At 250000 it reads 40%; at 50000 it waits for twice as many samples as the timer will ever produce and hangs.

*Verifier's correction:* The finding stands -- the detector hardcodes the counter index while the config chooses it, with no cross-check, no test, no doc, and a docs example (`0` -> Dev1/ctr0) that walks a new rig straight into it -- but three parts of the report need correcting.

SYMPTOM (severity down, not up). "The read blocks until teardown" is wrong. Reads go through `task.read(samples)` with nidaqmx's default 10.0 s timeout, so the first read past the missing clock raises DaqError -200284 after ~10 s; the worker catches it, logs a traceback, and _markRawFailed keeps the half-written volume from being published, so a recording ends with no data rather than with plausible garbage. The right description is: a dead point detector plus a DAQmx timeout that names the detector task and neither counter -- bad, but noisy in the log and not silent corruption.

BOARD DEPENDENCE. The silent variant is narrower than st

*The right shape:* The detector should ask the NidaqManager for the clock it actually created (terminal and rate) rather than restating them. At minimum, NidaqManager should expose the timer terminal derived from timerCounterChannel, and startInputTask should reject -- loudly, at scan build -- a clock source that names a counter no task is driving.


**Five camera managers can never reach real hardware and silently present a synthetic mock as a live camera** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/BaslerManager.py:171` · *bites on a different rig*

Not a number but the same shape: a try/except fallback sized for 'the SDK is not installed on this dev box' that is load-bearing on a rig. Five managers import an interface module that does not exist anywhere in this tree -- avcamera, baslercamera, esp32camera, gxipycamera, jetsoncam -- and fall through to MockCameraTIS, a 500x500 synthetic bead generator, behind a single logger.warning.

Anyone who puts AVManager, BaslerManager, ESP32CamManager, GXPIPYManager or JetsonCamManager in a setup file. ImSwitch starts normally, the detector appears in the GUI under its configured name with its configured 'Camera pixel size', live view shows a plausible moving-bead image, and recordings save that synthetic data with the rig's real calibration in the OME PhysicalSize. The only signal is one warning line at startup among hundreds. For ESP32Cam and PiCam, which take a host/port, the user is pointing at a real networked camera and getting fabricated frames. There is a second inconsistency inside the mock itself: it reports image_width/image_height = 800x800 while grabFrame() returns 500x500, so the manager's fullShape (and everything derived from it, including OME SizeX/SizeY) is 800x800 for 500x500 data.

*Verifier's correction:* The finding stands in substance -- four managers (AVManager, BaslerManager, ESP32CamManager, GXPIPYManager) construct successfully against a synthetic mock and can never reach hardware -- but the diagnosis, the location, the count and the severity are all wrong, and the fix implied by the report is wrong too.

CORRECTED DIAGNOSIS. The defect is not the try/except and not BaslerManager:171. Every one of these managers documents the fallback as its mocking mechanism ("set this string to an invalid value, e.g. the string 'mock' to load a mocker"), and for AVManager it is the repo's shipped, tested no-hardware path. The actual defect is that ImSwitch2 still ships and ADVERTISES five DetectorManagers whose drivers upstream deleted in November 2022: build_catalog offers all five in the config editor's detector list, README's "Minimal setup file" presents AVManager with `cameraListIndex: 0` as 

*The right shape:* A mock substituted for missing hardware must be visible at the level the user works at, not only in the log: either refuse to construct the detector unless the setup file opts in (the way HamamatsuManager/TISManager use an explicit 'mock' cameraListIndex), or mark the manager as mocked so the GUI and every saved file's metadata say so. A recording produced from synthetic frames must not be indistinguishable from a real one.


**ThorCam re-arm reuses frames-per-trigger as the ring-buffer depth, shrinking it from 4 frames to 1 on any runtime trigger-mode change** — **fixed on this branch**
`imswitch/imcontrol/model/interfaces/thorcamera_tsi.py:167` · *bites on a different rig*

Three different numbers for one quantity. ThorCamTSIManager arms with buffer_size=4 (lines 115, 238, 340); the interface's own signature defaults it to 2 (thorcamera_tsi.py:195); and the re-arm inside set_trigger_mode reads it from frames_per_trigger_zero_for_unlimited, which __init__ sets to 1 (thorcamera_tsi.py:49). Frames-per-trigger and frames-to-buffer are different quantities that happen to both be small integers.

A ThorCam TSI (Zelux/Kiralux/Quantalux) used for scan-triggered recording. The workflow is exactly the trigger: the camera is armed with 4 buffers in __init__, then the user switches 'Operation Mode' to 'Hardware' from the Settings panel, which calls set_trigger_mode -> disarm -> arm(1). The SDK ring is now one frame deep for the rest of the session (startAcquisition only re-arms when not armed, ThorCamTSIManager.py:339-340). Any recording-loop iteration slower than one inter-trigger interval loses frames; the recording then never reaches recFrames*numCamTTL and dies on the stall watchdog with a message about camera triggering and numCamTTL -- pointing the one diagnosis it has at the wrong thing.

> Recording drains via ThorCamTSIManager.getChunk once per loop iteration; RecordingManager.FRAME_POLL_INTERVAL is 100 us plus per-iteration Python/GIL work with a writer thread running, realistically 0.2-2 ms per iteration. Slack = ring depth / trigger rate. At depth 4 the loop tolerates a trigger rate up to 4/0.002 = 2 kHz; at depth 1 it tolerates 500 Hz. A tiling scan at 1 ms per position (1 kHz camera TTL) is inside the tolerance at depth 4 and outside it at depth 1 -- so the recording works until the user changes the trigger mode from the GUI, and then stops working, with nothing in between to connect the two.

*Verifier's correction:* The finding stands; the diagnosis (frames-per-trigger reused as frames-to-buffer, contradicting the function's own docstring "re-arm with the previous buffer size") is right. Three corrections.

A. THE VICTIM SCENARIO AS GIVEN IS NOT REACHABLE, AND IT IS THE ONE CASE WHERE THE BUG DOES NOT DISCRIMINATE. "A tiling scan at 1 ms per position (1 kHz camera TTL)" cannot happen with a Zelux/Kiralux/Quantalux: the configured camera (example_kiralux_teensy.json, serial 29718; mock sensor 2448x2048) has a 10.0 MB full frame, so 1 kHz is 10 GB/s into a writer whose queue is 512 MB (WRITER_QUEUE_MAX_BYTES) — at 1 kHz depth 4 (4 ms of slack) fails just as surely as depth 1, so that configuration proves nothing. The steady-state framing ("0.2-2 ms per iteration") is also the wrong model: at any rate these cameras actually run, both depths are fine in steady state. What the ring depth buys is toleranc

*The right shape:* Remember the depth the camera was actually armed with (or take it from a single configurable property next to flushFrameLimit, which is already config-driven at ThorCamTSIManager.py:41) and re-arm with that. Never derive a buffer depth from a frames-per-trigger setting; they are different units of the same integer.


**PhotometricsManager's mock fallback is a Hamamatsu mock that implements none of the API it is substituted into** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py:234` · *bites on a different rig*

Not a magnitude: a substitution written for one detector's API reused for its neighbour's. The except branch logs 'loading mocker' and hands back MockHamamatsu, which has none of sensor_size, name, scan_line_time, poll_frame, check_frame_status, start_live, abort, finish, exp_time, exp_mode, readout_port, binning, roi or close.

Anyone who configures a Photometrics camera on a machine without pyvcam, or with the camera unplugged. The warning says a mocker was loaded; the very next statement (PhotometricsManager.py:29, fullShape = self._camera.sensor_size) raises AttributeError, so ImSwitch fails to start with an error that names MockHamamatsu and gives no hint that the real cause was 'pyvcam missing'. The fallback exists precisely so that this case degrades gracefully, and it is the one case it cannot survive.

*Verifier's correction:* The bug is real and I could not refute it, but the diagnosis is wrong in three places and the severity framing inverts one argument.

WRONG LOCATION. It does not raise at PhotometricsManager.py:29 (`fullShape = self._camera.sensor_size`). _getCameraObj never returns: it raises one line before the `return`, at PhotometricsManager.py:237, `self.__logger.info(f'Initialized camera, model: {camera.name}')`. So `sensor_size` is never reached, and the "very next statement" characterisation is wrong. This matters for the fix: HamamatsuManager.py:530 is the same line reading `camera.camera_model` — the copy-paste changed the vendor attribute name but not the object handed back.

WRONG ABOUT THE MISSING HINT. The claim says the error "gives no hint that the real cause was 'pyvcam missing'". It does. The `exc_info=True` WARNING logged immediately before is the line directly above the traceback: `Fa

*The right shape:* Either write a Photometrics mock that answers the PVCAM surface the manager actually uses, or let the ImportError propagate with its own message. A fallback that cannot stand in is worse than no fallback, because it converts a clear 'pyvcam is not installed' into an AttributeError on an unrelated class.


**Photometrics trigger-source enum: the write map and the read-back map disagree on two of three values** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py:154` · *bites on a different rig*

Four PVCAM exp_mode codes, mapped inconsistently in two directions. _setTriggerSource writes 1792 for 'Internal trigger', 2048 for 'External start-trigger', 2560 for 'External frame-trigger' (lines 150/154/158). _updatePropertiesFromCamera reads 1792 -> Internal, 2304 -> start-trigger, 2048 -> frame-trigger (lines 202/204/206). 2560 maps to nothing on the way back and 2304 is never written.

A Photometrics user setting up a scan-triggered recording. Select 'External start-trigger': the hardware gets 2048, and the next _updatePropertiesFromCamera -- which runs after every exposure-time change (line 137) and at construction (line 60) -- reads 2048 back and rewrites the displayed parameter to 'External frame-trigger'. Select 'External frame-trigger': the hardware gets 2560, which matches no branch, so the displayed value silently stays at whatever it was, potentially 'Internal trigger'. The parameter dictionary is what the Settings panel shows and what gets snapshotted into the recording's metadata, so the saved file records a trigger mode the camera was not in. Adding a fourth mode requires remembering to touch both maps, and getting it wrong changes nothing visible until someone reads the metadata.

*Verifier's correction:* Three corrections; the defect survives but the diagnosis and the victim are wrong.

1. REFUTED — the 'frame-trigger' sub-case. The report says selecting 'External "frame-trigger"' leaves the display "at whatever it was, potentially 'Internal trigger'". False. `DetectorManager.setParameter` (:352) stores the requested value BEFORE `_setTriggerSource` writes, and the no-match branch in `_updatePropertiesFromCamera` leaves the stored value alone — which is already correct. Measured: display 'External "frame-trigger"', camera 2560, stable across exposure changes. The unmatched 2560 is harmless; it is what makes this case the one that works.

2. REFUTED — "the saved file records a trigger mode the camera was not in". False. Because the read-back re-enters the writing `setParameter`, the camera is rewritten to 2560 too. Display, sharedAttrs (SettingsController.updateSharedAttrs pushes every de

*The right shape:* One bidirectional table -- a single dict of label -> code with the reverse derived from it -- so the two directions cannot drift, and an explicit else-branch on read-back that logs an unrecognised code instead of leaving the displayed value stale.


### Scan and DAQ timing

**Beta stage scan hardcodes a 2 ms move + 2 ms settle inside each pixel dwell; the shipped default dwell is 1 ms, so the fast axis is driven to the wrong positions** — **fixed on this branch**
`imswitch/imcontrol/model/signaldesigners/BetaScanDesigner.py:153-154` · *bites someone today*

smooth = ceil(0.002 * sampleRate) and settling = ceil(0.002 * sampleRate) — 2 ms + 2 ms = 4 ms of the pixel dwell, evidently sized for a piezo/stage settle at a ~10 ms camera exposure. Nothing declares it; no positioner property feeds it.

Every setup using BetaScanDesigner (6 of the 8 shipped configs: example_coolLED, example_monalisa, example_no_hardware, example_snouty_smart_modes, mock_scan_setup, hamamatsu_mock_scan_setup, mixed_hamamatsu_apd_mock_scan_setup) whose dwell is under 4 ms. ScanWidgetBase.py:149 and ScanWidgetMoNaLISA.py:23 both default the dwell field to '1' ms, so this is the out-of-the-box state: press Run and the stage visits the wrong places. The operator sees a stage-scanned image whose fast axis is shifted by ~2 ms/dwell pixels, with several nominal positions never visited and the last few pixels of every line duplicated at the end position. No exception, no log line.

> At sampleRate=100000: smooth=settling=200 samples each. The write region for pixel s is [ (s+1)*seq - 400, (s+1)*seq ). With seq = dwell*1e5, that reaches back ceil(400/seq) = ceil(4 ms / dwell) pixel windows. dwell=1 ms -> 4 windows back; measured max position error 3.565 um on a 1 um step = 3.57 pixels. The guard `if (end - smooth - settling) > 0` only suppresses the first few iterations, so early pixels are silently overwritten by later ones rather than the case being rejected.

*Verifier's correction:* The finding stands, but the diagnosis and threshold are wrong in a way that UNDERSTATES it, and the victim list is overstated.

THRESHOLD IS WRONG — it is not a cliff at 4 ms. The 4 ms is stolen from the END of every dwell at every dwell: the ramp to pixel s+1 plus the settle at s+1 always occupy the last 400 samples of pixel s's window. Measured mean fast-axis position error per dwell window (real makeFullScan, 100 kHz, 1 um step, conv 1.587): 20 ms -> 0.178 px; 10 ms -> 0.356 px; 5 ms -> 0.713 px; 4 ms -> 0.891 px; 2 ms -> 1.781 px; 1 ms -> 3.565 px. The claim calls 10 ms "fine" and 5 ms "fine-but-40%-of-dwell-spent-at-the-next-position". Neither is fine: at 10 ms the image is systematically shifted 0.36 px with 40% of every exposure smeared across a full step, and at 5 ms it is 80% of the exposure and 0.71 px. The claim's own "40%" figure is misattributed — it applies to 10 ms, not 5 

*The right shape:* A settle time is a property of the mechanics, so it belongs in the positioner's managerProperties (e.g. settleTimeS, alongside the existing conversionFactor/minVolt/maxVolt), converted to samples per axis at design time. Whatever the source, it must be clamped against the dwell — settle_samples = min(round(settleTimeS*sampleRate), sequenceSamples) — and a dwell shorter than the settle time must be refused with a message, not silently folded back over the previous pixels.


**'maxScanTimeMin' — documented as a hard cap on scan duration — is only honored by GalvoScanDesigner, and is declared exclusively in setups that use BetaScanDesigner** — **fixed on this branch**
`imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:72-76 (only reader) vs imswitch/imcontrol/model/managers/ScanManagerBase.py:119 (Base/MoNaLISA makeFullScan, never calls checkSignalLength)` · *bites someone today*

maxScanTimeMin = 1 (minute), declared in 4 shipped setups; and the companion guard `scan_steps > 1e7` in the same method. The config-editor template calls it "Hard cap on scan duration. null = no limit" and docs/setupinfo-reference.rst documents it as effective.

Users of example_no_hardware, example_snouty_smart_modes, hamamatsu_mock_scan_setup and mixed_hamamatsu_apd_mock_scan_setup — all four declare "maxScanTimeMin": 1 and all four are scanWidgetType "Base" + BetaScanDesigner. ScanManagerBase.makeFullScan never calls checkSignalLength, and BetaScanDesigner never overrides the base no-op that returns True, so the cap is a double no-op. Only ScanManagerPointScan calls it — and example_sted, the one PointScan setup, is the one setup that does NOT declare maxScanTimeMin. The user sets a ROI, hits Run, and instead of a refusal gets a multi-gigabyte allocation and a scan running tens of minutes past their declared cap.

> Beta total samples = ny * (nx*ceil(dwell*fs) + ceil(return_time*fs)). With the shipped return_time=0.01 s, fs=100 kHz and a 10 ms dwell: 128x128 = 16,512,000 samples = 165 s (2.8x over the declared 1-minute cap, measured); 512x512 = 262,656,000 samples = 43.8 minutes and 4.2 GB for the two float64 AO arrays alone — before NidaqManager's `np.array(AOsignals).squeeze()` copy and the bool TTL arrays. Nothing refuses it.

*Verifier's correction:* The headline stands — maxScanTimeMin (and the companion `scan_steps > 1e7` guard) is a double no-op for every non-Galvo designer, and every shipped setup that declares it is Base+Beta, so the declaration never does anything. But the victim list and the severity are wrong in three ways.

(a) Half the named victims cannot scan at all. example_no_hardware and example_snouty_smart_modes do not list "Scan" in availableWidgets, so the dock is never created (ImConMainView.py:109), and each has only 2 forScanning positioners while BetaScanDesigner hard-raises `ValueError: BetaScanDesigner requires 3 target devices/axes` (verified by running it). "The user sets a ROI, hits Run" is impossible there. Only hamamatsu_mock_scan_setup and mixed_hamamatsu_apd_mock_scan_setup are reachable.

(b) "A scan running tens of minutes past their declared cap" is false in ALL FOUR named setups, because all four s

*The right shape:* Move the duration/size guard out of one designer and into SuperScanManager.makeFullScan so every widget type gets it, and express it in the two quantities that actually bind: seconds (scan_samples_total * scan_time_step, which every designer already reports in the ScanInfoContract) against maxScanTimeMin, and bytes (samples * 8 * n_AO_axes + samples * n_DO_lines) against a memory budget — not a pixel-step count.


**'D3 step delay (samples)' in the point-scan GUI is consumed as microseconds, giving 1/10 of the requested inter-slice settling at the shipped sample rate** — **fixed on this branch**
`imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:82 (`self.__paddingtime_d3step = int(parameterDict['d3step_delay'])  # inter-slice delay [µs]`) vs imswitch/imcontrol/view/widgets/ScanWidgetPointScan.py:143` · *bites someone today*

d3step_delay, entered in a field labelled "(samples)", used verbatim as µs and then divided by __timestep = 1e6/sampleRate = 10 µs. The label is only correct at sampleRate = 1 MHz; every shipped config uses 100 kHz.

The example_sted / PointScan operator who sets a per-slice delay so a Z piezo or the polarization rotator (docs/api/api.imcontrol.rst: "mostly relevant for the polarization during scan") can settle between planes. They type 1000 expecting 10 ms of settling at 100 kHz and get 1 ms. The device is still moving when the first line of the new slice is acquired; the plane is skewed or blurred, and since scan_throw_settling is what the APD/PMT throw away, detector and designer agree with each other about the wrong number. Nothing warns.

> settling_samples = round(d3step_delay_µs / (1e6/sampleRate)) = d3step_delay * sampleRate/1e6. At sampleRate=100000 that is d3step_delay/10 — a 10x shortfall against the label. Entering 1000: label promises 1000 samples = 10.0 ms; delivered 100 samples = 1.0 ms. (The neighbouring 'Phase delay (samples)' field, default 100, is the same confusion in the other direction: APDManager.py:1059 / PMTManager.py:970 take it raw into the 1 MHz detection-sample domain — it is the only scanInfoDict quantity not scaled by _frac_scan_det_rate — so it too is really µs.)

*Verifier's correction:* Two numbers and one side-note need fixing; the headline holds.

1. "They type 1000 ... and get 1 ms" is wrong. There is a ~2.5 ms accidental floor. Measured on example_sted: pre-slice dead time is 2.50 ms at d3step_delay=0 and 3.50 ms at 1000, versus 10.45 ms if the "(samples)" label were honoured. The operator's real shortfall is 7 ms (a factor of 3.0 on total settle), not a factor of 10 on delivered settle. The factor of 10 applies only to the field's own contribution (100 samples instead of 1000).

2. The 2.5 ms floor is itself worth flagging and is arguably the bigger latent defect at line 289: settlingtime = d3step_delay + (max(t_initpos) - min(t_initpos)) over SMOOTH axes only. Adding the piezo as a third smooth axis (vel_max=1000 µm/µs, acc_max=1000 vs. the galvos' 0.1/0.0001, t_acc = 1000 µs) inflates the spread from 0 to ~2048 µs. So the inter-slice settle a user actually gets i

*The right shape:* Both fields are times, not counts. Label and store them in µs (or seconds) and convert at the point of use — settling_samples = round(delay_s * sampleRate), phase_delay_samples = round(delay_s * detection_samplerate) — so neither meaning changes when the sample rate does.


**scan.sampleRate is honored by every designer, the simulator and the detectors, but NidaqManager clocks the real AO/DO scan from the hardwired 100 kHz timebase** — **fixed on this branch**
`imswitch/imcontrol/model/managers/NidaqManager.py:1265 (`scanclock = r'100kHzTimebase'`), 1274, 1296, and the 1 MHz/100 kHz ratio at 1191 and 1244` · *bites on a different rig*

100000 Hz / '100kHzTimebase', hardcoded at 6 sites, and `1e6/100e3` = 10 hardcoded at 2 more — against `scan.sampleRate`, a REQUIRED config field the editor template describes as "DAQ output sample rate" and docs/setupinfo-reference.rst as "DAQ sample rate in Hz". All 8 shipped setups happen to say 100000, so the two truths never diverge in-tree.

Anyone who raises sampleRate to get finer timing — the natural move on a point-scan rig, since at 100 kHz the smallest resolvable dwell is 10 µs and the shipped point-scan dwell default is 0.02 ms = 2 samples/pixel. With sampleRate=1e6 the designers build a waveform with 1 µs steps and NI-DAQmx plays it out at 10 µs steps (cfg_samp_clk_timing with an external `source` terminal uses that terminal's rate; the `rate` argument is only buffer sizing). The scan silently runs 10x slow, every dwell is 10x the requested one, and the recorded scan_time_step/dwell_time metadata claim the requested values. Worse for APD/PMT: _frac_scan_det_rate = round(1e6 * scan_time_step) = 1, so the finite counter task is armed for scan_samples_total samples while the timer task is armed for outputSamples*10 — the detector finishes reading after the first tenth of the scan.

> Requested dwell D at configured rate F yields round(D*F) samples; played at 100 kHz the realized dwell is round(D*F)/1e5 = D * F/1e5. F=1e6 -> every dwell, line time and total scan time is 10x too long, and the reported tot_scan_time_s is 10x too short. NidaqManager's own debug line even reads `scanSampsInScan / 0.1e6`, i.e. it assumes 100 kHz.

*Verifier's correction:* The finding stands but three things in it need correcting.

SITE COUNT IS OVERSTATED (6 -> effectively 1 decisive + 3 dependents). Lines 1019 and 1053 are `setDigital`/`setAnalog` one-shot writes of 100 and 10 IDENTICAL samples to hold a static level; the clock there only decides whether a constant write takes 1 ms or 0.1 ms, and no timing depends on it. They are not load-bearing and including them inflates the blast radius. The decisive site is 1265 alone. 1274 and 1296 pass `rate=100000`, which by nidaqmx semantics is only the expected-rate hint for an external clock and sets nothing; they are wrong-looking but inert. 1191 and 1244 (`1e6/100e3`) are correct-given-1265: they size the 1 MHz timer to the real 100 kHz-paced AO duration. Fix 1265 to derive from `scanInfoDict['scan_time_step']` (already in scope) and the ratio sites must move with it.

"SILENTLY" IS ONLY HALF TRUE, AND THE O

*The right shape:* Either derive the clock from the config (use the internal AO sample clock at setupInfo.scan.sampleRate, or select the timebase terminal that matches it) and derive the timer counter's rate ratio from it, or — if 100 kHz is a deliberate hardware constraint — delete sampleRate from the schema and docs and expose it as a read-only constant, so nothing can silently disagree with the hardware.


**APD and PMT hardwire their sample clock to 'ctr2InternalOutput' while nidaq.timerCounterChannel is a free config field the editor tells you to choose** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:69 and imswitch/imcontrol/model/managers/detectors/PMTManager.py:62, vs imswitch/imcontrol/model/SetupInfo.py:504-519` · *bites on a different rig*

the literal 'ctr2InternalOutput' (no device prefix, counter 2 fixed) in both point-detector managers, against `timerCounterChannel`, whose config-editor tip reads "Output counter channel for timing, e.g. Dev1/ctr2. Integer N is also accepted and translated to Dev1/ctr{N}". Also `deviceName` (default "Dev1") is configurable for the detector's own counter/AI channel but not for the clock terminal.

A point-scan rig whose ctr2 is already used (a very common collision — ctr2/ctr3 are the usual encoder or external-clock counters on X-series cards) and that therefore sets "timerCounterChannel": "Dev1/ctr0" or the documented integer form. NidaqManager then generates the 1 MHz pulse train on ctr0 while every APD/PMT counter/AI task is clocked from ctr2InternalOutput, which never ticks. Same on a two-card rig where the detector sets deviceName: "Dev2": the bare terminal resolves against Dev2's idle ctr2. The operator sees the scan start, hang for ~10 s, then die with a driver-level DaqError that names neither counter; nothing in ImSwitch says "your timer counter is not the one the detector listens to".

> The failure is binary rather than proportional: the CI/AI task acquires 0 of its _samples_total samples, and each read blocks for nidaqmx's default 10.0 s read timeout (verified in the installed nidaqmx: task.read(..., timeout=10.0)) before raising. Every scan on such a rig produces an empty image after a ~10 s stall.

*Verifier's correction:* The finding stands, but three parts of the diagnosis need correcting, and the victim is understated.

VICTIM IS BROADER — the field's own DEFAULT is a broken configuration. `NidaqInfo.timerCounterChannel` defaults to `None`, and `getTimerCounterChannel()` returns None, so NidaqManager.py:1240 skips creating the CO task entirely — no 1 MHz train exists anywhere — while APD/PMT still clock from `ctr2InternalOutput`. Six of the seven shipped setups have `"timerCounterChannel": null`; only example_sted sets ctr2. Worse, `docs/setupinfo-reference.rst:370` documents the example as "``0`` → ``"Dev1/ctr0"``" — the shipped Sphinx documentation's own example value is one that silences every point detector. That is a stronger and more concrete hazard than the config-editor tip (which at least says ctr2).

BETTER COLLISION STORY THAN THE CLAIM'S — the "ctr2/ctr3 are the usual encoder counters" ratio

*The right shape:* Derive the detector clock from the same config value the timer task uses — pass the timer counter's InternalOutput terminal down from setupInfo.nidaq (e.g. '<dev>/ctr<N>InternalOutput' built from getTimerCounterChannel), or, failing that, validate at startup that the configured timer counter is ctr2 on the detector's device and refuse with a clear message otherwise.


**The analog-input voltage range is accepted and then dropped: every PMT channel silently runs at nidaqmx's ±5 V default** — **fixed on this branch**
`imswitch/imcontrol/model/managers/NidaqManager.py:411-420 (min_val=-0.5/max_val=10.0 declared at 412, `add_ai_voltage_chan(channel)` called with no range at 420)` · *bites on a different rig*

min_val=-0.5, max_val=10.0 — a deliberate-looking PMT-shaped range that reaches no hardware, and the ±5.0 V that nidaqmx substitutes (verified: add_ai_voltage_chan defaults min_val=-5.0, max_val=5.0). PMTManager compounds it by passing None, None for those parameters, which only works because they are ignored.

Any PMT rig. If the preamp swings above 5 V (a 0-10 V preamp is ordinary), everything above 5.0 V is silently saturated by DAQmx — bright structures read as a flat ceiling with no error. If the PMT output is small (0-1 V, also ordinary), the channel wastes ~10x of the ADC range: on a 16-bit X-series card 153 µV/LSB at ±5 V versus 15.3 µV at ±0.5 V, i.e. a 10x quantization-noise penalty on a photon-starved signal. There is no config key to fix it — the manager exposes mockVoltageMin/mockVoltageMax (±5 V) which apply only in simulation, so the mock declares a range and the hardware does not.

> Full-scale/2^16: ±5 V -> 10 V/65536 = 153 µV per code; the (unused) declared -0.5..10 V range -> 160 µV; a properly matched ±0.5 V range -> 15.3 µV. So a PMT delivering 0-0.5 V loses a factor of 10 in effective resolution, and one delivering 0-10 V loses everything above 5 V.

*Verifier's correction:* The defect is real and reachable, but the diagnosis is half backwards and the reported severity is inflated. Three corrections:

1. THE QUANTIZATION HALF IS THE WRONG SIGN. NI cards expose a fixed list of *symmetric* AI ranges and DAQmx coerces a requested min/max up to the smallest supported range that contains it. -0.5..10 V is not a range any NI card has, so honouring the literal would select ±10 V -> 305.18 µV/LSB, exactly 2x WORSE than the 152.59 µV/LSB in force today. For the "0-1 V photon-starved PMT" the claim names as a victim, dropping the parameter is currently a 2x IMPROVEMENT, not a 10x loss. The 160 µV figure for the literal range is arithmetically right but hardware-unrealisable.

2. THE 10x IS NOT ATTRIBUTABLE TO THIS DEFECT, and is 5x on the stated card. ±0.5 V is the claim's own invention — the code never declares it and no config key offers it. Worse, NI X-series (63xx

*The right shape:* Pass the range through: `add_ai_voltage_chan(channel, min_val=min_val, max_val=max_val)`, and source min/max from the PMT's managerProperties (the manager already reads offset_v and mock ranges from there) rather than from a wrapper default. A None must then be rejected at the call site instead of silently working.


**TriggerScope firmware scans upload DAC start/length voltages that no one checks against the axis's declared minVolt/maxVolt** — **fixed on this branch**
`imswitch/imcontrol/controller/controllers/TriggerScopeRasterController.py:225-235, reached via imswitch/imcontrol/model/managers/ScanManagerTriggerScope.py:50-54` · *bites on a different rig*

the per-axis minVolt/maxVolt that are load-bearing on every other path — clamped with a warning in TriggerScopePositionerManager.setPosition, range-checked in TriggerScopeManager.setAnalog, checked per emitted waveform by Beta/Galvo checkSignalComp — and are consulted nowhere on the firmware scan path. (TriggerScopeManager.py:59-60 also substitutes a ±10 V default for devices that omit them.)

A TriggerScope rig operator who enters a scan ROI larger than the galvo's or piezo's safe travel. ScanManagerTriggerScope.makeFullScan raises NotImplementedError, so ScanManagerBase's checkSignalComp gate — the one that prints "Signal voltages outside scanner ranges: try scanning a smaller ROI" — is never reached; getTriggerscopeParameters just divides µm by conversionFactor and PARAMETER-uploads dimOneStartV/dimOneLenV, and the board ramps the DAC autonomously. The same operator jogging the same axis to the same position through the GUI gets clamped and warned. Red-zone: this is the path that physically drives the mirror.

> startV = startpos/conversionFactor and lenV = trimmedLength/conversionFactor, so the commanded excursion is (startpos + length)/conversionFactor volts with no upper bound. On an axis declared minVolt/maxVolt = ±5 with conversionFactor 10 µm/V, a 120 µm scan length asks the DAC for 12 V — 2.4x the declared limit, and 1.2x the board's ±10 V hardware range — with no refusal from ImSwitch.

*Verifier's correction:* Three corrections; the finding survives all of them.

1) HALF THE DIAGNOSIS IS WRONG: the START voltage is already bounded. `axis_startpos` is not operator input — `getParameters` (TriggerScopeRasterController.py:317) reads `positionersManager[name].position`, and `TriggerScopePositionerManager` only ever assigns `_position` from an already-clamped voltage (setPosition L80-95: `clampedVoltage = min(max(voltage, self._minVolt), self._maxVolt)` then `_position = clampedVoltage * conversionFactor`; `_restorePersistedPosition` L104-120 clamps identically). So startV is in [minVolt, maxVolt] by construction. The genuinely unchecked quantities are the SPAN (`dimNLenV`) and `dimNStepSizeV`, both straight from the unvalidated QLineEdit. Retitle: "firmware scans upload a DAC excursion LENGTH that no one checks against the axis's declared range".

2) THE TRIGGER IS FAR MORE ORDINARY THAN "an opera

*The right shape:* Give ScanManagerTriggerScope a real pre-flight check with the same contract as checkSignalComp: for each uploaded axis, verify startV and startV+lenV lie inside that device's [minVolt, maxVolt] before any PARAMETER is sent, and refuse the scan with the same message the NI path uses. Devices whose range is unknown should be rejected rather than defaulted to ±10 V.


### Things a new device must remember to declare

**SwabianTimeTagger declares isScanDriven but not rawFrameIsDeferred or drainChunk, so a FLIM recording stops on the first live-preview frame** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:962` · *bites on a different rig*

rawFrameIsDeferred = False (DetectorManager.py:538) and DetectorManager.drainChunk returning ChunkPayload(display=frames, raw=frames) (DetectorManager.py:561). Both defaults are correct for a camera, whose every frame is complete on arrival.

Any rig with a Swabian Time Tagger FLIM detector doing a scan-once/scan-lapse recording. The recording ends about 1 s after the scan starts and writes a lifetime image containing whatever photons had arrived by then, stamped recording:completion_outcome = complete. The scan keeps running; nothing in the log says anything. A snap taken mid-scan has the same problem: it returns the partial display buffer instead of raising RawFrameUnavailableError.

> SwabianFLIMWorker.LIVE_PREVIEW_S = 1.0 (SwabianTimeTaggerManager.py:1149): the worker emits a frame with is_final=False every 1 s, and _on_frame_ready sets _newFrameReady on every one of them. RecordingWorker._expectedFramesFor returns 1 for a scan-driven detector (RecordingManager.py:3627), so the loop stops the instant one frame arrives. A 256x256 FLIM scan at 1 ms dwell runs 65.5 s; the first frame arrives at t=1 s with 1/65 = 1.5% of pixels measured (measured 1.5% in the repro). At 10 ms dwell the scan is 655 s and the recorded frame holds 0.15% of the measurement. _stallReferenceTimeFor returns None while a scan-driven detector is scanning, so the stall watchdog cannot catch it either.

*Verifier's correction:* The finding stands; three details in the report need correcting, none of which reduce the severity.

1. WRONG MECHANISM FOR THE WATCHDOG. `_stallReferenceTimeFor` does NOT return None during a real scan. `markScanStarted` is called from the scan controller base (imswitch/imcontrol/controller/basecontrollers.py:1385) and records an EXPECTED completion time, so `scanCompletionTime(generation)` returns a future timestamp and `elapsed = now - referenceTime` is negative. Same conclusion (the watchdog cannot fire), different route. It returns None only when the scan info lacks `scan_samples_total`/`scan_time_step`.

2. THE SNAP HALF IS NARROWER THAN CLAIMED. `getLatestFrameShared(is_save=True)` only reaches the `rawFrameIsDeferred` guard when a chunk consumer is registered. A plain mid-scan snap with no recording/BeadRec/tiling consumer takes the other branch and calls `getLatestFrame(is_save=

*The right shape:* Declare the two facts the class already knows: rawFrameIsDeferred = True (the volume is whole only when the worker emits is_final), and a drainChunk override that returns display=getChunk() every tick but raw only once, at is_final - the shape APDManager.drainChunk (APDManager.py:806) and PMTManager already implement. Better still, derive the default from isScanDriven so a scan-driven detector has to opt OUT of deferral rather than opt in, and parametrise the existing point-detector test list over rawFrameIsDeferred/drainChunk the way it is already parametrised over isScanDriven.


### Recording and writer path

**DEFAULT_STALL_TIMEOUT is 10 wall-clock seconds applied to cameras, so any exposure longer than 10 s is killed at frame zero and its file deleted** — **fixed on this branch**
`imswitch/imcontrol/model/managers/RecordingManager.py:58 (constant), :3937 (check), :3641 _stallReferenceTimeFor` · *bites someone today*

DEFAULT_STALL_TIMEOUT = 10.0 s — 'watchdog triggers if no frames arrive within this period'. It is a proxy for 'this detector has died', measured against the wall clock from `_record()` entry, and it is sized for a camera that frames faster than once per 10 s. No production caller ever overrides it: grep for `stallTimeout=` across imswitch finds only test files, so 10.0 is what every real recording gets.

Anyone doing low-light fluorescence or luminescence in the default SpecFrames mode with an exposure over 10 s. Exposure is a free-form editable number in seconds (HamamatsuManager:49 'Set exposure time', valueUnits='s') and Orca-Fusion/Quest and Thorlabs TSI cameras go to minutes. The user sees the recording die before its first frame with `Detector 'Cam' stalled: no frames received for 10.0s ... Check camera triggering and numCamTTL configuration` — a message about scan TTL wiring, in a mode that has no scan — and the partial file is deleted by abortStream, so nothing survives to show what happened. The sibling branch already knows better: _stallReferenceTimeFor exempts scan-driven detectors using the scan duration that markScanStarted computes from `scan_samples_total * scan_time_step` (:2521). A camera co-recorded in the same ScanOnce falls through to `return lastFrameTime` and gets the flat 10 s, even though the same scanInfoDict gives its expected inter-TTL cadence.

> Abort condition is `elapsed > 10.0` with elapsed measured from the previous frame for that detector, or from `_record()` entry for the first. So the recording survives only if the camera's frame period < 10 s, i.e. frame rate > 0.1 Hz. A 30 s exposure (routine for luminescence) exceeds it by 3x and aborts at t=10 s having written 0 of N frames. Even a 12 s exposure fails. The margin the number was evidently sized for — a >=1 fps camera — is 100x away from a 0.033 fps one.

*Verifier's correction:* The finding stands; four details are wrong or imprecise. (a) 'default SpecFrames mode' is wrong: the recording widget defaults to UntilStop (RecordingWidget.py:264; persisted-state default 'UntilStop' at RecordingController.py:3243/3283), and UntilStop and SpecTime have no watchdog at all. The victims are SpecFrames, CameraLapse (each timepoint is a fresh 1-frame recording, RecordingController.py:783, so the clock restarts per point but every point must deliver its frame within 10 s of arming), and ScanOnce/ScanLapse with a co-recorded non-scan-driven camera. (b) 'killed at frame zero' is only the cold-start case; with the live view already running, the in-flight exposure lands one frame inside the window and the recording dies at the next gap with 'Current: 1 frames' -- and that file is deleted too (measured). For recFrames=1 (every CameraLapse point) a 12 s exposure succeeds per point 

*The right shape:* Derive the per-detector deadline from what the detector declares it will do: exposure (or 1/'Frame Rate', or 'Internal frame interval' — all already DetectorNumberParameters) times numCamTTL, plus a fixed grace; and in scan modes from the scan cadence markScanStarted already computes, the same source _stallReferenceTimeFor uses for the scan-driven branch. A flat seconds constant is only correct for detectors whose frame period nobody changed.


**WRITE_BATCH_FRAMES=32 is the only flush trigger, so a recording shorter than 32 frames has no dataset on disk at all until it finalizes** — **fixed on this branch**
`imswitch/imcontrol/model/managers/RecordingManager.py:84 (constant), :3132 (the sole flush condition)` · *bites someone today*

WRITE_BATCH_FRAMES = 32 frames — 'accumulate per detector before flushing to disk'. It is a proxy for a write size worth doing (bytes) and, since it is also the only thing that advances the `frames_committed` barrier, for how stale a live reader may be (seconds). One count cannot be both: 32 frames is 96 kB on a 76x20 ROI and 256 MiB on a 2048x2048 sensor, a factor of 2730.

The live-reconstruction / ImProcess live reader on any slow acquisition. `_flush_batch` is called only from `if self._batch_frame_counts[detectorName] >= WRITE_BATCH_FRAMES` and from `_flush_all_batches()` at the sentinel — there is no time-based flush. HDF5Storer.writeFrames creates the dataset, `frames_committed` and `stream_complete` lazily on its first call, so before the first flush the file contains no detector group whatsoever. A user watching a 20-frame timelapse live sees an empty file for its entire duration; at 1 fps the live view updates in 32-second steps.

> Barrier granularity = 32 / frame_rate. At the 9338 fps small-ROI rig that is 3.4 ms and 96 kB per write (too small to amortise the two h5py flushes each write performs). At 20 fps it is 1.6 s. At 1 fps it is 32 s. For any recording with recFrames < 32 it is the whole recording — nothing is committed until finalize. Measured write throughput at 76x20: 16,492 fps at batch=32 vs 17,351 fps at batch=256 on a local SSD, i.e. the 32 buys almost nothing on the fast end while costing the whole latency budget on the slow end.

*Verifier's correction:* The mechanism and arithmetic stand: the 32-frame count is the only flush trigger, so barrier granularity is 32/fps and a recording shorter than 32 frames has no dataset until finalize — demonstrated with the real reader. Three corrections. (a) Victim: 'a 20-frame timelapse' is wrong. Every timelapse timepoint (CameraLapse recFrames=1, ScanLapse per-scan) is a separate startRecording with its own writer and finalize, so the batch never spans timepoints, and single-file lapse stores are never streamed live anyway. The real victim is a single SpecFrames/SpecTime/UntilStop stream at a low frame rate. (b) Severity: this is silent latency, nothing fails and nothing is lost; downgrade from 'bites someone today'. The primary live consumer (MoNaLISA) must collect a whole first stack (nx_s x ny_s x linesteps frames, hundreds) before showing anything, so the batch adds at most 31 frames to that (<=

*The right shape:* Flush on whichever comes first: an accumulated BYTE target (the same TARGET_CHUNK_BYTES that should size the chunk, so a batch is one chunk on every detector) or an elapsed-time deadline of a few hundred ms. That makes the live barrier's staleness bounded in seconds regardless of frame rate, and the write size bounded in bytes regardless of frame size.


**HDF5/Zarr streaming chunk is 32 raw frames, so a point detector's chunk is 32 whole assembled volumes — over 128 MiB per frame the recording cannot be created at all** — **fixed on this branch**
`imswitch/imcontrol/model/managers/RecordingManager.py:972 (HDF5, chunks=(chunk_frames, *shape) at :980) and :594 (Zarr, chunks=(chunk_frames, *spatialShape) at :600)` · *bites someone today*

chunk_frames = min(WRITE_BATCH_FRAMES, 32) = 32 frames. A frame count, sized for a camera whose raw frame is one exposure (8 MiB at 2048x2048, so a 256 MiB chunk) and for a write batch of 32. The dataset's per-frame shape is taken from the data itself (`spatialShape = frames.shape[1:]`, HDF5Storer.writeFrames:1236), so for a scan-driven detector it is the whole assembled volume, not a plane.

Any point-scan rig (APDManager/PMTManager, ChunkKind.RAW) recording a Z-stack or a line-stepped scan. APDManager.drainChunk returns exactly ONE raw frame per scan — `np.expand_dims(np.array(self._image, copy=True), axis=0)`, shape (1, Nz, Ny, Nx) — so WRITE_BATCH_FRAMES=32 is a batch size this detector class can never reach, yet it sizes the chunk. Below 128 MiB per volume the user sees a recording that takes ~6x longer to finalize than it should and spikes ~1 GB of RAM at the finish line; at or above 128 MiB they get no recording at all, and the error names an HDF5 libver, not their scan settings.

> chunk bytes = 32 x frame bytes. HDF5 refuses a chunk > 4 GiB under the deliberately pinned HDF5_STREAM_LIBVER = ('v110','v110') (:94), so the hard ceiling on one raw frame is 4 GiB / 32 = 128 MiB. In scan terms: uint16 fails once Nx*Ny*Nz*S > 67,108,864 voxels (512x512x256, or 1024x1024x64); with _ttlmultiplying the APD buffer is float32 (APDManager.dtype:948) so it fails at half that — 512x512x128, or 512x512x32 with 4 line steps. Below the ceiling, measured on a 50x512x512 uint16 volume (26.2 MB): chunk = 838.9 MB; writing that ONE frame took 4.98 s and 924 MB peak RSS with chunks=(32,...) versus 0.80 s with chunks=(1,...) — 6.2x slower. Zarr is the same shape: 839 MB chunk, 949 MB peak RSS, 2.76 s.

*Verifier's correction:* Two precision corrections; neither changes the verdict or severity.

1) Boundary wording: the claim says "over 128 MiB per frame the recording cannot be created at all." My reproduction shows the failure begins AT exactly 128 MiB/frame (Nz=256, chunk exactly 4 GiB, which already exceeds HDF5's 2^32-1 byte chunk-size ceiling), not merely "over" it - the safe/unsafe boundary is "< 128 MiB survives, >= 128 MiB fails."

2) Backend scope: the claim bundles "HDF5/Zarr ... the recording cannot be created at all" as one failure mode for both backends. I confirmed the clean upfront ValueError is HDF5-specific: it comes from the deliberately pinned old-format libver bound (HDF5_STREAM_LIBVER = ('v110','v110')) refusing a >=4 GiB chunk. Zarr array creation succeeds without error at the identical nominal chunk size (tested 4.69 GiB) that makes HDF5 fail outright - Zarr has no equivalent upfront chun

*The right shape:* Derive the chunk's leading extent from a target chunk SIZE in bytes, not a frame count: chunk_frames = max(1, min(WRITE_BATCH_FRAMES, TARGET_CHUNK_BYTES // frame_nbytes)) with TARGET_CHUNK_BYTES on the order of 4-16 MiB (h5py's own rdcc default is 1 MiB, and Zarr/NGFF guidance is single-digit MiB). That gives a 2048x2048 camera 1-2 frames per chunk, a 76x20 ROI its full 32, and a point detector's volume exactly 1 — with no per-detector declaration and no 4 GiB cliff.


### Defaults standing in for declarations

**Camera pixel size silently defaults to 0.15 µm and is then written into every file as a measured calibration** — **fixed on this branch**
`imswitch/imcontrol/model/managers/detectors/DetectorManager.py:201 (default: float = 0.15), 225-243 (absent-key branch)` · *bites someone today*

0.15 µm sample-plane pixel size. Documented as "a typical high-mag value" — sized for a 60x/6.5 µm-pitch sCMOS (0.108 µm) or a 40x/6 µm (0.15 µm).

Every user of every setup file that does not spell `cameraPixelSizeUm` — which is all fifteen shipped example setups (`grep -c cameraPixelSizeUm` returns 0 for each of them, including example_sted, example_monalisa, example_snouty_smart_modes and all four mock scan setups). What they see: a recording whose OME `PhysicalSizeX`/`PhysicalSizeY` says 0.15 µm, a scale bar drawn at 0.15 µm/px, and a tiling mosaic whose tiles are placed at 0.15 µm/px. Nothing in the log and nothing in the file distinguishes "0.15 because the rig was measured" from "0.15 because nobody said". The near-miss warning at DetectorManager.py:233 only fires for a *misspelled* key; a plainly absent key returns the default with no log line at all.

> Hamamatsu ORCA (6.5 µm pitch) at 20x = 0.325 µm/px. Recorded as 0.15 µm/px: every distance measured off the file is 0.46x the truth; a 10 µm bead reads as 4.6 µm. For tiling, a 2048-px tile is 2048*0.325 = 665.6 µm of stage travel but the placer thinks it is 2048*0.15 = 307.2 µm, so consecutive tiles are laid down 2.17x too close and the mosaic overlaps itself by 54%. In the other direction, a Basler 3.45 µm at 40x = 0.086 µm/px recorded as 0.15 gives distances 1.74x too large.

*Verifier's correction:* The kernel survives: the absent-key branch of makeCameraPixelSizeParameter (DetectorManager.py:225-243) is the only one of its three branches that does not log, and the 0.15 um placeholder it returns is plausible enough to pass as calibration once it lands in OME PhysicalSizeX/Y, HDF5 element_size_um and the napari layer scale - and downstream code does consume that attr as calibration (improcess/reconstructors/snouty/metadata.py:108,204 reads 'Detector:<name>:Param:Camera pixel size'). Everything else in the claim is overstated. Not 'silent': it is documented (detectors.rst:74), tested as intended, and shown as an editable 'Camera pixel size' setting in the Settings widget of every camera; only the provenance is unrecorded. Not tiling: the stage step is the user's getTileStepUm(), never pixel-size-derived, so the '54% overlap / 2.17x too close' story is wrong - the real effect is a mosa

*The right shape:* Keep 0.15 as a runtime starting value but stop laundering it into the file as a declaration: `configuredCameraPixelSize()` already returns None for "undeclared", so carry that distinction into the recorded metadata (omit PhysicalSize, or stamp an explicit `calibration:pixel_size_source = default|config|user` annotation) and log once at startup that detector X is running on the default pixel size. Tiling should refuse, or loudly warn, on a detector whose pixel size is a default rather than silently mosaicking with it.


**Galvo phase delay is a rig calibration hardcoded twice, at different values, in two scan panels** — **fixed on this branch**
`imswitch/imcontrol/view/widgets/ScanWidgetPointScan.py:15 (`QLineEdit('100')`) vs imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py:33 (`QLineEdit("0")`); defaulted again at imswitch/imcontrol/model/managers/detectors/APDManager.py:1059 and imswitch/imcontrol/model/scan_parameters.py:120-122` · *bites on a different rig*

100 detection samples (Point Scan panel) versus 0 (Advanced panel), for the same physical quantity: the galvo mirror's response lag. Detection samples run at the hardcoded 1 MHz `_detection_samplerate`, so 100 samples = 100 µs.

Anyone driving the same galvo pair from both panels on the same microscope: the Point Scan panel discards the first 100 µs of every APD/PMT record and the Advanced panel discards none, so the two panels produce images of the same field offset from each other along the fast axis. On a fresh profile (no persisted widget state) neither number came from the rig — the lag is a property of the mirror and its driver, and nothing in the setup file can state it: ScanManagerBase.getScanSignalsDict overlays the widget's analog dict over `scanDesignerParams`, so a setup-file value would be overwritten. Two more places default it silently: scan_parameters.py:120-122 wraps `widget.getPhaseDelayPar()` in a bare `except Exception: = 0` (so a blank or comma-decimal entry becomes zero, not an error), and APDManager reads `scanInfoDict.get('phase_delay', 0)` while PMTManager reads `scanInfoDict["phase_delay"]` — the same fact, required on one detector and defaulted on the other.

> At the standard 1 MHz detection rate, 100 samples = 100 µs. Samples per pixel = dwell_time * 1e6, so at a 10 µs pixel dwell one pixel is 10 detection samples and a phase delay of 100 shifts the whole image by 100/10 = 10 pixels along the fast axis. On a 200x200 raster that is 5% of the field of view, and the Point Scan and Advanced panels differ from each other by exactly that 10-pixel offset on identical hardware. At a 100 µs dwell the same 100 samples is 1 pixel; at a 2 µs dwell it is 50 pixels — a quarter of a 200-pixel line.

*Verifier's correction:* The finding stands as arithmetic but the diagnosis and victim are wrong. Correct victim and mechanism: on the Advanced panel the '0' is not a fresh-profile default -- it is what every session runs with, because AdvancedScanParameterSerializer.apply() (imswitch/imcontrol/model/scan_parameters.py:310-452) never writes phase_delay (or d3step_delay) back to the widget while build_analog re-reads the widget on every getParameters(). An operator on any Advanced + GalvoScanDesigner + APD/PMT rig who calibrates the lag, has it auto-saved to imcontrol_widget_states/Scan/default.json (or a scan template via saveScanParamsToFile) and restarts, gets the field back at 0 and the persisted value silently overwritten on the next scan: the image is shifted by lag/dwell pixels (100 us lag at the 20 us default dwell = 5 px, 10 px at the 10 us minimum dwell) with nothing failing. The same restore hole exist

*The right shape:* Move the phase delay out of the widgets and into the setup file next to the galvo that has it (a `phaseDelayUs` on the scanning positioner or on `ScanInfo`), expressed in microseconds rather than in samples of an unrelated 1 MHz clock, with the widgets seeded from it. Make both detector managers read it the same way — required, like PMTManager already does — and drop the bare `except Exception: = 0` in scan_parameters.py in favour of catching only the missing-accessor case.


**Focus-lock reacquisition deadline is in seconds but the barrier it bounds advances in focus estimates, so a slow focus camera can never reacquire** — **fixed on this branch**
`imswitch/imcontrol/model/SetupInfo.py:251 (reacquireTimeoutS: float = 1.0) with :274 (reacquireSamples: int = 5)` · *bites on a different rig*

1.0 second deadline, guarding a window of 5 consecutive focus estimates that arrive at `updateFreq` Hz. Evidently sized for the one shipped rig: docs/setupinfo-reference.rst:547 says "at updateFreq: 10 a five-sample window needs ~0.5 s", and example_sted.json is the only setup that sets updateFreq, to 10.

Any rig whose focus estimate rate is at or below 5 Hz — set `updateFreq` to 5 or less, or run a focus camera whose exposure plus spot fit exceeds 200 ms (a dim IR lever spot on a USB camera is routinely 200-250 ms). The estimate rate is `min(updateFreq, camera frame rate, 1/fit time)`: ProcessDataThread.run (FocusLockController.py:1059-1088) waits `max(0.001, focusTime/1000 - processingTime)`, so a slow fit sets the rate on its own. What that operator sees: after every scan the lock says "Reacquiring", then a single log warning, then stays off — for every tile of a tiling run. TilingController._waitForFocus (TilingController.py:379-384) logs "continuing unlocked. Subsequent tiles may drift out of focus" and carries on, so a whole 3D tiling run is acquired out of focus and the data looks like a focus-drift problem, not a configuration one. No shipped setup declares reacquireTimeoutS, reacquireSamples or reacquireTolerancePx, so every rig runs on all three defaults.

> The barrier can only ever complete when `reacquireSamples / estimate_rate < reacquireTimeoutS`, i.e. estimate_rate > 5/1.0 = 5 Hz. At the shipped updateFreq of 10 Hz the margin is a single factor of two (0.5 s used of a 1.0 s budget) with no allowance for the settle itself; the docs quote a 0.15 s tile settle, so 0.5 + 0.15 = 0.65 s of the 1.0 s is already spent. At 4 Hz the window needs 1.25 s and the deadline fires at 1.0 s: the lock re-engages on 0% of scans, forever, regardless of how well the piezo actually settled. My simulation confirms it gives up on tick 5, before the window is ever full.

*Verifier's correction:* The finding stands as a real, reachable, correctly-computed bug with no other check that fails first or louder — verified against the live production code, not just re-derived by hand. Two narrative details need adjustment, both making the actual behavior slightly different from (not less severe than) as described: (1) The FocusLockController's own "Reacquiring" state, its give-up warning, and its red 'Lock lost after scan' GUI label fire only ONCE per continuous run — at the first tile after an actively-engaged lock is interrupted — not "for every tile" as stated; once that first reacquisition fails, the lock is already disengaged, so later tiles' scanUnlockFocus/scanLockFocus never re-enter _beginReacquire() (there is nothing actively held to suspend), and the widget reverts to a plain gray "Unlocked" the moment the next tile's scan starts, erasing even the brief red flag. What DOES re

*The right shape:* Derive the deadline from the thing it is actually waiting for rather than from a wall clock guess: `deadline = settle_allowance_s + reacquireSamples / updateFreq`, with `settle_allowance_s` the only configured number (that is the piezo property the operator can reason about). Failing that, refuse the combination at startup — if `reacquireSamples / updateFreq >= reacquireTimeoutS` the barrier is structurally impossible and should say so once at construction, not silently fail on every tile.


**getNumCamTTL counts rising edges per TTL cycle, but the point-scan designer's cycle spans many scan positions** — **fixed on this branch**
`imswitch/imcontrol/controller/basecontrollers.py:898-907, reading imswitch/imcontrol/model/signaldesigners/PointScanTTLCycleDesigner.py:357-365` · *bites on a different rig*

The per-detector integer `numCamTTL[detector]`, documented and consumed everywhere as "pulses per scan position". For the Beta and Advanced designers the stationary preview is one pixel's waveform, so edges-per-cycle really is edges-per-position (ScanControllerAdvanced even overrides getNumCamTTL onto `make_single_pixel_signal`, AdvancedScanTTLCycleDesigner.py:121-160, precisely to guarantee that). For PointScanTTLCycleDesigner the stationary preview is the decoded per-axis-step sequence, whose period is several positions.

A point-scan rig recording a camera in ScanOnce/ScanLapse mode with any TTL sequence other than the trivial 'h1'. example_sted.json is exactly this shape: scanWidgetType PointScan, PointScanTTLCycleDesigner, and a `WidefieldCamera` (TISManager) alongside the two APDs; SetupInfo.getTTLDevices (SetupInfo.py:712-727) makes any detector with a digitalLine a TTL device, so the camera gets a sequence and lands in numCamTTL. What the operator sees: the recording arms for several times the frames the camera will ever produce, sits waiting, and is eventually killed by the stall watchdog and written as `stopped_early` — while the embedded acquisition layout, built from the same number, states the inflated frame count with `provenance="recorded"` and confidence "certain". `validate_detector_edge_counts` (_acquisition_layout_source.py:59-79) is the check that would catch it, and it is wired only into ScanControllerAdvanced (ScanControllerAdvanced.py:615) — the point-scan path has no cross-check at all.

> A 200x200 point scan whose camera TTL sequence is 'h2,l2' on the X axis: getNumCamTTL returns 1, so `_expectedFramesFor` = recFrames * 1 = 40000 frames. The camera's line actually has one rising edge every 4 X steps, i.e. 50 per line and 10000 for the scan — the recording waits for 4x the frames that will ever arrive. With 'h1,l1' the factor is 2 (40000 expected, 20000 real); with 'h1,l2,h3,l1' the declaration is 2 per position, so 80000 are expected against 8/7 * 40000 ~= 45700 real, a factor of 1.75 the other way plus a wrong repeat loop in the layout.

*Verifier's correction:* The core claim stands -- getNumCamTTL genuinely returns 'edges in one untiled period', the point-scan path genuinely has no cross-check against the real tiled signal (unlike ScanControllerAdvanced), and this genuinely inflates the expected-frame count for a non-scan-driven detector fed a real period>1 TTL sequence, unguarded, with a real stall/'stopped_early' consequence -- but four supporting details in the write-up are wrong and should be fixed before this goes into a fix ticket:

1. example_sted.json is NOT the reachability evidence it's cited as: I read the actual shipped file and every detector in it (APDred, APDgreen, WidefieldCamera, FocusLockCamera) has "digitalLine": null, so SetupInfo.getTTLDevices() admits none of them -- WidefieldCamera never enters numCamTTL in that file as configured. The mechanism is still reachable (any rig where an operator sets a camera's digitalLine, w

*The right shape:* The number needs a stated denominator. Either give PointScanTTLCycleDesigner the single-pixel accessor the Advanced designer already has (`make_single_pixel_signal`) so `getNumCamTTL` really is per position for every designer, or change the contract to return `(edges, positions_per_period)` and let the recording gate and the layout builder do the division — a per-position count is not expressible for a sequence that fires on a subset of positions, and returning an integer forces a lie. In either case wire `validate_detector_edge_counts` into the point-scan path so the final TTL waveform arbitrates.


### ImProcess readers and limits

**getMeanData() reads the entire dataset on the GUI thread when a dataset is selected, with no size limit of any kind** — **fixed on this branch**
`imswitch/improcess/model/DataObj.py:352 (getMeanData), reached from imswitch/improcess/controller/DataFrameController.py:89 (showMean) via currentDataChanged at :125` · *bites someone today*

There is none — that is the finding. Every other display-only reduction in ImProcess is capped: contrast.sample_values bounds itself to _MAX_SAMPLE_VALUES = 2_000_000 samples specifically so "display-only operations stay cheap" (contrast.py:9-12). The mean-preview path, which runs on plain dataset selection rather than on request, reads 100% of the planes. Both of its branches do: the lazy branch walks every plane, and the mean_plane fallback (plane_navigation.py:146) does np.mean(np.asarray(array)) — a full materialization.

Anyone who clicks a large recording in the ImProcess data list. currentDataChanged is a comm-channel slot on the GUI thread, so the whole window is frozen with no progress indication for the duration. A 60,000-frame widefield timelapse is not exotic and is 503 GB.

> Measured 13 MB/s effective through the current plane-at-a-time path (finding 1 explains the 26x), 330 MB/s chunk-aligned. A 5,000-frame 2048x2048 uint16 recording is 41.9 GB: 3,302 s (55 min) frozen as shipped, still 127 s frozen even after finding 1 is fixed. A 2,000-frame 512x512 Zarr (1.05 GB) measured 58 s. The same preview from a 2,000,000-value strided sample would be under 0.5 s.

*Verifier's correction:* The finding stands, but its "who/when" is wrong and its severity is misattributed for the common case -- this needs a real rewrite, not just a footnote.

1. "Runs on plain dataset selection rather than on request" / "anyone who clicks a large recording in the ImProcess data list" is false. Selecting a row in the MultiData list never calls getMeanData -- it only fires the cheap updateInfo(). Reaching getMeanData always requires one of three explicit user actions: the "Set as current data" button (MultiDataFrameController.setAsCurrentData), the default "Open"/"Quick Load" action, or the distinctly-labeled "Open data as a lazy virtual stack" toolbar action (FileIOController.quickLoadVirtualData). All three are deliberate requests, not passive side effects of selection.

2. For the two default (non-virtual) actions -- almost certainly the common case -- MultiDataFrameController.setAsCurrentD

*The right shape:* Cap the preview the way sample_values already does: average a bounded subset of planes (stride the navigation axes to at most N planes, or reuse contrast.sample_values' own budget), and label it as a preview. If a true mean over every plane is wanted, it belongs off the GUI thread behind an explicit action, not on selection.


**A live source's poll() returns every unread frame in one call, so opening a finished recording materialises the whole thing** — **fixed on this branch**
`imswitch/improcess/live/sources.py:543 (ZarrLiveSource.poll), :1261 (Hdf5LiveSource.poll), :963 and :1704 (the lapse sources), consumed at imswitch/improcess/live/workers.py:117 and :183` · *bites someone today*

No bound at all. The loop is `while self._cursor < readable_length:` with `data = self._array[start:end]` appended to a list — every chunk is materialised before the first one is handed on. The only number in sight is _chunk_size = array.chunks[0] = 32 frames, which sizes each chunk but not how many are returned. This is the read-side mirror of the queue that commit 862786bd just rewrote to hold MAX_QUEUED_CONSUMER_BYTES instead of a frame count; the reader never got a budget of either kind. The lapse polls are worse: they walk forward across scan{N} groups in the same call, so a 100-timepoint lapse file comes back as 100 timepoints.

Anyone who switches live reconstruction on over a folder that already holds finished recordings — _scanForStores runs immediately at _startLive (LiveModeController.py:114) and queues every store it finds, and _is_store_ready (:295) admits a complete store first of all. The queue is serial, so the ordinary case of a recording finishing while the previous one is still reconstructing lands here too. The streaming path, whose entire point is bounded memory, then allocates the whole recording before processing a single frame.

> A 5,000-frame 2048x2048 uint16 recording is 41.9 GB requested in one poll() call; 60,000 frames is 503 GB. On any machine that cannot hold it the MemoryError kills the stream thread and silently wedges the live-mode queue for the rest of the session — every later recording is queued and never reconstructed, with nothing in the log after "Processing store: ...". Even when it does fit, a 41.9 GB allocation to feed a session designed to consume 32 frames at a time defeats the streaming path entirely.

*Verifier's correction:* The core claim stands and is reachable, but several specifics in the report are wrong or overstated:

1. Wrong citation: workers.py:183 is `chunks = self._pending_chunks` (draining the startup overflow buffer), not a poll() call. The actual second poll() call is at workers.py:191 inside `_poll_loop`, and -- unlike the one at :116-117 -- it IS wrapped in try/except (lines 190-205). The claim's own "try/except asymmetry" observation is correct, it's just cited to the wrong line.

2. The unguarded `_run_startup` path (workers.py:89-167, do_open=True) is reached ONLY for reconstructors with `supports_streaming=True`. I confirmed two real, actively-developed ones exist today: SmlmLocalizer (smlm/localizer.py:188) and MonalisaReconstructor (monalisa/reconstructor.py:30) -- so this is not dead code. But ordinary/batch reconstructors (`_start_batch_fallback_path`, workers.py:292-308) construct `

*The right shape:* Give poll() a byte budget, computed the way the detector queue now computes its own: stop appending once sum(chunk.data.nbytes) exceeds a fixed MiB budget, leave the cursor where it is, and return — the next poll continues. The frame count then follows from the frame, as it does on the write side. Separately, wrap the startup poll in the same try/except the steady loop already has, so a source error there fails the store instead of the session.


**The "is this array small enough to read whole" threshold is in elements, but the cost it gates is bytes — and 8.5x bytes at that** — **fixed on this branch**
`imswitch/improcess/model/contrast.py:11 (_SAMPLE_ELEMENT_THRESHOLD = 64 * 1024 * 1024), used at :31 in _should_sample and :80 in finite_values` · *bites on a different rig*

67,108,864 ELEMENTS. As bytes that is 64 MiB of uint8, 128 MiB of uint16, 256 MiB of float32, 512 MiB of float64 — the number is dtype-blind, and the operation it permits costs 8x the element count regardless of dtype, because _flatten_finite (:15) does astype(float64) then an isfinite mask then a boolean fancy-index, three full-size allocations. So the multiplier is 17x the array for uint8 and about 2x for float64: the threshold measures the one quantity that does not determine the cost.

Every processor result goes through finite_range() to compute display levels — filters, background, math_ops, scale_type, image_calculator, channel_merge, combine, projection, segmentation. A user applying a Gaussian filter to a 16-frame 2048x2048 uint16 stack (exactly the threshold, a perfectly ordinary Z-stack or short burst) pays a ~1 GiB transient and over a second of stall to compute two numbers for the contrast slider. On a 16 GB laptop already running napari and Qt that is a real allocation failure; on a workstation it is a mysterious pause. Integer camera data pays for the isfinite pass too, which cannot ever be False.

> At the threshold: 128 MiB array -> 1.06 GiB peak (8.5x) and 1.30 s. One element over: 17 MiB and 0.007 s. That is a 64x memory step and a 186x time step across a one-element change in size, and the discontinuity faces the wrong way — the larger array is the cheap one. For uint8 the worst case is 64 MiB of data costing 1.09 GiB.

*Verifier's correction:* Two calibration notes, neither changes the verdict. (1) Not every processor in the "who it bites" list hits the quoted 8.5x/17x multipliers: Filter and Subtract-Background upcast their input to float32 before running scipy/skimage filters, so their own call into finite_range sees roughly 4.25-5.25x overhead relative to their float32 bytes, not the full 8.5x quoted for native uint16. The 8.5x/17x figures are real and reachable, but most directly via the raw/native-dtype path -- auto-contrast or opening the histogram/contrast dialog on a freshly acquired or loaded 16-bit or 8-bit camera stack before any processing, which is arguably the single most common real trigger of this subsystem, not an edge case. (2) The claim's "about 2x for float64" only matches under a "total resident footprint including the base array" accounting convention (I measure ~2.125x that way); using the same "new allo

*The right shape:* Gate on the bytes the operation will allocate, not on the element count: sample when arr.size * 8 * 3 (the float64 copy, the mask, the compacted result) exceeds a fixed byte budget. Better still, drop the special case — sample_values already handles a plain ndarray correctly and costs 17 MiB, so _should_sample could simply always return True and the threshold disappears.


### Timeouts and waits

**focusLock updateFreq is documented in milliseconds and consumed as hertz; asking for a slow loop gives the fastest possible one** — **fixed on this branch**
`imswitch/imcontrol/controller/controllers/FocusLockController.py:139 and :178; declared at imswitch/imcontrol/model/SetupInfo.py:223-224; documented at docs/setupinfo-reference.rst:504` · *bites someone today*

updateFreq. SetupInfo.py:224 says 'Update frequency, in milliseconds' and docs/setupinfo-reference.rst:504 repeats 'Update frequency in milliseconds'. The code computes focusTime = 1000 / self.updateFreq  # focus signal update interval (ms) and then self.timer.start(int(self.focusTime)) -- i.e. it treats the field as Hz. Only the shipped example value (10) happens to be sane under both readings.

Any rig author who configures focusLock from the documentation. Writing updateFreq: 100 to mean 'every 100 ms' yields a 100 Hz loop: focusTime = 10 ms, the GUI timer fires 100x/s and the worker thread runs a gaussian filter + peak search + centroid on every tick. Writing updateFreq: 1000 to mean 'every second' yields int(1000/1000) = 1 ms, a 1000 Hz GUI timer. Anything above 1000 yields int(0.999) = 0, and QTimer.start(0) fires whenever the event queue drains -- the focus lock busy-loops the GUI thread and the whole application stops responding. Nothing validates the field, so the operator sees a frozen UI and no message tying it to focusLock. updateFreq: 0 raises ZeroDivisionError during controller construction.

> Documented reading vs actual: a requested 500 ms period (updateFreq: 500) becomes a 2 ms period -- 250x faster than asked. The inversion is total: larger configured values produce faster loops, and every value > 1000 collapses to a 0 ms timer. Secondary coupling: the reacquisition barrier needs reacquireSamples (default 5, SetupInfo.py:274) fresh estimates inside reacquireTimeoutS (default 1.0 s, SetupInfo.py:251), i.e. it requires updateFreq > 5 Hz just to fill its window; nothing checks reacquireTimeoutS * updateFreq > reacquireSamples, so on a rig with a slow focus camera the lock can never re-engage after a scan and every tile of a tiling run is acquired unlocked (TilingController.py:380 logs a warning and continues).

*Verifier's correction:* Stands, with one mechanism nuance and one severity-phrasing softening -- the underlying finding and its reachability/arithmetic are fully correct.

Mechanism nuance: "the worker thread runs a gaussian filter + peak search + centroid on every [GUI] tick" reads as if the GUI timer directly triggers the heavy math. In the current code that coupling is indirect: ProcessDataThread runs the filter/peak-search/centroid on its own thread, paced by the same shared `focusTime` (floored at 1 ms), and the GUI timer's update() only calls `takeResult()` and, when a result is ready, does widget work (setValue/setImage/setData). The two loops are coupled through the same mis-scaled value, not through the GUI thread executing the numeric work itself.

Severity phrasing: "the whole application stops responding" is stronger than what the code guarantees. What's demonstrable is sustained, uncapped GUI-threa

*The right shape:* Pick one unit and make the name say it: updateIntervalMs (consumed directly) or updateFreqHz (consumed as 1000/x), fix the docstring and docs/setupinfo-reference.rst to match, floor the timer at 1 ms, and reject updateFreq <= 0 at setup load. Validate reacquireTimeoutS against reacquireSamples/updateFreq at load rather than discovering it as a per-tile warning.


**Autofocus waits a hardcoded 150 ms for the Z stage and then reads whatever frame is newest, landing exactly one sweep step off** — **fixed on this branch**
`imswitch/imcontrol/controller/controllers/AutofocusController.py:11 (_SETTLE_S = 0.15), :274-276` · *bites on a different rig*

_SETTLE_S = 0.15 s -- a fixed post-move settle, followed immediately by detector.getLatestFrameShared() with no freshness boundary. There is no config path: AutofocusInfo (SetupInfo.py:280-300) has no settle or exposure field.

Any rig whose camera exposure + readout exceeds 150 ms, or whose Z drive is a stepper/DC focus drive rather than a piezo. getLatestFrameShared -> getLatestFrame -> (HamamatsuManager.py:97) self._camera.getLast() returns the newest COMPLETED frame, whose exposure began at t - exposure - readout. With exposure >= 0.15 s that frame belongs to the previous Z position, so every focus_vals[i] is really the metric at z_positions[i-1]. The routine then fits its parabola, logs 'Autofocus: optimal Z = ...', moves there and reports success. There is no warning, no status, nothing distinguishes the wrong plane from the right one. The neighbouring module solved exactly this: TilingController._grabSettledFrame (TilingController.py:2033-2084) opens a chunk-consumer boundary and requires two frames past it, and its settle time is configurable (TilingInfo.settleTimeMs, SetupInfo.py:333, overridable per run from the widget). Autofocus got the same 150 ms as a literal and none of the handshake.

> A uniform one-sample lag shifts the fitted parabola vertex by exactly one sweep step = resolutionz. At the @APIExport defaults autoFocus(rangez=100.0, resolutionz=10.0) that is a 10.00 um error, always in the sweep direction. On a 100x/1.4NA objective (depth of field ~0.5 um) that is 20 depths of field -- completely out of focus, reported as a successful autofocus.

*Verifier's correction:* The mechanism, reachability, and "silently wrong, no warning" consequence all stand, but the arithmetic's precision ("lands exactly one sweep step off", "a uniform one-sample lag shifts the vertex by exactly resolutionz", "10.00 um error, always in the sweep direction") is an idealization that does not generalize and overstates how clean the failure is.

The claim's model implicitly treats all 11 samples -- including the very first -- as uniformly shifted by one step, as if a fictitious point one step before the sweep's start had been visited. Physically that first sample instead reflects wherever the stage actually was immediately before the sweep began: current_z, the CENTER of the range (AutofocusController.py:261-267: z_positions = linspace(current_z-rangez/2, current_z+rangez/2, ...)), not an extrapolated point 60 um outside it. That single sample becomes an outlier against the othe

*The right shape:* Take the settle time from config the way tiling does (add settleTimeMs to AutofocusInfo, defaulting to TilingInfo's 150 ms) and take the frame through the same fresh-frame handshake TilingController already implements -- open a chunk-consumer boundary after the move and require two frames past it -- rather than sleeping a constant and trusting getLast().


**Point-scan detectors inherit nidaqmx's implicit 10 s read timeout, capping the fast-axis line period, and the failure is reported as a camera-trigger problem** — **fixed on this branch**
`imswitch/imcontrol/model/managers/NidaqManager.py:461-470 (readInputTask); callers APDManager.py:1162,1186 and PMTManager.py:998,1008` · *bites on a different rig*

None -- and that is the defect. readInputTask(taskName, samples, timeout=False) falls into `return self.tasks[taskName].read(samples)`, and nidaqmx.Task.read defaults to timeout=10.0 seconds (verified against the installed nidaqmx 1.5.0). No APD or PMT caller ever passes the timeout argument the method already exposes.

Point-scanning confocal/STED/FLIM operators using long pixel dwell times. Each readdata() call blocks for one whole fast-axis period, so a scan whose line period exceeds 10 s raises DaqError on every line. APDWorker.run (APDManager.py:1245-1268) catches it, logs, calls _markRawFailed and emits acqDoneSignal, so the assembled volume is never published. The operator's actual message comes from the recording stall watchdog 10 s later (RecordingManager.py:3945-3961): "Detector 'APD' stalled: no assembled frame received ... Check the detector input, scan trigger, and sample-clock configuration." They go hunting for a wiring fault in a scan that is running perfectly; the cause is a driver default nobody chose.

> Line period > 10 s breaks every read. The dwell time that reaches the cap: 256 px/line -> 39.06 ms/px; 512 px/line -> 19.53 ms/px; 1024 px/line -> 9.77 ms/px; 2048 px/line -> 4.88 ms/px. Photon-starved STED/FLIM routinely uses 1-10 ms/px, so a 1024- or 2048-pixel line is already at or over the cliff.

*Verifier's correction:* The core defect stands and is reachable — NidaqManager.readInputTask really does drop straight through to nidaqmx's built-in 10.0 s read timeout, no APD/PMT caller overrides it, and a fast-axis line whose active dwell time exceeds ~10 s (achievable with ordinary, unbounded-by-any-validator dwell/pixel-count combinations, e.g. 1024 px @ 15 ms/px) will raise a DaqError that silently kills that scan generation's assembled volume via `_markRawFailed`, while the physical galvo/stage waveform keeps running to completion regardless (confirmed: `acqDoneSignal` only triggers local cleanup; the AO/DO output task's own `wait_until_done` is what drives `sigScanDone`).

Two parts of the claim's diagnosis are wrong and need correcting:

1. Timing is off by 1-3 orders of magnitude. The claim says the operator's message comes "10 s later" from the stall watchdog. In fact, for scan-driven detectors (APD/

*The right shape:* Pass an explicit timeout derived from what is being read: timeout = samples / detection_samplerate * k + margin (k ~= 2). readInputTask already has the parameter; the sample count and the rate are both in hand at the call site.


**The scan-start arm wait (5 s) is shorter than the writer-open budget (30 s) it contains, so the inner timeout is unreachable and a slow file open aborts the scan blaming the detectors** — **fixed on this branch**
`imswitch/imcontrol/model/managers/RecordingManager.py:60 (RECORDING_ARM_TIMEOUT = 5.0) vs :85 (WRITER_OPEN_TIMEOUT_S = 30.0); consumed at controller/controllers/RecordingController.py:1166-1181` · *bites on a different rig*

RECORDING_ARM_TIMEOUT = 5.0 s, commented 'max wait for detectors to arm before starting a scan'. But the event it waits on is set only after the writer has opened the file: _signalAcquisitionStarted() is at RecordingManager.py:3843, downstream of writerThread.wait_for_open() at :3776, which itself budgets WRITER_OPEN_TIMEOUT_S = 30.0 s.

Anyone recording to a network share, a slow disk, or a Zarr/HDF5 destination that takes more than a few seconds to create -- and anyone whose save folder already holds many recordings, because getSaveFilePath (:2817-2832) resolves the name by stat-ing _1, _2, _3 ... one at a time. All three scan-driven start paths gate on this (RecordingController.py:444, :1016, :1308). On expiry the operator gets 'Detectors did not report armed within 5.0s; scan was not started.' and the recording is aborted -- while the step that actually overran was file creation, and the writer's own 30 s budget for that step can never be reached because the outer wait kills it at 5 s first.

> Inner budget / outer budget = 30 / 5 = 6x. The 5 s must cover hardware startAcquisition on every recorded detector + filename de-duplication (one os.path.exists per existing file) + OME metadata construction + thread start + openStream; the 30 s covers only the last of those. Any open slower than 5 s is guaranteed to be reported as a detector-arming failure, and WRITER_OPEN_TIMEOUT_S is dead code on this path.

*Verifier's correction:* Line numbers only: the constant/call-site lines the claim cites (RecordingManager.py :60, :85, :3776, :3843; getSaveFilePath :2817-2832) are stale by the width of comments 5 unrelated commits already added on this branch since the audit doc was written -- current lines are :70, :111, :3898, :3965, and :2856-2871 respectively. Same values, same mechanism, no content is wrong.

One substantive correction that makes the finding WORSE, not weaker: the write-up implies a clean failure at t=5s ('on expiry the operator gets [message]'). In fact the abort path (_handleRecordingFailure -> abortRecording(wait=True) -> QThread.wait(..., 30000)) runs on the same thread that ran the 5s wait, and cannot return until the writer thread's in-flight, unpreemptible openStream() call itself completes. So for an open that takes, say, 12s, the caller (the GUI thread, per @APIExport(runOnUIThread=True) and the

*The right shape:* One budget, expressed once: either have the controller wait for (arm budget + WRITER_OPEN_TIMEOUT_S), or have the worker signal 'armed' when the detectors are armed and report writer-open separately so the two failures are distinguishable. Whichever is chosen, the outer wait must be strictly larger than every inner wait it contains.


### Assumptions about scale

**The write-batch frame count is also the on-disk chunk depth, so every camera bigger than a crop gets an uncacheable chunk and a ~30x read amplification** — **fixed on this branch**
`imswitch/imcontrol/model/managers/RecordingManager.py:84 (constant), :972 (HDF5 streaming dataset), :594 (Zarr streaming array)` · *bites someone today*

WRITE_BATCH_FRAMES = 32 frames. Used twice, for two different jobs: how many frames to accumulate before flushing, and -- via `chunk_frames = min(WRITE_BATCH_FRAMES, 32)` -- the depth of the HDF5/Zarr chunk, `chunks=(32, *spatialShape)`. The comment says "Match batch size for efficiency", i.e. it was sized as a write-batching convenience, on a detector whose frames are small.

Anyone who records with a camera whose frame is bigger than a small crop and then opens the file in ImProcess (or any per-plane reader) and moves the frame slider, or lets the Data panel compute its mean image. A 32-frame chunk is 256 MiB on a 2048x2048 uint16 sensor and 16 MiB on 512x512; h5py's default chunk cache is 8 MiB on HDF5 2.0 and 1 MiB on the widely deployed 1.14, and HDF5 does not cache a chunk larger than the cache. So each of the 32 frames sharing a chunk re-reads and re-inflates the whole chunk. Measured on realistic (compressible, ~7:1) synthetic microscopy frames: 2048x2048 -> 973.3 ms to read one frame vs 30.0 ms with a 1-frame chunk; 512x512 -> 58.8 ms vs 1.9 ms. Zarr is the same shape (no chunk cache at all): 512x512 -> 23.8 ms/frame vs 1.21 ms. A bulk `[:]` read is identical either way (2.04 s vs 1.96 s), so nothing in a whole-file load exposes it -- only the per-plane access pattern ImProcess actually uses.

> chunk bytes = 32 x Y x X x 2. 2048x2048 -> 268,435,456 B = 256 MiB; 512x512 -> 16 MiB; 76x20 crop -> 97 kB. The chunk stops being cacheable once 32 x frame_bytes > rdcc_nbytes, i.e. frame > 256 KiB (512x256 uint16) on an 8 MiB cache and frame > 32 KiB (128x128 uint16) on a 1 MiB cache -- essentially every camera. Amplification is then the chunk depth itself: measured 973.3/30.0 = 32.4x (2048^2 HDF5), 58.8/1.9 = 31x (512^2 HDF5), 23.8/1.21 = 20x (512^2 Zarr). Scrubbing a 1000-frame 2048^2 recording frame by frame: 973 s (16 min) instead of 30 s.

*Verifier's correction:* Stands, with one correction to the "WHO." The claim's phrasing ("opens the file in ImProcess ... and moves the frame slider") implies ordinary/default file-opening is affected. It is not: ImProcess's default Load-data action (quickLoadData / drag-drop) eagerly materializes the whole array into a plain numpy array before the Data panel ever sees it (DataObj.py:99-109 `data` property -> `source.array.asarray()`; FileIOController.py:267-289 `_loadAsCurrent` defaults `virtual=False`), so extract_plane/getMeanData then just index RAM and the chunk depth is irrelevant -- exactly consistent with the claim's own bulk-read benchmark showing near parity. The real, reachable trigger is the separate "Virtual load data..." menu action (FileIOController.quickLoadVirtualData; ImProcessMainView.py:157-167, tooltip "Open data as a lazy virtual stack") -- an intentional feature for recordings too large to

*The right shape:* Derive the chunk depth from a byte budget, the same way the chunk queue and writer queue were just re-derived: `chunk_frames = max(1, TARGET_CHUNK_BYTES // frame_bytes)` with TARGET_CHUNK_BYTES around 1-8 MiB (h5py documents 10 KiB-1 MiB; matching the file's own rdcc_nbytes is the principled choice, and rdcc_nbytes can be raised at h5py.File open time if a deeper chunk is wanted). frame_bytes is already available at the creation site as `np.dtype(declared).itemsize * prod(spatialShape)`. Keep WRITE_BATCH_FRAMES for what it actually names -- how much to accumulate before flushing -- and stop making a detector's on-disk layout a function of it. Nothing then needs to declare anything: a crop camera keeps deep chunks, a full-sensor camera gets shallow ones, and both cost the same memory.


## Unverified leads

Found but never challenged by a verifier. Ordered by the severity their finder assigned, which nobody checked -- except where a fix settled the question.

- **The writer's finalize deadline is a hardcoded 30 s while its queue budget is 512 MiB — together they assert an undeclared minimum disk throughput, and blowing it deletes a completed recording** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:3306 (self.join(timeout=30.0)), against :72 (WRITER_QUEUE_MAX_BYTES = 512 MiB) and :61 (RECORDING_THREAD_STOP_TIMEOUT_MS = 30000)` · *bites on a different rig* · Anyone recording to storage slower than the implied floor — a NAS/SMB share, an external spinning disk, a busy shared drive — or with two or more detectors. On timeout, finish() raises TimeoutError, the worker's `finally` catches it at :4017 and calls writerThread.abort(), and abortStream DELETES the file. The recording completed, the frames were captured, and the user loses all of it at the finis
- **The recording stall watchdog is a fixed 10 s with no config path, so a long-exposure camera is aborted as a stalled one** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:58 (DEFAULT_STALL_TIMEOUT = 10.0), applied at :2263, enforced at :3937-3965` · *bites on a different rig* · Anyone whose camera frame interval exceeds 10 s: long-exposure fluorescence, chemiluminescence, or a hardware-triggered camera waiting on a slow external event. In SpecFrames / ScanOnce / ScanLapse / CameraLapse the watchdog raises RuntimeError, emits sigRecordingStalled and routes through abort/partial-output cleanup -- the recording is destroyed, not merely warned about. The message they get is 
- **The writer's 30 s stop join and the 512 MB queue budget are unrelated numbers; below ~18 MB/s the escalation deletes a completed recording** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:3306 (self.join(timeout=30.0)) against :72 (WRITER_QUEUE_MAX_BYTES = 512 * 1024 * 1024)` · *bites on a different rig* · Anyone whose sustained write path is slower than ~18 MB/s: gzip-compressed HDF5 (the default -- HDF5Storer.__init__ takes compression='gzip' at :896) on an older workstation, a network share, or a Zarr codec that is slow on that CPU. The escalation is deliberate and documented in the code (:3086-3090), which makes the consequence worse: finish() raises TimeoutError, the caller at :4016-4030 catche
- **recorded_frames_per_time_point still walks every recorded frame when spans are present -- the branch above it was already fixed for exactly this** — **fixed on this branch**  
  `imswitch/imcommon/model/acquisition_layout.py:1640-1643` · *bites on a different rig* · A rig recording gated Advanced / line-step scans (pLS-RESOLFT, MoNaLISA, any detector whose mask drops some conditions) with a time loop, then running live reconstruction on it. recorded_event_spans is non-None exactly when the detector is gated, which is exactly the configuration this branch's span machinery exists for. The call is on LiveSource.open() (sources.py:520 and :1245, via _frames_per_s
- **The chunk-consumer overflow trim uses list.pop(0) on a queue whose length is now tens of thousands of frames** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/detectors/DetectorManager.py:783` · *bites on a different rig* · The 9338 fps 76x20 crop camera that motivated the byte budget, when some consumer is registered but has stopped polling (a BeadRec or tiling consumer whose thread died, or one that was never released). The queue sits at the 84,733-frame cap and every subsequent drain pops as many frames off the front as it pushes on the back, each pop memmoving the whole remaining pointer array -- all of it inside
- **The completion-outcome HDF5 attribute is fixed at S13 — exactly the length of the longer of the two current values, so a third outcome truncates silently and 'stopped_early_on_stall' becomes a valid-but-wrong 'stopped_early'** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:1032 (dtype="S13"), against imswitch/imcommon/model/acquisition_metadata.py:14 (VALID_COMPLETION_OUTCOMES)` · *bites when extended* · The next person to add a completion outcome — which the codebase is already reaching for, since FailureKind (:1707) distinguishes failure classes the outcome field cannot express. Their HDF5 recordings carry a truncated marker with no exception and no warning, on HDF5 only (Zarr :826 and TIFF write plain strings, so the same recording reports different outcomes in different formats). Worst case is
- **Basler and Jetson getChunk return a 4-D chunk, breaking the (numFrames, H, W) contract the broker fans out on** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/detectors/BaslerManager.py:100` · *bites when extended* · Anyone configuring a Basler or Jetson camera. Their interface's getLastChunk() already returns (1, H, W), so the extra expand_dims makes it (1, 1, H, W) and each 'frame' handed to a consumer is (1, H, W) instead of (H, W). The ZarrStorer takes spatialShape from frames.shape[1:] (RecordingManager.py:753), so it silently creates a 4-D dataset with a degenerate axis, mislabelled against the OME axis 
- **NidaqManager.readInputTask's `timeout=False` branch reads as 'no timeout' but delivers nidaqmx's 10 s default, capping the longest scan line** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/NidaqManager.py:461-470` · *bites when extended* · A point-scan user acquiring a slow, wide line — photon-starved APD/STED imaging, where multi-millisecond dwells are the point. APDManager.run_loop_d2 issues one read per fast-axis line period (readdata(self._samples_d2_period)), so the whole line must arrive within 10 s. Beyond that, every line read raises DaqError -200474 mid-scan and the acquisition dies with a driver message about a timeout rat
- **The acquisition-layout preflight is opt-in per reconstructor, and eight of nine never opt in — so the uncalibrated-loop refusal is dead code** — **fixed on this branch**  
  `imswitch/improcess/reconstructors/base.py:196 (`acquisition_requirements: AcquisitionRequirements | None = None`) and :30 (`requires_calibrated_loops: frozenset[str] = frozenset()`); the guard it disables is at :121-133` · *bites when extended* · Anyone whose recording carries a layout with an uncalibrated loop (`step=None`, `unit=None`) — which the producer side can legitimately emit: `_acquisition_layout_source._physical_loops` (lines 224-229) sets `step=None` whenever `pixel_sizes` is short or unparseable, and `build_triggerscope_resolft_layouts`'s `step()` helper (line 668-674) returns None for a missing `cycleStepSizeUm`/`roStepSizeUm
- **Processor.id defaults to "unnamed", which silently defeats both the plugin loader's missing-id guard and the footprint's class-name fallback** — **fixed on this branch**  
  `imswitch/improcess/processors/base.py:96` · *bites when extended* · Anyone writing an ImProcess drop-in analysis plugin - the documented user extension point - who sets `name` but not `id`. Their tool loads and runs, but every result it produces carries a processing footprint that says the step was `unnamed`. If they write a second plugin that also omits `id`, that one silently does not appear in the tool list at all; the only trace is a log warning naming a dupli
- **The live stall watchdog's 300 s default is switched off by a class flag each new source author must remember to set** — **fixed on this branch**  
  `imswitch/improcess/live/sources.py:300 (LiveSource.idles_between_stacks = False), overridden at :753, :862, :1497, :1597; consumed at imswitch/improcess/controller/LiveReconstructionController.py:105-117; default from imswitch/improcess/model/processing_config.py:149` · *bites when extended* · Whoever adds the seventh live source — a new format, a plugin reconstructor's own reader, a remote store — and does not know the flag exists. If their source idles longer than five minutes between logical stacks (a slow timelapse, a scan waiting on a trigger or a focus-lock settle), the watchdog declares the writer crashed and finalises with partial data. The result is a silently short reconstruct
- **Pulse-generator stop() joins its worker with a timeout and never checks whether it stopped** — **fixed on this branch**  
  `imswitch/imcontrol/model/managers/pulsegen/PulseStreamerManager.py:293-294; same shape at pulsegen/TeensyPulseManager.py:277-278 and interfaces/teensypulse.py:614-615` · *bites when extended* · A rig whose Pulse Streamer is reached over Ethernet, where the worker can be blocked inside pulseStreamer.stream() or isStreaming() for longer than 1 s (a long sequence upload, a congested link). stop() returns claiming success while the worker's finally clause has not run, so __running stays True. The operator's next action then fails with 'A sequence is already running' (run(), :248) or 'Cannot 

## What to do with this

Everything here is fixed and tested on this branch; the companion
[magic-number-audit-fixes.md](magic-number-audit-fixes.md) says how, finding by
finding, and lists the behaviour changes that need a look on a rig. Each
verified entry above still carries the verifier's corrections and the shape the
fix was meant to take, so the reasoning can be checked against what was done.
