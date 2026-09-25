# Acquisition layout: what each rig step should print

**Companion to [acquisition-layout-rig-validation.md](acquisition-layout-rig-validation.md).**
That guide says what to do and why. This one says what a correct result *looks
like*, so at the microscope you compare output against a reference instead of
judging by eye.

Every block below was produced by running the step against the **simulated**
rig — the real signal designers, the real producers, the real recording gate,
worker and storers, the real resolver, with mock detectors and a simulated
NI-DAQ. Nothing here is imagined. What the simulation cannot supply is the
hardware itself, and each card says which of its claims only the rig can
settle.

Numbers will differ from yours wherever your scan is a different size. The
*shape* of the output is the reference: which lines appear, in what order, and
which values must agree with each other.

---

## Step 0 — multicolor pLS-RESOLFT, 10 cycles x 20 RO steps

### Run

```bash
# --- BEFORE THE SAMPLE GOES ON (all on the instrument computer) ---
cd <the checkout ImSwitch will run>  &&  git log --oneline -1        # must be the commit you mean to validate -- see section 0 of the guide
python -c "import imswitch, os; print(os.path.dirname(imswitch.__file__))"   # must be that checkout, not a release install
python -c "import json,sys; d=json.load(open(sys.argv[1])); print({n: i.get('digitalLine') for n,i in d['detectors'].items()})" <your imcontrol setup .json>   # the camera MUST print 'Triggerscope/TTL<n>' = the firmware's camera line
QT_QPA_PLATFORM=offscreen python -m pytest -p no:napari -q imswitch/imcontrol/_test/unit/test_triggerscope_scan_geometry.py imswitch/imcontrol/_test/unit/test_chunk_contract.py   # 40 passed
# --- IN THE GUI ---
# 1. TriggerScope pLS-RESOLFT Multicolor panel: timeLapsePoints=1, cycleSteps=10, roSteps=20.
# 2. LOOK AT 'Camera used for detection'. If it is BLANK, an older saved value was dropped -- re-pick the Orca. A blank box means the recording is refused at arm.
# 3. Recording widget: Scan source = TriggerScopePLSRMulticolor (or TriggerScopeScan if the rig uses the unified panel -- it must be the panel you will press Scan on), mode = Scan once, format = HDF5. Press Record: the log says 'Recording armed (scan-once): start the scan from its scan widget'.
# 4. Press Scan on the multicolor panel. The Recording widget's frame counter must run 1..200 and the recording must finalize by itself.
# --- READ THE FILE BACK, IN THIS ORDER ---
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recording>.hdf5 --frames 22   # --frames 22 matters: the default 12 never reaches the cycle rollover of a 20-plane inner loop
python -c "import h5py,sys; f=h5py.File(sys.argv[1]); d=f[list(f)[0]]['data']; print(d.shape); print({k:(v.decode() if isinstance(v,bytes) else v) for k,v in d.attrs.items() if k.startswith('recording:')})" <recording>.hdf5
QT_QPA_PLATFORM=offscreen python -c "import sys; sys.path.insert(0,'.'); from imswitch.improcess.model import DataObj; from imswitch.improcess.reconstructors.snouty.metadata import recorded_snouty_geometry; p=sys.argv[1]; d=DataObj(p, DataObj.getDatasetNames(p)[0], path=p); print('layout ->', [(l.kind,l.count) for l in d.acquisition_layout.layout.event_loops]); print('SNOUTY reads (cycles, planes_in_cycle, timepoints) ->', recorded_snouty_geometry(d))" <recording>.hdf5
# --- THE LOG, FROM ARM TO FINALIZE ---
grep -nE "Recording producer|beyond the .* planned|fell behind by more than|Skipping the (scan-position|pulses-per-position)|Recording failed|stalled" <imswitch log>
```

### Expect

```
ALL OUTPUT BELOW IS REAL, captured on branch tip 2c2014ae against the mock Orca + the real
RecordingManager/worker/HDF5 storer. Only the firmware's own scan is absent.

--------------------------------------------------------------------------
1. GEOMETRY THE SOFTWARE DERIVES FROM 10 cycles x 20 RO steps (no hardware)
--------------------------------------------------------------------------
getNumScanPositions() -> 200
getNumCamTTL()        -> {'OrcaStraight': 1}

payload_kind         : detector-frame-stream
modality / source    : snouty / TriggerScopePLSRMulticolorController
provenance           : recorded
storage_axes         : ['frame', 'detector_y', 'detector_x']
event loops (outermost first):
  time   count=1    step=None None
  cycle  count=10   step=0.2 um
  plane  count=20   step=0.05 um
traversal            : [('time', 'forward'), ('cycle', 'forward'), ('plane', 'forward')]
scan_position_count  : 200
  frame 0    -> time=0  cycle=0  plane=0
  frame 19   -> time=0  cycle=0  plane=19
  frame 20   -> time=0  cycle=1  plane=0
  frame 199  -> time=0  cycle=9  plane=19

--------------------------------------------------------------------------
2. WHAT THE ARM-TIME GATE DOES WITH IT (no writer opened on a refusal)
--------------------------------------------------------------------------
[the real thing: 200 frames, one pulse per position]
  accepted (writer would open: True)

[recFrames disagreeing with the layout]
  refused, writer opened: False
  [error] SCAN_POSITION_COUNT_MISMATCH: Layout describes 200 scan positions but recFrames is 100

[camera not declared by the scan (numCamTTL empty)]
  refused, writer opened: False
  [error] DETECTOR_PULSES_UNDECLARED: The scan declares no TTL pulse per position for detector 'OrcaStraight' (getNumCamTTL has no entry), so its frames cannot be tied to scan positions. Gate it in the scan, deselect it, or record it in a non-scan mode.

With the camera box left BLANK the producer refuses before the gate is even reached:
  ValueError: Detector 'OrcaStraight' is selected for this scan recording, but the scan does not gate it: getNumCamTTL() declares no pulse per position for it. Recording an ungated detector in scan mode would label free-running frames as scan positions. Deselect it, gate it in the scan, or record it in a non-scan mode. In this TriggerScope mode the firmware gates the camera on a fixed line, so the scan cannot add a pulse for it -- it can only be told which detector receives that pulse. Set "Camera used for detection" in the scan panel (no camera is selected).
(the GUI shows this as: `Recording failed: <that text>`)

A counter that came back from saved state unusable is named, never read as 1:
  ValueError: RESOLFT scan parameter 'roSteps' is '', which is not a number.
  ValueError: RESOLFT scan parameter 'cycleSteps' is missing, so the number of scan positions cannot be determined.

--------------------------------------------------------------------------
3. THE CHUNK-CAP FIX, PROVEN IN TWO NUMBERS (this is what aborted on main)
--------------------------------------------------------------------------
rawFrameIsDeferred                 : False
cap for the recording consumer     : 1000        <- main applied 16 here
MAX_QUEUED_RAW_FRAMES (deferred)   : 16
MAX_QUEUED_CONSUMER_FRAMES         : 1000
and if it ever does overflow, the message now quotes the cap that tripped:
  OrcaStraight: readChunk consumer "RecordingManager" fell behind by more than 1000 frames; its stream is incomplete
NOTE: this scan plans only 200 frames, so a correctly triggered camera CANNOT reach a
1000-frame backlog. On this branch step 0 cannot fail the way it failed on main.

--------------------------------------------------------------------------
4. THE INSPECTOR ON THE RESULTING FILE  (--frames 22)
--------------------------------------------------------------------------
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  source      : explicit-metadata
  confidence  : certain
  provenance  : recorded
  usable      : True   (geometry may be taken from it)
  authoritative: True   (may refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : OrcaStraight
  modality    : snouty    scan source: TriggerScopePLSRMulticolorController
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    time         count=1      pitch=-
    cycle        count=10     pitch=0.2 um
    plane        count=20     pitch=0.05 um
  traversal:
    time: forward
    cycle: forward
    plane: forward
  issues      : none
  lifecycle   : writer=finalized  outcome=complete  frames=200 of 200  partitions=1 of 1

  first 22 stored frames:
    frame 0     -> time=0  cycle=0  plane=0
    ...
    frame 19    -> time=0  cycle=0  plane=19
    frame 20    -> time=0  cycle=1  plane=0
    frame 21    -> time=0  cycle=1  plane=1
(the rollover at frame 20 is the whole point of the table; frames 0-19 must be ONE cycle)

--------------------------------------------------------------------------
5. THE FILE'S OWN NUMBERS
--------------------------------------------------------------------------
dataset shape : (200, 256, 256) uint16      [at the rig: (200, <Orca ROI>) ]
  axes = TYX
  recording:actual_frames = 200
  recording:completion_outcome = complete
  recording:discarded_frames = 0            <- the number step 0 is about
  recording:expected_frames = 200
  recording:planned_frames = 200
  recording:actual_partitions = 1
  recording:planned_partitions = 1

DataObj:
  getDatasetNames : ['OrcaStraight']
  source/confidence: explicit-metadata certain
  layout loops     : [('time', 1), ('cycle', 10), ('plane', 20)]
  data.shape       : (200, 256, 256)
  lifecycle        : writer=finalized outcome=complete frames=200 of 200 discarded=0
  SNOUTY reads (cycles, planes_in_cycle, timepoints) -> (10, 20, 1)

--------------------------------------------------------------------------
6. THE PRODUCER-STALL WARNING (forced here with a deliberately slow storer)
--------------------------------------------------------------------------
2026-09-05 17:03:33 WARNING Recording producer has been blocked for 1s enqueuing frames for "OrcaStraight": the writer is not draining fast enough. Frames arriving meanwhile are held in the detector's chunk queue, which is bounded.
2026-09-05 17:03:34 WARNING Recording producer resumed for "OrcaStraight" after 1.6s blocked on the writer queue.
and the run that produced them still ended normally: `ended=True failed=[]`, file complete,
frames 200 of 200, discarded_frames 0. IT IS A WARNING, NOT A FAILURE.
It has no `[Component]` prefix (plain module logger), so grep for `Recording producer`.

--------------------------------------------------------------------------
7. WHAT A CAMERA THAT OVER-TRIGGERS LOOKS LIKE (recorded on purpose, for comparison)
--------------------------------------------------------------------------
2026-09-05 17:03:30 WARNING [RecordingWorker] Detector 'OrcaStraight' delivered 3 frame(s) beyond the 200 planned for this recording; they are not written. The recording is not the clean scan its metadata describes -- check numCamTTL and the camera trigger mode.
  recording:discarded_frames = 3
inspector:
  lifecycle   : writer=finalized  outcome=complete  frames=200 of 200  partitions=1 of 1
    [warning] DISCARDED_SURPLUS_FRAMES: 3 frame(s) arrived beyond the 200 this recording planned and were not written. The detector produced more than the scan accounted for, so the frames that were kept may not be the ones the layout describes.
NOTE the trap: `outcome` is still `complete` and the frame table is unchanged. The ONLY
places a surplus shows are `recording:discarded_frames` and that `[warning]` line.

--------------------------------------------------------------------------
8. THE SETUP-FILE TRAP, REPRODUCED (multicolor mode only)
--------------------------------------------------------------------------
[camera declares Triggerscope/TTL6 in the setup file]
  scan started; CameraTTLChan=6
[camera declares no digitalLine (the shipped Snouty case)]
  scan start raised KeyError: 'OrcaStraight'
  parameters uploaded before the failure: ['onLaserTTLChan', 'offLaserTTLChan', 'roLaserTTLChan', 'Laser2TTLChan', 'Laser3TTLChan']
i.e. the recording arms fine and the SCAN dies, logged by the panel as `Scan failed` plus a
KeyError traceback naming the camera. Fix = give that detector
`"digitalLine": "Triggerscope/TTL<n>"` in the setup file (which also keeps the camera
selectable on the older, published tip).
```

### A real fault looks like

- `recording:discarded_frames` is anything but 0. That is the one number step 0 exists to read. 3 frames = a boundary hiccup; ~400 on a 200-frame plan = the camera produced 3 frames per position (see the multicolor question below); a large number with `Detector 'X' delivered N frame(s) beyond the 200 planned` in the log = the camera is free-running, i.e. its Trigger source parameter is 'Internal trigger' rather than external.
- The recording aborts with `readChunk consumer "RecordingManager" fell behind by more than 1000 frames`. With only 200 planned frames this is NOT reachable by a correctly triggered camera -- it means the camera delivered >1000 frames, so it is free-running. (On main the same abort came from a 16-frame cap and was a writer hiccup; on this branch that reading is gone.)
- The frame table's rollover is in the wrong place: frames 0..19 must all be `cycle=0`, and frame 20 must be `cycle=1 plane=0`. If the firmware instead nests cycle inside plane, the file is confidently mislabelled and SNOUTY will restack it wrong -- this is guide 3.1 and the highest-risk item in the whole session. Capture the file and the firmware settings; the fix is reordering RESOLFT_COUNTERS in _acquisition_layout_source.py, and only the rig can say which order.
- `Detector 'OrcaStraight' stalled: no frames received for Ns` followed by a failed recording. That is the watchdog, not the new stall warning: no frames at all arrived, so the firmware is not pulsing the camera line (wrong CameraTTLChan, or the camera is not in external trigger mode).
- The scan dies at start with `KeyError: '<camera name>'` after uploading five laser TTL parameters. The camera detector has no `Triggerscope/TTL<n>` digitalLine in the setup file; the multicolor mode is the only RESOLFT mode that programs CameraTTLChan, so this fault appears in step 0 and nowhere else.
- `Recording failed: ... Set "Camera used for detection" in the scan panel (no camera is selected)` -- the combo is blank because an older saved selection (a TTL device/laser name) is no longer offered; `findText` returned -1 and cleared it. Re-pick the camera. Not a code fault, but it will stop the session at the first Record.
- Inspector says `source: *-legacy` or `confidence: low`, or the loops read anything other than time/cycle/plane -- the file was written by a different ImSwitch than the one reading it. Re-check the `interpreted by:` line and `git log --oneline -1`.
- `Skipping the scan-position cross-check` or `Skipping the pulses-per-position cross-check` in the log at arm. For this scan neither should ever appear; both mean the layout is not the one this rehearsal produced.

### Only the rig can answer

