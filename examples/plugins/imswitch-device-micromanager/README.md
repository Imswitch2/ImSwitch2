# imswitch-device-micromanager

Micro-Manager device bridge for ImSwitch2, built on
[`pymmcore-plus`](https://github.com/pymmcore-plus/pymmcore-plus): every device a
Micro-Manager adapter can drive becomes an ordinary ImSwitch2 device. The design
and the numbers behind it are in `docs/design/plans/micromanager-bridge.md` in
the ImSwitch2 repository.

## Status: Phase 2 (hub, camera, stage, light, flip mirror, converter)

| Contribution id | Kind | Class | What it is |
|---|---|---|---|
| `micromanager.core` | rs232 | `MicroManagerCoreManager` | The shared MMCore hub: owns the one `CMMCorePlus`, its circular buffer and the frame drain thread |
| `micromanager.camera` | detector | `MicroManagerCameraManager` | One Micro-Manager Camera device as an ImSwitch2 detector (mono only) |
| `micromanager.stage` | positioner | `MicroManagerStageManager` | One Micro-Manager Stage (one axis) or XYStage (axes `X`, `Y`) as an ImSwitch2 positioner |
| `micromanager.light` | laser | `MicroManagerLightManager` | A Micro-Manager Shutter, State device position or two-valued property as an on/off light, with an optional numeric power property |
| `micromanager.flip-mirror` | flip_mirror | `MicroManagerFlipMirrorManager` | A Micro-Manager State device with two or more positions (flip mount, filter wheel, objective turret, light path), or a Shutter as a two-state switch, as a flip mirror (an N-position selector) |

Every bridged device names the hub in `managerProperties.rs232device`. The user
documentation (install matrix per platform, every manager, the converter, known
limits) is `docs/devices/micromanager.rst` in the ImSwitch2 repository. The hub never lets a startup failure
escape: a missing install, missing adapters or a bad `.cfg` becomes an ERROR
status carrying the Micro-Manager message, and a dependent camera is then built
in the same ERROR state (reconnecting the hub from Hardware status brings it
up). A wrong label, a label that is not a camera, stage or the device type the light's
`enable` needs, wrong axes for the
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
circular buffer). While a camera runs and a recording is reading its frames
(the camera is leased and a `RAW` chunk consumer is registered), no other camera
can start streaming, live view included, and no other camera's ROI can change:
both clear MMCore's one buffer, and a frame the recording camera produced at
that moment would vanish without a trace. The call raises `MMCoreHostError`
naming the recording camera; stop the recording first, or start the cameras
together before it begins. Live view of two cameras without a recording is not
affected.

**Frames.** `getChunk` returns this camera's frames from the host queue as a
list of `(height, width)` arrays; `getLatestFrame` shows the newest frame
without taking frames away from a recording, so live view keeps running while a
recording reads the chunk path. Lost frames (the buffer overflowed, or the
stream ended by itself) make the next `getChunk` and the `stopAcquisition` raise
`FramesLostError`, which the detector lease table turns into the hardware fault.
A fault found while a setting is changed on a running stream (a property write, a
binning change) does not cancel the change or leave the stream stopped: the
change is applied, the stream restarts, the fault stays pending for the
recording's next `getChunk` / `stopAcquisition`, and the setting call raises it
as well. Live view keeps showing the last frame after a fault, so the camera's
status (still connected) carries a "lost frames" summary and the fault text in
its details until the stream is stopped or restarted.

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

## The light

A light is an ImSwitch2 laser. `label` is the Micro-Manager device that switches
emission, and `enable` says how. The loading modes are the camera's (`.cfg` label,
or inline `adapter` + `device` + optional `parent` and `preInitProperties`); the
properties are documented in `schemas/micromanager_light.schema.json`.

A `Shutter` device (`setShutterOpen`), which is what the template uses:

```json
"lasers": {
  "LED": {
    "managerName": "micromanager.light",
    "managerProperties": {
      "rs232device": "MMCore", "label": "LED Shutter", "enable": {"kind": "shutter"}
    },
    "wavelength": 470,
    "valueRangeMin": null,
    "valueRangeMax": null
  }
}
```

A position of a `State` device (`setState`): `on` and `off` are state labels or
state indices, and a label the device does not have is refused at startup with the
device's labels:

