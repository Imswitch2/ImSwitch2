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

**Phase 0 passed on a DMK 33UX250 (serial 43710086) on 2026-07-22:** 10 software
triggers produced 10 frames, all 10 distinct. That validates the premise — IC4's
QueueSink delivers one distinct frame per trigger, which the legacy poll-based
path could not.

Still outstanding: the same test driven by **external TTL** from the TriggerScope,
and the in-app tests (Phases 1-3 of the test plan). Do not remove the in-tree
`TISManager` before those pass.

### Measured on the DMK 33UX250

These came off the camera, not the datasheet, and the driver is written against
them:

| Property | Values | Note |
|---|---|---|
| `PixelFormat` | `Mono8`, `Mono16` | ships on **Mono8** |
| `TriggerSelector` | `FrameStart`, `ExposureActive` | driver sets `FrameStart` |
| `TriggerSource` | `Line1`, `Software`, `Any` | ships on `Any`; use **`Line1`** for TriggerScope TTL |
| `TriggerActivation` | `RisingEdge`, `FallingEdge` | ships on **`FallingEdge`** — see below |
| Full frame | 2448 x 2048 | min sink buffers: 6 |

Two of these bit us:

- `numpy_copy()` returns **(H, W, 1)** for mono formats, not (H, W). The driver
  squeezes the channel axis; without that, `getChunk` stacks to (N, H, W, 1) and
  `readChunk` hands the recorder 3-D "frames".
- The camera ships on **`FallingEdge`**, so it latches on the *trailing* edge of
  a TriggerScope pulse. Frames still arrive, delayed by the pulse width — skewed
  timing rather than an obvious failure. Set `trigger_activation` explicitly.

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

### Real hardware, TriggerScope-triggered

Values below are the ones measured on the DMK 33UX250; substitute your own
serial. `trigger_activation` matters — the camera does *not* default to
`RisingEdge`.

```json
{
  "detectors": {
    "TISCam": {
      "managerName": "tis.camera-ic4",
      "managerProperties": {
        "cameraSerial": "43710086",
        "cameraPixelSizeUm": 0.15,
        "pixelFormat": "Mono16",
        "defaults": {
          "exposure_us": 5000,
          "trigger_mode": "Off",
          "trigger_source": "Line1",
          "trigger_activation": "RisingEdge"
        }
      },
      "forAcquisition": true
    }
  }
}
```

Leave `trigger_mode` on `"Off"` for first light — switch to `Hardware` from the
detector settings once a free-running image looks right.

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

46 tests, no hardware required. The load-bearing ones were verified by mutation
rather than assumed: reintroducing the duplicate-frame bug fails
`test_each_trigger_yields_a_distinct_frame`, returning a 2-D chunk fails seven
tests including the `readChunk` fan-out, dropping the channel-axis squeeze fails
six (`getChunk` goes 4-D), and reverting any of the driver fixes (library-init
guard, retained live frame, ROI clamping) fails its own test.

`test_ic4_driver_contracts.py` covers the parts of the real IC4 path that do not
need the SDK, using fakes shaped like the API surface verified against the
vendor's own sources.

## License

GPL-3.0-or-later, matching ImSwitch. The `imagingcontrol4` SDK is a pip
dependency under The Imaging Source's own terms and is not vendored here.