- The loop ORDER itself (guide 3.1). The layout says time outer, cycle, plane innermost, and every layer downstream now agrees with that statement. Nothing in software can falsify it -- `getNumScanPositions()` and the layout are built from the same table, so the arm-time SCAN_POSITION_COUNT_MISMATCH check is structurally vacuous for this family (the source says so at _acquisition_layout_source.py:603). Only a sample with distinguishable planes/cycles, or a firmware trace, decides it.
- Whether the multicolor firmware emits ONE camera pulse per (cycle, plane) or one per COLOUR. `getNumCamTTL()` declares exactly one, and the layout has no colour/channel loop at all, so the recording is armed for 200 frames. If the firmware reads out once per Laser2/Laser3/multicolor position, the camera delivers ~600 and the run will stop at frame 200 with a large `recording:discarded_frames` and the surplus warning -- i.e. you would record one third of the scan and the file would claim to be complete. This is checkable in one run and I could not settle it from the code.
- Whether 10x20 now completes at the rig's real frame rate and frame size. What was proven here is that the cap is 1000 rather than 16 and that a 1.6 s writer block is survivable; the rig's Orca frames are far larger than the 256x256 used here and its disk is not this one.
- Whether the producer-stall warning fires at all on the rig, and how long the gap is. On main the log showed a ~2 s unattributed gap before the abort; this line is what will attribute it. It cannot be predicted from software -- it is a property of that machine's disk and that camera's data rate.
- Whether the rig's camera detector actually declares `Triggerscope/TTL<n>` in its setup file, and which line the firmware really gates. The repo ships no TriggerScope setup file (example_snouty_smart_modes.json has no triggerScope block and no digital lines), so the reproduction above used a constructed one.
- The plane/cycle step sizes recorded in the layout (`0.05 um`, `0.2 um` here) are whatever the panel holds; that they match the piezo's real travel is a calibration question, not a software one.

---

## Step 1 — plain point scan, then BeadRec

### Run

```bash
# ---- 0. prove which code will run (guide §0) -------------------------------
cd /path/to/the/ImSwitch/checkout/the/rig/runs
git log --oneline -1
git status -sb        # must be clean and not 'behind'; see guide correction 4

# ---- 1. record (GUI) -------------------------------------------------------
# Scan panel: a plain point scan, X fast / Y slow, e.g. 6 x 5 positions,
#   equal X and Y step, no line steps (n_linesteps = 1).
# The camera must be gated exactly ONE pulse per position. Two or more pulses
#   records a 'repeat' loop and BeadRec refuses the file outright (see below);
#   zero pulses is refused at arm with DETECTOR_PULSES_UNDECLARED.
# Recording panel: Scan once, Save on disk, HDF5 (Zarr and OME-TIFF also carry
#   the layout for this step - verified - but HDF5 is what §3.5 asks for).

# ---- 2. inspect (guide §2) -------------------------------------------------
python tools/inspect_acquisition_layout.py <recording>
python tools/inspect_acquisition_layout.py <recording> | tee <recording>.layout.txt
# The tool binds to its own checkout, so it may be run from any directory;
# the 'interpreted by:' line is the proof of which ImSwitch answered.

# ---- 3. read back + BeadRec, from the shell (guide §2 -> DataObj -> BeadRec)
# Run from the checkout root. Add QT_QPA_PLATFORM=offscreen if the rig box has
# no display. This is the ONLY place geometry_source is printed (correction 1).
python - <recording> <<'PY'
import sys
import numpy as np
from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors.beadrec import BeadRecReconstructor

path = sys.argv[1]
names = list(DataObj.getDatasetNames(path))
print("datasets        :", names)
obj = DataObj(path, names[0], path=path)
res = obj.acquisition_layout
print("layout source   :", res.source, "| authoritative:", res.is_authoritative)
print("data shape      :", obj.data.shape)

out = BeadRecReconstructor().process(obj, {"fit_model": "none"})
print("geometry_source :", out.metadata["geometry_source"])
print("scan_dims (x,y) :", out.metadata["scan_dims"])
print("n_frames        :", out.metadata["n_frames"])
print("conditions      :", out.metadata.get("condition_labels"))
print("image shape     :", out.data.shape, out.axis_labels)

cols, rows = out.metadata["scan_dims"]
frames = np.asarray(obj.data, dtype=float)
means = frames.reshape(len(frames), -1).mean(axis=1)
if out.data.shape == (rows, cols):
    diff = np.abs(np.asarray(out.data, float) - means.reshape(rows, cols)).max()
    print("placement check : max|recon - row-major frame means| =", diff)
else:
    print("placement check : skipped (image rescaled for anisotropic pitch)")
PY

# ---- 4. BeadRec in the ImProcess GUI --------------------------------------
# Open the file, pick the 'Bead reconstruction' tool, and:
#   a) leave 'Scan X pixels (0=auto)' and 'Scan Y pixels (0=auto)' at 0,
#      'Full frame' ticked, 'Fit model' = none -> Reconstruct.
#      A correct image with both boxes at 0 IS the GUI proof that the geometry
#      came from the layout: with no layout, 0/0 is an error (quoted below).
#   b) positive control, 5 seconds: type a wrong Scan X/Y (e.g. 5 x 6) and
#      reconstruct again. It must refuse, naming both numbers (quoted below).
#      Then set them back to 0.
# 'Step X' / 'Step Y' in the panel are IGNORED when the recording declares a
# pitch - do not bother typing the scan step there.
```

### Expect

```
ALL OUTPUT BELOW IS REAL, captured on codex/acquisition-layout-schema (HEAD moved
2c2014ae during the rehearsal; output identical before and after) against the
simulated rig: mock Hamamatsu + simulated NI-DAQ + real GalvoScanDesigner +
real AdvancedScanTTLCycleDesigner + real producer accessor + real
RecordingManager/HDF5 storer + real ImProcess resolver and BeadRec.
Rehearsed scan: 6 x 5 positions, 0.5 um step, 1 camera pulse/position, 32x32 frames.
Substitute the rig's own counts; the SHAPE of every line should match.

=== 2. inspector =============================================================

$ python tools/inspect_acquisition_layout.py .../point_scan_Camera.hdf5

/private/tmp/.../out/point_scan_Camera.hdf5
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  source      : explicit-metadata
  confidence  : certain
  provenance  : recorded
  usable      : True   (geometry may be taken from it)
  authoritative: True   (may refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : Camera
  modality    : None    scan source: _PointScanController
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    scan_y       count=5      pitch=0.5 um        device=Y  direction=+1
    scan_x       count=6      pitch=0.5 um        device=X  direction=+1
  traversal:
    scan_y: forward
    scan_x: forward
  issues:
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
  lifecycle   : writer=finalized  outcome=complete  frames=30 of 30  partitions=1 of 1

  first 12 stored frames:
    frame 0     -> scan_y=0  scan_x=0
    frame 1     -> scan_y=0  scan_x=1
    frame 2     -> scan_y=0  scan_x=2
    frame 3     -> scan_y=0  scan_x=3
    frame 4     -> scan_y=0  scan_x=4
    frame 5     -> scan_y=0  scan_x=5
    frame 6     -> scan_y=1  scan_x=0
    frame 7     -> scan_y=1  scan_x=1
    frame 8     -> scan_y=1  scan_x=2
    frame 9     -> scan_y=1  scan_x=3
    frame 10    -> scan_y=1  scan_x=4
    frame 11    -> scan_y=1  scan_x=5

Line-by-line, what the rig's copy must say:
* interpreted by   -> the checkout under test, not a release install.
* source/confidence/provenance -> exactly explicit-metadata / certain / recorded.
  Anything else (ome-ngff, *-legacy, shape-inference, generic-fallback) means the
  file carries NO producer layout and step 1 has not been tested.
* scan source      -> the real controller class name, e.g. ScanControllerPointScan,
  ScanControllerMoNaLISA or TriggerScopeRasterController. '_PointScanController'
  above is the rehearsal's test stub and will never appear on the rig.
* device=X / device=Y -> present only when the producer knows the positioner.
  Verified: the TriggerScope raster panel omits it (build_triggerscope_raster_layouts
  passes directions but not devices), so its loop lines read
    'scan_y       count=5      pitch=0.5 um        direction=+1'
  with no device=. That absence is normal, not a fault.
* pitch            -> the realized step in um; must equal the step typed in the
  panel. Note the HDF5 attr 'element_size_um = [1. 0.15 0.15]' is the CAMERA
  pixel size, not the scan pitch - do not check the pitch from Fiji.
* issues           -> the single LOSSY_OME_TIME_PROJECTION warning is EXPECTED on
  every camera frame-stream recording that has no time loop, because the storer
  writes 'axes = TYX'. Ignore it. Any other issue is new information.
* lifecycle        -> outcome=complete and frames=N of N with N = X*Y.
* frame table      -> scan_x runs 0..(X-1) then scan_y increments. The wrap must
  happen at the rig's X count, not somewhere else.

=== 3. read-back: DataObj + BeadRec ==========================================

$ python - .../point_scan_Camera.hdf5 <<'PY' ... PY
datasets        : ['Camera']
layout source   : explicit-metadata | authoritative: True
data shape      : (30, 32, 32)
geometry_source : layout
scan_dims (x,y) : (6, 5)
n_frames        : 30
conditions      : ('condition_0',)
image shape     : (5, 6) ['Y', 'X']
placement check : max|recon - row-major frame means| = 0.0009765625

* geometry_source MUST be 'layout'. 'manual' means the layout was not used.
* scan_dims is (x, y); image shape is (y, x) - not a transpose.
* conditions is ('condition_0',) for a plain point scan (one unnamed condition).
* placement check: a small nonzero number (float32 rounding of uint16 means) or
  0.0. It is 0.0009765625 here and 0.0 on the same scan saved as OME-TIFF.
* ANISOTROPIC STEP (verified separately, X 0.5 um / Y 1.0 um, 6 x 4 grid):
  scan_dims stays (6, 4) but 'image shape' becomes (8, 6) - BeadRec rescales to
  square pixels from the RECORDED pitch. Correct, not a bug; the placement check
  then prints 'skipped'.

=== the two positive controls (real error text) ==============================

Stale manual entries, same file (this is GUI check 4b):
  ValueError: Scan X/Y is set to 5x6, but this recording declares 6x5. Clear the
  manual entries to use the recorded geometry, or persist a layout override if the
  recording is wrong.

Same file with its AcquisitionLayout attrs stripped (i.e. a pre-branch file),
Scan X/Y left at 0 - this is what proves 0/0 + a picture means 'layout':
  ValueError: BeadRec cannot determine the raster: this source carries no
  acquisition layout and neither Scan X nor Scan Y pixels were set. Enter the scan
  size, or open a recording that carries its acquisition layout.
  (the same file with Scan X = 6 reconstructs, with geometry_source 'manual')

=== the refusal step 1 can walk into (not in the guide) ======================

If the camera is declared with 2 pulses per position, the layout gains a third
loop and the inspector shows:
    scan_y       count=5      pitch=0.5 um        direction=+1
    scan_x       count=3      pitch=0.5 um        direction=+1
    repeat       count=2      pitch=-
    frame 0     -> scan_y=0  scan_x=0  repeat=0
    frame 1     -> scan_y=0  scan_x=0  repeat=1
and BeadRec refuses the file - with Scan X/Y at 0 AND with them set:
  ValueError: BeadRec does not place event loop(s) 'repeat' (kind 'repeat',
  count 2), and does not declare how to fold them. Folding a loop silently mixes
  unrelated frames into one output; place it, or fold it on purpose. Loops this
  consumer places: ['condition', 'scan_x', 'scan_y']; folds: [].
There is no workaround in the panel. Record step 1 with one pulse per position.

=== software state ===========================================================
QT_QPA_PLATFORM=offscreen python -m pytest \
  imswitch/imcontrol/_test/unit/test_acquisition_layout_recording_seam.py -p no:napari -q
  -> 7 passed
QT_QPA_PLATFORM=offscreen python -m pytest \
  imswitch/improcess/_test/test_beadrec_reconstructor.py -p no:napari -q
  -> 16 passed

Rehearsal scripts and every captured .txt:
/private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step1-point-scan-beadrec/
```

### A real fault looks like