```json
"managerProperties": {
  "rs232device": "MMCore", "label": "LED",
  "enable": {"kind": "state", "on": "470nm", "off": "Closed"}
}
```

A property that takes one value for emission and another for dark
(`setProperty`); when the property lists allowed values both must be among them
(numbers match by value, so `1` is `"1"`), and a read-only property is refused:

```json
"managerProperties": {
  "rs232device": "MMCore", "label": "White Light Shutter",
  "enable": {"kind": "property", "name": "State", "on": "1", "off": "0"}
}
```

The device type must fit `enable.kind` (a `Shutter` for `shutter`, a `State`
device for `state`; `property` takes any device); a mismatch is refused naming
the type found. Every switch is read back from the device, and a device that does
not show the new state is a failed command: `setEnabled` logs it and returns
`False` (the laser panel shows the light off), `applyEnabled` raises
`RawDriveError`.

**Power.** Without a `power` block the light is binary: an on/off switch, no power
field. With one, a numeric (float or integer) property sets the output:

```json
"lasers": {
  "LED": {
    "managerName": "micromanager.light",
    "managerProperties": {
      "rs232device": "MMCore", "label": "LED Shutter", "enable": {"kind": "shutter"},
      "power": {"label": "DA", "property": "Voltage", "units": "V", "decimals": 2}
    },
    "wavelength": 470,
    "valueRangeMin": 0.0,
    "valueRangeMax": 5.0
  }
}
```

`units` and `decimals` are **required** with `power`: Micro-Manager properties
carry neither, and the bridge does not guess `mW` or a precision. `power.label`
defaults to `label` (a power device on another label must be loaded by the hub's
`.cfg`). `valueRangeMin` and `valueRangeMax` become required numbers: they are the
safe range of the property, they must lie inside the limits Micro-Manager states
for it (a range Micro-Manager cannot reach is refused at startup, both ranges in
the message), and a value outside them is refused even where Micro-Manager would
take it. The value is written with exactly `decimals` decimals and read back; the
laser panel and `applyRawDrive` get what the device shows.

**What it does not do.** Power and emission are separate switches: `setValue`
writes the property and does not switch emission (the `enabled` argument is
ignored). There is no TTL path, so a scan cannot gate the light
(`for_scanning` is ignored), and no modulation. After a hub reconnect and when it
finalizes, the light is switched off and must read back off before it counts as
connected.

## The flip mirror

```json
"flipMirrors": {
  "WhiteLight": {
    "managerName": "micromanager.flip-mirror",
    "managerProperties": {"rs232device": "MMCore", "label": "White Light Shutter"},
    "state_names": {"0": "Closed", "1": "Open"}
  }
}
```

