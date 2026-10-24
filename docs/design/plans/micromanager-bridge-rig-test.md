# Micro-Manager bridge: what to test on the rig (Hamamatsu, native against bridged)

Status: Active — written 2026-10-24, rig session pending. Belongs to P3 of
`micromanager-bridge.md` (§5): one Windows rig with a Hamamatsu sCMOS camera
that both ImSwitch2's `HamamatsuManager` (DCAM-API through ctypes) and
Micro-Manager's `HamamatsuHam` adapter (through `imswitch-device-micromanager`)
can drive. The goal is a 1:1 comparison: same camera, same frames, same
exposure semantics, same geometry, and what the bridge adds or loses.

Everything below passed in software only: the real MMCore with the demo
camera (616 plugin tests plus the comparison kit's own) and the native
manager on its mock. The rig is where a vendor DLL, DCAM, a real sensor and
the Windows exit answer. Ordered by what blocks a merge first.

**The kit.** Three things make the session short and reproducible:

- the setup `setup_templates/hamamatsu_native_vs_bridge.json` (plugin
  package; **not** in the manifest, it needs hardware): the hub, the native
  camera `HamamatsuNative` (enabled) and the bridged camera `HamamatsuMM`
  (inline mode: `adapter: HamamatsuHam`, `device: HamamatsuHam_DCAM`) under
  the top-level `disabledDevices`, which is what the config editor writes for
  **Disable device**;
- `tools/compare_cameras.py measure` / `diff` (plugin folder): boots the real
  app headless on a setup, measures one detector the same way whichever driver
  runs it, writes a JSON report; `diff` prints the Markdown table below with
  PASS / CHECK;
- this plan: rows with what to write down.

**Why sequential.** DCAM allows one open handle per camera, so the native
manager and MMCore's adapter cannot drive the same camera in one process at
the same time. One setup file carries both entries and exactly one is enabled
per run (switch it in the config editor: right-click the device card, **Disable
device** / **Enable device**, or move the entry between `detectors` and
`disabledDevices` by hand). The hub stays enabled in both variants: it starts
MMCore but loads no device, so DCAM is untouched while the native camera runs
(its log lines in the native run are expected).

**Before the rig**

- Back up the rig's setup file and the whole `ImSwitchConfig` folder (a pull
  updates the shipped defaults; edited files are kept, but keep a copy). The
  tool leaves that folder as it finds it (a test pins it: a second start changes
  none of its files), but the GUI sessions in the rows below do not.
- A Python 3.11 or newer environment (pymmcore does not support 3.10; the
  installers bundle 3.10, so this is a from-source install):
  `pip install -e ".[test]"` in the ImSwitch2 checkout, then
  `pip install -e "examples/plugins/imswitch-device-micromanager"` (the `test`
  extra only adds the demo adapters; the rig does not need it).
- Micro-Manager's adapters: `mmcore install` (downloads the nightly that matches
  pymmcore's device interface), **or** point `MICROMANAGER_PATH` / the hub's
  `adapterPaths` at an existing Micro-Manager folder whose device interface is
  the same. Check: `mmcore --version` against
  `python -c "import pymmcore; print(pymmcore.__version__)"` (the fourth
  component, 75 on 2026-10-24, is the device interface version). A mismatch
  shows as "device interface version" in the hub's error status.
- Open the camera once in **Micro-Manager's own Hardware Configuration
  Wizard** (Devices menu of the Micro-Manager GUI) and confirm it works there: the
  adapter library `HamamatsuHam`, the device name (expected `HamamatsuHam_DCAM`)
  and its property names. **Save that `.cfg`**: it is the input of rows C1 and
  C2 and the source of the property-name column below. Close Micro-Manager
  before starting ImSwitch2 (one DCAM handle).