- Frame table wraps at the wrong index: scan_x must run 0..(X-1) and only then scan_y increments. A wrap at a different count, or scan_y advancing every frame, means the producer's loop order or img_dims disagrees with what the hardware ran - the exact failure this session exists to catch. Capture the file.
- source is not 'explicit-metadata' / provenance is not 'recorded' / authoritative is False. Seen values on a file WITHOUT a producer layout: 'source: ome-ngff, confidence: medium, authoritative: False' with a single 'time count=30' loop and OME_AXES_WITHOUT_ACQUISITION_LOOPS. On a fresh recording this means the layout gate skipped the detector (check the arm-time log) or the rig is running older code.
- 'interpreted by:' is not the checkout under test. Everything below that line then describes different software.
- pitch on a loop line differs from the step typed into the scan panel, or is '-' (missing). The pitch is the designer's realized pixel size; a mismatch is a scan-designer/calibration fault, and it silently sets the reconstruction's pixel scale. (Do not cross-check it against Fiji's pixel size - that is element_size_um, the camera pixel size.)
- lifecycle says outcome != complete, or frames=N of M with N != M, or the file's recording:discarded_frames > 0. Under the simulated DAQ these are always complete / 30 of 30 / 0, so any deviation is hardware or throughput, not software.
- An event loop appears that is not scan_y/scan_x for a plain point scan - 'repeat' (camera gated more than one pulse per position) or 'condition' (line steps left enabled). Both change what BeadRec will accept; 'repeat' makes it refuse the file with no workaround.
- BeadRec errors at all with Scan X/Y at 0. Each error text names the conflicting numbers - capture the text, it usually locates the cause without the file.
- geometry_source prints 'manual' while both spinboxes are 0. That combination is impossible if a layout was read; it means the layout was rejected as unusable.
- The reconstructed bead image is mirrored or transposed relative to the widefield/eyepiece view WHILE the frame table reads forward. Verified cause: a loop line saying direction=-1. The frame table does not show mirroring - it still prints scan_x=0,1,2 - but BeadRec applies the physical direction, and I confirmed the output is then exactly the column-reversed image (np.fliplr, max deviation 1325 counts vs 0.001 when correct). The direction flags come from isPositiveDirection in the setup file, not from the stage, so a mirrored image is a setup-file question.
- The placement check prints a large max|delta| (hundreds or thousands of counts) instead of ~1e-3 or 0.0 - the frames were not placed in arrival order, i.e. a direction flag, serpentine traversal or a gate is in play that a plain point scan should not have.

### Only the rig can answer

- Whether the camera actually delivers exactly one frame per scan position, in the order the layout declares. In the rehearsal the same designer output drives both the TTL and the layout, so producer and 'hardware' cannot disagree by construction - the agreement I measured is therefore not evidence about the rig.
- Whether stored frame 0 is the scan's first position. A frame left in the camera buffer from live view, or camera arming latency, shifts the whole raster by one and the frame table cannot see it. A recognisable sample (bead grid, known pattern) is the only detector for this.
- Whether scan_y=0 is the physically top row and scan_x increases in the direction you expect at the eyepiece. The direction=+/-1 flags are read from the setup file's isPositiveDirection, never measured from the stage.
- Whether the realized pitch printed by the inspector equals the real um travelled per step (galvo volts-per-um / stage calibration). The layout copies the designer's pixel_sizes, which is a software number all the way down.
- Whether recording:discarded_frames stays 0 with a real camera. Under the simulated DAQ nothing can free-run or double-pulse, so 0 is guaranteed there and means nothing about the rig.
- Whether the reconstruction looks like the sample - the only check that can falsify a self-consistent but wrong layout.
- Throughput: the writer/producer stall warning (§5 step 0) cannot fire on a 30-frame mock scan. A real scan's frame rate is the only thing that exercises it.
- Which class name appears on the 'scan source:' line for the panel the rig actually uses, and whether that panel's producer passes device= for its positioners (the TriggerScope raster panel does not; the point-scan controllers do).

---

## Step 2 — Advanced Scan with two line steps

> A second agent reproducing this card independently found small
> differences; where its account and this one disagree, trust what you
> see at the rig and capture it.

### Run

```bash
# 0. Confirm which code is about to run (guide §0). I rehearsed at fc410fc2; while I worked, 2c2014ae landed on top (TriggerScope-only + a docs edit; it does not touch the Advanced line-step path).
cd <checkout>  &&  git log --oneline -1
# 1. RECORD, in the GUI, from the Advanced Scan widget:
#      X (fast) 18 steps, Y (slow) 18 steps, 2 line steps;
#      the camera's line-step enable ticked for BOTH line steps, 1 pulse each;
#      the two line steps set to visibly different laser power (this is the whole point of the step);
#      Save format HDF5 or Zarr (NOT OME-TIFF); Save mode Disk.
#    While it arms, keep the log. Two lines matter, both should say status=OK:
#      [LineStepDiag][AdvancedScan] requested_S=2 scan_S=2 UI_Ny=18 img_dims=[18, 18] expected_flat_lines=36 line_clock_edges=36 ... status=OK
#      [LineStepDiag][Nidaq] S=2 img_dims=[18, 18] ... line_clock_edges=36 ... status=OK
# 2. Inspect. --frames 40 is REQUIRED: the default of 12 stops inside condition 0 of row 0 and proves nothing.
python tools/inspect_acquisition_layout.py <recording>_Camera.hdf5 --frames 40 | tee <recording>.inspect.txt
# 3. Read it back through DataObj (guide §read-back item 2).
python -c "from imswitch.improcess.model import DataObj; from imswitch.imcommon.model.acquisition_layout import recorded_frame_coordinates as C; p='<recording>_Camera.hdf5'; n=DataObj.getDatasetNames(p); print(n); o=DataObj(p,n[0],path=p); r=o.acquisition_layout; print(r.source, r.confidence, r.is_authoritative); o.checkAndLoadData(); print(o.data.shape); [print(i, C(r.layout,i)) for i in (0,17,18,35,36,o.data.shape[0]-1)]"
# 4. Falsify the recorded condition order against the raw pixels. THIS is the hardware check.
python condition_contrast.py <recording>_Camera.hdf5
# 5. Reconstruct in ImProcess with the MoNaLISA reconstructor, method 'Fast Gauss MoNaLISA' (the default).
#    Expect a 6D result (1, 1, 2, 1, 54, 54) and T=0/T=1 = the two line steps.
# 6. Repeat 1-5 once at 3x2 with 2 line steps (guide §3.2's fixture size) - 12 frames, instant, same shape of answer.
# Helper scripts I wrote for 4 and for the classic-method placement check (copy into the checkout root, run from there):
#   /private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step2-advanced-line-steps/condition_contrast.py
#   /private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step2-advanced-line-steps/classic_placement.py
```

### Expect

```
ALL OUTPUT BELOW IS REAL, captured from the simulated rig (mock Hamamatsu, simulated
NI-DAQ, real GalvoScanDesigner + AdvancedScanTTLCycleDesigner, real
build_advanced_scan_layouts, real RecordingManager gate + HDF5 storer, real ImProcess
resolver and MoNaLISA reconstructor) in the worktree at fc410fc2.
Files: .../scratchpad/rehearse-step2-advanced-line-steps/files/advanced_18x18x2_Camera.hdf5
       .../files/advanced_3x2x2_Camera.hdf5
       .../files/synthetic_contrast_Camera.hdf5   (same layout, pixels replaced so the
                                                   two line steps are 4x apart)

=== 2. INSPECTOR, 18x18 with 2 line steps (--frames 40) ===

  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  source      : explicit-metadata
  confidence  : certain
  provenance  : recorded
  usable      : True   (geometry may be taken from it)
  authoritative: True   (may refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : Camera
  modality    : None    scan source: ScanControllerAdvanced
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    scan_y       count=18     pitch=1.0 um        device=Y  direction=+1
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_x       count=18     pitch=1.0 um        device=X  direction=+1
  traversal:
    scan_y: forward
    condition: forward
    scan_x: forward
  issues:
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
  lifecycle   : writer=finalized  outcome=complete  frames=648 of 648  partitions=1 of 1

  first 40 stored frames:
    frame 0     -> scan_y=0  condition=0  scan_x=0
    frame 1     -> scan_y=0  condition=0  scan_x=1
    ...
    frame 17    -> scan_y=0  condition=0  scan_x=17
    frame 18    -> scan_y=0  condition=1  scan_x=0
    ...
    frame 35    -> scan_y=0  condition=1  scan_x=17
    frame 36    -> scan_y=1  condition=0  scan_x=0
    frame 37    -> scan_y=1  condition=0  scan_x=1
    frame 38    -> scan_y=1  condition=0  scan_x=2
    frame 39    -> scan_y=1  condition=0  scan_x=3

This is character-for-character what guide section 2 predicts for the 18x18 case, so the
producer's chronology (scan_y -> condition -> scan_x) is confirmed in software.

  IN TERMS OF THE WIDTH W (= the fast-axis count, 18 above): condition flips every W
  frames, scan_y advances every 2*W frames. Frame index n maps to
  scan_y = n // (2*W),  condition = (n // W) % 2,  scan_x = n % W.
  The LOSSY_OME_TIME_PROJECTION warning is NORMAL and appears on every HDF5/Zarr scan
  recording: the container labels its leading axis 'T' (dataset attr axes = TYX) for OME
  interop while the layout calls it 'frame'. Severity warning; blocks nothing.

=== 2b. INSPECTOR, the 3x2 x 2-line-step fixture size (guide 3.2) ===

  event loops (outermost first):
    scan_y       count=2      pitch=1.0 um        device=Y  direction=+1
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_x       count=3      pitch=1.0 um        device=X  direction=+1
  lifecycle   : writer=finalized  outcome=complete  frames=12 of 12  partitions=1 of 1

  first 12 stored frames:
    frame 0     -> scan_y=0  condition=0  scan_x=0
    frame 1     -> scan_y=0  condition=0  scan_x=1
    frame 2     -> scan_y=0  condition=0  scan_x=2
    frame 3     -> scan_y=0  condition=1  scan_x=0
    frame 4     -> scan_y=0  condition=1  scan_x=1
    frame 5     -> scan_y=0  condition=1  scan_x=2
    frame 6     -> scan_y=1  condition=0  scan_x=0
    frame 7     -> scan_y=1  condition=0  scan_x=1
    frame 8     -> scan_y=1  condition=0  scan_x=2
    frame 9     -> scan_y=1  condition=1  scan_x=0
    frame 10    -> scan_y=1  condition=1  scan_x=1
    frame 11    -> scan_y=1  condition=1  scan_x=2

  = guide 3.2's "frames 0..2 are condition 0 of row 0, frames 3..5 are condition 1 of the
  same row", exactly.

=== 3. DataObj read-back ===

getDatasetNames  : ['Camera']
source           : explicit-metadata
confidence       : certain
authoritative    : True
loops            : [('scan_y', 18), ('condition', 2), ('scan_x', 18)]
scan_source      : ScanControllerAdvanced
data shape       : (648, 32, 32) uint16
  frame 0    -> {'scan_y': 0, 'condition': 0, 'scan_x': 0}
  frame 17   -> {'scan_y': 0, 'condition': 0, 'scan_x': 17}
  frame 18   -> {'scan_y': 0, 'condition': 1, 'scan_x': 0}
  frame 35   -> {'scan_y': 0, 'condition': 1, 'scan_x': 17}
  frame 36   -> {'scan_y': 1, 'condition': 0, 'scan_x': 0}
  frame 647  -> {'scan_y': 17, 'condition': 1, 'scan_x': 17}

Dataset attrs worth reading straight off the file (h5py, Camera/data):
  recording:expected_frames = 648      recording:actual_frames = 648
  recording:discarded_frames = 0       recording:completion_outcome = complete
  recording:planned_partitions = 1     axes = TYX

=== 4. condition_contrast.py, on the mock camera (no laser) ===

648 frames, fast axis 18, 2 conditions
per-frame mean, first 2 rows worth of frames:
  frames    0..17   condition=0  mean=   1692.95  sd=   30.25
  frames   18..35   condition=1  mean=   1699.32  sd=   28.18
  frames   36..53   condition=0  mean=   1697.54  sd=   33.38
  frames   54..71   condition=1  mean=   1697.32  sd=   25.45
condition separation 4.32 against a frame-to-frame spread of 30.69
  -> the two line steps are NOT distinguishable in this data; this check cannot confirm
     or refute the recorded order.

=== 4b. the SAME check on data where the two line steps really do differ (4x) ===
     (this is what the rig should look like with two laser powers)

  frames    0..17   condition=0  mean=    136.83  sd=    0.00
  frames   18..35   condition=1  mean=    248.48  sd=    0.00
  frames   36..53   condition=0  mean=    136.83  sd=    0.00
  frames   54..71   condition=1  mean=    248.48  sd=    0.00
condition separation 111.64 against a frame-to-frame spread of 55.82
  -> the two line steps are distinguishable; the block boundaries above must fall exactly
     on multiples of the fast-axis count.

=== 5. MoNaLISA, method 'Fast Gauss MoNaLISA' (the widget default) ===

scan dialog pre-fill from the layout:
  dimensions       ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints']
  directions       ['pos', 'pos', 'pos']
  steps            ['18', '18', '1', '2']
  step_sizes       ['1000.0', '1000.0', '1', '1']
  n_linesteps      2
  unidirectional   True

geometry the fast path derives from the layout:
  frames_per_stack   648
  num_timepoints     1
  n_linesteps        2
  scan_params.steps  [18, 18, 1, 2]

INFO [MonalisaLiveSession] Scan geometry: nx_s=18, ny_s=18, linesteps=2, frames_per_stack=648, timepoints=1
INFO [MonalisaLiveSession] Allocated output buffer: shape (1, 1, 2, 1, 54, 54), dtype float32
INFO [MonalisaReconstructor] Fast Gauss reconstruction complete: shape (1, 1, 2, 1, 54, 54)
  result shape (dataset, base, T, Z, Y, X): (1, 1, 2, 1, 54, 54)
  output pixel size nm: (1000.0, 1000.0)

  -> IT RECONSTRUCTS. Fast Gauss does NOT refuse a fully-enabled interleaved line-step
     scan (see guide correction 1). The output T axis is the condition axis: T = 2 = 1
     timepoint x 2 conditions. Y/X = 54 = 18 scan positions x a 3x3 focus grid; on the rig
     the grid is nx_c x ny_c from the pattern period, so expect 18 * nx_c.

  On the 4x-contrast data the per-T means came out [292.78, 1170.76] - ratio 3.999 against
  an injected amplitude ratio of 4.0. CONDITION 0 LANDS IN T=0, CONDITION 1 IN T=1, and the
  reconstruction preserves the ratio of the two line steps' brightness with the constant
  background removed.

  On the 3x2 fixture: result shape (1, 1, 2, 1, 6, 9), scan_params.steps [3, 2, 1, 2].

=== 5b. MoNaLISA, method 'MoNaLISA' (classic) ===

Cannot run off Windows:
  RuntimeError: SignalExtractor initialization failed: This module does unfortunately
  currently not support non-Windows operating systems.. MoNaLISA reconstruction requires
  Windows + CUDA libraries.
The rig is Windows, so it will run there. Everything the layout work changed in that path
is platform independent and I drove it directly (classic_placement.py):

interleaved per line : True
rows x cols          : 18 x 18
conditions           : 2  timepoints: 1  output T axis: 2
condition labels     : ('condition_0', 'condition_1')
slots                : 648 for 648 stored frames
  frame 0  -> (0, 0, 0, 0) (t, z, y, x)
  frame 17 -> (0, 0, 0, 17)
  frame 18 -> (1, 0, 0, 0)
  frame 36 -> (0, 0, 1, 0)
  frame -1 -> (1, 0, 17, 17)
assembled image shape: (2, 1, 54, 54) (T, Z, rows*grid, cols*grid)
frames landing where the layout says: 648 / 648
output T=0: stored frame per scan position (first 3 rows x 6 cols)
[[ 0  1  2  3  4  5]
 [36 37 38 39 40 41]
 [72 73 74 75 76 77]]
output T=1: stored frame per scan position (first 3 rows x 6 cols)
[[18 19 20 21 22 23]
 [54 55 56 57 58 59]
 [90 91 92 93 94 95]]

  -> condition 0 is frames 0..17, 36..53, 72..89 (the FIRST line of every pair), never the
     first 324 frames. That is the motivating bug, not reproduced.

=== EXACT TEXT OF THE REFUSALS THIS STEP CAN TRIP ===

Camera with no line-step enable mask, on a 2-line-step scan (blocked in the producer,
before any file is written):
  ValueError: This scan has 2 line steps, but detector 'Camera' has no line-step enable
  mask or pulse count, so the order of its frames cannot be described. Enable the
  detector's TTL for this scan, or record it with a single-line-step scan.

Camera enabled on ONE line step only (mask [True, False]) - the layout is fine and records
324 frames with recorded_event_spans = (RecordedEventSpan(start=0, count=18, stride=1,
period=36, repeats=18),), but Fast Gauss then refuses:
  ValueError: Fast Gauss MoNaLISA reassembles contiguous X/Y stacks and cannot represent a
  detector gated to part of the scan. Use the MoNaLISA reconstruction method, which places
  every frame by its recorded coordinate.

Frame count disagreeing with the layout (e.g. a truncated file):
  ValueError: The acquisition layout records 648 frames but signal extraction produced 647.
  Frames are never dropped or padded to fit a scan shape.
  ValueError: The acquisition layout records 648 frames but this source has 647.

Arm-time gate, if recFrames is given as the FRAME count rather than the POSITION count:
  AcquisitionLayoutError: Invalid acquisition layout for 'Camera'
  [error] SCAN_POSITION_COUNT_MISMATCH: Layout describes 324 scan positions but recFrames
  is 648
(The GUI passes positions; this is what a wrong pulses-per-position declaration looks like
from the other side, and it is also what DETECTOR_PULSE_COUNT_MISMATCH guards.)
```

### A real fault looks like

- The frame table's condition does NOT flip every W frames. Any other period is a real fault: 2*W would mean the hardware runs both line steps per position rather than per line; W*ROWS would mean two contiguous half-scans (the motivating bug, and then the producer's declared chronology is wrong); a flip at 1 would mean the conditions alternate per pixel.
- condition_contrast.py reports the two line steps as distinguishable but the mean-intensity blocks do NOT start on multiples of the fast-axis count, or are offset by a constant. A half-block offset means the hardware starts recording one line step out of phase with what the producer declares; that is exactly the case no software test can catch, and it is the reason this step exists.
- condition_contrast.py's per-condition means are equal (separation below the frame-to-frame spread) even though the two line steps were set to different laser power. Either the line-step powers are not actually being applied, or the frames are being labelled with the wrong condition - both are real, and the second one silently invalidates every reconstruction from that rig.
- recording:discarded_frames is non-zero in the file (read it from the dataset attrs; the inspector does not print it). A real camera that free-runs or double-triggers produces surplus frames the worker drops, with a one-per-detector warning in the log. The simulated run had 0.
- 'lifecycle: ... outcome=complete frames=N of N' does not hold - frames < planned means the recording stopped early (guide step 7's path, not this one), frames > planned cannot happen and would print '(N frame(s) beyond the ... layout describes are stored and unaccounted for)'.
- [LineStepDiag][AdvancedScan] or [LineStepDiag][Nidaq] logs status=MISMATCH. For an 18x18 two-line-step scan both should say line_clock_edges=36 = ROWS * n_linesteps (4 for the 3x2 fixture). 'line_edges=X!=expected=Y' means the designers and the delivered line clock disagree about how many expanded lines the scan has, which is upstream of everything above.
- The reconstruction's T axis has the wrong length. It must be n_timepoints * n_conditions = 2 here. A T of 1 means the condition loop was lost; a T of 324 or 648 means the frames were flattened into time - the pre-branch failure mode.
- 'Detected scan orientation ... disagrees with the recorded layout on axis x/y ... using the detected orientation'. The fast path OVERRIDES the layout's direction with a total-variation heuristic. On a featureless sample this fires spuriously (it did on the mock camera's noise, and vanished as soon as the data had real structure). On a structured sample it means either isPositiveDirection in the setup file is wrong or the detection is - and the classic method, which honours the layout, will then produce a mirrored image from the same file. Capture the file if it fires on a sample with visible structure.
- A refusal fires on a recording you believe is valid, with any of the exact texts quoted above. Capture the file and the inspector output rather than working around it.
- COSMETIC, not faults: the LOSSY_OME_TIME_PROJECTION warning (present on every HDF5/Zarr scan recording); 'modality: None'; negative values and negative display levels in the reconstruction (the fast path subtracts a background fit); the mock camera's 'ctypes has no attribute windll' line, which cannot appear on the rig.

