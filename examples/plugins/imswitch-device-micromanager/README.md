# imswitch-device-micromanager

Micro-Manager device bridge for ImSwitch2, built on
[`pymmcore-plus`](https://github.com/pymmcore-plus/pymmcore-plus): every device a
Micro-Manager adapter can drive becomes an ordinary ImSwitch2 device. The design
and the numbers behind it are in `docs/design/plans/micromanager-bridge.md` in
the ImSwitch2 repository.

## Status: Phase 1a (hub only)

| Contribution id | Kind | Class | What it is |
|---|---|---|---|
| `micromanager.core` | rs232 | `MicroManagerCoreManager` | The shared MMCore hub: owns the one `CMMCorePlus`, its circular buffer and the frame drain thread |

Camera, stage, light and flip-mirror managers follow in later phases; they all
name the hub in `managerProperties.rs232device`. The hub never lets a startup
failure escape: a missing install, missing adapters or a bad `.cfg` becomes an
ERROR status carrying the Micro-Manager message, and the dependent devices then
fail one by one with "hub is not connected".

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