- Copy the template next to the rig's setups and make it yours:
  `python -c "import shutil, pathlib, imswitch_device_micromanager as m; shutil.copy(pathlib.Path(m.__file__).parent / 'setup_templates/hamamatsu_native_vs_bridge.json', pathlib.Path.home() / 'Documents/ImSwitchConfig/imcontrol_setups')"`
  (adjust the target to the rig's `ImSwitchConfig/imcontrol_setups`). Then edit:
  - **`cameraPixelSizeUm`: set the same measured value in both entries.** It is
    deliberately absent from the template (no invented value): the native
    manager then falls back to its 0.15 µm placeholder (with a warning) and the
    bridge to Micro-Manager's pixel-size config or the same placeholder, so the
    pixel-size row of the diff is CHECK until you set it.
  - the native entry's `hamamatsu` block is the MoNaLISA `Green` block
    (`example_monalisa.json`) without its `digitalLine`: set `subarray_hsize` /
    `subarray_vsize` to the sensor if it is not 2048 x 2048, and
    `cameraListIndex` to the camera's index in the DCAM list if it is not 0.
  - the rig's `readout_speed`, `trigger_*` numbers are the MoNaLISA ones; keep
    them only if this camera is the same model, else read row N1 first.
- Pick the scene once and keep it: the diff prints the mean intensities
  without a verdict, and they only mean something if both runs looked at the
  same thing, same illumination, same shutter. A static bright-ish scene with
  no saturation is best; note the light level.
- Disk: each recording is `--frames` x frame bytes (1.7 GB at 200 frames of
  2048 x 2048 16 bit) in a temporary folder, deleted after its statistics are
  read. `--record-dir DIR` keeps them.

**The two commands per variant**

```
python examples/plugins/imswitch-device-micromanager/tools/compare_cameras.py measure SETUP.json \
    --detector HamamatsuNative --out native.json --exposures 10,20,50 --frames 200 --seconds 5 \
    --roi 301,203,512,512 --binning 2
(enable the bridged camera, disable the native one)
python examples/plugins/imswitch-device-micromanager/tools/compare_cameras.py measure SETUP.json \
    --detector HamamatsuMM --out bridge.json --exposures 10,20,50 --frames 200 --seconds 5 \
    --roi 301,203,512,512 --binning 2
python examples/plugins/imswitch-device-micromanager/tools/compare_cameras.py diff native.json bridge.json
```

The ROI offset is chosen off the sensor's step grid on purpose (the Settings
panel rounds ROI fields up to multiples of 4 and DCAM snaps to its own
step: the rows below read what each side ends up with). With `--roi` and
`--binning` together the tool runs a third round, the ROI at that binning.