### Only the rig can answer

- Whether the camera actually delivers one frame per TTL pulse, in the order the pulses were issued. The simulated DAQ hands the mock camera exactly the edges the designer generated, so delivery order is assumed, never observed. This is the single assumption the whole step exists to test, and only the intensity-block check in condition_contrast.py against genuinely different line steps can falsify it.
- Whether the camera can keep up. Two line steps at a 1 ms sequence time is 2 frames per millisecond per position; a real camera with a longer exposure or readout will drop or merge frames. In simulation that never shows. recording:discarded_frames and the producer-stall warning are the rig-side symptoms.
- Whether the two line steps really carry different illumination. I had to synthesise that (make_distinguishable.py) because the mock camera has no laser; the mock's two conditions are statistically identical, so the software run cannot confirm or refute the order at all - only the shape of the answer.
- Whether the layout's direction=+1 on scan_x/scan_y is true. It is copied from isPositiveDirection in the setup file, i.e. a configuration claim, never a measurement. The fast path's TV auto-detect is the only thing that ever contradicts it, and when it does it wins silently over the layout, so the classic and fast methods can disagree about the same file. A sample with a recognisable asymmetry settles it.
- The classic 'MoNaLISA' reconstruction method end to end. SignalExtractor is Windows+CUDA only and refuses to initialise here, so I could only drive its placement half (which is exact: 648/648 frames land where the layout says). The extraction itself, and whether the classic and fast methods produce the same orientation on the same file, are rig questions.
- Whether the pattern period/offset the operator uses gives a sensible focus grid. My 32x32 mock frames with the default 11.05 px period gave a 3x3 grid; the real frame size and pattern determine the output size (18 * nx_c), and a wrong pattern makes the reconstruction look wrong for reasons that have nothing to do with the layout.

---

## Step 3 — complementary detectors, and multiple pulses per position

> A second agent reproducing this card independently found small
> differences; where its account and this one disagree, trust what you
> see at the rig and capture it.

### Run

```bash
# 0. Confirm the code under test (guide §0). Ran against head 2c2014ae, clean tree.
cd /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout && git log --oneline -1
# 1. GUI, recording A (complementary). Scan panel = Advanced Scan.
#    - '#Line repeats:' = 2   (this is n_linesteps)
#    - tick 'Line program devices' to reveal the per-line-step enable matrix
#    - camera A row: tick line step 1 only.  camera B row: tick line step 2 only.
#    - tick 'Advanced Line Program', mode 'Timing windows'; for each camera set
#      ONE pulse window inside the dwell on its own line step (Device: / Line step:
#      selectors, start/end in ms; must end before the dwell so each pixel gives a
#      clean rising edge).
#    - keep the scan small: 3 x 2 positions is enough, 6 x 4 if you want more frames.
#    - Recording widget: select BOTH cameras, 'Scan once', HDF5 or Zarr (NOT OME-TIFF), Record.
# 2. Inspect both files. Save the output next to each file.
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <REC>_<CamA>.hdf5 --frames 14
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <REC>_<CamB>.hdf5 --frames 14
# 3. The one number the inspector does NOT print: discarded_frames.
QT_QPA_PLATFORM=offscreen python -c "import h5py,sys; d=h5py.File(sys.argv[1])[sys.argv[2]]['data']; [print(k,'=',d.attrs[k]) for k in sorted(d.attrs) if k.startswith('recording:')]" <REC>_<CamA>.hdf5 <CamA>
QT_QPA_PLATFORM=offscreen python -c "import h5py,sys; d=h5py.File(sys.argv[1])[sys.argv[2]]['data']; [print(k,'=',d.attrs[k]) for k in sorted(d.attrs) if k.startswith('recording:')]" <REC>_<CamB>.hdf5 <CamB>
# 4. Reconstruct each file, in ImProcess. For a bead/known-pattern sample use
#    'Bead reconstruction' with Scan X and Scan Y left at 0 (so the layout wins).
#    For MoNaLISA use the STANDARD method, not Fast Gauss (see expected output).
#    Check the result metadata names the ONE condition that detector recorded.
# 5. GUI, recording B (multi-pulse). Same scan, but give camera B TWO pulse
#    windows on its line step (e.g. 0.0-0.4 ms and 0.6-0.9 ms in a 1 ms dwell).
#    Record both cameras again to a new name.
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <REC2>_<CamB>.hdf5 --frames 12
# 6. The refusal the operator will meet (guide §3.4). Untick EVERY line step for
#    camera B, leave it selected in the Recording widget, and press Record.
#    Expect a refusal before any file is written; capture the exact text.
# 7. To reproduce this reference card on the simulated rig at any time:
cd /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout && QT_QPA_PLATFORM=offscreen python /private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step3-complementary-detectors/rehearse.py /tmp/step3 && QT_QPA_PLATFORM=offscreen python /private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step3-complementary-detectors/beadrec.py /tmp/step3
```

### Expect

```
All of this is real output from the simulated rig (two mock Hamamatsu cameras
CamA/CamB, simulated NI-DAQ, real Galvo + AdvancedScan TTL designers, real
producer, gate, worker, HDF5 storer, real ImProcess resolver and
reconstructors), worktree Imswitch2-acquisition-layout at head 2c2014ae.
Scan: 3 x 2 positions, 2 line steps, CamA on step 1, CamB on step 2.

=================================================================
A. RECORDING A - complementary. What the producer built and ran
=================================================================
scan            : 3 x 2 positions, n_linesteps=2
per-condition pulses (producer) : {'CamA': (1, 1), 'CamB': (1, 1)}
getNumCamTTL()  : {'CamA': 1, 'CamB': 1}
TTL rising edges in the full scan signal: {'CamA': 6, 'CamB': 6, 'line_clock': 4, 'frame_start_clock': 1, 'frame_end_clock': 1}
validate_detector_edge_counts: OK (layout == generated TTL edges)
wrote: ['complementary_CamA.hdf5', 'complementary_CamB.hdf5']

=================================================================
B. INSPECTOR - complementary_CamA.hdf5  (the reference frame table)
=================================================================
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  source      : explicit-metadata
  confidence  : certain
  provenance  : recorded
  usable      : True   (geometry may be taken from it)
  authoritative: True   (may refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : CamA
  modality    : None    scan source: ScanControllerAdvanced
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    scan_y       count=2      pitch=1.0 um        device=Y  direction=+1
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_x       count=3      pitch=1.0 um        device=X  direction=+1
  traversal:
    scan_y: forward
    condition: forward
    scan_x: forward
  detector gating: 1 recorded span(s)
    start=0 count=3 stride=1 period=6 repeats=2
  issues:
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
  lifecycle   : writer=finalized  outcome=complete  frames=6 of 6  partitions=1 of 1

  first 6 stored frames:
    frame 0     -> scan_y=0  condition=0  scan_x=0
    frame 1     -> scan_y=0  condition=0  scan_x=1
    frame 2     -> scan_y=0  condition=0  scan_x=2
    frame 3     -> scan_y=1  condition=0  scan_x=0
    frame 4     -> scan_y=1  condition=0  scan_x=1
    frame 5     -> scan_y=1  condition=0  scan_x=2

complementary_CamB.hdf5 is identical except for these two blocks:
  detector gating: 1 recorded span(s)
    start=3 count=3 stride=1 period=6 repeats=2
  first 6 stored frames:
    frame 0     -> scan_y=0  condition=1  scan_x=0
    ...
    frame 5     -> scan_y=1  condition=1  scan_x=2

Read the span line as: frames = count x repeats. 3 x 2 = 6, and 6 = the
lifecycle "frames=6 of 6". CamA starts at event 0 (condition 0), CamB starts
at event 3 (condition 1) -- that offset IS the complementarity, and every
frame of each file carries a single, constant condition index.
The LOSSY_OME_TIME_PROJECTION warning is on every HDF5 recording this branch
writes, gated or not (verified against a plain ungated point scan); it is not
a step-3 signal.

=================================================================
C. THE FILE ITSELF (h5py) - both detectors, recording A
=================================================================
recording:actual_frames = 6
recording:actual_partitions = 1
recording:completion_outcome = b'complete'
recording:dataset_path = /CamB/data
recording:detector_name = CamB
recording:discarded_frames = 0
recording:expected_frames = 6
recording:frames_per_stack = 6
recording:lapse_index = 0
recording:num_timepoints = 1
recording:planned_frames = 6
recording:planned_partitions = 1
recording:single_lapse_file = False
recording:source_format = HDF5

DataObj.getDatasetNames -> ['CamA'] / ['CamB']; dataset shape (6, 32, 32); axes 'TYX'.

=================================================================
D. RECONSTRUCTION - "each reconstructs only its own condition"
=================================================================
Bead reconstruction (Scan X/Y left at 0):

  ===== complementary_CamA.hdf5 =====
    data shape   : (2, 3)   axes ['Y', 'X']
    metadata     : {'scan_dims': (3, 2), 'n_frames': 6, 'geometry_source': 'layout', 'condition_labels': ('condition_0',)}

  ===== complementary_CamB.hdf5 =====
    data shape   : (2, 3)   axes ['Y', 'X']
    metadata     : {'scan_dims': (3, 2), 'n_frames': 6, 'geometry_source': 'layout', 'condition_labels': ('condition_1',)}

condition_labels is the check: ('condition_0',) for the step-1 camera,
('condition_1',) for the step-2 camera, one dense 3x2 raster each,
geometry_source 'layout'. No Condition axis appears because each detector
recorded exactly one condition.

MoNaLISA, same two files:
  classic MoNaLISA : OK, 6 slots, output T=2, this file fills T=[0]   (CamA)
  classic MoNaLISA : OK, 6 slots, output T=2, this file fills T=[1]   (CamB)
  Fast Gauss REFUSED: ValueError: Fast Gauss MoNaLISA reassembles contiguous X/Y stacks and cannot represent a detector gated to part of the scan. Use the MoNaLISA reconstruction method, which places every frame by its recorded coordinate.

That Fast Gauss error is EXPECTED for every gated detector and is not a fault.
The classic method keeps a 2-slot T axis (one per condition of the scan) and
fills only this detector's slot; the other slot is empty by design.

=================================================================
E. RECORDING B - one detector with two pulses per position
=================================================================
per-condition pulses (producer) : {'CamA': (1, 1), 'CamB': (1, 2)}
getNumCamTTL()  : {'CamA': 1, 'CamB': 2}
TTL rising edges in the full scan signal: {'CamA': 6, 'CamB': 12, ...}

inspector, multipulse_CamB.hdf5 (CamA is byte-identical in shape to recording A):
  event loops (outermost first):
    scan_y       count=2      pitch=1.0 um        device=Y  direction=+1
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_x       count=3      pitch=1.0 um        device=X  direction=+1
    repeat       count=2      pitch=-
  detector gating: 1 recorded span(s)
    start=6 count=6 stride=1 period=12 repeats=2
  lifecycle   : writer=finalized  outcome=complete  frames=12 of 12  partitions=1 of 1

  first 12 stored frames:
    frame 0     -> scan_y=0  condition=1  scan_x=0  repeat=0
    frame 1     -> scan_y=0  condition=1  scan_x=0  repeat=1
    frame 2     -> scan_y=0  condition=1  scan_x=1  repeat=0
    frame 3     -> scan_y=0  condition=1  scan_x=1  repeat=1
    frame 4     -> scan_y=0  condition=1  scan_x=2  repeat=0
    frame 5     -> scan_y=0  condition=1  scan_x=2  repeat=1
    frame 6     -> scan_y=1  condition=1  scan_x=0  repeat=0
    frame 7     -> scan_y=1  condition=1  scan_x=0  repeat=1
    frame 8     -> scan_y=1  condition=1  scan_x=1  repeat=0
    frame 9     -> scan_y=1  condition=1  scan_x=1  repeat=1
    frame 10    -> scan_y=1  condition=1  scan_x=2  repeat=0
    frame 11    -> scan_y=1  condition=1  scan_x=2  repeat=1

The 'repeat' loop is the whole point: the two pulses at one position are
adjacent frames, repeat=0 then repeat=1, and the position advances only after
both. 6 count x 2 repeats = 12 = the lifecycle count.

RECONSTRUCTION OF A MULTI-PULSE FILE IS REFUSED BY EVERY SCAN RECONSTRUCTOR:
  BeadRec REFUSED: ValueError: BeadRec does not place event loop(s) 'repeat' (kind 'repeat', count 2), and does not declare how to fold them. Folding a loop silently mixes unrelated frames into one output; place it, or fold it on purpose. Loops this consumer places: ['condition', 'scan_x', 'scan_y']; folds: [].
  classic MoNaLISA REFUSED: ValueError: MoNaLISA placement does not place event loop(s) 'repeat' (kind 'repeat', count 2), and does not declare how to fold them. ... Loops this consumer places: ['condition', 'scan_x', 'scan_y', 'scan_z', 'time']; folds: [].
  Fast Gauss REFUSED: (the same UnconsumedLoopError text)
This is deliberate (it replaces the old behaviour where the second pulse
silently overwrote the first). 'View only' still opens the file. Only the SMLM
localizer folds 'repeat'. So for the multi-pulse detector the step-3 check is
the frame table, not a reconstruction.

=================================================================
F. THE REFUSAL (guide §3.4). Three different texts, all before any file
=================================================================
(a) Advanced Scan, 2 line steps, detector with no line-step tick and no pulses
    -- this is what unticking every line step for one camera produces:
    "This scan has 2 line steps, but detector 'CamB' has no line-step enable
     mask or pulse count, so the order of its frames cannot be described.
     Enable the detector's TTL for this scan, or record it with a
     single-line-step scan."

(b) 1 line step (or a plain point scan / MoNaLISA panel), ungated detector:
    "Detector 'CamB' is selected for this scan recording, but the scan does not
     gate it: getNumCamTTL() declares no pulse per position for it. Recording
     an ungated detector in scan mode would label free-running frames as scan
     positions. Deselect it, gate it in the scan's TTL cycle, or record it in a
     non-scan mode."

(c) the recording gate's own code, reached only when a layout with pulses
    arrives while numCamTTL has no entry (a producer/gate disagreement, and on
    the TriggerScope pLS-RESOLFT / galvo-detection panels):
    AcquisitionLayoutError: Invalid acquisition layout for 'CamB'
      [error] DETECTOR_PULSES_UNDECLARED: The scan declares no TTL pulse per
      position for detector 'CamB' (getNumCamTTL has no entry), so its frames
      cannot be tied to scan positions. Gate it in the scan, deselect it, or
      record it in a non-scan mode.
    writer opened: False   file written: False

(d) a gated detector whose pulses are declared as zero:
    "Detector 'CamA' is gated by this scan but receives no TTL pulse per
     position (0); nothing would be recorded for it. Give it a pulse in the TTL
     cycle or deselect it."

On the Advanced / PointScan / MoNaLISA panels the operator sees (a) or (b),
NOT the string DETECTOR_PULSES_UNDECLARED -- the producer refuses while
RecordingController is reading the scan geometry, before startRecording is
called. All four are refusals at arm; nothing is written in any of them.

=================================================================
G. WHAT A REAL FAULT LOOKS LIKE (deliberately mis-declared runs)
=================================================================
Over-triggered (scan really pulses CamB twice per position, everything declares
once), 6 x 4 scan, 48 edges delivered against 24 planned:
  WARNING [RecordingWorker] Detector 'CamB' delivered 3 frame(s) beyond the 24
  planned for this recording; they are not written. The recording is not the
  clean scan its metadata describes -- check numCamTTL and the camera trigger mode.
  recording:discarded_frames = 3
  recording:actual_frames = 24
  recording:completion_outcome = b'complete'
and the inspector adds one line under lifecycle:
    [warning] DISCARDED_SURPLUS_FRAMES: 3 frame(s) arrived beyond the 24 this
    recording planned and were not written. The detector produced more than the
    scan accounted for, so the frames that were kept may not be the ones the
    layout describes.
NOTE: 24 surplus pulses produced a count of 3, and the SAME fault on the small
3 x 2 scan produced recording:discarded_frames = 0 with outcome 'complete' and
a frame table that looks perfect. The counter only sees a surplus that lands in
the same buffer read as the last planned frame; once the plan is met the worker
stops reading. discarded_frames > 0 proves a fault, discarded_frames == 0
proves nothing.

Under-triggered (everything declares two pulses, the scan delivers one) -- the
recording fails instead:
  ERROR [RecordingWorker] Detector 'CamB' stalled: no frames received for 2.0s
  (timeout: 2.0s). Current: 6 frames, expected: 12 frames. Check camera
  triggering and numCamTTL configuration.
and the aborted recording leaves no file.

Note also that the producer's own cross-check catches both cases when the
mismatch is in the GENERATED signal:
  "Advanced detector 'CamB' layout selects 6 frame(s), but the final TTL signal
   has 12 rising edge(s)"
It cannot catch a camera that is triggered by something other than the
generated TTL -- which is exactly the class of fault this rig step exists for.
```

