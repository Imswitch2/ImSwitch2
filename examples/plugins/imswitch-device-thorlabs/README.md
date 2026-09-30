# imswitch-device-thorlabs

Thorlabs device support for ImSwitch2, as an external plugin. This package
holds **extractions** of in-tree ImSwitch device managers, moved out of the
core tree behind the `imswitch.manifest` entry point. It demonstrates that one
plugin can provide managers of several device kinds from the same vendor.

| Contribution id | Kind | Class | Vendor SDK |
|---|---|---|---|
| `thorlabs.tsi-camera` | detector | `ThorCamTSIManager` | `thorlabs-tsi-sdk` |
| `thorlabs.kinesis-stage` | positioner | `KinesisStageManager` | `pylablib` |

Cameras: Zelux, Kiralux, and Quantalux via `thorlabs_tsi_sdk`. Stages: MLS203
two-axis Kinesis motorized stages via `pylablib`.

The legacy class names (`ThorCamTSIManager`, `KinesisStageManager`) are kept as
aliases, so existing setup files that use them as `managerName` resolve to this
plugin unchanged once it is installed.

## Install

The plugin is not published on PyPI. Install it from an ImSwitch2 source
checkout, in the environment ImSwitch2 runs in:

```bash
python -m pip install "./examples/plugins/imswitch-device-thorlabs"            # mock-capable, no SDK
python -m pip install "./examples/plugins/imswitch-device-thorlabs[hardware]"  # + vendor SDKs
```

The `hardware` extra pulls in `thorlabs-tsi-sdk` (camera) and `pylablib`
(stage), which also need the corresponding Thorlabs runtimes and (on Windows)
DLLs. `thorlabs-tsi-sdk` is not on PyPI either: install it from the ThorCam
download first (see the ImSwitch2 installation docs, "Vendor SDKs"), and the
extra then finds it. The mock paths need none of that.

## Mock mode

- **Camera:** set `cameraSerial` to a value starting with `MOCK_` (e.g.
  `"MOCK_KIRALUX"`) to load a deterministic mock camera producing synthetic
  frames. It also falls back to the mock automatically if the TSI SDK can't be
  imported or the camera can't be opened.
- **Stage:** set `useMock: true` in `managerProperties` to force the simulated
  stage. Without it, the manager tries the real driver first and falls back to
  the mock when `pylablib`/hardware is unavailable.

See `setup_templates/*.json` for ready-to-use mock blocks and `schemas/` for
all `managerProperties`.

## Migrating from the in-tree managers

During the transition the managers exist both in the ImSwitch core tree and
here. Installing this plugin makes the registry resolve the legacy class names
(and the `thorlabs.*` ids) to the plugin; uninstalling falls back to the
in-tree copies. Once this package is published, the in-tree copies will be
removed and ImSwitch will tell users to `pip install imswitch-device-thorlabs`.

## License

GPL-3.0-or-later. Replace the placeholder `LICENSE` with the full GPL-3.0 text
before publishing.
