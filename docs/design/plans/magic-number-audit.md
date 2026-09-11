# Magic numbers and shortcuts: an audit

**Status:** Reference — audit of `codex/acquisition-layout-schema`, 2026-09-10.
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
listing constants, and every serious finding was handed to a second agent told
to refute it.

## How much of this is verified

Seventeen findings survived adversarial verification. Three were refuted and are
not listed. The remaining thirty-six were **never challenged**: the verification
stage hit the account's session limit partway through, so five of the eight
areas lost their verifiers. Several of those carry a serious severity from their
finder and no second opinion at all — they are marked below and should be
treated as leads rather than as conclusions.

Nothing here is a regression introduced by this branch except the three noted as
already fixed, which were consequences of the byte budgets this branch added.

## Verified

Each of these was reproduced by one agent and survived a second agent's attempt to refute it.

### Detector managers

**FLIM TCSPC window defaults to 2.048 ns against the 80 MHz laser period the same constructor declares**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:88` · *bites someone today*

n_bins default 64 (line 88) x binwidth_ps default 32 ps (line 89) = a 2.048 ns TCSPC histogram window. Five lines further down, laser_rep_rate_mhz defaults to 80.0 MHz (line 93) = a 12.5 ns excitation period. The window covers 16% of the period it is meant to sample.

Anyone running the Swabian TimeTagger FLIM detector on the shipped defaults, which docs/devices/detectors.rst:709-715 reproduces verbatim as the copy-paste example config. Every fluorophore with a lifetime above ~2 ns reports 0.84-0.95 ns, and the FLIM image loses essentially all lifetime contrast: a 2 ns and a 4 ns species differ by 0.08 ns in the reported map where the truth is a factor of two. There is no warning, the intensity image looks correct, min_counts_per_pixel and the 5x outlier clamp behave normally, and the numbers are physically plausible -- so it reads as a real (short) lifetime, not as a truncated histogram.

> Reported vs true tau, moment fit, n_bins=64 binwidth=32ps (window 2.048 ns): 0.5->0.45 (-10%), 1.0->0.68 (-32%), 2.0->0.84 (-58%), 2.5->0.87 (-65%), 3.0->0.89 (-70%), 4.0->0.92 (-77%), 6.0->0.95 ns (-84%). Closed form: measured = tau - T/(exp(T/tau)-1), which saturates at ~T/2 as tau grows, so the whole 2-6 ns range collapses into 0.84-0.95 ns. With n_bins=390 at the same 32 ps (window 12.48 ns ~ one 80 MHz period) the same code returns 0.98/1.96/2.40/2.79/3.41 ns -- errors of 2-15% instead of 32-84%. Secondary: only 1-exp(-2.048/2.5) = 56% of a 2.5 ns emitter's photons land inside the window at all, so the intensity image and the min_counts_per_pixel=20 validity gate are both short by ~44%.

*The right shape:* Do not default the window independently of the rate. Derive it: n_bins = ceil((1/laser_rep_rate) / binwidth_ps) for the declared rep rate, or at minimum refuse to start (or warn loudly, once) when n_bins*binwidth_ps < 0.8 * (1/laser_rep_rate_mhz). The two numbers are one physical fact expressed twice; only one of them should be a free default.


**SwabianTimeTagger is scan-driven but does not declare rawFrameIsDeferred, so a FLIM recording saves the one-second live-preview snapshot as the finished measurement**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:962` · *bites someone today*

Two per-detector declarations that default to the camera answer and that a scan-driven detector must remember to override: rawFrameIsDeferred (DetectorManager.py:538, default False) and drainChunk (DetectorManager.py:561, default raw = display). Swabian overrides isScanDriven, pixelSizeUm, scale, dtype, finishScan -- and neither of these two. Combined with FlimWorker.LIVE_PREVIEW_S = 1.0 s (line 1149), the raw stream becomes 'whatever the scan had reached one second in'.

Anyone doing a ScanOnce or ScanLapse recording of a FLIM scan. RecordingManager.py:3831 registers the recording as ChunkKind.RAW; RecordingManager.py:3625-3627 plans exactly ONE frame for a scan-driven detector. The FLIM worker publishes a non-final preview frame every LIVE_PREVIEW_S and sets _newFrameReady unconditionally (line 783), so the recording's plan is satisfied by the first preview tick, should_stop() fires, and the file is finalised as a clean, complete scan. The saved lifetime image contains only the lines acquired in the first second; the rest is zeros. Nothing downstream can tell -- no warning, no stall, no short-frame message. APDManager (:760, :806) and PMTManager (:819, :860) both implement the deferral correctly, so this is the one scan-driven detector where the contract silently reverts to the camera behaviour it was written to replace.

> A 256x256 FLIM scan at 1 ms/pixel takes 65.5 s. The first preview tick lands at t = LIVE_PREVIEW_S = 1.0 s, i.e. ~1000 of 65536 pixels = 1.5% of the measurement. The recording's plan for a scan-driven detector is 1 frame, so it stops there: 98.5% of the acquisition is discarded and the remaining frames are counted as 'delivered beyond the plan'. The error scales with scan length -- every FLIM scan longer than one second is affected, and the longer (i.e. better) the scan, the smaller the fraction that gets saved.

*The right shape:* Give SwabianTimeTaggerManager the same latch APD and PMT have: rawFrameIsDeferred -> True, and a drainChunk that publishes the volume exactly once, when _on_frame_ready arrives with is_final=True (and never for a generation that aborted). Better still, make the base class refuse the default: a manager whose isScanDriven is True should not be allowed to inherit rawFrameIsDeferred=False silently -- assert it, or derive the raw contract from isScanDriven rather than from a second flag that must agree with it.


**APDManager clips real photon counts to mockPhotonCountMax on the live NI-DAQ path**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:1212` · *bites someone today*

_mock_photon_count_max, default 5000 counts (APDManager.py:82-92, config key mockPhotonCountMax). It is a synthetic-data bound -- docs/mock-infrastructure.rst:78 lists it alongside mockPhotonCountMean/mockRandomSeed as parameters of the mock generator -- and randomInput() at :1388 is its intended consumer. samples_to_pixels() applies it to every pixel, gated only on _ttlmultiplying, never on _simulation_mode.

Any real APD confocal/STED acquisition with a per-pixel count above 5000. The clip is a flat top at exactly 5000 on bright features, which is indistinguishable from APD/counter saturation, so the user blames the detector or the sample. PMTManager, written by the same hand, keeps its mock bounds strictly inside randomInput() (:1150-1162) -- the APD is the one that leaks. Nobody will find it by reading the config either: the only way to raise the ceiling is to set a key called 'mockPhotonCountMax' on a real rig.

> The clip threshold in count-rate terms is 5000/dwell: dwell 100 us -> 50 Mcps (above APD saturation, safe); dwell 500 us -> 10 Mcps; dwell 1 ms -> 5 Mcps; dwell 10 ms -> 0.5 Mcps. Measured: at dwell 10 ms and 1 Mcps the true 10000 counts/pixel is reported as 5000 -- a factor of 2 low, silently. A bright bead at 5 Mcps with a 1 ms dwell sits exactly on the threshold. Note also that the ONLY clip warning in the manager (_warned_mock_count_clip, :645-651) guards the uint16 dtype range 0..65535, which this 5000 clip makes permanently unreachable -- so the clip that fires is the one that is never reported, and the reporting machinery guards a clip that can no longer happen.

*The right shape:* Gate the clip on self._manager._simulation_mode, or move it into randomInput() where the other mock bounds live. If a real-data ceiling is genuinely wanted it should be a separate, differently named property expressed in counts-per-second (a detector property) rather than counts-per-pixel (which silently depends on the dwell time).


**A camera with no cameraPixelSizeUm silently gets 0.15 um/px, and no shipped setup declares it**
`imswitch/imcontrol/model/managers/detectors/DetectorManager.py:201` · *bites someone today*

default: float = 0.15 um -- 'a typical high-mag value' per the docstring. It is the sample-plane pixel size used by tiling, stitching, scale bars, napari layer scale and the saved OME PhysicalSizeX/Y.

Every camera detector whose setup file omits cameraPixelSizeUm -- which is all 15 shipped setups. The function goes to real trouble to catch a MISSPELLED key (DetectorManager.py:228-239 warns about near-misses) and to catch an unparseable value (:246-252 warns about decimal commas), but the plain-absent case at :240-243 returns 0.15 with no log line at all. Because configuredCameraPixelSize then returns None, the parameter is also left out of configOwnedParameters (:325-330), so the fallback becomes ordinary persisted widget state rather than a calibration the config owns. The user sees a scale bar and an OME PhysicalSize that look like real calibration and cannot be told from one. Separately, a camera manager that forgets makeCameraPixelSizeParameter entirely gets a different silent default: pixelSizeUm returns [1.0, 1.0, 1.0] (:485-489), i.e. 1 um/px.

> A common real configuration -- 6.5 um sensor pitch at 20x -- is 0.325 um/px. Against the silent 0.15 that is a 2.17x scale error in every stitched mosaic, every measured distance and every saved PhysicalSize. At 60x on the same sensor the true value is 0.108 um/px and the default is 1.39x too large in the other direction, so the sign of the error is not even consistent.

*The right shape:* Treat 'absent' the same way the function already treats 'misspelled': warn once, naming the detector and the value being assumed. Better, make the fallback self-identifying -- record in the parameter (and hence in the saved metadata) that the pixel size is an assumed default rather than a declared calibration, so a file written with it cannot be mistaken for a calibrated one.


**APD and PMT hardcode the NI-DAQ sample clock terminal to counter 2 while the setup file chooses which counter generates it**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:69` · *bites on a different rig*