### A real fault looks like

- The frame table's condition index is not constant down one detector's file. Each of the two complementary detectors must show ONE condition value on every frame (condition=0 for the step-1 camera, condition=1 for the step-2 camera). A file that alternates condition values, or whose two detectors show the SAME condition, means the hardware did not gate the line steps the way the TTL program said.
- The two detectors' recorded spans are not offset by one condition. CamA must read 'start=0', CamB 'start=3' on a 3-wide scan (start = x_count for the second condition). Same start for both = both cameras fired on the same line step.
- count x repeats on the 'detector gating' line does not equal the lifecycle 'frames=N of N'. That arithmetic is the guide's 'frame count matches its recorded spans' check, and it is done for you by the lifecycle line only if they agree.
- recording:discarded_frames is non-zero, or the inspector prints DISCARDED_SURPLUS_FRAMES. The detector was pulsed more often than declared, or free-ran. Real, and the count is a lower bound on how many extra frames there were.
- A stall error naming the detector and two counts: "Detector 'X' stalled: no frames received for Ns ... Current: 6 frames, expected: 12 frames." That is the under-triggered case -- the camera is not receiving the pulses the scan declared. The recording aborts and leaves no file.
- The recording completes but the two cameras' frame counts are not what the TTL program implies (e.g. both 6 when one was given two pulses per position). Compare recording:actual_frames per file against pulses-per-position x positions.
- Bead reconstruction reports condition_labels containing BOTH conditions for a single-gated detector, or geometry_source 'manual' instead of 'layout'. Either means the layout was not consulted or the gating did not reach the file.
- The image reconstructed from the step-2 camera is spatially offset by one position from the step-1 camera's, or the two are transposed relative to each other, on a sample with known structure. Frame counts and layouts can be self-consistent and still describe the wrong physical row order -- this is the only check that can catch that, and it needs a recognisable sample.
- NOT a fault: 'LOSSY_OME_TIME_PROJECTION' on every file (it is on every HDF5 recording this branch writes). NOT a fault: Fast Gauss MoNaLISA refusing a gated detector. NOT a fault: BeadRec/MoNaLISA refusing the multi-pulse file with UnconsumedLoopError on the 'repeat' loop. NOT a fault: the classic MoNaLISA output having an empty second T slot.

### Only the rig can answer

- Whether the camera actually gated on the line step the TTL program assigned it. Everything above is cross-checked against the GENERATED signal; the simulated rig delivers exactly one frame per generated rising edge by construction, so it cannot disagree. Only a sample with visible structure, plus visibly different line steps (different laser, different power, different exposure), can show that camera A really saw line step 1 and camera B line step 2 and not the other way round.
- Whether the second pulse of a two-pulse position lands on the SAME physical position as the first, i.e. whether the stage is still parked when the second pulse fires. The layout asserts repeat=0 and repeat=1 are one position; that is a dwell-timing fact of the galvo and the camera's readout, not something software can check.
- Whether a camera is triggered by anything other than the generated TTL (free-running, an external trigger source, a stale trigger mode). The producer's edge cross-check only compares the layout with the signal ImSwitch generated. The only in-file evidence is recording:discarded_frames, and that is a lower bound -- it saw 3 of 24 surplus frames in one rehearsal run and 0 of 6 in another.
- The real value of recording:discarded_frames under rig timing. It depends on how frames batch into one buffer read, which depends on camera readout rate, chunk sizes and writer speed -- none of which the simulator reproduces.
- Whether the camera's actual frame-arrival rate keeps up when one detector is pulsed twice per position. The producer-stall warning (guide §5 step 0) is the thing to watch; it never fired in simulation.
- Whether the per-line-step pulse windows the operator types in the widget (ms, inside the dwell) survive the real camera's minimum exposure and trigger-to-readout delay. In simulation any window inside the dwell yields one clean edge per pixel.

---

## Step 6 — scan lapse, both storage modes

> A second agent reproducing this card independently found small
> differences; where its account and this one disagree, trust what you
> see at the rig and capture it.

### Run

```bash
cd /path/to/the/checkout/ImSwitch/is/run/from   # guide §0
git log --oneline -1   # write this down; compare with the commit you mean to test
# --- RUN A: single-file lapse -------------------------------------------------
# UI: Recording widget -> radio 'Timelapse scan'; timepoints = 2; Freq [s] = 0;
# UI: TICK 'Save all timepoints in a single file'; File format: HDF5;
# UI: Rec save mode: 'Save on disk'; file name base: lapseA; (multi-scanner rig: pick a Scan source) -> REC
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseA_<Detector>.hdf5 | tee <recdir>/lapseA.inspect.txt
python -c "import h5py,sys; f=h5py.File(sys.argv[1]); f.visit(print)" <recdir>/lapseA_<Detector>.hdf5
QT_QPA_PLATFORM=offscreen python -c "import sys; sys.path.insert(0,'.');\nfrom imswitch.improcess.model import DataObj\np = sys.argv[1]\nfor n in DataObj.getDatasetNames(p):\n    o = DataObj(p, n, path=p)\n    print(n, o.data.shape, o.acquisition_layout.layout.partitions)" <recdir>/lapseA_<Detector>.hdf5
# --- RUN B: one file per timepoint --------------------------------------------
# UI: same as A but UNTICK 'Save all timepoints in a single file'; file name base: lapseB -> REC
ls <recdir>/lapseB*
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseB_scan0_<Detector>.hdf5 | tee <recdir>/lapseB_scan0.inspect.txt
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseB_scan1_<Detector>.hdf5 | tee <recdir>/lapseB_scan1.inspect.txt
# --- RUN C: single file + 'Save on disk and keep in memory' (fixed this week) ---
# UI: as A, but Rec save mode: 'Save on disk and keep in memory'; file name base: lapseC -> REC
# UI: watch the image display: one new memory entry appears per timepoint, all named lapseC_<Detector>.hdf5
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseC_<Detector>.hdf5 | tee <recdir>/lapseC.inspect.txt
# --- RUN D: the same lapse TWICE, WITHOUT restarting ImSwitch (fixed this week) -
# UI: as A, file name base: lapseD -> REC; wait for the lapse to finish; press REC again with the SAME base name
ls <recdir>/lapseD*
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseD_<Detector>.hdf5 | tee <recdir>/lapseD.inspect.txt
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <recdir>/lapseD_<Detector>_1.hdf5 | tee <recdir>/lapseD_1.inspect.txt
# --- reconstruction: open one item from A and one from B in ImProcess -----------
# UI: ImProcess -> open lapseA_<Detector>.hdf5; the dataset chooser must list scan0/<Detector> and scan1/<Detector>
# UI: run the reconstructor for the modality (BeadRec for a plain point scan) on one item of A and one of B
```

### Expect