`measure` exits 0 when done, 1 when a phase failed (the report says which and
the other phases still ran), 2 on a boot failure, 3 when the detector is on a
mock or not confirmed connected (a native camera that fell back to its mock
would otherwise report mock numbers as the camera's). It never saves the
widget state, never writes a setup file, does not move `ImSwitchConfig`, and
does not restore the saved widget state unless `--restore-saved-state` is
given (the state is keyed by detector name, so the two variants would start
from different values).

## P0 — the bridge drives the camera as the native manager does (must pass before merging)

| # | Rig | Do | Passes if / write down |
|---|---|---|---|
| B1 | Native variant | Start ImSwitch2 normally on the template with `HamamatsuNative` enabled; open Settings, View, Recording | The log is clean apart from the hub's startup lines; the camera is adopted; live view shows an image. **Write down:** the camera model string ("Initialized camera, model: …"), the sensor shape in the Settings panel, the number of WARNING / ERROR lines and their text |
| B2 | Bridged variant | The same with `HamamatsuMM` enabled and `HamamatsuNative` disabled | The hub logs the adapter folder it chose (`MICROMANAGER_PATH` / `adapterPaths` / `find_micromanager()`) and `Device API version 75`; the camera is adopted with the model `HamamatsuHam/HamamatsuHam_DCAM`; live view shows an image. **Write down:** model string, adapter source line, seconds from start to first frame, WARNING / ERROR lines |
| W1 | Bridged variant, Windows | Boot B2 with **only** `adapterPaths` (or `MICROMANAGER_PATH`) pointing at Micro-Manager. This is the P0 row the spike could not measure (§3, "Windows adapter DLL path") | The camera loads. The adapter needs `dcamapi.dll`, which is DCAM-API's own system DLL and so should resolve without the adapter folder on the path: this is the first thing to try. **Write down:** pass/fail; on failure the exact message (the hub status carries MMCore's) and whether adding the folder that holds the DLL to `PATH` before starting fixes it (pymmcore-plus prepends the adapter folder to `PATH` but does not call `add_dll_directory`) |
| M1 | Native | `measure … --detector HamamatsuNative` (command above) | Exit 0, "runs on the real backend" will be PASS. **Write down:** exit code, `identity.model`, `full_shape`, `dtype`, `bit_depth`, `supported_binnings`, the warnings the tool printed |
| M2 | Bridged | `measure … --detector HamamatsuMM` | The same. **Write down:** the same; `hub.adapter_paths` and `hub.version_info` from the report |
| D0 | Both | `diff native.json bridge.json`; paste the table into Results | Read the rows below against it. Rows that are CHECK are not failures by themselves; each needs an explanation in the Results table |
| E1 | Both | Exposure read-back at 10, 20 and 50 ms (the tool sets it through the Settings controller: native `Set exposure time` in s, bridge `Exposure` in ms; the report converts both to ms) | `exposure read-back` within 1 % between A and B. **Write down:** both read-backs for each exposure and the native `Real exposure time` / `Internal frame interval` / `Readout time` / `Internal frame rate` values (in `identity.parameters` of the native report), so the quantisation to the sensor's line time is on record |
| F1 | Both | Live frame rate at 10, 20 and 50 ms (`live fps`, counted by hashing `getLatestFrame` polls; see the tool's docstring) | Within 10 % between A and B, and each close to `1 / exposure` or to the camera's own `Internal frame rate` (whichever is the lower limit). If the poll rate in the warnings is under twice the frame rate the live number undercounts: use the recording fps. **Write down:** live fps of both at each exposure, the bridge's exact counter (`live.bridge.fps_from_counter`), the polls per second |
| R1 | Both | A 200-frame recording at each exposure through the real `RecordingManager` (the tool reads the HDF5 back) | Frames written 200 on both, shape `(200, h, w)` equal, dtype `uint16`, `stream_complete` 1; on the bridge `frames_lost` 0 and `fault` null at every exposure. **Write down:** wall seconds and recording fps of both (includes start and stop latency, so compare A to B, not to live), `mean_of_means` / `std_of_means` of both. A recording fps well under the live fps on both sides is the start latency; a gap between the two drivers is the finding |
| R2 | Bridged | Re-run R1 at the shortest exposure the camera allows with the full sensor (the worst case for the single drain thread), watching the `lost` column | Zero lost frames; if not: `frames_lost`, `fault.kind` and `fault.message` from the report, and the Task Manager CPU of the python process. The spike measured 11 748 frames without a gap on the demo camera; a 2048 x 2048 16-bit stream is 8 MB per frame |
| Y1 | Both | Dtype and bit depth: `identity.dtype`, `bit_depth`, and Micro-Manager's own pixel-format properties (the bridge shows them in `identity.parameters`) | `uint16` on both. **Write down:** MM's `PixelType` and any bit-depth property values (e.g. 12 or 16 bit modes), and whether the native manager has a counterpart (a DCAM pixel-type property, if one is listed in `identity.advanced_properties`) |
| P1 | Both | Pixel size: first with `cameraPixelSizeUm` absent from both entries, then set to the rig's measured value in both | Absent: the logs say where each value came from (native: placeholder warning; bridge: Micro-Manager's pixel-size config or the placeholder). Set: `pixel size` row PASS. **Write down:** the two absent-case values and the log lines |
| G1 | Both | Binning 2 (`binning` round) | `binning: applied` 2 and `frames written` equal on both. Expect a CHECK on `applied shape (manager)` and `recorded shape` on the real camera: the native manager's `setBinning` does not update its `shape` (the frames themselves are binned), while the bridge reports a binned `fullShape`/`shape` (the mocks show it: native 512 x 512 unchanged against bridge 256 x 256). **Write down:** both manager shapes, the recorded frame shape (the truth), `pixel_size_um` after binning on both (neither rescales it), and which side the GUI shows right |
| G2 | Both | ROI 512 x 512 at (301, 203) (`ROI` round) | `ROI: applied frame start` / `applied shape` equal on both, and `recorded shape` equals the applied shape. DCAM snaps the subarray to its step and the Settings panel rounds up to multiples of 4, so the applied start will not be 301, 203. **Write down:** requested, the applied start and shape on both, the recorded frame shape, and the log line where either side says it snapped the request |
| G3 | Both | ROI at binning 2 (`ROI at binning` round: `--roi` and `--binning` together) | Recorded frame shape equals the requested width x height on both sides. **Known to disagree in design:** the Settings panel multiplies its ROI fields by the binning before it calls `crop()` (native `crop()` takes unbinned sensor coordinates), while the bridge's `crop()` takes Micro-Manager's binned pixels (`camera.py`). On the demo camera this session saw a request of x0 16, y0 16, 64 x 64 at binning 2 become start (32, 32), 128 x 128 in a bridged recording, i.e. the ROI doubles. The demo and native mocks cannot show the effect in the diff (the mock does not bin). **Write down:** requested, applied start and shape and recorded shape on both; if the bridge's recorded shape is twice the request, that is the finding and a decision for Lenny (the bridge divides by the binning for the Settings convention, or the panel learns which convention a detector uses) |
| N1 | Both | Property-name mapping: read the diff's "Property names that exist in both" table and fill the mapping table below | Every ImSwitch2 setup id the rig uses has a Micro-Manager counterpart, or is recorded as having none. **Write down:** for each, the native value and its `text_options` (from `identity.advanced_properties` in the native report) and the MM value and allowed values (`identity.parameters.<name>.options` in the bridge report) |
| T1 | Both, only if the DAQ has a TTL line to the camera | Externally triggered frames. Native: set `Trigger source` to *External "frame-trigger"* and run the rig's usual scan (the template has no `digitalLine`; add the rig's, as in `example_monalisa.json`). Bridged: add the same `digitalLine` to `HamamatsuMM`, set the property `TRIGGER SOURCE` to `EXTERNAL` in the Settings panel (and `TRIGGER MODE` if the adapter has it), run the same short scan with a recording | **Not required to pass.** `isScanDriven` is False for both managers (it means APD / PMT / Time Tagger: the scan owns the acquisition; a trigger-driven camera is free-running by that definition, `DetectorManager.isScanDriven`), so that flag does not separate them. What does: the bridge has no `Trigger source` parameter with the native three choices (start-trigger / frame-trigger map to `trigger_source` + `trigger_mode`), no test in this repository exercises a sequence acquisition on an external trigger, and the plan lists scanning as native (§4.4, §1). Scan-triggered recording through the bridge is therefore **not expected to work in P1**. **Write down what happens:** frames recorded against the frames the scan expected, the recording's end state and message, whether Stop ends it, whether the stream faults (`fault.kind`), and the native result for reference |
| H1 | Bridged | Live view running; Hardware → Hardware status, reconnect the `MMCore` hub (and the camera) | The status goes through reconnecting back to connected; live view resumes at the same frame shape and dtype; no lost-frames fault; the camera is usable afterwards. **Write down:** seconds to resume, the status text, any log ERROR. Then close ImSwitch2 and start the **native** variant: if it opens the camera, the reload released DCAM |
| X1 | Bridged | Close the window with live view running; then, in the same shell, `echo %ERRORLEVEL%` (cmd) or `$LASTEXITCODE` (PowerShell); repeat after a finished recording | The window closes without a hang, the process ends, exit code 0, no crash dialog and no "Application Error" in the Windows Event Viewer. The next start of the native variant opens the camera at once (DCAM released). **Write down:** seconds from clicking close to the process ending, exit code, any event-log entry. The macOS end-to-end test measured a clean close and a clean exit with the demo camera; Windows was never measured |
| K1 | Rig with two Hamamatsu cameras (MoNaLISA `Green` / `Red`) | Both bridged with a `.cfg` from the wizard that lists both (system-config mode: `configFile` on the hub, `label` per camera; the device name of the second camera is whatever the wizard shows, verify it there). Same ROI, binning and pixel type on both; live view of both, then a recording of both | Both stream together (the host requires equal frame size and pixel type); frame rates close to each side alone. By design **a recording of one camera refuses the other camera's start** or an ROI change while it records (the single MMCore buffer is cleared by any start or `setROI`): start both before the recording. **Write down:** fps of each alone and together, lost frames of both, the refusal text if you start the second camera mid-recording |