_nidaq_clock_source = 'ctr2InternalOutput' (APDManager.py:69, PMTManager.py:62) and _detection_samplerate = 1e6 (APDManager.py:68, PMTManager.py:61). Both are hardcoded in the detector; the pulse train they name is created by NidaqManager from setupInfo.nidaq.timerCounterChannel (NidaqManager.py:128, :1240-1250) at rate=1e6. The detector hardcodes the counter INDEX; the config chooses it.

A point-scanning rig whose timer counter is anything but ctr2 -- e.g. an X-series board where ctr0/ctr1 are the free ones, or a second device. SetupInfo.py:504-519 accepts any string or integer and the config-editor template (utility_scripts/builtin_templates/sections/nidaq.json:8) only suggests 'Dev1/ctr2' as an example. Set timerCounterChannel to 0 and the APD/PMT input task is clocked from ctr2InternalOutput, a terminal driven by nothing: the finite CI/AI read never receives its sample clock, so the scan produces no data and the read blocks until teardown. Nothing cross-checks the two, and the failure names neither counter. Worse, timerCounterChannel defaults to None (SetupInfo.py:504), in which case NidaqManager.py:1240 skips creating the timer task entirely while the detector still arms an input task clocked from it. Also note the terminal string carries no device prefix, while the APD's own counter line does honour a configurable deviceName (APDManager.py:71-74).

> Related and measurable: the ratio between the two clocks is derived from config on the detector side and hardcoded on the NI-DAQ side. APDManager.py:1010 computes _frac_scan_det_rate = round(1e6 * scan_time_step) where scan_time_step = 1/setupInfo.scan.sampleRate, while NidaqManager.py:1244 computes the timer's finite sample count as outputSamples * (1e6/100e3) and clocks the AO from a hardcoded '100kHzTimebase' at 100000 Hz (NidaqManager.py:1264-1272). Every shipped setup uses sampleRate 100000, so both give 10. Set scan.sampleRate to 200000 (a natural thing to try for smoother galvo waveforms) and the APD arms for N*5 detection samples while the hardware still runs the scan for N*10 -- the APD stops reading halfway and the second half of every image is blank. At 250000 it reads 40%; at 50000 it waits for twice as many samples as the timer will ever produce and hangs.

*The right shape:* The detector should ask the NidaqManager for the clock it actually created (terminal and rate) rather than restating them. At minimum, NidaqManager should expose the timer terminal derived from timerCounterChannel, and startInputTask should reject -- loudly, at scan build -- a clock source that names a counter no task is driving.


**Five camera managers can never reach real hardware and silently present a synthetic mock as a live camera**
`imswitch/imcontrol/model/managers/detectors/BaslerManager.py:171` · *bites on a different rig*

Not a number but the same shape: a try/except fallback sized for 'the SDK is not installed on this dev box' that is load-bearing on a rig. Five managers import an interface module that does not exist anywhere in this tree -- avcamera, baslercamera, esp32camera, gxipycamera, jetsoncam -- and fall through to MockCameraTIS, a 500x500 synthetic bead generator, behind a single logger.warning.

Anyone who puts AVManager, BaslerManager, ESP32CamManager, GXPIPYManager or JetsonCamManager in a setup file. ImSwitch starts normally, the detector appears in the GUI under its configured name with its configured 'Camera pixel size', live view shows a plausible moving-bead image, and recordings save that synthetic data with the rig's real calibration in the OME PhysicalSize. The only signal is one warning line at startup among hundreds. For ESP32Cam and PiCam, which take a host/port, the user is pointing at a real networked camera and getting fabricated frames. There is a second inconsistency inside the mock itself: it reports image_width/image_height = 800x800 while grabFrame() returns 500x500, so the manager's fullShape (and everything derived from it, including OME SizeX/SizeY) is 800x800 for 500x500 data.

*The right shape:* A mock substituted for missing hardware must be visible at the level the user works at, not only in the log: either refuse to construct the detector unless the setup file opts in (the way HamamatsuManager/TISManager use an explicit 'mock' cameraListIndex), or mark the manager as mocked so the GUI and every saved file's metadata say so. A recording produced from synthetic frames must not be indistinguishable from a real one.


**ThorCam re-arm reuses frames-per-trigger as the ring-buffer depth, shrinking it from 4 frames to 1 on any runtime trigger-mode change**
`imswitch/imcontrol/model/interfaces/thorcamera_tsi.py:167` · *bites on a different rig*

Three different numbers for one quantity. ThorCamTSIManager arms with buffer_size=4 (lines 115, 238, 340); the interface's own signature defaults it to 2 (thorcamera_tsi.py:195); and the re-arm inside set_trigger_mode reads it from frames_per_trigger_zero_for_unlimited, which __init__ sets to 1 (thorcamera_tsi.py:49). Frames-per-trigger and frames-to-buffer are different quantities that happen to both be small integers.

A ThorCam TSI (Zelux/Kiralux/Quantalux) used for scan-triggered recording. The workflow is exactly the trigger: the camera is armed with 4 buffers in __init__, then the user switches 'Operation Mode' to 'Hardware' from the Settings panel, which calls set_trigger_mode -> disarm -> arm(1). The SDK ring is now one frame deep for the rest of the session (startAcquisition only re-arms when not armed, ThorCamTSIManager.py:339-340). Any recording-loop iteration slower than one inter-trigger interval loses frames; the recording then never reaches recFrames*numCamTTL and dies on the stall watchdog with a message about camera triggering and numCamTTL -- pointing the one diagnosis it has at the wrong thing.