```
REHEARSED, NOT IMAGINED. Everything below was produced on 2026-09-05 in
/Users/lenny/PycharmProjects/Imswitch2-acquisition-layout (head fc410fc2) by driving the
real GalvoScanDesigner + AdvancedScanTTLCycleDesigner + RecordingManager + recording worker
+ HDF5/Zarr storers with the mock Hamamatsu camera under the simulated NI-DAQ. Scripts and
the full 408-line dump are in
/private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step6-scan-lapse/
(run_lapse.py, run_extras.py, run_empty_tp.py, readback.py, beadrec_check.py,
inspector_reference.txt).

Rig substitutions: the rehearsal scan is 3 x 2 = 6 positions, detector 'Camera', 32x32 px,
2 timepoints. On the rig, `count=`, `frames=N of N`, the shape and the detector name change;
`scan source:` will be the real controller class (e.g. ScanController), not
`_PointScanController`. EVERY OTHER LINE BELOW SHOULD BE CHARACTER-FOR-CHARACTER THE SAME.

=========================================================================
1. FILE NAMES (verified, all four runs)
=========================================================================
A  single file, 2 timepoints        -> lapseA_Camera.hdf5
B  one file per timepoint, 2 tp     -> lapseB_scan0_Camera.hdf5, lapseB_scan1_Camera.hdf5
C  single file + keep in memory     -> lapseC_Camera.hdf5   (one file, all timepoints)
D  same single-file lapse run twice -> lapseD_Camera.hdf5 AND lapseD_Camera_1.hdf5

The per-timepoint index goes INSIDE the savename ('_scanNN'), before the '_<Detector>'
suffix, and is zero-padded to the digit count of the total. Verified with a 10-timepoint
run:
    F_ten_scan00_Camera.hdf5 ... F_ten_scan09_Camera.hdf5
The second run's de-duplication suffix goes at the END, before the extension: '_1'.
Zarr behaves identically: a single-file lapse gives one directory store,
G_zarr_Camera.zarr, holding scan0/Camera and scan1/Camera.

=========================================================================
2. INSPECTOR, RUN A (single file) -- the full reference block
=========================================================================
  /.../A_single_Camera.hdf5
    interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
    datasets    : 2 -> scan0/Camera, scan1/Camera

    --- scan0/Camera ---
    source      : explicit-metadata
    confidence  : certain
    provenance  : recorded
    usable      : True   (geometry may be taken from it)
    authoritative: True   (may refuse a reconstruction)
    payload     : detector-frame-stream
    detector    : Camera
    modality    : None    scan source: _PointScanController
    storage axes: ['frame', 'detector_y', 'detector_x']
    event loops (outermost first):
      scan_y       count=2      pitch=1.0 um        device=Y  direction=+1
      scan_x       count=3      pitch=1.0 um        device=X  direction=+1
    traversal:
      scan_y: forward
      scan_x: forward
    partition   : time index=0 of 2  storage=one-group-per-item
    issues:
      [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
    lifecycle   : writer=finalized  outcome=complete  frames=6 of 6  partitions=1 of 1

    first 6 stored frames:
      frame 0     -> scan_y=0  scan_x=0
      frame 1     -> scan_y=0  scan_x=1
      frame 2     -> scan_y=0  scan_x=2
      frame 3     -> scan_y=1  scan_x=0
      frame 4     -> scan_y=1  scan_x=1
      frame 5     -> scan_y=1  scan_x=2

    --- scan1/Camera ---
    [identical, except:]
    partition   : time index=1 of 2  storage=one-group-per-item

  exit code 0.

THE FOUR LINES THAT ARE STEP 6:
  * 'datasets    : 2 -> scan0/Camera, scan1/Camera'  (one entry per timepoint)
  * one '--- scanN/Camera ---' block per timepoint
  * 'partition   : time index=N of M  storage=one-group-per-item'  with N counting 0,1,...
  * the frame table identical in every block

=========================================================================
3. INSPECTOR, RUN B (one file per timepoint)
=========================================================================
Each file holds ONE dataset, so there is no 'datasets :' line and no '---' heading. The
only line that differs from run A is the partition line:

  lapseB_scan0_Camera.hdf5 -> partition   : time index=0 of 2  storage=one-file-per-item
  lapseB_scan1_Camera.hdf5 -> partition   : time index=1 of 2  storage=one-file-per-item
  (both: lifecycle   : writer=finalized  outcome=complete  frames=6 of 6  partitions=1 of 1)

A 10-timepoint per-file run, item 7:
  partition   : time index=7 of 10  storage=one-file-per-item

=========================================================================
4. INSPECTOR, RUN C (Disk + keep in memory) and RUN D (run twice)
=========================================================================
C: byte-for-byte the same shape of output as A --
  datasets    : 2 -> scan0/Camera, scan1/Camera
  scan0/Camera: partition   : time index=0 of 2  storage=one-group-per-item
  scan1/Camera: partition   : time index=1 of 2  storage=one-group-per-item
  both:         lifecycle   : writer=finalized  outcome=complete  frames=6 of 6  partitions=1 of 1
  no recording failure was emitted (sigRecordingFailed: []).
A 3-timepoint Disk+RAM rehearsal emitted ONE memory hand-off per timepoint, all naming the
SAME file, with a growing group list -- this is what the image display should show:
  ('E_diskram3_Camera.hdf5', ['scan0'],                    saved=True)
  ('E_diskram3_Camera.hdf5', ['scan0','scan1'],            saved=True)
  ('E_diskram3_Camera.hdf5', ['scan0','scan1','scan2'],    saved=True)

D: TWO files, each a complete independent lapse --
  lapseD_Camera.hdf5    -> datasets : 2 -> scan0/Camera, scan1/Camera; indices 0 of 2, 1 of 2
  lapseD_Camera_1.hdf5  -> datasets : 2 -> scan0/Camera, scan1/Camera; indices 0 of 2, 1 of 2
Neither file contains scan2/scan3, and no file contains two items claiming 'index=0'.
Rehearsed with ONE RecordingManager reused across both runs (what the app does; the repo's
regression test builds a fresh manager per run and therefore does not cover this).

=========================================================================
5. READ BACK THE WAY THE GUIDE ORDERS IT (DataObj)
=========================================================================
  $ python -c "...DataObj..." lapseA_Camera.hdf5
  scan0/Camera (6, 32, 32) (AcquisitionPartition(kind='time', index=0, planned_count=2, storage='one-group-per-item'),)
  scan1/Camera (6, 32, 32) (AcquisitionPartition(kind='time', index=1, planned_count=2, storage='one-group-per-item'),)

  $ python -c "...DataObj..." lapseB_scan0_Camera.hdf5
  Camera (6, 32, 32) (AcquisitionPartition(kind='time', index=0, planned_count=2, storage='one-file-per-item'),)

  $ python -c "import h5py,sys; f=h5py.File(sys.argv[1]); f.visit(print)" lapseA_Camera.hdf5
  scan0
  scan0/Camera
  scan0/Camera/data
  scan0/Camera/frames_committed
  scan0/Camera/metadata
  scan0/Camera/metadata/acquisition
  scan0/Camera/stream_complete
  scan1
  scan1/Camera
  scan1/Camera/data
  ... (same five children)
  (a per-timepoint file has the flat form: Camera, Camera/data, Camera/frames_committed,
   Camera/metadata, Camera/metadata/acquisition, Camera/stream_complete)

Per-item attributes on <group>/<Detector>/data, identical in all four runs:
  recording:lapse_index        = 0, then 1
  recording:num_timepoints     = 2
  recording:single_lapse_file  = True   (False in run B)
  recording:completion_outcome = 'complete'
  recording:expected_frames    = 6
  recording:actual_frames      = 6
  recording:discarded_frames   = 0
  axes                         = 'TYX'

=========================================================================
6. "RECONSTRUCT IDENTICALLY", MADE CHECKABLE
=========================================================================
Across all 10 items of runs A+B+C+D, once the lapse identity is removed the encoded layout
is ONE value:
  distinct layouts once the lapse identity is removed: 1
  10 items share one geometry: [A:scan0/Camera, A:scan1/Camera, B_scan0:Camera,
   B_scan1:Camera, C:scan0/Camera, C:scan1/Camera, D:scan0/Camera, D:scan1/Camera,
   D_1:scan0/Camera, D_1:scan1/Camera]
BeadRec run headlessly on every one of those 10 items returned the same metadata:
  {'scan_dims': '(3, 2)', 'n_frames': '6', 'geometry_source': 'layout'}
On the rig, 'geometry_source': 'layout' and identical scan_dims for every item, in both
storage modes, is the pass.

=========================================================================
7. EXPECTED NOISE -- DO NOT STOP THE SESSION FOR THESE
=========================================================================
  * '[warning] LOSSY_OME_TIME_PROJECTION ...' under 'issues:'. It fires on EVERY HDF5/Zarr
    scan recording this branch writes -- verified on a lapse item AND on a plain ScanOnce
    file. It only says the container's OME 'T' label is an interop projection of the
    layout's 'frame' axis. usable/authoritative stay True.
  * 'partitions=1 of 1' on the lifecycle line of every lapse item. Those counters describe
    the writer session (one timepoint), not the lapse. The lapse count lives on the
    'partition' line above it ('of 2').
  * 'modality    : None' for a plain point scan.
  * A second file 'lapseD_Camera_1.hdf5' after a second run is correct, not a duplicate.
  * Passing the DETECTOR name to the inspector on a single-file lapse is an operator error,
    not a file fault; it exits 1 and tells you the item names:
      NO SUCH DATASET: 'Camera'. This file holds: scan0/Camera, scan1/Camera
    Pass 'scan1/Camera' instead, or no second argument at all.

=========================================================================
8. REFUSALS SPECIFIC TO THIS STEP (quoted from the controller, not captured)
=========================================================================
  'Single-file scan timelapse supports HDF5 and ZARR; select separate files for TIFF.'
      -> tick 'Save all timepoints in a single file' with File format TIFF; expected, fix
         is to untick or choose HDF5/ZARR.
  'Select a scan source in the Recording widget before starting a timelapse scan: this
   setup has more than one scan widget that could be driven.'
      -> only on a setup with >1 capable scan widget; pick one in the Scan source list.
Both fire before any file is written. Neither is in the guide's §4 table.
```

### A real fault looks like

