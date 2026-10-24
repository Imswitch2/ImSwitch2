# imswitch-device-micromanager

Micro-Manager device bridge for ImSwitch2, built on
[`pymmcore-plus`](https://github.com/pymmcore-plus/pymmcore-plus): every device a
Micro-Manager adapter can drive becomes an ordinary ImSwitch2 device. The design
and the numbers behind it are in `docs/design/plans/micromanager-bridge.md` in
the ImSwitch2 repository.

## Status: Phase 1c (hub, camera and stage)

| Contribution id | Kind | Class | What it is |
|---|---|---|---|
| `micromanager.core` | rs232 | `MicroManagerCoreManager` | The shared MMCore hub: owns the one `CMMCorePlus`, its circular buffer and the frame drain thread |
| `micromanager.camera` | detector | `MicroManagerCameraManager` | One Micro-Manager Camera device as an ImSwitch2 detector (mono only) |
| `micromanager.stage` | positioner | `MicroManagerStageManager` | One Micro-Manager Stage (one axis) or XYStage (axes `X`, `Y`) as an ImSwitch2 positioner |

Light and flip-mirror managers follow in later phases; they all name the
hub in `managerProperties.rs232device`. The hub never lets a startup failure
escape: a missing install, missing adapters or a bad `.cfg` becomes an ERROR
status carrying the Micro-Manager message, and a dependent camera is then built
in the same ERROR state (reconnecting the hub from Hardware status brings it
up). A wrong label, a label that is not a camera or stage, wrong axes for the
stage type, or a colour camera is a setup error and refuses to start with a
message that says what to change.

```json
"rs232devices": {
  "MMCore": {
    "managerName": "micromanager.core",
    "managerProperties": {
      "configFile": "~/MMConfig_scope.cfg",
      "adapterPaths": [],
      "circularBufferMB": null,
      "logDir": null,
      "adoptExistingCore": false
    }
  }
}
```

All properties are optional (`schemas/micromanager_core.schema.json`). Adapter
folders are taken from `adapterPaths`, then the `MICROMANAGER_PATH` environment
variable, then whatever `pymmcore-plus` finds (which falls back to the
`mm-test-adapters` demo adapters).

## The camera

```json
"detectors": {
  "Camera": {
    "managerName": "micromanager.camera",
    "managerProperties": {"rs232device": "MMCore", "label": "Camera"},
    "forAcquisition": true
  }
}
```

**Two ways to bring a camera in.**

*System-config mode* (above): the hub's `.cfg` loaded the device and `label`
names it. A typo is reported with the list of loaded cameras.

*Inline mode*: the entry carries `adapter` and `device` (and optionally `parent`
for the hub device and `preInitProperties`), and the manager loads the camera
itself under `label` and unloads it when it finalizes:

```json
"detectors": {
  "Camera": {
    "managerName": "micromanager.camera",
    "managerProperties": {
      "rs232device": "MMCore", "label": "Camera2",
      "adapter": "DemoCamera", "device": "DCam",
      "preInitProperties": {"MaximumExposureMs": 5000}
    },
    "forAcquisition": true
  }
}
```

Both modes can be mixed in one setup. The properties are documented in
`schemas/micromanager_camera.schema.json`.

**What it presents.** Exposure (ms) is a first-class parameter; every other
Micro-Manager property of the camera becomes a parameter in the group
"Camera properties" (choices become lists, numbers stay numbers, read-only and
free-text properties are shown but not editable; pre-init properties are
skipped; Micro-Manager has no units, so none are shown). `Binning`'s allowed
values are the supported binnings. A value is always read back from the camera,
so the settings show what it took. A change that may alter the frame
(any property, binning, ROI) stops the stream, applies it, re-reads the frame
size and pixel type and restarts the stream. `fullShape` is the size of the
full frame at the current binning, and `crop` stores the ROI the adapter
applied (adapters snap), in Micro-Manager's binned pixels. `dtype` follows the
camera's `PixelType` before the first frame (8-bit cameras are `uint8`).

**Pixel size.** `cameraPixelSizeUm` in the setup file wins. Without it the
manager takes Micro-Manager's current pixel-size configuration
(`getPixelSizeUm()`, divided by the binning, because Micro-Manager includes the
binning), marks the provenance as "Micro-Manager pixel-size config" and, as
for a setup value, does not let a saved widget state override it. The demo
template omits the key on purpose: `MMConfig_demo.cfg`'s `Res10x` supplies
1.0 um. If neither has a value (Micro-Manager reports 0.0 when no pixel-size
configuration matches) ImSwitch2's 0.15 um placeholder applies and is flagged
"assumed default".

**Mono only.** Colour cameras (`32bitRGB`, `64bitRGB`) are refused at start and
when a `PixelType` change would select one (the change is undone): ImSwitch2's
chunk contract is `(frames, height, width)`, which the Zarr writer and napari
layers assume. A per-component split is a later feature. Two cameras can stream
at once only with the same frame size and pixel type (Micro-Manager's single
circular buffer).

**Frames.** `getChunk` returns this camera's frames from the host queue as a
list of `(height, width)` arrays; `getLatestFrame` shows the newest frame
without taking frames away from a recording, so live view keeps running while a
recording reads the chunk path. Lost frames (the buffer overflowed, or the
stream ended by itself) make the next `getChunk` and the `stopAcquisition` raise
`FramesLostError`, which the detector lease table turns into the hardware fault.

## The stage

```json
"positioners": {
  "Z": {
    "managerName": "micromanager.stage",
    "managerProperties": {"rs232device": "MMCore", "label": "Z"},
    "axes": ["Z"],
    "forPositioning": true
  },
  "XY": {
    "managerName": "micromanager.stage",
    "managerProperties": {"rs232device": "MMCore", "label": "XY", "homeable": true},
    "axes": ["X", "Y"],
    "forPositioning": true
  }
}
```

The loading modes are the camera's (`.cfg` label, or inline `adapter` +
`device` + optional `parent` and `preInitProperties`); the properties are
documented in `schemas/micromanager_stage.schema.json`.