> Recording drains via ThorCamTSIManager.getChunk once per loop iteration; RecordingManager.FRAME_POLL_INTERVAL is 100 us plus per-iteration Python/GIL work with a writer thread running, realistically 0.2-2 ms per iteration. Slack = ring depth / trigger rate. At depth 4 the loop tolerates a trigger rate up to 4/0.002 = 2 kHz; at depth 1 it tolerates 500 Hz. A tiling scan at 1 ms per position (1 kHz camera TTL) is inside the tolerance at depth 4 and outside it at depth 1 -- so the recording works until the user changes the trigger mode from the GUI, and then stops working, with nothing in between to connect the two.

*The right shape:* Remember the depth the camera was actually armed with (or take it from a single configurable property next to flushFrameLimit, which is already config-driven at ThorCamTSIManager.py:41) and re-arm with that. Never derive a buffer depth from a frames-per-trigger setting; they are different units of the same integer.


**PhotometricsManager's mock fallback is a Hamamatsu mock that implements none of the API it is substituted into**
`imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py:234` · *bites on a different rig*

Not a magnitude: a substitution written for one detector's API reused for its neighbour's. The except branch logs 'loading mocker' and hands back MockHamamatsu, which has none of sensor_size, name, scan_line_time, poll_frame, check_frame_status, start_live, abort, finish, exp_time, exp_mode, readout_port, binning, roi or close.

Anyone who configures a Photometrics camera on a machine without pyvcam, or with the camera unplugged. The warning says a mocker was loaded; the very next statement (PhotometricsManager.py:29, fullShape = self._camera.sensor_size) raises AttributeError, so ImSwitch fails to start with an error that names MockHamamatsu and gives no hint that the real cause was 'pyvcam missing'. The fallback exists precisely so that this case degrades gracefully, and it is the one case it cannot survive.

*The right shape:* Either write a Photometrics mock that answers the PVCAM surface the manager actually uses, or let the ImportError propagate with its own message. A fallback that cannot stand in is worse than no fallback, because it converts a clear 'pyvcam is not installed' into an AttributeError on an unrelated class.


**Photometrics trigger-source enum: the write map and the read-back map disagree on two of three values**
`imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py:154` · *bites on a different rig*

Four PVCAM exp_mode codes, mapped inconsistently in two directions. _setTriggerSource writes 1792 for 'Internal trigger', 2048 for 'External start-trigger', 2560 for 'External frame-trigger' (lines 150/154/158). _updatePropertiesFromCamera reads 1792 -> Internal, 2304 -> start-trigger, 2048 -> frame-trigger (lines 202/204/206). 2560 maps to nothing on the way back and 2304 is never written.

A Photometrics user setting up a scan-triggered recording. Select 'External start-trigger': the hardware gets 2048, and the next _updatePropertiesFromCamera -- which runs after every exposure-time change (line 137) and at construction (line 60) -- reads 2048 back and rewrites the displayed parameter to 'External frame-trigger'. Select 'External frame-trigger': the hardware gets 2560, which matches no branch, so the displayed value silently stays at whatever it was, potentially 'Internal trigger'. The parameter dictionary is what the Settings panel shows and what gets snapshotted into the recording's metadata, so the saved file records a trigger mode the camera was not in. Adding a fourth mode requires remembering to touch both maps, and getting it wrong changes nothing visible until someone reads the metadata.

*The right shape:* One bidirectional table -- a single dict of label -> code with the reverse derived from it -- so the two directions cannot drift, and an explicit else-branch on read-back that logs an unrecognised code instead of leaving the displayed value stale.


### Scan and DAQ timing

**Beta stage scan hardcodes a 2 ms move + 2 ms settle inside each pixel dwell; the shipped default dwell is 1 ms, so the fast axis is driven to the wrong positions**
`imswitch/imcontrol/model/signaldesigners/BetaScanDesigner.py:153-154` · *bites someone today*

smooth = ceil(0.002 * sampleRate) and settling = ceil(0.002 * sampleRate) — 2 ms + 2 ms = 4 ms of the pixel dwell, evidently sized for a piezo/stage settle at a ~10 ms camera exposure. Nothing declares it; no positioner property feeds it.

Every setup using BetaScanDesigner (6 of the 8 shipped configs: example_coolLED, example_monalisa, example_no_hardware, example_snouty_smart_modes, mock_scan_setup, hamamatsu_mock_scan_setup, mixed_hamamatsu_apd_mock_scan_setup) whose dwell is under 4 ms. ScanWidgetBase.py:149 and ScanWidgetMoNaLISA.py:23 both default the dwell field to '1' ms, so this is the out-of-the-box state: press Run and the stage visits the wrong places. The operator sees a stage-scanned image whose fast axis is shifted by ~2 ms/dwell pixels, with several nominal positions never visited and the last few pixels of every line duplicated at the end position. No exception, no log line.

> At sampleRate=100000: smooth=settling=200 samples each. The write region for pixel s is [ (s+1)*seq - 400, (s+1)*seq ). With seq = dwell*1e5, that reaches back ceil(400/seq) = ceil(4 ms / dwell) pixel windows. dwell=1 ms -> 4 windows back; measured max position error 3.565 um on a 1 um step = 3.57 pixels. The guard `if (end - smooth - settling) > 0` only suppresses the first few iterations, so early pixels are silently overwritten by later ones rather than the case being rejected.

*The right shape:* A settle time is a property of the mechanics, so it belongs in the positioner's managerProperties (e.g. settleTimeS, alongside the existing conversionFactor/minVolt/maxVolt), converted to samples per axis at design time. Whatever the source, it must be clamped against the dwell — settle_samples = min(round(settleTimeS*sampleRate), sequenceSamples) — and a dwell shorter than the settle time must be refused with a message, not silently folded back over the previous pixels.


**'maxScanTimeMin' — documented as a hard cap on scan duration — is only honored by GalvoScanDesigner, and is declared exclusively in setups that use BetaScanDesigner**
`imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:72-76 (only reader) vs imswitch/imcontrol/model/managers/ScanManagerBase.py:119 (Base/MoNaLISA makeFullScan, never calls checkSignalLength)` · *bites someone today*

maxScanTimeMin = 1 (minute), declared in 4 shipped setups; and the companion guard `scan_steps > 1e7` in the same method. The config-editor template calls it "Hard cap on scan duration. null = no limit" and docs/setupinfo-reference.rst documents it as effective.

Users of example_no_hardware, example_snouty_smart_modes, hamamatsu_mock_scan_setup and mixed_hamamatsu_apd_mock_scan_setup — all four declare "maxScanTimeMin": 1 and all four are scanWidgetType "Base" + BetaScanDesigner. ScanManagerBase.makeFullScan never calls checkSignalLength, and BetaScanDesigner never overrides the base no-op that returns True, so the cap is a double no-op. Only ScanManagerPointScan calls it — and example_sted, the one PointScan setup, is the one setup that does NOT declare maxScanTimeMin. The user sets a ROI, hits Run, and instead of a refusal gets a multi-gigabyte allocation and a scan running tens of minutes past their declared cap.

> Beta total samples = ny * (nx*ceil(dwell*fs) + ceil(return_time*fs)). With the shipped return_time=0.01 s, fs=100 kHz and a 10 ms dwell: 128x128 = 16,512,000 samples = 165 s (2.8x over the declared 1-minute cap, measured); 512x512 = 262,656,000 samples = 43.8 minutes and 4.2 GB for the two float64 AO arrays alone — before NidaqManager's `np.array(AOsignals).squeeze()` copy and the bool TTL arrays. Nothing refuses it.

*The right shape:* Move the duration/size guard out of one designer and into SuperScanManager.makeFullScan so every widget type gets it, and express it in the two quantities that actually bind: seconds (scan_samples_total * scan_time_step, which every designer already reports in the ScanInfoContract) against maxScanTimeMin, and bytes (samples * 8 * n_AO_axes + samples * n_DO_lines) against a memory budget — not a pixel-step count.