- A single-file lapse of M timepoints whose inspector output says 'datasets : 1' or lists fewer scanN blocks than M -- a timepoint recorded nothing, or wrote somewhere else. (Rehearsed: a lapse whose first timepoint recorded no frames leaves ONE group named scan0 whose partition line reads 'time index=1 of 2'. Group names are assigned by taking the first free scanN, so scanN is NOT the timepoint index; the honest index is the partition line and recording:lapse_index.)
- During ONE single-file run, a second file appearing with a '_1' suffix (lapseA_Camera_1.hdf5). That means a later timepoint failed to reuse the path pinned at timepoint 0 -- the pre-fix behaviour, each timepoint in its own file.
- Two items in ONE file both printing 'time index=0' -- the run-twice append bug is back (the second run appended into the first run's file).
- A second run producing groups scan2/scan3 in the FIRST run's file instead of a fresh lapseD_Camera_1.hdf5.
- No 'partition' line at all on a lapse item. The lapse identity was never recorded; nothing downstream can tell the timepoints apart.
- storage=one-group-per-item on a per-timepoint file, or storage=one-file-per-item on a file that contains scanN groups -- the recorded mode disagrees with what was actually written.
- 'outcome=stopped_early' or 'frames=X of Y' with X<Y on a timepoint you did not stop; on a lapse this usually means one timepoint's scan under-delivered while the others were fine, so compare the lifecycle line across the items.
- recording:discarded_frames greater than 0 in any item (guide §3.4): the detector delivered more frames than the timepoint's scan declared and the surplus was dropped.
- 'Save on disk and keep in memory' failing at timepoint 2 with a writer/HDF5 open error, or the file vanishing after the failure. That is exactly the bug fixed this week (the held read handle blocked the next timepoint's append, the abort then deleted the shared file) and it means the rig is not running the intended commit -- check the 'interpreted by:' line and git log first.
- 'interpreted by:' pointing at anything other than the checkout under test (e.g. a site-packages install). Every reading below it then describes different software.
- BeadRec (or the modality's reconstructor) reporting geometry_source 'manual', or raising "cannot determine the raster", on a lapse item -- the layout did not survive into that item.
- Two items of the same lapse producing different frame tables, different event-loop counts, or different pitch. Every timepoint runs the same scan; a difference there is a producer or firmware fault, not a storage one.
- ImProcess showing a single-file lapse as empty / 'does not contain any datasets' -- that is the discovery bug this step exists to re-check, and it means the running code predates the fix.

### Only the rig can answer

- Whether the scan really is re-armed and re-run identically at every timepoint. The simulated DAQ replays the same waveform by construction, so the rehearsal cannot show a timepoint that starts at the wrong stage origin, drifts, or is armed late.
- Whether the interval (Freq [s]) is honoured between timepoints on real firmware, and whether a non-zero interval changes any of the above (the rehearsal ran back-to-back timepoints with no wait).
- Whether the real detector delivers exactly the declared number of frames per timepoint. discarded_frames = 0 in the rehearsal only proves the mock camera obeys; free-running cameras and extra pulses exist only on hardware.
- Whether the writer keeps up with a full-size camera over many timepoints -- the producer-stall warning and the chunk-queue cap. The rehearsal wrote 6 frames of 32x32 per timepoint; nothing was ever under pressure.
- Whether 'Save on disk and keep in memory' still works when the recording directory is a network share. HDF5 file locking on NFS/SMB differs from the local filesystem the rehearsal used, and the fixed bug was exactly a locking interaction.
- Whether a Zarr single-file lapse behaves acceptably on the rig's storage (a directory store with many small files), as opposed to correctness, which the rehearsal confirms.
- On a multi-scanner rig, whether the Scan source chooser resolves to the scanner you meant; the rehearsal drove one controller directly and never exercised the chooser.
- Whether a positioning provider (if one is configured) puts the sample in the right place before each timepoint -- the rehearsal had none.
- Whether stopping a lapse mid-run leaves the already-recorded timepoints intact and readable. Only the empty-first-timepoint case was rehearsed here; the interrupted-lapse path belongs to step 7 and has never met hardware.
- Whether the reconstruction of each timepoint actually looks right on a structured sample. The rehearsal proves the geometry is identical between timepoints and modes; it cannot prove the geometry is TRUE.

---

## Step 7 — a recording stopped early

### Run

```bash
# 0. Confirm which code is running (guide §0). Do this once.
cd /path/to/checkout && git log --oneline -1

# ---- RUN A: stopped DURING a scan. Must be recording mode 'Scan once'.
# (GUI) Recording widget: mode = Scan once, Save format = HDF5, Save mode = Save on disk.
# (GUI) Press REC to arm, start the scan, then press REC again roughly halfway through the scan.
# Note in your log what fraction of the scan had run when you pressed REC.
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.hdf5

# Repeat RUN A twice more, changing only Save format, to Zarr and then to TIFF:
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.zarr
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.ome.tiff
# NB the OME-TIFF's dataset is named 'Image0', not the detector name. Pass no detector
# argument, or 'Image0'. Passing the detector name is refused (that is correct behaviour).

# ---- RUN B: stopped BEFORE the first frame. Same mode, same three formats.
# (GUI) Press REC to arm. Do NOT start the scan. Press REC again.
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.hdf5
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.zarr
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <savename>_<Detector>.ome.tiff
# Exit code 1 on all three RUN B files is EXPECTED, not a tool failure.

# ---- Read-back step 2: DataObj, for each RUN A file
QT_QPA_PLATFORM=offscreen python - <<'PY'
from imswitch.improcess.model import DataObj
path = '<file>'
names = DataObj.getDatasetNames(path)
print('datasets:', names)
obj = DataObj(path, names[0], path=path)
print('shape   :', obj.data.shape)
life = obj.recording_lifecycle
print('lifecycle:', life.writer_state, life.completion_outcome,
      life.actual_frames, 'of', life.planned_frames)
for issue in obj.acquisition_layout.issues:
    print(f'  [{issue.severity}] {issue.code}: {issue.message}')
for key in sorted(k for k in dict(obj.attrs) if str(k).startswith('recording:')):
    print(' ', key, '=', dict(obj.attrs)[key])
PY

# ---- Read-back step 3: the reconstructors, in ImProcess
# (GUI) File > open the RUN A file. It loads under 'View only' and displays.
#       Read the coloured line at the top of the Parameters dock (see expected_output).
# (GUI) Switch the reconstructor to 'Bead reconstruction'. The dock line turns red and
#       gains a third line. Click Reconstruct current: it is refused.
# (GUI) File > open a RUN B file. Nothing loads; the ImProcess log says
#       "Could not read datasets from <path>: File does not contain any datasets".
#       That is the expected outcome - the inspector, not ImProcess, is what reads that file.
```

### Expect

```
REHEARSED ON THE SIMULATED RIG, 2026-09-05, worktree
/Users/lenny/PycharmProjects/Imswitch2-acquisition-layout at fc410fc2.
Mock Hamamatsu camera under the simulated NI-DAQ, real Galvo + Advanced TTL
designers, real producer layout builder, real RecordingManager gate/worker,
real HDF5/Zarr/TIFF storers. Scan 6 x 4 = 24 positions; REC switched off after
9 frames. Your counts will differ; the STRUCTURE below is the reference.
Files + drivers: /private/tmp/claude-501/-Users-lenny-PycharmProjects-Imswitch2/e719334d-538f-4ade-8f91-2aef149f9e5e/scratchpad/rehearse-step7-stopped-early/
(out/, make_stopped.py, inspector-reference.txt)

================================================================
RUN A - stopped DURING the scan.  HDF5:
================================================================
$ QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py short_hdf5_Camera.hdf5

/.../out/short_hdf5_Camera.hdf5
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  source      : explicit-metadata
  confidence  : certain
  provenance  : recorded
  usable      : True   (geometry may be taken from it)
  authoritative: True   (may refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : Camera
  modality    : None    scan source: _PointScanController
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    scan_y       count=4      pitch=1.0 um        device=Y  direction=+1
    scan_x       count=6      pitch=1.0 um        device=X  direction=+1
  traversal:
    scan_y: forward
    scan_x: forward
  issues:
    [warning] FRAME_COUNT_MISMATCH: Layout selects 24 frames but source contains 9
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
  lifecycle   : writer=finalized  outcome=stopped_early  frames=9 of 24  partitions=1 of 1

  first 9 stored frames:
    frame 0     -> scan_y=0  scan_x=0
    frame 1     -> scan_y=0  scan_x=1
    frame 2     -> scan_y=0  scan_x=2
    frame 3     -> scan_y=0  scan_x=3
    frame 4     -> scan_y=0  scan_x=4
    frame 5     -> scan_y=0  scan_x=5
    frame 6     -> scan_y=1  scan_x=0
    frame 7     -> scan_y=1  scan_x=1
    frame 8     -> scan_y=1  scan_x=2
    (15 planned frame(s) were never recorded: scan_y=1  scan_x=3; scan_y=1  scan_x=4; scan_y=1  scan_x=5, and 12 more)

  [exit 0]

Note on --frames: it caps only the PRINTED rows. With `--frames 4` the trailer
still reads "(15 planned frame(s) were never recorded: ...)", not 20. If the
trailer's count is not (planned - stored), something is wrong.

ZARR and OME-TIFF, RUN A: BYTE-IDENTICAL to the block above from
"source      :" downwards. Only the first line (the path) differs. This is the
answer to "the three behaved differently until this week" - they no longer do.
The only container-level differences are in the raw attributes (below).

================================================================
RUN B - stopped BEFORE the first frame.  All three formats:
================================================================
$ ... inspect_acquisition_layout.py empty_hdf5_Camera.hdf5

/.../out/empty_hdf5_Camera.hdf5
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  CANNOT LIST DATASETS: RuntimeError: File does not contain any datasets
  the container describes itself as:
    recording:actual_frames = 0
    recording:completion_outcome = stopped_early
    recording:detector_name = Camera
    recording:lapse_index = 0
    recording:num_timepoints = 1
    recording:planned_frames = 24
  [exit 1]

ZARR: character-for-character the same six self-description lines.

OME-TIFF - DIFFERENT, and this is a real gap (see guide_corrections):

/.../out/empty_tiff_Camera.ome.tiff
  interpreted by: /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout/imswitch
  CANNOT LIST DATASETS: RuntimeError: File does not contain any datasets
  [exit 1]

The empty OME-TIFF is 16 bytes - a BigTIFF header with a null first-IFD offset -
and carries no recording: metadata at all. It looks exactly like a corrupt file.
Expect this; it is not something you did wrong.

Log lines at finalize, RUN B (INFO level, quoted verbatim):
  HDF5: "Recording for 'Camera' finalized with no frames; the file holds no image and is marked stopped_early."
  Zarr: "Recording for 'Camera' finalized with no frames; the store holds no image and is marked stopped_early."
  TIFF: nothing.

Log lines at finalize, RUN A: NONE. A short recording is silent in the log; the
only INFO lines are "[RecordingManager] Starting recording" and
"[RecordingManager] Stopping recording". Do not wait for a warning that says the
recording was cut short - the file says it, the log does not.

================================================================
Read-back step 2 - DataObj (identical for all three RUN A formats)
================================================================
  getDatasetNames -> ['Camera']            (OME-TIFF: ['Image0'])
  .data.shape     -> (9, 32, 32)  dtype=uint16
  nonzero frames  -> 9 of 9
  layout source   -> explicit-metadata / certain / usable=True authoritative=True
    [warning] FRAME_COUNT_MISMATCH: Layout selects 24 frames but source contains 9
    [warning] LOSSY_OME_TIME_PROJECTION: Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
  lifecycle       -> writer=finalized outcome=stopped_early frames=9 of 24 partitions=1 of 1

RUN B, all three: getDatasetNames -> RuntimeError: File does not contain any datasets

Raw recording: attributes actually carried (this is where the three formats
still differ, harmlessly):

  HDF5 (14 keys)          Zarr (15 keys)          OME-TIFF (6 keys)
  actual_frames=9         actual_frames=9         actual_frames=9
  actual_partitions=1     actual_partitions=1     actual_partitions=1
  completion_outcome=     completion_outcome=     completion_outcome=
    stopped_early           stopped_early           stopped_early
  discarded_frames=0      discarded_frames=0      discarded_frames=0
  planned_frames=24       planned_frames=24       planned_frames=24
  planned_partitions=1    planned_partitions=1    planned_partitions=1
  expected_frames=24      expected_frames=24        --
  frames_per_stack=24     frames_per_stack=24       --
  detector_name=Camera    detector_name=Camera      --
  dataset_path=/Camera/.. dataset_path=/Camera/..   --
  source_format=HDF5      source_format=ZARR        --
  lapse_index=0           lapse_index=0             --
  num_timepoints=1        num_timepoints=1          --
  single_lapse_file=False single_lapse_file=False   --
    --                    frames_committed=9        --
  axes = TYX              axes = ['T','Y','X']    axes = None (in the OME-XML)

All three also carry AcquisitionLayout:json + AcquisitionLayout:schema
(= imswitch.acquisition-layout/1). The OME-TIFF carries them in an OME
MapAnnotation, namespace https://imswitch.org/ns/acquisition-metadata/1, and its
OME-XML reports SizeT="9" - what is stored - while the annotation reports
planned_frames=24. That is correct: the container never claims the missing frames.

Writer-liveness markers after a clean early stop:
  HDF5:  /Camera/stream_complete = [1],  /Camera/data.attrs['writing'] = False,
         /Camera/frames_committed = [9]
  Zarr:  data attrs writing = False, recording:frames_committed = 9, shape [9,32,32]

================================================================
Read-back step 3 - what the Parameters dock shows
================================================================
The dock label is the issue messages of the active reconstructor's
inspect_source(), one per line, orange for warning and red for error.

  --- Parameters dock, "View only" active  [warning / orange]
      Layout selects 24 frames but source contains 9
      Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'

  --- Parameters dock, "Bead reconstruction" active  [error / red]
      Layout selects 24 frames but source contains 9
      Container axis 'T' is only an interoperability projection; the acquisition layout records storage role 'frame'
      The stored data does not contain the complete declared acquisition layout

Metadata the inspection carries (not displayed, but logged and available to a
plugin's own widget), identical for all three formats:
  {'acquisition_layout_source': 'explicit-metadata',
   'acquisition_layout_confidence': 'certain',
   'acquisition_payload_kind': 'detector-frame-stream'}

Reconstructing:
  View only  -> shape=(9, 32, 32) axis_labels=['T', 'Y', 'X']    (opens and displays)
  BeadRec    -> AcquisitionPreflightError: The stored data does not contain the
                complete declared acquisition layout
  BeadRec with Scan X=6 / Scan Y=4 typed in -> the SAME refusal. Manual entries
  do not get you past it. The counts are in the dock line, not in this message.

================================================================
Contrast file kept for comparison
================================================================
scratchpad/rehearse-step7-stopped-early/contrast/nomarker_hdf5_Camera.hdf5 is the
same 9-of-24 recording with the stopped_early stamp stripped out. It is what a
BROKEN finalize looks like:

  RESOLUTION FAILED: AcquisitionLayoutResolutionError: Explicit acquisition layout is invalid; automatic fallback is forbidden
    [error] FRAME_COUNT_MISMATCH: Layout selects 24 frames but source contains 9

and BeadRec then says "AcquisitionPreflightError: Layout selects 24 frames but
source contains 9". If your rig file looks like THIS instead of the reference
above, the stop path did not stamp the outcome.
```

### A real fault looks like

- No `lifecycle` line in the inspector output at all. The line is unconditional for a finalized file; its absence means recording:completion_outcome was never written, and the layout then fails validation outright ('RESOLUTION FAILED: ... Explicit acquisition layout is invalid; automatic fallback is forbidden' with FRAME_COUNT_MISMATCH at severity [error], not [warning]). Capture the file - this is the finalize path failing, not a partial scan.
- FRAME_COUNT_MISMATCH printed as [error] rather than [warning]. The demotion to warning is what makes a stopped-early file readable; an error means the stopped_early marker is missing or unreadable in that container.
- `outcome=complete` on a recording you deliberately stopped, when `frames=N of M` shows N < M. The outcome is derived from actual < planned, so complete-with-a-shortfall is self-contradictory and means the counts are being written from different sources.
- `outcome=stopped_early` with frames=N of M where N == M or N > M. The inspector also prints a distinct trailer for the second case: '(k frame(s) beyond the N the layout describes are stored and unaccounted for)'. Either means the detector delivered frames the scan did not plan.
- `recording:discarded_frames` greater than 0. On a clean early stop it is 0 (verified on all three formats). Non-zero means the detector produced more frames than the scan declared and the surplus was dropped - a free-running or over-pulsed camera, unrelated to the stop.
- The frame table's first rows do not start at the first scan position, or the stored count does not match the fraction of the scan that had run when you pressed REC. My reference stops mid-row (frame 8 -> scan_y=1 scan_x=2) because frames arrive strictly in scan order; a real stop should also land wherever the scan actually was.
- The 'never recorded' trailer count is not (planned - stored). It is computed from the layout, so a wrong number means the layout and the stored count disagree about the plan.
- For RUN B on HDF5 or Zarr: no 'the container describes itself as:' block after 'CANNOT LIST DATASETS'. Both formats stamp the root of an empty container at finalize; nothing there means the empty-file stamp did not run. (For OME-TIFF this block is legitimately absent - see guide_corrections.)
- Zarr `writing` still True, or HDF5 `/<Detector>/stream_complete` = [0], after the recording has stopped. The writer never finalized; anything you read from the file is a live snapshot, not a result.
- The file is GONE after you press Stop. That means the abort path ran, not the finalize path - you were in a lapse mode (see guide_corrections item 3), or something raised during finalize. abortStream deletes the partial output by design; I confirmed on all three formats that an aborted run leaves nothing on disk.
- A RUN A file that will not open under 'View only'. View only never refuses data; if it does, the failure is in the container, not in the layout.

### Only the rig can answer

- Whether the frames the file holds really are the FIRST N positions the scan visited. The simulated coordinator delivers frames strictly in scan order, so the reference frame table is guaranteed correct by construction. On the rig, a camera that free-runs or a scan that aborts mid-line could store frames the table attributes to earlier positions - and the table would look exactly like the reference. Only a structured sample (bead grid, intensity ramp across rows) falsifies this.
- What the hardware does between the moment REC is switched off and the moment the writer closes: whether the galvos keep scanning, whether the detector keeps delivering into a closed writer, and whether that shows up as discarded_frames > 0 or as the producer-stall warning. My run had at most one 30 ms tick of frames in flight; a real camera at real frame rates has a deeper bounded chunk queue.
- Whether frames already handed to the writer but not yet committed at the moment of the stop reach the file. In my runs stored == committed == 9 on both HDF5 and Zarr, but the queue was nearly empty. A rig stop under load is the only test of that boundary.
- Whether 'stopped before the first frame' is even reachable with a real detector. With a camera that free-runs or an APD already armed, a frame may land before you can press REC a second time, in which case RUN B silently becomes a 1-frame RUN A. If you cannot produce an empty file, say so rather than forcing one.
- The TriggerScope RESOLFT and other firmware-run scans: the firmware owns the loop, so what a mid-scan stop does to the scan itself - and whether the recording finalizes at all rather than stalling to the watchdog - cannot be simulated. Do step 7 on a NI-DAQ point scan first and only then, if you have time, repeat it on a firmware scan.
- Whether a stopped-early OME-TIFF opens correctly in Fiji/OMERO. The OME-XML is honest (SizeT = frames stored), but no external reader has ever seen one.
- Whether an empty OME-TIFF placeholder is even created on the rig, or whether the file system leaves something else behind. Mine was a 16-byte BigTIFF header; a network share or a different tifffile version may differ.

---

## Step 8 — an old recording

### Run

```bash
cd /Users/lenny/PycharmProjects/Imswitch2-acquisition-layout && git log --oneline -1   # must be the commit under test; rehearsal ran on 2c2014ae
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <old_point_scan>_Camera.hdf5
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <old_linestep_scan>_Camera.hdf5
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <old_linestep_scan>_APDred.hdf5   # a point detector on the same old scan
QT_QPA_PLATFORM=offscreen python tools/inspect_acquisition_layout.py <old_snouty>_OrcaStraight.hdf5
echo $?   # 0 = a layout was produced, 1 = RESOLUTION FAILED / no datasets
QT_QPA_PLATFORM=offscreen python -c "import sys; sys.path.insert(0,'.'); from imswitch.improcess.model import DataObj; p=sys.argv[1]; n=DataObj.getDatasetNames(p); o=DataObj(p,n[0],path=p); r=o.acquisition_layout; print(n, o.data.shape); print(r.source, r.confidence, r.is_usable, r.is_authoritative); print([(l.kind,l.count) for l in r.layout.event_loops])" <old_file>
QT_QPA_PLATFORM=offscreen python -c "import h5py,sys; f=h5py.File(sys.argv[1]); g=f[sys.argv[2]+'/metadata/ScanStage']; [print(k,'=',v) for k,v in g.attrs.items()]" <old_file> <DetectorName>   # what the adapter actually read; the inspector does not echo it
# then open the same file in ImProcess and run the reconstructor for that modality (BeadRec / MoNaLISA / SNOUTY)
```

### Expect

```
HEALTHY LEGACY READINGS -- rehearsed on files written by the real RecordingManager +
storers with acquisitionLayouts withheld, then stripped of the seven recording:* keys
this branch added, so they are byte-for-byte what the pre-branch writer produced.

1) PLAIN CAMERA POINT SCAN (3x2, HDF5). Claimed by the LAST, least specific adapter:

  source      : scan-stage-legacy
  confidence  : medium
  provenance  : legacy-adapter
  usable      : True   (geometry may be taken from it)
  authoritative: False   (cannot refuse a reconstruction)
  payload     : detector-frame-stream
  detector    : Camera
  modality    : None    scan source: scan-stage-legacy
  storage axes: ['frame', 'detector_y', 'detector_x']
  event loops (outermost first):
    scan_z       count=1      pitch=1.0 None      device=Z  direction=+1
    scan_y       count=2      pitch=1.0 None      device=Y  direction=+1
    scan_x       count=3      pitch=1.0 None      device=X  direction=+1
  traversal:
    scan_z: forward
    scan_y: forward
    scan_x: forward
  issues:
    [warning] LEGACY_SCAN_GEOMETRY_ASSUMPTION: Scan dimensions were adapted from legacy ScanStage/ScanTTL metadata
  lifecycle   : writer=finalized  frames=6 of None

  first 6 stored frames:
    frame 0     -> scan_z=0  scan_y=0  scan_x=0
    frame 1     -> scan_z=0  scan_y=0  scan_x=1
    frame 2     -> scan_z=0  scan_y=0  scan_x=2
    frame 3     -> scan_z=0  scan_y=1  scan_x=0

The same recording saved as Zarr is IDENTICAL. On a rig with a fourth positioner
(X, Y, Z, PiezoZ) one extra line appears and is normal:

    [warning] LEGACY_INACTIVE_AXIS_DROPPED: Device 'PiezoZ' was listed as a scan axis at a single position and shares its guessed axis 'scan_z' with another device; it did not move, so it was left out of the geometry

2) CAMERA LINE-STEP SCAN (Advanced, 3x2 x 2 conditions, 12 frames):

  source      : advanced-scan-legacy
  confidence  : medium
  provenance  : legacy-adapter
  usable      : True   authoritative: False
  event loops (outermost first):
    scan_z       count=1      pitch=1.0 None      device=Z  direction=+1
    scan_y       count=2      pitch=1.0 None      device=Y  direction=+1
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_x       count=3      pitch=1.0 None      device=X  direction=+1
  issues:
    [warning] LEGACY_SCAN_GEOMETRY_ASSUMPTION: Scan dimensions were adapted from legacy ScanStage/ScanTTL metadata

  first 12 stored frames:
    frame 0     -> scan_z=0  scan_y=0  condition=0  scan_x=0
    frame 1     -> scan_z=0  scan_y=0  condition=0  scan_x=1
    frame 2     -> scan_z=0  scan_y=0  condition=0  scan_x=2
    frame 3     -> scan_z=0  scan_y=0  condition=1  scan_x=0
    frame 6     -> scan_z=0  scan_y=1  condition=0  scan_x=0
    frame 9     -> scan_z=0  scan_y=1  condition=1  scan_x=0

  This is the guide's §2 ordering (conditions alternate per physical row) with an
  extra leading scan_z=0. The alternation is the check; the scan_z=0 is noise.

3) POINT DETECTOR ON THE SAME LINE-STEP SCAN (APD, assembled image (1,2,2,3)):

  source      : advanced-scan-legacy-assembled
  confidence  : high                        <-- note: HIGHER than the camera above
  provenance  : legacy-adapter
  usable      : True   authoritative: False
  payload     : assembled-image
  storage axes: ['frame', 'condition', 'scan_y', 'scan_x']
  event loops (outermost first):
    condition    count=2      pitch=-             labels=['condition_0', 'condition_1']
    scan_y       count=2      pitch=1.0 None      device=Y  direction=+1
    scan_x       count=3      pitch=1.0 None      device=X  direction=+1
  issues:
    [warning] LEGACY_SCAN_GEOMETRY_ASSUMPTION: ...
  (assembled payload: axes map directly to the array, no frame table)

4) SNOUTY / pLS-RESOLFT (cycleSteps=3, roSteps=4, 12 frames):

  source      : snouty-legacy
  confidence  : high
  provenance  : legacy-adapter
  usable      : True   authoritative: False
  modality    : snouty    scan source: triggerscope-resolft-legacy
  event loops (outermost first):
    cycle        count=3      pitch=1.0 um
    plane        count=4      pitch=0.25 um
  issues:
    [warning] LEGACY_SNOUTY_ORDER_ASSUMPTION: Cycle/plane order was adapted from the legacy SNOUTY contract

  first 12 stored frames:
    frame 0     -> cycle=0  plane=0
    frame 3     -> cycle=0  plane=3
    frame 4     -> cycle=1  plane=0
    frame 8     -> cycle=2  plane=0

  Verified: the reading is BYTE-IDENTICAL whether or not the same file also carries
  ScanStage/ScanTTL from another scan widget that happened to be open. Snouty is
  tried before the raster and stage adapters, so precedence holds.

5) ANY OLD OME-TIFF -- geometry is simply not there:

  source      : ome-ngff
  confidence  : medium
  provenance  : ome-ngff
  detector    : Image0                      <-- the detector name is lost
  event loops (outermost first):
    time         count=6      pitch=-
  issues:
    [warning] OME_AXES_WITHOUT_ACQUISITION_LOOPS: Container axes were used because no acquisition-loop contract was recorded

  Confirmed by reading the OME-XML: 'ScanStage' and 'axis_length' do not appear in
  it at all. An old TIFF cannot be given its raster back; type it in.

INVARIANTS THAT MUST HOLD ON EVERY HEALTHY LEGACY FILE
  * provenance : legacy-adapter (or ome-ngff for TIFF)
  * authoritative: False  -- always. A legacy file can never refuse a reconstruction.
  * exactly one adapter claims it, and 'scan source' names the ADAPTER, not a controller
  * pitch unit is always None ("pitch=1.0 None"). ImSwitch never wrote
    ScanStage:axis_step_size_unit (grep: zero writers). Not a fault.
  * a 2D scan on a >=3-positioner rig shows scan_z count=1. New recordings drop it.
  * exit code 0.

WHAT BeadRec DOES WITH A HEALTHY LEGACY POINT SCAN (measured, not guessed):
  no Scan X/Y     -> ValueError: BeadRec cannot determine the raster: this source
                     carries no acquisition layout and neither Scan X nor Scan Y
                     pixels were set. ...
  Scan X/Y = 3x2  -> OK  {'scan_dims': (3, 2), 'n_frames': 6, 'geometry_source': 'manual'}
  Reason: the count-1 scan_z loop is a loop BeadRec does not place, so the inferred
  layout is declined and the manual entries stay in charge. 'geometry_source: manual'
  on an old file is CORRECT. On a 2-positioner rig the scan_z loop is absent.

WHAT THE MoNaLISA SCAN DIALOG PRE-FILLS FROM A LEGACY FILE (measured):
  legacy point scan | scan-stage-legacy
     -> {'dimensions': ['Right-Left','Up-Down','Back-Front','Timepoints'],
         'steps': ['3','2','1','1'], 'step_sizes': ['1000.0','1000.0','1000.0','1'],
         'n_linesteps': 1, 'unidirectional': True}
  legacy line-step  | advanced-scan-legacy
     -> steps ['3','2','1','2'], n_linesteps: 2
  legacy snouty     -> None      (correct: no scan axes)
  legacy TIFF       -> None      (correct: fields left alone)

COSMETIC DIFFERENCE, NOT A FAULT: diffing the inspector output of a file written by
THIS branch without a layout against the same file with the branch's new recording:*
keys stripped, exactly one line differs:
  <   lifecycle   : writer=finalized  frames=6 of None
  >   lifecycle   : writer=finalized  outcome=complete  frames=6 of 6  partitions=1 of 1
An old file has no outcome and no planned count. Everything above the lifecycle line
is identical.

FAULT SIGNATURES, as actually produced:

  a) stage geometry that no longer explains the frame count (axis_length edited from
     3 to 4 on a 6-frame file) -- degrades SILENTLY, exit code 0:
       source      : ome-ngff
       confidence  : medium
       usable      : True
       event loops : time  count=6
       issues: [warning] OME_AXES_WITHOUT_ACQUISITION_LOOPS: ...
     Identical output to a file that never was a scan. The reason (an internal
     AMBIGUOUS_LEGACY_SCAN_GEOMETRY) is swallowed by the stage adapter's decline.

  b) line-step counters that disagree with the frames -- HARD FAILURE, exit code 1:
       RESOLUTION FAILED: AcquisitionLayoutResolutionError: Legacy scan geometry is ambiguous
         [error] AMBIGUOUS_LEGACY_SCAN_GEOMETRY: Legacy size/endpoint conventions do not yield one frame-count match

  c) SNOUTY counters that do not multiply out -- exit code 1:
       RESOLUTION FAILED: AcquisitionLayoutResolutionError: SNOUTY cycle/plane metadata does not match the stored frame count
         [error] SNOUTY_FRAME_COUNT_MISMATCH: Expected 20 or 20 frames, found 12

  d) two positioners whose names both guess the same axis and BOTH moved -- also
     degrades silently to source: ome-ngff / time, exit 0.
```

### A real fault looks like

- `source: ome-ngff` with a single `time` loop on a file you KNOW was a scan. Exit code is 0 and confidence reads `medium`, so it looks healthy. It means the stage adapter rejected the ScanStage metadata and the reason was swallowed. Confirm by dumping `<Detector>/metadata/ScanStage`: multiply `axis_length[i]/axis_step_size[i]` (rounded, min 1) across axes and compare with the frame count. If they disagree, the file's own metadata is wrong; if they agree, the resolver is wrong and that is a finding.
- `RESOLUTION FAILED: ... AMBIGUOUS_LEGACY_SCAN_GEOMETRY` on an old line-step recording where a detector was gated onto only SOME line steps. This is a BUG IN THIS BRANCH, not bad data. `ScanTTL:linestep_enable` is written to file as the JSON sentinel string `__imswitch_json__:{"Camera": [true, false]}`, and only `SharedAttributes._decodeAttrValue` decodes that prefix -- the ImProcess resolver never does. `_detector_linestep_mask` therefore sees a str, not a Mapping, returns None, and the retry that exists precisely for gated detectors can never fire. Proven: feeding the identical attrs with that one value json-decoded resolves cleanly to `advanced-scan-legacy` with `spans: (RecordedEventSpan(start=0, count=3, stride=1, period=6, repeats=2),)`. Consequence measured: BeadRec then raises `AcquisitionPreflightError: Legacy size/endpoint conventions do not yield one frame-count match` EVEN WITH Scan X/Y typed in, so the old file cannot be reconstructed at all. `LEGACY_LINESTEP_ROLE_ASSUMED` is unreachable for the same reason. Capture such a file -- it is the highest-value artefact of this step.
- `authoritative: True` on a file you believe predates the branch. That means the file carries `AcquisitionLayout:json` and is NOT a legacy file -- it was written by this branch. Check `interpreted by:` and the recording date.
- A frame table whose innermost coordinate is not `scan_x`, or where `condition` is not between `scan_y` and `scan_x` on a line-step file. The legacy adapter asserts chronology Z -> Y -> condition -> X; if the old hardware interleaved otherwise, everything downstream now agrees with a wrong order.
- `LEGACY_INACTIVE_AXIS_DROPPED` naming a device that you know DID move during that scan. The adapter drops it because the file records it at a single position; if it moved, the file's geometry is wrong.
- `SNOUTY_FRAME_COUNT_MISMATCH: Expected N or M frames, found K` where N really is cycleSteps*roSteps. (When timepoints=1 the message prints the same number twice -- 'Expected 20 or 20' -- which is confusing wording, not a fault.)
- `detector : Image0` and `source: ome-ngff` on an HDF5 or Zarr file. For TIFF that is normal; for HDF5/Zarr it means dataset discovery fell back and the detector-named metadata group was not found.
- `interpreted by:` pointing at anything other than the checkout under test. Everything below that line then describes the wrong software.
- A different adapter claiming the file than the modality implies -- e.g. an old MoNaLISA recording claimed by `advanced-scan-legacy` or `scan-stage-legacy` rather than `monalisa-scan-legacy`. `adapt_monalisa_scan_metadata` requires `ScanTTL:Nx` AND `ScanTTL:Ny` AND no `ScanTTL:n_linesteps` claim ahead of it; `ScanControllerMoNaLISA` writes neither Nx nor Ny (only ScanControllerAdvanced does, via scan_parameters.py), so `monalisa-scan-legacy` may be unreachable for files this rig wrote. Worth checking against a real MoNaLISA file.
- `source: triggerscope-raster-legacy` never appearing on a genuine old TriggerScope raster recording. That adapter requires the file to NAME TriggerScope in `recording:scan_source`, `scan_source` or `controller`, and grep shows nothing in imcontrol writes any of those three keys -- they are only ever read. Expect `scan-stage-legacy` instead. If you DO see `triggerscope-raster-legacy`, something wrote a key we could not find.

### Only the rig can answer

- Whether the frame ORDER the legacy adapters assert matches what the hardware actually delivered when those old files were written. The adapters were derived from the same controller code the new producers were, so software can only prove they agree with each other. For the point scan and line-step cases the assertion is Z -> Y -> condition -> X; for SNOUTY it is cycle -> plane. Only a legacy file of a sample whose correct reconstruction you recognise can falsify it.
- Whether the operator's real old files carry the attribute categories at all. The rehearsal assumed the current flattened SharedAttributes shape (ScanStage:/ScanTTL:/MS-RESOLFT_Scan: written into <Detector>/metadata/<Category> groups). Files from an older ImSwitch2, a patched rig build, or ImSwitch1 Zarr (which nests them under ImswitchData) may differ, and only the rig's archive shows which.
- Whether any real old line-step recording has a per-detector `linestep_enable` that is not all-True. That is the exact condition that trips the JSON-sentinel bug above. If the rig only ever recorded one detector enabled on every line step, the bug never shows and the branch reads those files fine.
- Whether the rig's real positioner names collide under the adapter's substring guess ('x' then 'y' then 'z' in the lowercased device name). 'ND-PiezoZ' beside 'PiezoZ', 'GalvoX' driving Z, 'Mock Kinesis XY' beside 'Mock X' all guess wrongly or collide. Only the rig's setup file says which names are in play, and only the operator knows which physical axis each drove.
- Whether an old file's `ScanStage:axis_length` meant the scan SIZE or the scan ENDPOINT. The resolver keeps both conventions and lets the frame count arbitrate, but `axis_startpos` is written as a list-of-lists, which `_number()` cannot read, so the endpoint convention is never actually computed for any ImSwitch2 file -- only length/step is tried. If an old file used the endpoint convention it will be declined (degrading to `ome-ngff`/time) rather than adapted. `LEGACY_SCAN_CONVENTION_ASSUMED` and `AMBIGUOUS_LEGACY_TRAILING_AXIS` are consequently unreachable on ImSwitch2-written files; if either appears at the rig, that file came from somewhere else and is worth capturing.
- Whether an old SNOUTY file's cycle/plane ordering is what the firmware ran -- the same §3.1 risk as for new recordings, except that on a legacy file the branch marks it `authoritative: False`, so nothing will refuse a wrong reconstruction.
- How the archive is actually stored. The rehearsal covered HDF5, Zarr and OME-TIFF written by this repo's storers; single-file lapse containers, externally converted files and files renamed by hand were not exercised.

---