**Axes.** A Micro-Manager `Stage` has one axis: `axes` is a one-element list
with any name (`["Z"]`, `["Focus"]`). An `XYStage` has exactly `["X", "Y"]`.
Any other combination, or a label that is not a stage, refuses to start naming
what Micro-Manager has. Positions are micrometres in both systems; nothing is
scaled. `forScanning` must stay false: Micro-Manager stages have no analog scan
line.

**Position.** The tracked position is what the hardware says, never a zero:
`initialPosition` is read from the device, and after a move the position is
read back, so a stage that snaps to its step size shows the position it
reached. The device's own position notifications (a joystick, a front panel)
update it as well. A stage whose hub is down at startup starts with the
position `nan` ("unknown") and refuses every command with
`DeviceNotConnectedError` until the hub is reconnected; the reconnect re-reads
the position (`updatePosition` does the same on demand).

**`waitForDevice`** (default `true`). A move blocks until the device reports it
finished, then the position is read back. With `false` the command returns at
once and the position follows the device's position-changed events; not every
adapter sends them, so use `updatePosition` for an authoritative read. A
blocking move holds the hub lock while it waits, so a long move briefly stops
the camera frame drain.

**`homeable`** (default `false`). Micro-Manager cannot say whether an adapter
supports a home command and homing moves hardware, so it is never tried on its
own. With `homeable: true` the setup declares that it works: the positioner
then starts *unreferenced* and the Reference action (`reference()`) homes the
whole device, waits for it, re-reads the position and marks it referenced. An
adapter without a home command (the demo `Z`) reports Micro-Manager's own
"Unsupported device command" on that action, never at startup. Without
`homeable` the reference action does nothing. A reference position cannot be
given: homing defines the adapter's own origin.

## Install

This plugin needs **Python 3.11 or newer** (`pymmcore` does not support 3.10),
while ImSwitch2 itself still supports 3.10. The Windows and macOS installers
currently bundle 3.10, so installer users cannot load it yet.

```
pip install imswitch-device-micromanager        # from this folder: pip install -e .
```

Which device adapters exist depends on the platform (Micro-Manager's own
nightlies are built for Windows x64 and Intel macOS only):