## P1 — settings and configuration paths

| # | Rig | Do | Passes if / write down |
|---|---|---|---|
| S1 | Bridged | In the Settings panel, while live, edit: the exposure; then a Micro-Manager property such as `READOUT SPEED` (name per row N1); then `Binning` | The exposure applies without restarting the stream (the image does not blink); any other property stops and restarts the stream (documented: MMCore cannot say which properties change the frame), live view resumes with no lost-frames fault. The native advanced-property tab does the equivalent. **Write down:** per edit, whether the stream restarted, seconds to resume, and whether the read-back equals what you set |
| C1 | Bridged | In the config editor, **Import Micro-Manager…** on the wizard's `.cfg` | The camera appears as a `micromanager.camera` detector with the right `label` (the label picker lists the `.cfg`'s cameras); with the hub's `configFile` pointing at the `.cfg` the setup boots and row B2 repeats. **Write down:** the generated entry (paste the JSON), the report dialog's lines |
| C2 | Bridged | `python -m imswitch_device_micromanager convert wizard.cfg -o rig.json` then `python -m imswitch.imcontrol.model.plugins validate-setup rig.json` | `validate-setup` clean; the conversion report lists the devices it did not map (Hub, Core, Serial, AutoFocus and the like, one line each) and says which values you still fill in (`cameraPixelSizeUm` is left out on purpose). **Write down:** the report, and whether the converted camera entry equals the template's `HamamatsuMM` entry apart from `adapter` / `device` (system-config mode has `label` only) |