**'D3 step delay (samples)' in the point-scan GUI is consumed as microseconds, giving 1/10 of the requested inter-slice settling at the shipped sample rate**
`imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:82 (`self.__paddingtime_d3step = int(parameterDict['d3step_delay'])  # inter-slice delay [µs]`) vs imswitch/imcontrol/view/widgets/ScanWidgetPointScan.py:143` · *bites someone today*

d3step_delay, entered in a field labelled "(samples)", used verbatim as µs and then divided by __timestep = 1e6/sampleRate = 10 µs. The label is only correct at sampleRate = 1 MHz; every shipped config uses 100 kHz.

The example_sted / PointScan operator who sets a per-slice delay so a Z piezo or the polarization rotator (docs/api/api.imcontrol.rst: "mostly relevant for the polarization during scan") can settle between planes. They type 1000 expecting 10 ms of settling at 100 kHz and get 1 ms. The device is still moving when the first line of the new slice is acquired; the plane is skewed or blurred, and since scan_throw_settling is what the APD/PMT throw away, detector and designer agree with each other about the wrong number. Nothing warns.

> settling_samples = round(d3step_delay_µs / (1e6/sampleRate)) = d3step_delay * sampleRate/1e6. At sampleRate=100000 that is d3step_delay/10 — a 10x shortfall against the label. Entering 1000: label promises 1000 samples = 10.0 ms; delivered 100 samples = 1.0 ms. (The neighbouring 'Phase delay (samples)' field, default 100, is the same confusion in the other direction: APDManager.py:1059 / PMTManager.py:970 take it raw into the 1 MHz detection-sample domain — it is the only scanInfoDict quantity not scaled by _frac_scan_det_rate — so it too is really µs.)

*The right shape:* Both fields are times, not counts. Label and store them in µs (or seconds) and convert at the point of use — settling_samples = round(delay_s * sampleRate), phase_delay_samples = round(delay_s * detection_samplerate) — so neither meaning changes when the sample rate does.


**scan.sampleRate is honored by every designer, the simulator and the detectors, but NidaqManager clocks the real AO/DO scan from the hardwired 100 kHz timebase**
`imswitch/imcontrol/model/managers/NidaqManager.py:1265 (`scanclock = r'100kHzTimebase'`), 1274, 1296, and the 1 MHz/100 kHz ratio at 1191 and 1244` · *bites on a different rig*

100000 Hz / '100kHzTimebase', hardcoded at 6 sites, and `1e6/100e3` = 10 hardcoded at 2 more — against `scan.sampleRate`, a REQUIRED config field the editor template describes as "DAQ output sample rate" and docs/setupinfo-reference.rst as "DAQ sample rate in Hz". All 8 shipped setups happen to say 100000, so the two truths never diverge in-tree.