A flip mirror is a switch with N named positions. `label` is a Micro-Manager
**State device with two or more positions** (a flip mount, a filter wheel, an
objective turret, a multi-way light path) or a **Shutter** presented as a
two-state switch (closed is state 0, open is state 1). Every position of a State
device is the state of the same number (Micro-Manager's state index) and is named
by its state label; the Flip Mirrors panel shows up to four positions as buttons
and more as a drop-down. The loading modes are the camera's; the properties are
documented in `schemas/micromanager_flip_mirror.schema.json`. The `FlipMirrorInfo`
fields work as for the Thorlabs MFF: `initial_state` moves the device at startup
to a state `0` to `N-1` (`null` leaves it), and `state_names` (keys `"0"` to
`"N-1"`) renames positions. A state it does not name is called what Micro-Manager
calls the position: its state label, or `Closed` / `Open` for a Shutter.

`invert` swaps logical states 0 and 1 and exists for **two-state devices only**:
it is refused for a device with more positions. The optional
`states: [a, b]` (two state labels or indices used as logical 0 and 1) only orders
the two positions of a **two-position** device; a device with more positions
exposes every position, so `states` is refused there. A State device with fewer
than two positions is refused with its count, and the number of positions is
fixed at startup (a reconnect that finds another count is an error). Two flip
mirrors can follow each other only when they have the same number of positions.
A move is waited for and read back: a device that does not show the new position
is a failed move and raises. A reconnect only observes where the device is; it
never moves it.

## The converter

```
python -m imswitch_device_micromanager convert MMConfig_scope.cfg -o scope_setup.json
```

turns a Micro-Manager `.cfg` into a setup-file skeleton: the hub, plus one entry
per Camera (`detectors`), Stage / XYStage (`positioners`), Shutter (`lasers`) and
State device with a known count of two or more positions (`flipMirrors`: filter
wheels, turrets and light paths are presented as multi-position switches). Device types come from the adapter
libraries (`getAvailableDeviceTypes`, nothing is loaded or initialised);
`--type LABEL=camera|stage|xystage|shutter|state` supplies or overrides one,
`--no-classify` skips the lookup, `--adapter-path DIR` names the adapter folder.
Everything else (State devices with fewer than two or an unknown number of positions,
AutoFocus, SignalIO, Hubs, unresolved devices ...) goes only to the sidecar `scope_setup.conversion-report.txt`
(also printed), one line per device with its label, library, device name, type
and the reason. Values the converter cannot derive are never invented and the
JSON never carries comments: `cameraPixelSizeUm` and a light's `wavelength` are
left out (the report lists them as TODO; `validate-setup` warns about the
wavelength until it is set) and the axis name of a Stage that is not the focus
device is the string `"TODO"`. Exit code 0 when the setup was written, 2 on a
malformed `.cfg` (the message names the line). The output passes
`python -m imswitch.imcontrol.model.plugins validate-setup`.

## In the config editor

Config Studio (Hardware > Edit hardware configuration...) lists the five managers
in its palette and builds their forms from the schemas in this package.

- **Import Micro-Manager...** (toolbar, and under the setup list) takes a `.cfg`,
  runs the converter above and merges the hub, the devices and `availableWidgets`
  into the open config. Taken names get a suffix (`Camera_1`), nothing is
  overwritten, and a window shows the conversion report: written entries, TODOs
  and the devices that were not written. Without the adapters on the machine only
  the hub is imported and the window says so.
- The `label` field of a bridge device is a drop-down of the labels in the hub's
  `.cfg`, narrowed to the manager's device types when they can be classified;
  free text stays possible. `rs232device` lists the `micromanager.core` hubs.

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
`Camera` (a `DemoCamera`, 512 x 512, 16 bit), a `Z` stage, a homeable `XY`
stage, an `LED` light, a `WhiteLight` flip mirror and a `Dichroic` flip mirror
(the demo's ten-position filter wheel). It needs `pip install mm-test-adapters` (or `pip install -e ".[test]"`
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
   panel records a chosen number of frames (HDF5, TIFF or Zarr). The Laser panel
   lists `LED` with an on/off switch that opens and closes the demo `LED Shutter`; the
   Flip Mirrors panel lists `WhiteLight` with the buttons *Closed* and *Open* for the
   demo `White Light Shutter` (the demo configuration leaves it open) and `Dichroic`
   as a drop-down of the wheel's ten positions (`400DCLP`, `Q505LP`, ...).

The template has the plugin's own sections (`rs232devices`, `detectors`,
`positioners`, `lasers`, `flipMirrors`; described above) and exactly one more key:
`availableWidgets`, which lists the panels ImControl builds (Settings, View,
Recording, Image, Positioner, Laser, FlipMirror). ImControl builds no panel that is
not listed, so without it (the default is an empty list) the window would have no
panels at all. Every other section a shipped setup has (`scan`, `rois`, `nidaq`,
`tiling`, ...) is optional: a missing one takes its default and the matching panels
are not listed. The template validates with
`python -m imswitch.imcontrol.model.plugins validate-setup <template>`, and
`tests/test_e2e_app.py` boots the real app on it unchanged (live view, a
30-frame recording read back, both stages, the light, the flip mirror, the close
order, and a clean interpreter exit).

## Tests

The tests run against the real MMCore with the demo adapters; no hardware and no
mocks. `tests/test_e2e_app.py` goes all the way: it boots ImControl headlessly
on the demo template (only the napari canvas is replaced, it cannot get an
OpenGL context offscreen), so it needs the same environment as ImSwitch2's own
UI tests: `QT_QPA_PLATFORM=offscreen` on a machine without a display. The
tests do not share the MMCore between processes, so do not run them with
`pytest-xdist`; `test_e2e_app.py` shares one app between tests that run in file
order and skips itself under `pytest-xdist` and `pytest-randomly`.

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
only start with the frame shape and dtype of the one already streaming, and not
at all while another running camera is being recorded (a start or `setROI` clears
the buffer; the frame a running camera inserts after the drain would be lost
silently).
