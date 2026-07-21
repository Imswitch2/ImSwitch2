# imswitch-device-tis

The Imaging Source camera support for ImSwitch2, built on **IC Imaging Control 4**
(`imagingcontrol4`).

Contributes one detector manager:

| Contribution id | Kind | Class | Hardware |
|---|---|---|---|
| `tis.camera-ic4` | detector | `TISCameraIC4Manager` | TIS GenTL/USB3 Vision cameras (developed against the DMK 33UX250) |

## Why this exists

ImSwitch's in-tree `TISManager` drives the same cameras through `pyicic`, a
vendored ctypes wrapper around the legacy IC3 `tisgrabber` DLL. That path reads a
**single buffer the driver overwrites in place**, so it cannot capture one
distinct frame per hardware trigger:

- a triggered scan returns the *same* image at every position;
- a 10-frame recording differs only in the first frame.

IC4's `QueueSink` retains every delivered frame until it is drained, which is the
actual fix. It also makes the hardware trigger a software-settable property, so a
scan can arm and disarm it instead of relying on a manual step in the vendor's
properties dialog.

Full rationale, the verified vendor-API details, and the rig-validation plan:
`docs/design/plans/tis-camera-ic4-migration.md` in the ImSwitch repository.

## Status

**The mock path is complete and tested. The real IC4 path is written but has not
run against hardware yet** — it is gated on the Phase 0 rig probe in the plan
(does the camera enumerate under the IC4 GenTL producer, and does one TTL pulse
yield exactly one frame). Do not remove the in-tree `TISManager` before that
passes.

## This plugin does not claim the name `TISManager`

Deliberately, and unlike the sibling `imswitch-device-thorlabs` plugin.

That one was a pure extraction — the same code moved to a new home — so
inheriting the legacy class name as an alias was safe. This is a **rewrite onto a
different SDK**. Since `MultiManager._resolveManagerClass` resolves registry
contributions *ahead of* in-tree managers (with only a log warning), aliasing
`TISManager` would silently switch every existing TIS setup from IC3 to this
driver the moment someone ran `pip install` — a hardware-affecting change nobody
asked for.

So migration is **opt-in**: point a detector at `tis.camera-ic4` in your setup
file. Once the rig signs off and the in-tree `TISManager` is deleted, the alias
can be added and old setups will follow automatically — as one deliberate
migration rather than a side effect of installing a package.

## Installation

```bash
pip install imswitch-device-tis            # mock only
pip install imswitch-device-tis[hardware]  # + the imagingcontrol4 SDK
```

The `hardware` extra installs the Python bindings. It **cannot** install the
**IC4 GenTL Producer (USB3 Vision)**, which is a separate system-level component
from The Imaging Source and is required for a camera to enumerate at all. A
camera still bound only to the legacy IC3 driver will not appear in
`ic4.DeviceEnum.devices()`.

## Setup file

```json
{
  "detectors": {
    "TISCam": {
      "managerName": "tis.camera-ic4",
      "managerProperties": {
        "cameraSerial": null,
        "cameraPixelSizeUm": 0.15,
        "pixelFormat": "Mono16",
        "defaults": { "exposure_us": 5000, "trigger_mode": "Off" }
      },
      "forAcquisition": true
    }
  }
}
```

`cameraSerial` identifies the camera by serial rather than by a positional list
index (as the legacy `cameraListIndex` did), because an index silently rebinds to
a different camera when USB enumeration order changes. `null` opens the first
device found.

### Headless / no hardware

Set `cameraSerial` to any string starting with `MOCK_` to load `MockIC4Camera`,
which needs neither the SDK nor a camera. See
`src/imswitch_device_tis/setup_templates/tis_camera_ic4_mock.json`.

The mock is **trigger-gated**: frames appear only after a software or hardware
trigger, and every frame is distinct. Both properties are load-bearing — a mock
that emitted frames freely, or emitted identical ones, would pass tests that the
bug this plugin exists to fix should fail.

## Triggering

| Trigger Mode | Behaviour |
|---|---|
| `Off` | Free-running; frames arrive at the device's own rate |
| `Hardware` | Camera waits for an external pulse on `trigger_source` (e.g. `Line1`) |

Set it from the detector settings GUI, from `defaults.trigger_mode`, or
programmatically via `manager.setTriggerEnabled(True)` so a scan can arm before a
bead scan and disarm afterwards.

## Tests

```bash
pip install -e ".[test]"
pytest tests/
```

39 tests, no hardware required. The load-bearing ones were verified by mutation
rather than assumed: reintroducing the duplicate-frame bug fails
`test_each_trigger_yields_a_distinct_frame`, returning a 2-D chunk fails seven
tests including the `readChunk` fan-out, and reverting any of the driver fixes
(library-init guard, retained live frame, ROI clamping) fails its own test.

`test_ic4_driver_contracts.py` covers the parts of the real IC4 path that do not
need the SDK, using fakes shaped like the API surface verified against the
vendor's own sources.

## License

GPL-3.0-or-later, matching ImSwitch. The `imagingcontrol4` SDK is a pip
dependency under The Imaging Source's own terms and is not vendored here.