| Platform | Real hardware adapters | Hardware-free demo adapters |
|---|---|---|
| Windows x64 | `mmcore install` (downloads the nightly that matches `pymmcore`'s device interface) | `pip install mm-test-adapters` |
| macOS Intel | `mmcore install` | `pip install mm-test-adapters` |
| macOS Apple Silicon | none: Micro-Manager publishes no arm64 adapters | `pip install mm-test-adapters` |
| Linux x64 | none: no Micro-Manager nightlies for Linux | `pip install mm-test-adapters` |

`mm-test-adapters` ships the real MMCore `DemoCamera` / `Utilities` /
`SequenceTester` adapters and `MMConfig_demo.cfg`. The bundled setup template
(`setup_templates/demo_scope.json`) points the hub at that demo configuration.

## Run the demo in ImSwitch2

The bundled template `setup_templates/demo_scope.json` is a complete, bootable
setup for the Micro-Manager demo devices (`MMConfig_demo.cfg`): the hub, the
`Camera` (a `DemoCamera`, 512 x 512, 16 bit), a `Z` stage and a homeable `XY`
stage. It needs `pip install mm-test-adapters` (or `pip install -e ".[test]"`
from this folder) next to the plugin, and Python 3.11 or newer.

1. Copy the template into ImSwitch2's setup folder (`~/ImSwitchConfig/imcontrol_setups`,
   or `Documents\ImSwitchConfig\imcontrol_setups` on Windows):

   ```
   python -c "import shutil, pathlib, imswitch_device_micromanager as m; shutil.copy(pathlib.Path(m.__file__).parent / 'setup_templates/demo_scope.json', pathlib.Path.home() / 'ImSwitchConfig/imcontrol_setups')"
   ```

2. Start ImSwitch2 (`imswitch`) and choose the setup: on the very first start
   the setup chooser opens by itself, afterwards use **Hardware > Pick hardware
   setup...** (ImSwitch2 restarts).
3. What you should see: the Settings panel with the camera's Exposure and the
   "Camera properties" group; **Live view** shows the DemoCamera's test image
   in the Image panel; the Positioner panel lists `Z` and
   `XY` (axes `X` and `Y`) with step controls; `XY` starts unreferenced
   and its **Reference** action homes it (the demo `Z` has nothing to
   reference, because the template does not mark it `homeable`); the Recording
   panel records a chosen number of frames (HDF5, TIFF or Zarr).

Besides `rs232devices`, `detectors` and `positioners` (the plugin's own
sections, described above) the template carries exactly one more key:
`availableWidgets`, which lists the panels ImControl builds (Settings, View,
Recording, Image, Positioner). ImControl builds no panel that is not listed,
so without it (the default is an empty list) the window would have no panels at all. Every other section a
shipped setup has (`lasers`, `scan`, `rois`, `nidaq`, `tiling`, ...) is
optional: a missing one takes its default and the matching panels are not
listed. The template validates with
`python -m imswitch.imcontrol.model.plugins validate-setup <template>`, and
`tests/test_e2e_app.py` boots the real app on it unchanged (live view, a
30-frame recording read back, both stages, the close order, and a clean
interpreter exit).

## Tests

The tests run against the real MMCore with the demo adapters; no hardware and no
mocks. `tests/test_e2e_app.py` goes all the way: it boots ImControl headlessly
on the demo template (only the napari canvas is replaced, it cannot get an
OpenGL context offscreen), so it needs the same environment as ImSwitch2's own
UI tests: `QT_QPA_PLATFORM=offscreen` on a machine without a display. The
tests do not share the MMCore between processes, so do not run them with
`pytest-xdist`.

```
pip install -e ".[test]"
python -m pytest -p no:napari examples/plugins/imswitch-device-micromanager/tests
```

(run from the ImSwitch2 repository root; the discovery tests are skipped until
the plugin is installed into the environment).

## Frame path in one paragraph

MMCore has a single circular buffer for all cameras, clears it whenever a
sequence starts or a ROI is set, and does not check that two cameras deliver
equally sized frames. `MMCoreHost` (`_core.py`) therefore owns the buffer: one
drain thread pops with `popNextImageAndMD` into bounded per-camera queues, the
buffer is drained before every state change, sequences run with
`stopOnOverflow=True` and an ended or overflowed sequence becomes a recorded
fault that the next `pop_frames` / `stop_stream` raises, and a second camera can
only start with the frame shape and dtype of the one already streaming.