Anyone who raises sampleRate to get finer timing — the natural move on a point-scan rig, since at 100 kHz the smallest resolvable dwell is 10 µs and the shipped point-scan dwell default is 0.02 ms = 2 samples/pixel. With sampleRate=1e6 the designers build a waveform with 1 µs steps and NI-DAQmx plays it out at 10 µs steps (cfg_samp_clk_timing with an external `source` terminal uses that terminal's rate; the `rate` argument is only buffer sizing). The scan silently runs 10x slow, every dwell is 10x the requested one, and the recorded scan_time_step/dwell_time metadata claim the requested values. Worse for APD/PMT: _frac_scan_det_rate = round(1e6 * scan_time_step) = 1, so the finite counter task is armed for scan_samples_total samples while the timer task is armed for outputSamples*10 — the detector finishes reading after the first tenth of the scan.

> Requested dwell D at configured rate F yields round(D*F) samples; played at 100 kHz the realized dwell is round(D*F)/1e5 = D * F/1e5. F=1e6 -> every dwell, line time and total scan time is 10x too long, and the reported tot_scan_time_s is 10x too short. NidaqManager's own debug line even reads `scanSampsInScan / 0.1e6`, i.e. it assumes 100 kHz.

*The right shape:* Either derive the clock from the config (use the internal AO sample clock at setupInfo.scan.sampleRate, or select the timebase terminal that matches it) and derive the timer counter's rate ratio from it, or — if 100 kHz is a deliberate hardware constraint — delete sampleRate from the schema and docs and expose it as a read-only constant, so nothing can silently disagree with the hardware.


**APD and PMT hardwire their sample clock to 'ctr2InternalOutput' while nidaq.timerCounterChannel is a free config field the editor tells you to choose**
`imswitch/imcontrol/model/managers/detectors/APDManager.py:69 and imswitch/imcontrol/model/managers/detectors/PMTManager.py:62, vs imswitch/imcontrol/model/SetupInfo.py:504-519` · *bites on a different rig*

the literal 'ctr2InternalOutput' (no device prefix, counter 2 fixed) in both point-detector managers, against `timerCounterChannel`, whose config-editor tip reads "Output counter channel for timing, e.g. Dev1/ctr2. Integer N is also accepted and translated to Dev1/ctr{N}". Also `deviceName` (default "Dev1") is configurable for the detector's own counter/AI channel but not for the clock terminal.

A point-scan rig whose ctr2 is already used (a very common collision — ctr2/ctr3 are the usual encoder or external-clock counters on X-series cards) and that therefore sets "timerCounterChannel": "Dev1/ctr0" or the documented integer form. NidaqManager then generates the 1 MHz pulse train on ctr0 while every APD/PMT counter/AI task is clocked from ctr2InternalOutput, which never ticks. Same on a two-card rig where the detector sets deviceName: "Dev2": the bare terminal resolves against Dev2's idle ctr2. The operator sees the scan start, hang for ~10 s, then die with a driver-level DaqError that names neither counter; nothing in ImSwitch says "your timer counter is not the one the detector listens to".

> The failure is binary rather than proportional: the CI/AI task acquires 0 of its _samples_total samples, and each read blocks for nidaqmx's default 10.0 s read timeout (verified in the installed nidaqmx: task.read(..., timeout=10.0)) before raising. Every scan on such a rig produces an empty image after a ~10 s stall.

*The right shape:* Derive the detector clock from the same config value the timer task uses — pass the timer counter's InternalOutput terminal down from setupInfo.nidaq (e.g. '<dev>/ctr<N>InternalOutput' built from getTimerCounterChannel), or, failing that, validate at startup that the configured timer counter is ctr2 on the detector's device and refuse with a clear message otherwise.


**The analog-input voltage range is accepted and then dropped: every PMT channel silently runs at nidaqmx's ±5 V default**
`imswitch/imcontrol/model/managers/NidaqManager.py:411-420 (min_val=-0.5/max_val=10.0 declared at 412, `add_ai_voltage_chan(channel)` called with no range at 420)` · *bites on a different rig*

min_val=-0.5, max_val=10.0 — a deliberate-looking PMT-shaped range that reaches no hardware, and the ±5.0 V that nidaqmx substitutes (verified: add_ai_voltage_chan defaults min_val=-5.0, max_val=5.0). PMTManager compounds it by passing None, None for those parameters, which only works because they are ignored.

Any PMT rig. If the preamp swings above 5 V (a 0-10 V preamp is ordinary), everything above 5.0 V is silently saturated by DAQmx — bright structures read as a flat ceiling with no error. If the PMT output is small (0-1 V, also ordinary), the channel wastes ~10x of the ADC range: on a 16-bit X-series card 153 µV/LSB at ±5 V versus 15.3 µV at ±0.5 V, i.e. a 10x quantization-noise penalty on a photon-starved signal. There is no config key to fix it — the manager exposes mockVoltageMin/mockVoltageMax (±5 V) which apply only in simulation, so the mock declares a range and the hardware does not.

> Full-scale/2^16: ±5 V -> 10 V/65536 = 153 µV per code; the (unused) declared -0.5..10 V range -> 160 µV; a properly matched ±0.5 V range -> 15.3 µV. So a PMT delivering 0-0.5 V loses a factor of 10 in effective resolution, and one delivering 0-10 V loses everything above 5 V.

*The right shape:* Pass the range through: `add_ai_voltage_chan(channel, min_val=min_val, max_val=max_val)`, and source min/max from the PMT's managerProperties (the manager already reads offset_v and mock ranges from there) rather than from a wrapper default. A None must then be rejected at the call site instead of silently working.


**TriggerScope firmware scans upload DAC start/length voltages that no one checks against the axis's declared minVolt/maxVolt**
`imswitch/imcontrol/controller/controllers/TriggerScopeRasterController.py:225-235, reached via imswitch/imcontrol/model/managers/ScanManagerTriggerScope.py:50-54` · *bites on a different rig*

the per-axis minVolt/maxVolt that are load-bearing on every other path — clamped with a warning in TriggerScopePositionerManager.setPosition, range-checked in TriggerScopeManager.setAnalog, checked per emitted waveform by Beta/Galvo checkSignalComp — and are consulted nowhere on the firmware scan path. (TriggerScopeManager.py:59-60 also substitutes a ±10 V default for devices that omit them.)

A TriggerScope rig operator who enters a scan ROI larger than the galvo's or piezo's safe travel. ScanManagerTriggerScope.makeFullScan raises NotImplementedError, so ScanManagerBase's checkSignalComp gate — the one that prints "Signal voltages outside scanner ranges: try scanning a smaller ROI" — is never reached; getTriggerscopeParameters just divides µm by conversionFactor and PARAMETER-uploads dimOneStartV/dimOneLenV, and the board ramps the DAC autonomously. The same operator jogging the same axis to the same position through the GUI gets clamped and warned. Red-zone: this is the path that physically drives the mirror.

> startV = startpos/conversionFactor and lenV = trimmedLength/conversionFactor, so the commanded excursion is (startpos + length)/conversionFactor volts with no upper bound. On an axis declared minVolt/maxVolt = ±5 with conversionFactor 10 µm/V, a 120 µm scan length asks the DAC for 12 V — 2.4x the declared limit, and 1.2x the board's ±10 V hardware range — with no refusal from ImSwitch.

*The right shape:* Give ScanManagerTriggerScope a real pre-flight check with the same contract as checkSignalComp: for each uploaded axis, verify startV and startV+lenV lie inside that device's [minVolt, maxVolt] before any PARAMETER is sent, and refuse the scan with the same message the NI path uses. Devices whose range is unknown should be rejected rather than defaulted to ±10 V.


### Things a new device must remember to declare

**SwabianTimeTagger declares isScanDriven but not rawFrameIsDeferred or drainChunk, so a FLIM recording stops on the first live-preview frame**
`imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:962` · *bites on a different rig*

rawFrameIsDeferred = False (DetectorManager.py:538) and DetectorManager.drainChunk returning ChunkPayload(display=frames, raw=frames) (DetectorManager.py:561). Both defaults are correct for a camera, whose every frame is complete on arrival.

Any rig with a Swabian Time Tagger FLIM detector doing a scan-once/scan-lapse recording. The recording ends about 1 s after the scan starts and writes a lifetime image containing whatever photons had arrived by then, stamped recording:completion_outcome = complete. The scan keeps running; nothing in the log says anything. A snap taken mid-scan has the same problem: it returns the partial display buffer instead of raising RawFrameUnavailableError.

> SwabianFLIMWorker.LIVE_PREVIEW_S = 1.0 (SwabianTimeTaggerManager.py:1149): the worker emits a frame with is_final=False every 1 s, and _on_frame_ready sets _newFrameReady on every one of them. RecordingWorker._expectedFramesFor returns 1 for a scan-driven detector (RecordingManager.py:3627), so the loop stops the instant one frame arrives. A 256x256 FLIM scan at 1 ms dwell runs 65.5 s; the first frame arrives at t=1 s with 1/65 = 1.5% of pixels measured (measured 1.5% in the repro). At 10 ms dwell the scan is 655 s and the recorded frame holds 0.15% of the measurement. _stallReferenceTimeFor returns None while a scan-driven detector is scanning, so the stall watchdog cannot catch it either.

*The right shape:* Declare the two facts the class already knows: rawFrameIsDeferred = True (the volume is whole only when the worker emits is_final), and a drainChunk override that returns display=getChunk() every tick but raw only once, at is_final - the shape APDManager.drainChunk (APDManager.py:806) and PMTManager already implement. Better still, derive the default from isScanDriven so a scan-driven detector has to opt OUT of deferral rather than opt in, and parametrise the existing point-detector test list over rawFrameIsDeferred/drainChunk the way it is already parametrised over isScanDriven.


## Unverified leads

Found but never challenged, because the verification stage ran out of session. Ordered by the severity their finder assigned, which nobody checked.

- **HDF5/Zarr streaming chunk is 32 raw frames, so a point detector's chunk is 32 whole assembled volumes — over 128 MiB per frame the recording cannot be created at all**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:972 (HDF5, chunks=(chunk_frames, *shape) at :980) and :594 (Zarr, chunks=(chunk_frames, *spatialShape) at :600)` · *bites someone today* · Any point-scan rig (APDManager/PMTManager, ChunkKind.RAW) recording a Z-stack or a line-stepped scan. APDManager.drainChunk returns exactly ONE raw frame per scan — `np.expand_dims(np.array(self._image, copy=True), axis=0)`, shape (1, Nz, Ny, Nx) — so WRITE_BATCH_FRAMES=32 is a batch size this detector class can never reach, yet it sizes the chunk. Below 128 MiB per volume the user sees a recordin
- **DEFAULT_STALL_TIMEOUT is 10 wall-clock seconds applied to cameras, so any exposure longer than 10 s is killed at frame zero and its file deleted**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:58 (constant), :3937 (check), :3641 _stallReferenceTimeFor` · *bites someone today* · Anyone doing low-light fluorescence or luminescence in the default SpecFrames mode with an exposure over 10 s. Exposure is a free-form editable number in seconds (HamamatsuManager:49 'Set exposure time', valueUnits='s') and Orca-Fusion/Quest and Thorlabs TSI cameras go to minutes. The user sees the recording die before its first frame with `Detector 'Cam' stalled: no frames received for 10.0s ... 
- **WRITE_BATCH_FRAMES=32 is the only flush trigger, so a recording shorter than 32 frames has no dataset on disk at all until it finalizes**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:84 (constant), :3132 (the sole flush condition)` · *bites someone today* · The live-reconstruction / ImProcess live reader on any slow acquisition. `_flush_batch` is called only from `if self._batch_frame_counts[detectorName] >= WRITE_BATCH_FRAMES` and from `_flush_all_batches()` at the sentinel — there is no time-based flush. HDF5Storer.writeFrames creates the dataset, `frames_committed` and `stream_complete` lazily on its first call, so before the first flush the file 
- **Camera pixel size silently defaults to 0.15 µm and is then written into every file as a measured calibration**  
  `imswitch/imcontrol/model/managers/detectors/DetectorManager.py:201 (default: float = 0.15), 225-243 (absent-key branch)` · *bites someone today* · Every user of every setup file that does not spell `cameraPixelSizeUm` — which is all fifteen shipped example setups (`grep -c cameraPixelSizeUm` returns 0 for each of them, including example_sted, example_monalisa, example_snouty_smart_modes and all four mock scan setups). What they see: a recording whose OME `PhysicalSizeX`/`PhysicalSizeY` says 0.15 µm, a scale bar drawn at 0.15 µm/px, and a til
- **Recordings are chunked 32 frames at a time; every ImProcess display read takes one plane, so each frame costs a whole chunk**  
  `imswitch/improcess/model/plane_navigation.py:128 (iter_planes) and :116 (extract_plane), against imswitch/imcontrol/model/managers/RecordingManager.py:594 and :972 (chunk_frames = min(WRITE_BATCH_FRAMES, 32))` · *bites someone today* · Anyone opening an ImSwitch2 camera recording in the ImProcess data panel. Dragging the frame slider on a 2048x2048 Zarr recording steps at ~3 fps instead of ~77 fps; on a gzip HDF5 recording (the shipped default, RecordingManager.py:896 compression='gzip') the mean-image preview shown on dataset selection takes 26x longer than it needs to. Nothing errors and nothing logs; it just feels broken. A p
- **getMeanData() reads the entire dataset on the GUI thread when a dataset is selected, with no size limit of any kind**  
  `imswitch/improcess/model/DataObj.py:352 (getMeanData), reached from imswitch/improcess/controller/DataFrameController.py:89 (showMean) via currentDataChanged at :125` · *bites someone today* · Anyone who clicks a large recording in the ImProcess data list. currentDataChanged is a comm-channel slot on the GUI thread, so the whole window is frozen with no progress indication for the duration. A 60,000-frame widefield timelapse is not exotic and is 503 GB.
- **A live source's poll() returns every unread frame in one call, so opening a finished recording materialises the whole thing**  
  `imswitch/improcess/live/sources.py:543 (ZarrLiveSource.poll), :1261 (Hdf5LiveSource.poll), :963 and :1704 (the lapse sources), consumed at imswitch/improcess/live/workers.py:117 and :183` · *bites someone today* · Anyone who switches live reconstruction on over a folder that already holds finished recordings — _scanForStores runs immediately at _startLive (LiveModeController.py:114) and queues every store it finds, and _is_store_ready (:295) admits a complete store first of all. The queue is serial, so the ordinary case of a recording finishing while the previous one is still reconstructing lands here too. 
- **ThorCam TSI polls 10 ms for a frame its own default config exposes for 50 ms, then returns a zero image as if it were data**  
  `imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:165-173 (getLatestFrame), :272-278 (getChunk)` · *bites someone today* · Anyone running a Thorlabs Zelux/Kiralux/Quantalux at any exposure above ~12 ms in Software trigger mode -- which is the shipped default, the documented default, and the value in the shipped rig config for real camera serial 29718 (imswitch/_data/user_defaults/imcontrol_setups/example_kiralux_teensy.json, and the identical example in docs/devices/detectors.rst:892). Live view shows black frames; 'S
- **focusLock updateFreq is documented in milliseconds and consumed as hertz; asking for a slow loop gives the fastest possible one**  
  `imswitch/imcontrol/controller/controllers/FocusLockController.py:139 and :178; declared at imswitch/imcontrol/model/SetupInfo.py:223-224; documented at docs/setupinfo-reference.rst:504` · *bites someone today* · Any rig author who configures focusLock from the documentation. Writing updateFreq: 100 to mean 'every 100 ms' yields a 100 Hz loop: focusTime = 10 ms, the GUI timer fires 100x/s and the worker thread runs a gaussian filter + peak search + centroid on every tick. Writing updateFreq: 1000 to mean 'every second' yields int(1000/1000) = 1 ms, a 1000 Hz GUI timer. Anything above 1000 yields int(0.999)
- **The write-batch frame count is also the on-disk chunk depth, so every camera bigger than a crop gets an uncacheable chunk and a ~30x read amplification**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:84 (constant), :972 (HDF5 streaming dataset), :594 (Zarr streaming array)` · *bites someone today* · Anyone who records with a camera whose frame is bigger than a small crop and then opens the file in ImProcess (or any per-plane reader) and moves the frame slider, or lets the Data panel compute its mean image. A 32-frame chunk is 256 MiB on a 2048x2048 uint16 sensor and 16 MiB on 512x512; h5py's default chunk cache is 8 MiB on HDF5 2.0 and 1 MiB on the widely deployed 1.14, and HDF5 does not cach
- **A display-only mean image is computed by reading every frame of the selected dataset, on the GUI thread, with no cap**  
  `imswitch/improcess/model/DataObj.py:367 (the walk), reached from imswitch/improcess/controller/DataFrameController.py:126 (`currentDataChanged` -> `showMean`) and DataEditController.py:22` · *bites someone today* · Anyone who clicks a long recording in the ImProcess data list. Selecting a dataset -- not opening the edit window, not pressing anything -- runs `getMeanData()` synchronously on the GUI thread and reads every plane through the lazy h5py/Zarr handle. For the tens-to-hundreds-of-frame stacks the panel was written for this is invisible. For a 648-frame line-step scan it is still fine. For a camera ti
- **The writer's finalize deadline is a hardcoded 30 s while its queue budget is 512 MiB — together they assert an undeclared minimum disk throughput, and blowing it deletes a completed recording** — **already fixed**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:3306 (self.join(timeout=30.0)), against :72 (WRITER_QUEUE_MAX_BYTES = 512 MiB) and :61 (RECORDING_THREAD_STOP_TIMEOUT_MS = 30000)` · *bites on a different rig* · Anyone recording to storage slower than the implied floor — a NAS/SMB share, an external spinning disk, a busy shared drive — or with two or more detectors. On timeout, finish() raises TimeoutError, the worker's `finally` catches it at :4017 and calls writerThread.abort(), and abortStream DELETES the file. The recording completed, the frames were captured, and the user loses all of it at the finis
- **WRITER_OPEN_TIMEOUT_S = 30 s is unreachable because the controller abandons the arm handshake at 5 s, and the failure it reports blames the detectors for a storage delay**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:60 (RECORDING_ARM_TIMEOUT = 5.0) and :85 (WRITER_OPEN_TIMEOUT_S = 30.0); imswitch/imcontrol/controller/controllers/RecordingController.py:1166-1181` · *bites on a different rig* · Anyone whose openStream is slow: a network/SMB share, a cold external volume, an HDF5 lapse file reopened in 'a' mode, or several detectors each creating their own file. `_record()` runs `writerThread.wait_for_open()` at :3776 and only reaches `_signalAcquisitionStarted()` at :3843 after the chunk consumers are registered; `__acqStartedEvent` is cleared inside startRecording at :2267, so the contr
- **Focus-lock reacquisition deadline is in seconds but the barrier it bounds advances in focus estimates, so a slow focus camera can never reacquire**  
  `imswitch/imcontrol/model/SetupInfo.py:251 (reacquireTimeoutS: float = 1.0) with :274 (reacquireSamples: int = 5)` · *bites on a different rig* · Any rig whose focus estimate rate is at or below 5 Hz — set `updateFreq` to 5 or less, or run a focus camera whose exposure plus spot fit exceeds 200 ms (a dim IR lever spot on a USB camera is routinely 200-250 ms). The estimate rate is `min(updateFreq, camera frame rate, 1/fit time)`: ProcessDataThread.run (FocusLockController.py:1059-1088) waits `max(0.001, focusTime/1000 - processingTime)`, so 
- **focusLock.updateFreq is documented in milliseconds and used as hertz**  
  `imswitch/imcontrol/model/SetupInfo.py:223-224 (`updateFreq: int` / "Update frequency, in milliseconds.") consumed at imswitch/imcontrol/controller/controllers/FocusLockController.py:139` · *bites on a different rig* · Anyone configuring a focus lock for a new rig from the field documentation instead of copying example_sted.json. They read "Update frequency in milliseconds", write the interval they want, and get its reciprocal. The docs contradict themselves on the same page: :504 says milliseconds while :547 reasons about "updateFreq: 10" as an estimate rate, and the AutofocusInfo example at :609 ships `"update
- **Galvo phase delay is a rig calibration hardcoded twice, at different values, in two scan panels**  
  `imswitch/imcontrol/view/widgets/ScanWidgetPointScan.py:15 (`QLineEdit('100')`) vs imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py:33 (`QLineEdit("0")`); defaulted again at imswitch/imcontrol/model/managers/detectors/APDManager.py:1059 and imswitch/imcontrol/model/scan_parameters.py:120-122` · *bites on a different rig* · Anyone driving the same galvo pair from both panels on the same microscope: the Point Scan panel discards the first 100 µs of every APD/PMT record and the Advanced panel discards none, so the two panels produce images of the same field offset from each other along the fast axis. On a fresh profile (no persisted widget state) neither number came from the rig — the lag is a property of the mirror an
- **getNumCamTTL counts rising edges per TTL cycle, but the point-scan designer's cycle spans many scan positions**  
  `imswitch/imcontrol/controller/basecontrollers.py:898-907, reading imswitch/imcontrol/model/signaldesigners/PointScanTTLCycleDesigner.py:357-365` · *bites on a different rig* · A point-scan rig recording a camera in ScanOnce/ScanLapse mode with any TTL sequence other than the trivial 'h1'. example_sted.json is exactly this shape: scanWidgetType PointScan, PointScanTTLCycleDesigner, and a `WidefieldCamera` (TISManager) alongside the two APDs; SetupInfo.getTTLDevices (SetupInfo.py:712-727) makes any detector with a digitalLine a TTL device, so the camera gets a sequence an
- **The "is this array small enough to read whole" threshold is in elements, but the cost it gates is bytes — and 8.5x bytes at that**  
  `imswitch/improcess/model/contrast.py:11 (_SAMPLE_ELEMENT_THRESHOLD = 64 * 1024 * 1024), used at :31 in _should_sample and :80 in finite_values` · *bites on a different rig* · Every processor result goes through finite_range() to compute display levels — filters, background, math_ops, scale_type, image_calculator, channel_merge, combine, projection, segmentation. A user applying a Gaussian filter to a 16-frame 2048x2048 uint16 stack (exactly the threshold, a perfectly ordinary Z-stack or short burst) pays a ~1 GiB transient and over a second of stall to compute two numb
- **sample_values caps the number of values returned, not the number of chunks it decompresses to get them**  
  `imswitch/improcess/model/contrast.py:44 (sample_values), cap _MAX_SAMPLE_VALUES = 2_000_000 at :12` · *bites on a different rig* · Anyone auto-levelling or histogramming a single large plane rather than a stack — a stitched tiling canvas, an SMLM super-resolution render, a max projection. These are exactly the results whose spatial extent is large and whose leading axes are size 1, so the leading-axis strategy has nothing to work with. It happens on the GUI thread, from the toolbar's auto-contrast and histogram (ImageToolbarC
- **The 2 GiB snapshot refusal is evaluated one line after the read it exists to refuse**  
  `imswitch/improcess/view/ROIManagerWidget.py:1439 (the check), :1433 (the read), constant MAX_SNAPSHOT_BYTES = 2 * 1024**3 at :185` · *bites on a different rig* · A user measuring ROIs on a layer whose data is lazy rather than an in-memory ndarray — ImProcess routinely puts LazySubsetArray (model/lazy_array.py, from ArrayProcessingResult.duplicate) and _LazyHistogramPreview (model/localization_result.py:38) into napari image layers, and VirtualImageArray exposes to_dask(). For a plain ndarray np.asarray is a free view and the guard works; for a lazy handle 
- **Autofocus waits a hardcoded 150 ms for the Z stage and then reads whatever frame is newest, landing exactly one sweep step off**  
  `imswitch/imcontrol/controller/controllers/AutofocusController.py:11 (_SETTLE_S = 0.15), :274-276` · *bites on a different rig* · Any rig whose camera exposure + readout exceeds 150 ms, or whose Z drive is a stepper/DC focus drive rather than a piezo. getLatestFrameShared -> getLatestFrame -> (HamamatsuManager.py:97) self._camera.getLast() returns the newest COMPLETED frame, whose exposure began at t - exposure - readout. With exposure >= 0.15 s that frame belongs to the previous Z position, so every focus_vals[i] is really 
- **Point-scan detectors inherit nidaqmx's implicit 10 s read timeout, capping the fast-axis line period, and the failure is reported as a camera-trigger problem**  
  `imswitch/imcontrol/model/managers/NidaqManager.py:461-470 (readInputTask); callers APDManager.py:1162,1186 and PMTManager.py:998,1008` · *bites on a different rig* · Point-scanning confocal/STED/FLIM operators using long pixel dwell times. Each readdata() call blocks for one whole fast-axis period, so a scan whose line period exceeds 10 s raises DaqError on every line. APDWorker.run (APDManager.py:1245-1268) catches it, logs, calls _markRawFailed and emits acqDoneSignal, so the assembled volume is never published. The operator's actual message comes from the r
- **The scan-start arm wait (5 s) is shorter than the writer-open budget (30 s) it contains, so the inner timeout is unreachable and a slow file open aborts the scan blaming the detectors**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:60 (RECORDING_ARM_TIMEOUT = 5.0) vs :85 (WRITER_OPEN_TIMEOUT_S = 30.0); consumed at controller/controllers/RecordingController.py:1166-1181` · *bites on a different rig* · Anyone recording to a network share, a slow disk, or a Zarr/HDF5 destination that takes more than a few seconds to create -- and anyone whose save folder already holds many recordings, because getSaveFilePath (:2817-2832) resolves the name by stat-ing _1, _2, _3 ... one at a time. All three scan-driven start paths gate on this (RecordingController.py:444, :1016, :1308). On expiry the operator gets
- **The recording stall watchdog is a fixed 10 s with no config path, so a long-exposure camera is aborted as a stalled one**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:58 (DEFAULT_STALL_TIMEOUT = 10.0), applied at :2263, enforced at :3937-3965` · *bites on a different rig* · Anyone whose camera frame interval exceeds 10 s: long-exposure fluorescence, chemiluminescence, or a hardware-triggered camera waiting on a slow external event. In SpecFrames / ScanOnce / ScanLapse / CameraLapse the watchdog raises RuntimeError, emits sigRecordingStalled and routes through abort/partial-output cleanup -- the recording is destroyed, not merely warned about. The message they get is 
- **Triggered workflow snaps wait a hardcoded 2 s for a frame whose exposure is a parameter, then write a fabricated zero image into the calibration CSV**  
  `imswitch/imcontrol/model/workflows/calibration.py:93-104; the same pattern at imswitch/imcontrol/model/workflows/z_stack.py:312-324` · *bites on a different rig* · Anyone raising exposure_us for a weak polarisation-calibration or Z-stack signal. On expiry the code logs one warning, calls stop_acquisition() -- truncating the exposure that was still in flight, so a frame that would have arrived at 2.1 s is destroyed -- then falls through to `return np.zeros((1804, 1804), dtype=np.uint16)`. run_polarisation_calibration (:150-165) computes the quad-pixel means p
- **The writer's 30 s stop join and the 512 MB queue budget are unrelated numbers; below ~18 MB/s the escalation deletes a completed recording**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:3306 (self.join(timeout=30.0)) against :72 (WRITER_QUEUE_MAX_BYTES = 512 * 1024 * 1024)` · *bites on a different rig* · Anyone whose sustained write path is slower than ~18 MB/s: gzip-compressed HDF5 (the default -- HDF5Storer.__init__ takes compression='gzip' at :896) on an older workstation, a network share, or a Zarr codec that is slow on that CPU. The escalation is deliberate and documented in the code (:3086-3090), which makes the consequence worse: finish() raises TimeoutError, the caller at :4016-4030 catche
- **A completed Zarr recording reports its entire length as one "stack", so the live reader materialises the whole file in a single poll -- the HDF5 source of the same recording says one frame**  
  `imswitch/improcess/live/sources.py:526 (`frames_per_stack = self._expected_frames`), consumed at imswitch/improcess/live/workers.py:111-160` · *bites on a different rig* · Anyone running LiveModeController's automatic store processing (imswitch/improcess/controller/LiveModeController.py:609) over .zarr recordings that have finished writing before the queue reaches them -- the normal case for a queue of stores. LiveStreamWorker._run_startup then buffers frames_per_stack frames into a Python list and calls np.concatenate on it (workers.py:160), so peak memory is twice
- **recorded_frames_per_time_point still walks every recorded frame when spans are present -- the branch above it was already fixed for exactly this** — **already fixed**  
  `imswitch/imcommon/model/acquisition_layout.py:1640-1643` · *bites on a different rig* · A rig recording gated Advanced / line-step scans (pLS-RESOLFT, MoNaLISA, any detector whose mask drops some conditions) with a time loop, then running live reconstruction on it. recorded_event_spans is non-None exactly when the detector is gated, which is exactly the configuration this branch's span machinery exists for. The call is on LiveSource.open() (sources.py:520 and :1245, via _frames_per_s
- **The chunk-consumer overflow trim uses list.pop(0) on a queue whose length is now tens of thousands of frames** — **already fixed**  
  `imswitch/imcontrol/model/managers/detectors/DetectorManager.py:783` · *bites on a different rig* · The 9338 fps 76x20 crop camera that motivated the byte budget, when some consumer is registered but has stopped polling (a BeadRec or tiling consumer whose thread died, or one that was never released). The queue sits at the 84,733-frame cap and every subsequent drain pops as many frames off the front as it pushes on the back, each pop memmoving the whole remaining pointer array -- all of it inside
- **The completion-outcome HDF5 attribute is fixed at S13 — exactly the length of the longer of the two current values, so a third outcome truncates silently and 'stopped_early_on_stall' becomes a valid-but-wrong 'stopped_early'**  
  `imswitch/imcontrol/model/managers/RecordingManager.py:1032 (dtype="S13"), against imswitch/imcommon/model/acquisition_metadata.py:14 (VALID_COMPLETION_OUTCOMES)` · *bites when extended* · The next person to add a completion outcome — which the codebase is already reaching for, since FailureKind (:1707) distinguishes failure classes the outcome field cannot express. Their HDF5 recordings carry a truncated marker with no exception and no warning, on HDF5 only (Zarr :826 and TIFF write plain strings, so the same recording reports different outcomes in different formats). Worst case is
- **Basler and Jetson getChunk return a 4-D chunk, breaking the (numFrames, H, W) contract the broker fans out on**  
  `imswitch/imcontrol/model/managers/detectors/BaslerManager.py:100` · *bites when extended* · Anyone configuring a Basler or Jetson camera. Their interface's getLastChunk() already returns (1, H, W), so the extra expand_dims makes it (1, 1, H, W) and each 'frame' handed to a consumer is (1, H, W) instead of (H, W). The ZarrStorer takes spatialShape from frames.shape[1:] (RecordingManager.py:753), so it silently creates a 4-D dataset with a degenerate axis, mislabelled against the OME axis 
- **NidaqManager.readInputTask's `timeout=False` branch reads as 'no timeout' but delivers nidaqmx's 10 s default, capping the longest scan line**  
  `imswitch/imcontrol/model/managers/NidaqManager.py:461-470` · *bites when extended* · A point-scan user acquiring a slow, wide line — photon-starved APD/STED imaging, where multi-millisecond dwells are the point. APDManager.run_loop_d2 issues one read per fast-axis line period (readdata(self._samples_d2_period)), so the whole line must arrive within 10 s. Beyond that, every line read raises DaqError -200474 mid-scan and the acquisition dies with a driver message about a timeout rat
- **The acquisition-layout preflight is opt-in per reconstructor, and eight of nine never opt in — so the uncalibrated-loop refusal is dead code**  
  `imswitch/improcess/reconstructors/base.py:196 (`acquisition_requirements: AcquisitionRequirements | None = None`) and :30 (`requires_calibrated_loops: frozenset[str] = frozenset()`); the guard it disables is at :121-133` · *bites when extended* · Anyone whose recording carries a layout with an uncalibrated loop (`step=None`, `unit=None`) — which the producer side can legitimately emit: `_acquisition_layout_source._physical_loops` (lines 224-229) sets `step=None` whenever `pixel_sizes` is short or unparseable, and `build_triggerscope_resolft_layouts`'s `step()` helper (line 668-674) returns None for a missing `cycleStepSizeUm`/`roStepSizeUm
- **Processor.id defaults to "unnamed", which silently defeats both the plugin loader's missing-id guard and the footprint's class-name fallback**  
  `imswitch/improcess/processors/base.py:96` · *bites when extended* · Anyone writing an ImProcess drop-in analysis plugin - the documented user extension point - who sets `name` but not `id`. Their tool loads and runs, but every result it produces carries a processing footprint that says the step was `unnamed`. If they write a second plugin that also omits `id`, that one silently does not appear in the tool list at all; the only trace is a log warning naming a dupli
- **The live stall watchdog's 300 s default is switched off by a class flag each new source author must remember to set**  
  `imswitch/improcess/live/sources.py:300 (LiveSource.idles_between_stacks = False), overridden at :753, :862, :1497, :1597; consumed at imswitch/improcess/controller/LiveReconstructionController.py:105-117; default from imswitch/improcess/model/processing_config.py:149` · *bites when extended* · Whoever adds the seventh live source — a new format, a plugin reconstructor's own reader, a remote store — and does not know the flag exists. If their source idles longer than five minutes between logical stacks (a slow timelapse, a scan waiting on a trigger or a focus-lock settle), the watchdog declares the writer crashed and finalises with partial data. The result is a silently short reconstruct
- **Pulse-generator stop() joins its worker with a timeout and never checks whether it stopped**  
  `imswitch/imcontrol/model/managers/pulsegen/PulseStreamerManager.py:293-294; same shape at pulsegen/TeensyPulseManager.py:277-278 and interfaces/teensypulse.py:614-615` · *bites when extended* · A rig whose Pulse Streamer is reached over Ethernet, where the worker can be blocked inside pulseStreamer.stream() or isStreaming() for longer than 1 s (a long sequence upload, a congested link). stop() returns claiming success while the worker's finally clause has not run, so __running stays True. The operator's next action then fails with 'A sequence is already running' (run(), :248) or 'Cannot 

## What to do with this

The three marked *already fixed* were consequences of this branch's own byte
budgets and are done.

Of the rest, nothing is in this PR's blast radius. They belong to other owners
and other branches: the FLIM detector, the Photometrics and ThorCam managers,
the focus lock, the scan designers' timing. Two deserve attention soon on their
own merits, both in the recording path this branch touches:

- a recording shorter than the write batch has no dataset on disk until it
  finalizes, so a live reader sees an empty file for the whole acquisition;
- the recording stall watchdog is a fixed ten wall-clock seconds, so any
  exposure longer than that is killed at frame zero and its file deleted, with
  a message about scan TTL wiring in a mode that has no scan.

Both are pre-existing and neither is caused by this work.