## Property-name mapping to fill in (row N1)

Native setup ids are DCAM property names lower-cased with spaces replaced by
underscores (`hamamatsu.py` `convertPropertyName`); `getAdvancedPropertyInfo()` /
`setAdvancedProperty()` take them verbatim, and a `hamamatsu` block in the setup
file is `setPropertyValue(id, number)` per entry. Micro-Manager's `HamamatsuHam`
adapter should expose DCAM properties under their DCAM names in upper case;
everything in the Micro-Manager column is **to verify in the Hardware
Configuration Wizard** (it could not be confirmed from source). The diff
matches names on letters and digits only (`trigger_source` ↔ `TRIGGER SOURCE`)
and prints every match with both values.

| Native id / manager parameter | What the repo says it is | Micro-Manager property (verify) | Native value → MM value (fill in) |
|---|---|---|---|
| `exposure_time` (parameters `Set exposure time`, `Real exposure time`) | seconds; the manager reads `Real exposure time` back after every write | `Exposure` (milliseconds; the bridge's `Exposure` parameter, `setExposure`) | |
| `binning` (parameter via `setBinning`) | written as the text `NxN` (`1x1`, `2x2`, `4x4`); manager offers 1, 2, 4 | `Binning` (the bridge reads its allowed values as `supportedBinnings`; expect `1`, `2`, `4`) | |
| `trigger_source` | `1` = internal, `2` = external (`Trigger source` choices *Internal trigger* / *External "start-trigger"* / *External "frame-trigger"*) | `TRIGGER SOURCE`, `INTERNAL` / `EXTERNAL` | |
| `trigger_mode` | `1` with source 2 = *frame-trigger*; `6` with source 2 = *start-trigger* | `TRIGGER MODE` (values not known here) | |
| `trigger_active` | `2` in the MoNaLISA block; meaning not stated in the repo | `TRIGGER ACTIVE` | |
| `trigger_polarity` | `2` in the MoNaLISA block; meaning not stated in the repo | `TRIGGER POLARITY` | |
| `trigger_global_exposure` | `5` in the MoNaLISA block; meaning not stated in the repo | `TRIGGER GLOBAL EXPOSURE` | |
| `readout_speed` | `3` in the MoNaLISA block; meaning not stated in the repo | `READOUT SPEED` (the adapter may spell it `ReadoutSpeed`) | |
| `subarray_hpos`, `subarray_vpos`, `subarray_hsize`, `subarray_vsize`, `subarray_mode` | the ROI in unbinned sensor pixels; mode text `ON` / `OFF` (numeric `ON` is 2); hardware step granularity, the manager reads back what DCAM applied | the bridge writes `setROI(label, x, y, w, h)`, which the adapter maps to the subarray; whether `SUBARRAY …` / `SENSOR MODE` properties are also exposed is to verify | |
| `image_width`, `image_height`, `image_framebytes` | read-only sensor geometry | `CCD size` style properties or none; the bridge takes the geometry from `getImageWidth/Height` | |
| `timing_readout_time`, `internal_frame_interval`, `internal_frame_rate` (parameters `Readout time`, `Internal frame interval`, `Internal frame rate`) | read-only timings in seconds, seconds and frames per second | possibly the read-only `TIMING READOUT TIME`, `INTERNAL FRAME INTERVAL`, `INTERNAL FRAME RATE`; the diff's mapping table will show them if present | |
| pixel type (a DCAM pixel-type property, if `identity.advanced_properties` lists one) | storage format | `PixelType` (`16bit` as the property value, `GRAY16` as the frame tag) | |
| `Camera pixel size` (manager parameter, from `cameraPixelSizeUm`) | µm per pixel; default 0.15 | the bridge parameter of the same name; falls back to MM's pixel-size config (`getPixelSizeUm()` with the binning divided out) | |

The numeric codes (`readout_speed: 3`, `trigger_active: 2`, …) are DCAM mode
values whose meaning is not written anywhere in this repository. The native
report carries each property's `text_options` (text → code), and the bridge
report carries each property's allowed values: that pairing is the table.

## What the kit does not measure

- The scene: both runs must look at the same thing; the mean intensities are
  printed without a verdict.
- Timing on a wire (a scope on the camera's trigger input, exposure out): the
  frame rates are software counts. If the rig has a scope on the camera's
  exposure-out line, a 10 ms exposure at both drivers is the better check of
  "same exposure semantics".
- Anything other than mono 16-bit full-frame streaming, binning and one ROI:
  colour, other pixel formats and the camera's special modes (light-sheet
  readout, photon number resolving) are out of scope of the bridge (§4.4).

## Results

Fill in during the session: one row per plan row, numbers pasted, the commit of
the build, the camera model and the Micro-Manager nightly used. Paste the diff
table of D0 under the table.

| # | Date | Native | Bridged | Passes? | Note / decision |
|---|---|---|---|---|---|
| B1 | | | | | |
| B2 | | | | | |
| W1 | | | | | |
| M1 | | | | | |
| M2 | | | | | |
| D0 | | | | | |
| E1 | | | | | |
| F1 | | | | | |
| R1 | | | | | |
| R2 | | | | | |
| Y1 | | | | | |
| P1 | | | | | |
| G1 | | | | | |
| G2 | | | | | |
| G3 | | | | | |
| N1 | | | | | |
| T1 | | | | | |
| H1 | | | | | |
| X1 | | | | | |
| K1 | | | | | |
| S1 | | | | | |
| C1 | | | | | |
| C2 | | | | | |

Build: commit ______ · camera ______ · Micro-Manager nightly ______ · pymmcore ______ · Python ______
