# imswitch-device-thorlabs

Thorlabs scientific camera (TSI SDK) support for ImSwitch2, as an external
plugin. This is the first **extraction** of an in-tree ImSwitch device manager
into a plugin package: the `ThorCamTSIManager` and its camera interface were
moved out of the core tree behind the `imswitch.manifest` entry point.

Supports Zelux, Kiralux, and Quantalux cameras via `thorlabs_tsi_sdk`.

| Contribution id | Kind | Class |
|---|---|---|
| `thorlabs.tsi-camera` | detector | `ThorCamTSIManager` |

The legacy class name `ThorCamTSIManager` is kept as an alias, so existing setup
files that use `"managerName": "ThorCamTSIManager"` resolve to this plugin
unchanged once it is installed.

## Install

```bash
python -m pip install imswitch-device-thorlabs            # mock-capable, no SDK
python -m pip install "imswitch-device-thorlabs[hardware]" # + thorlabs_tsi_sdk
```

The `hardware` extra pulls in `thorlabs-tsi-sdk`, which also needs the Thorlabs
LabOne/ThorCam runtime and (on Windows) the ThorCam DLLs. The mock path needs
none of that.

## Mock mode

Set `cameraSerial` to a value starting with `MOCK_` (e.g. `"MOCK_KIRALUX"`) to
load a deterministic mock camera that produces synthetic frames — useful for
discovery, headless tests and CI without hardware. The manager also falls back
to the mock automatically if the TSI SDK can't be imported or the camera can't
be opened.

See `setup_templates/thorcam_tsi_mock.json` for a ready-to-use mock detector
block, and `schemas/thorcam_tsi.schema.json` for all `managerProperties`.

## Migrating from the in-tree manager

During the transition the manager exists both in the ImSwitch core tree and
here. Installing this plugin makes the registry resolve `ThorCamTSIManager`
(and `thorlabs.tsi-camera`) to the plugin; uninstalling falls back to the
in-tree copy. Once this package is published, the in-tree copy will be removed
and ImSwitch will tell users to `pip install imswitch-device-thorlabs`.

## License

GPL-3.0-or-later. Replace the placeholder `LICENSE` with the full GPL-3.0 text
before publishing.
