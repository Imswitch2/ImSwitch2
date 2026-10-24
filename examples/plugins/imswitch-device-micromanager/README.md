# imswitch-device-micromanager

Micro-Manager device bridge for ImSwitch2, built on
[`pymmcore-plus`](https://github.com/pymmcore-plus/pymmcore-plus): a camera, stage,
light or multi-position switch that a Micro-Manager device adapter can drive becomes an
ordinary ImSwitch2 detector, positioner, laser or flip mirror, and a converter turns an
existing Micro-Manager hardware configuration (`*.cfg`) into a setup-file skeleton. The
plugin needs no change to the ImSwitch2 core and runs on the real MMCore, with
Micro-Manager's own demo adapters when there is no hardware.

This file is the package overview, the install notes and the test instructions. The
reference (every setup field, the converter, the limits, troubleshooting) is
[`docs/devices/micromanager.rst`](../../../docs/devices/micromanager.rst) in the ImSwitch2
repository; the design record and the measurements behind it are
[`docs/design/plans/micromanager-bridge.md`](../../../docs/design/plans/micromanager-bridge.md).

## What the package contains

| Contribution id | Setup section | Class | Presents a Micro-Manager ... as |
|---|---|---|---|
| `micromanager.core` | `rs232devices` | `MicroManagerCoreManager` | the shared MMCore hub: owns the one `CMMCorePlus`, its circular buffer and the frame drain thread |
| `micromanager.camera` | `detectors` | `MicroManagerCameraManager` | Camera, as a detector (mono only) |
| `micromanager.stage` | `positioners` | `MicroManagerStageManager` | Stage (one axis) or XYStage (axes `X`, `Y`), as a positioner |
| `micromanager.light` | `lasers` | `MicroManagerLightManager` | Shutter, State device position or two-valued property, as an on/off light with an optional numeric power property |
| `micromanager.flip-mirror` | `flipMirrors` | `MicroManagerFlipMirrorManager` | State device with two or more positions (flip mount, filter wheel, objective turret, light path), or a Shutter as a two-state switch, as a flip mirror (an N-position switch) |

Besides the managers: the converter (`python -m imswitch_device_micromanager convert`),
a JSON schema per manager for the config editor (`src/imswitch_device_micromanager/schemas/`),
and a bootable setup template for the demo devices
(`src/imswitch_device_micromanager/setup_templates/demo_scope.json`).

Every bridged device names the hub in `managerProperties.rs232device`. A device is brought in
either from the `.cfg` the hub loads (`label` names a device in it) or inline (`adapter` and
`device` load just that device). The hub never lets a startup failure escape: a missing
install, missing adapters or a bad `.cfg` becomes an error status carrying Micro-Manager's
message, and the devices that depend on it start in the same state.

```json
"rs232devices": {
  "MMCore": {
    "managerName": "micromanager.core",
    "managerProperties": {"configFile": "~/MMConfig_scope.cfg"}
  }
},
"detectors": {
  "Camera": {
    "managerName": "micromanager.camera",
    "managerProperties": {"rs232device": "MMCore", "label": "Camera"},
    "forAcquisition": true
  }
}
```

### How frames reach ImSwitch2

MMCore has a single circular buffer for all cameras, clears it whenever a sequence starts or
a ROI is set, and does not check that two cameras deliver equally sized frames. The hub
(`MMCoreHost` in `_core.py`) therefore owns the buffer: one drain thread pops with
`popNextImageAndMD` into bounded per-camera queues, sequences run with `stopOnOverflow=True`,
and an overflow or a sequence that ends by itself becomes a fault that the next chunk read
raises, so a recording that lost frames fails instead of finishing short. A second camera
starts only with the frame shape and pixel type of the one already streaming, and no camera
starts or changes its ROI while another running camera is being recorded.

### Limits

- **Mono cameras only.** Colour cameras are refused.
- **One buffer: equal frame sizes** for cameras that stream at the same time.
- **Lights are not scan-gated**: there is no TTL path and no modulation; stages have no analog
  scan line. Scanning stays with ImSwitch2's native waveform generation.
- **Python 3.11 or newer**, so no installer build yet (the installers bundle 3.10).
- **Not validated on a rig yet**: everything is verified against the real MMCore with the demo
  adapters only. The Windows vendor-DLL case (an adapter whose SDK DLLs live outside the adapter
  folder) is unmeasured.

## Install

The plugin needs **Python 3.11 or newer** (`pymmcore` does not support 3.10), while ImSwitch2
itself still supports 3.10 and the Windows and macOS installers bundle 3.10, so installer users
cannot load it yet. It is not published on PyPI. Install ImSwitch2 from source into a Python
3.11 or newer environment, then the plugin from this folder (run from the repository root):

```
pip install -e ".[test]"                                                  # ImSwitch2 itself
pip install -e "examples/plugins/imswitch-device-micromanager[test]"
```

The plugin depends on `imswitch2>=0.1` and `pymmcore-plus>=0.18,<0.19`. The `test` extra adds
`mm-test-adapters`, `pytest` and `jsonschema`; leave it out on a rig that uses real adapters
only. `pymmcore-plus` pulls in `ome-writers` and `tensorstore`, so the install is larger than
that of the other plugins.

Which device adapters exist depends on the platform. Micro-Manager's own nightly builds exist for
Windows x64 and Intel macOS only:

| Platform | Real hardware adapters | Hardware-free demo adapters |
|---|---|---|
| Windows x64 | `mmcore install` (downloads the nightly that matches the device interface version of `pymmcore`) | `pip install mm-test-adapters` |
| macOS Intel | `mmcore install` | `pip install mm-test-adapters` |
| macOS Apple Silicon | none: Micro-Manager publishes no arm64 adapters | `pip install mm-test-adapters` |
| Linux x64 | none: there are no Micro-Manager nightlies for Linux | `pip install mm-test-adapters` |

`mmcore` is the command line of `pymmcore-plus` and is installed with the plugin: `mmcore install`
downloads the matching Micro-Manager adapters into a per-user folder, `mmcore list` shows the
installs it knows, and `mmcore --version` shows the device interface version the adapters must
match. `mm-test-adapters` ships the real MMCore `DemoCamera`, `Utilities` and `SequenceTester`
adapters and `MMConfig_demo.cfg`.

**Which adapters the hub uses**, in this order: the `adapterPaths` list of the hub's
`managerProperties`; the `MICROMANAGER_PATH` environment variable (the folder of a Micro-Manager
install); whatever `pymmcore-plus` finds on its own (which falls back to the `mm-test-adapters`
demo adapters). The hub logs the folder it chose.

## Try it without hardware

`setup_templates/demo_scope.json` is a complete, bootable setup for the demo devices: the hub, a
`Camera` (a `DemoCamera`, 512 x 512, 16 bit), a `Z` stage, a homeable `XY` stage, an `LED` light,
a `WhiteLight` flip mirror and a `Dichroic` flip mirror (the demo's ten-position filter wheel). It
needs `mm-test-adapters` next to the plugin.

```
python -c "import shutil, pathlib, imswitch_device_micromanager as m; shutil.copy(pathlib.Path(m.__file__).parent / 'setup_templates/demo_scope.json', pathlib.Path.home() / 'ImSwitchConfig/imcontrol_setups')"
```

Then start ImSwitch2 (`imswitch`) and choose the setup: on the very first start the setup chooser
opens by itself, afterwards use **Hardware > Pick hardware setup...** (ImSwitch2 restarts). The
page `docs/devices/micromanager.rst` lists what you should see in each panel.

## Convert a Micro-Manager configuration

```
python -m imswitch_device_micromanager convert /full/path/MMConfig_scope.cfg -o scope_setup.json
python -m imswitch.imcontrol.model.plugins validate-setup scope_setup.json
```

writes the setup skeleton and a `scope_setup.conversion-report.txt` that lists every device it did
not write and every value you still have to fill in (a light's `wavelength`, the axis name of a
stage that is not the focus device). Nothing is loaded or initialised, and nothing is invented. In
the config editor the same conversion is **Import Micro-Manager...** (Hardware > Edit hardware
configuration...), which also gives bridge devices a label drop-down filled from the hub's `.cfg`.

## Tests

The tests run against the real MMCore with the demo adapters; there is no hardware and no hand-written
fake core. `tests/test_e2e_app.py` goes all the way: it boots ImControl headlessly on the demo template
(only the napari canvas is replaced; it cannot get an OpenGL context offscreen), so it needs the same
environment as ImSwitch2's own UI tests, with `QT_QPA_PLATFORM=offscreen` on a machine without a display.
From the ImSwitch2 repository root:

```
pip install -e ".[test]"
pip install -e "examples/plugins/imswitch-device-micromanager[test]"
python -m pytest -p no:napari examples/plugins/imswitch-device-micromanager/tests
```

The tests do not share an MMCore between processes, so do not run them with `pytest-xdist`;
`test_e2e_app.py` shares one app between tests that run in file order and skips itself under
`pytest-xdist` and `pytest-randomly`. The discovery tests are skipped until the plugin is installed
into the environment. CI runs the suite in its own job on Python 3.11.

`tools/p0_probe.py` is the measurement script behind the numbers in the design record (frame rate,
drain latency, overflow behaviour, signal backend); it is not part of the package.

## License

GPL-3.0-or-later, like the other bundled plugins. MMCore is LGPL-2.1 (through the `pymmcore` wheel); the
Micro-Manager device adapters have their own licenses, some wrapping proprietary vendor SDKs.
