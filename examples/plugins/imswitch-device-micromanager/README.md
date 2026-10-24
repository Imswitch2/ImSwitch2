# imswitch-device-micromanager

Micro-Manager device bridge for ImSwitch2, built on
[`pymmcore-plus`](https://github.com/pymmcore-plus/pymmcore-plus): every device a
Micro-Manager adapter can drive becomes an ordinary ImSwitch2 device. The design
and the numbers behind it are in `docs/design/plans/micromanager-bridge.md` in
the ImSwitch2 repository.

## Status: Phase 1b (hub and camera)

| Contribution id | Kind | Class | What it is |
|---|---|---|---|
| `micromanager.core` | rs232 | `MicroManagerCoreManager` | The shared MMCore hub: owns the one `CMMCorePlus`, its circular buffer and the frame drain thread |
| `micromanager.camera` | detector | `MicroManagerCameraManager` | One Micro-Manager Camera device as an ImSwitch2 detector (mono only) |

Stage, light and flip-mirror managers follow in later phases; they all name the
hub in `managerProperties.rs232device`. The hub never lets a startup failure
escape: a missing install, missing adapters or a bad `.cfg` becomes an ERROR
status carrying the Micro-Manager message, and a dependent camera is then built
in the same ERROR state (reconnecting the hub from Hardware status brings it
up). A wrong label, a label that is not a camera, or a colour camera is a setup
error and refuses to start with a message that says what to change.

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

## Tests

The tests run against the real MMCore with the demo adapters; no hardware and no
mocks.

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
