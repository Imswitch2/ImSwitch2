# WidefieldStarss → ImSwitch integration plan

Plan for porting device modules from `WidefieldStarss` (WFS) into ImSwitch.
Scope of this document: agreed tasks **#1–#5**. The Arduino/Teensy trigger
port (#6) and the PAX1000 polarimeter (#7) are intentionally out of scope
here and will be tracked separately after design discussion.

Source paths refer to:
- WFS: `/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/`
- ImSwitch: `imswitch/imcontrol/model/`

## Conventions recap (what every port must satisfy)

- **Driver vs manager split.** Hardware-talking code goes under
  `model/interfaces/` (or imports a third-party lib directly). The
  `*Manager` class under `model/managers/<category>/` adapts that driver
  to ImSwitch's abstract base (`PositionerManager`, `RotatorManager`,
  `DetectorManager`).
- **Setup-file plumbing.** Each new manager must be importable from its
  category's `__init__.py` so JSON setup files can name it as a
  `managerName`.
- **Mock fallback.** Follow the `StandaRotatorManager` pattern: if the
  real backend fails to load (missing DLL, no hardware), drop into a
  `Mock*` driver from `model/interfaces/` so headless test profiles
  still boot.
- **No application policy in the driver.** Calibrated H/V positions,
  keypress bindings, GUI widget updates, plotting — all of that stays
  in WFS or moves to ImSwitch controllers, never into the manager.
- **Settings file example.** Each port adds at least a minimal stanza
  to `imswitch/_data/user_defaults/imcontrol_setups/example_no_hardware.json`
  (or a dedicated `*_demo.json`) so the manager is exercised by CI.

---

## #1 — K10CR1 Kinesis rotator

**Source:** [thorlabsrotator_K10CR1.py](../WidefieldStarss/src/WFS/thorlabsrotator_K10CR1.py)
**Target manager:** `imswitch/imcontrol/model/managers/rotators/KinesisRotatorManager.py`
**Driver:** import `pylablib.devices.Thorlabs.KinesisMotor` directly — no
custom interface module needed.

### Interface mapping

| `RotatorManager` API | Backing call |
|---|---|
| `__init__` | `KinesisMotor(snr, is_rack_system=True)` |
| `move_abs(pos_deg)` | `move_to(int(pos_deg * units_per_dg))`, then `wait_move()` |
| `move_rel(d_deg)` | `move_by(int(d_deg * units_per_dg))`, then `wait_move()` |
| `position` (property) | `get_position() / units_per_dg` |
| `finalize()` | `close()` |

### `managerProperties` schema

```json
{
  "snr": "55000194",
  "unitsPerDegree": 136533.33,
  "homeOnInit": true
}
```

`unitsPerDegree` defaults to `136533.33` (K10CR1 spec). Allow override
because Kinesis device drift between firmware revisions is a thing.

### What to drop from the WFS class

- `v_pos`, `h_pos`, `move_to_v`, `move_to_h`, `chained_move_to_*` —
  calibrated angle helpers belong in a controller/script, not the
  driver. Surface them in user scripts via `move_abs(v_pos)`.

### Verification

- Add `KinesisRotatorManager` to a no-hardware mock fallback (use the
  existing `MockStandaMotor` pattern — write a tiny `MockKinesisMotor`
  that just stores position).
- Smoke-test profile: `kinesis_demo.json` with one rotator entry; load
  via `imcontrol` headless to confirm `imswitch.imcontrol._test.unit`
  passes.

**Estimated effort:** small (≤ 1 day). Validates the rotator-port
pattern before tackling #2.

---

## #2 — ELL14 / ELL14K Elliptec rotator

**Source:** [thorlabsrotator_ELL14.py](../WidefieldStarss/src/WFS/thorlabsrotator_ELL14.py)
**Target manager:** `imswitch/imcontrol/model/managers/rotators/ElliptecRotatorManager.py`
**Driver helper:** `imswitch/imcontrol/model/interfaces/elliptecbus.py`
  (port the WFS `_SharedElliptecBus` here).

### Why the shared bus matters

Elliptec is a **multidrop** protocol: multiple rotators (HWP + QWP +
filter wheels …) share one COM port, distinguished by `addr`. The
underlying `pylablib.devices.Thorlabs.ElliptecMotor` connection is
exclusive — opening a second `ElliptecMotor` on the same port fails.

The WFS `_SharedElliptecBus` is a refcounted, port-keyed singleton
that solves exactly this. **Port it verbatim** into
`interfaces/elliptecbus.py`. Each `ElliptecRotatorManager` calls
`get_bus(port, scale)` + `acquire()`/`release()` instead of opening its
own `ElliptecMotor`.

### Interface mapping

| `RotatorManager` API | Backing call (with bus lock held) |
|---|---|
| `move_abs(pos_deg)` | `bus.stage.move_to(pos_deg, addr=self._addr)` |
| `move_rel(d_deg)` | `bus.stage.move_to(get_position() + d_deg, addr=self._addr)` |
| `position` | `bus.stage.get_position(addr=self._addr)` |
| `finalize()` | `bus.release()` (closes bus when last user releases) |

All Elliptec operations are synchronous (no async `wait_move` needed).

### `managerProperties` schema

```json
{
  "port": "COM20",
  "address": 1,
  "scale": "stage",
  "homeOnInit": false
}
```

### Things to preserve

- Refcount + per-bus `RLock` — multiple managers can move concurrently
  *between addresses* but each command must be atomic on the wire.
- `update_connected_addrs()` after open — required for multidrop.
- The fail-retry loop in `move` (retry up to 5× on exception). Elliptec
  occasionally NAKs on bus contention.

### What to drop

- Same as #1: H/V helpers, `chained_move_to_*`, hardcoded `HWP_*` /
  `QWP_*` module constants.

**Estimated effort:** small-medium (1–2 days). The bus singleton is
the only novel piece; once it's there, the manager itself is ~60 lines.

---

## #3 — Jena piezo Z-stage

**Source:** [ZPiezoControl.py](../WidefieldStarss/src/WFS/ZPiezoControl.py)
**Target manager:** `imswitch/imcontrol/model/managers/positioners/JenaPiezoZManager.py`

### Pattern to copy

Mirror [PiezoconceptZManager.py](imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager.py)
— it is a near-identical role (RS232 single-axis Z piezo). Differences
to handle:

- **Wire protocol.** Jena commands are `cl` / `i1` / `i0` / `rd` /
  `wr,<pos>` (CR-terminated), not Piezoconcept's `MOVEZ`/`GET_Z`.
- **External-control toggle.** Jena requires `i1` to enter external
  control mode before `wr` writes are accepted. Auto-enter on first
  `setPosition`; leave the explicit `activate_ext_control` /
  `deactivate_ext_control` available on the manager for scripts that
  want to release the piezo to its front panel.
- **Closed-loop polling.** WFS polls `read_pos_um()` every 100 ms until
  within 0.1 µm of target, retrying the write at 5 polls, raising
  `TimeoutError` at 10. **Keep this** — most ImSwitch positioners
  blind-write, which is fine for piezos with reliable closed loops but
  Jena units occasionally stall. Make it opt-in via a
  `waitForSettle` manager property (default `True`).

### Interface mapping

| `PositionerManager` API | Backing call |
|---|---|
| `setPosition(value, axis)` | `set_pos_um(value)` (range-clamped) |
| `move(dist, axis)` | `set_pos_um(self._position[axis] + dist)` |
| `position` | refresh via `read_pos_um()`, return dict |
| `finalize()` | `send('i0')`, `ser.close()` |

### `managerProperties` schema

```json
{
  "rs232device": "jenaZ",
  "posRangeUm": [0, 100],
  "waitForSettle": true,
  "settleToleranceUm": 0.1
}
```

Note: like Piezoconcept, route the serial port via an
[RS232Manager](imswitch/imcontrol/model/managers/RS232sManager.py)
entry instead of opening `serial.Serial` directly in the manager. That
keeps reconnection/teardown consistent with the rest of ImSwitch.

### Mock

Stateful mock that stores a position and returns it from `rd` —
necessary so the settle loop doesn't spin forever in tests.

**Estimated effort:** small (≤ 1 day).

---

## #4 — Thorlabs Kinesis MLS203 XY stage

**Source:** [module_stage.py](../WidefieldStarss/src/WFS/module_stage.py)
**Target manager:** `imswitch/imcontrol/model/managers/positioners/KinesisStageManager.py`
**Driver:** `pylablib.devices.Thorlabs.KinesisMotor` (same lib as #1,
but two-channel).

### Two-axis design

`KinesisMotor` with `scale="MLS203"` handles unit conversion natively;
WFS layers an additional `unit_to_mm = 2.5/50000` on top for legacy
reasons. **Do not** carry that second layer over — let `pylablib` do
the scaling and treat positions in mm directly. ImSwitch already
expects positioner units in the axis' natural unit (µm or mm depending
on the manager).

### Interface mapping

| `PositionerManager` API | Backing call |
|---|---|
| `setPosition(value, 'X')` | `move_to(value, channel=1)` |
| `setPosition(value, 'Y')` | `move_to(value, channel=2)` |
| `move(dist, 'X')` | `move_by(dist, channel=1)` |
| `move(dist, 'Y')` | `move_by(dist, channel=2)` |
| `position` | `{'X': get_position(channel=1), 'Y': get_position(channel=2)}` |
| `finalize()` | `close()` |

### `managerProperties` schema

```json
{
  "snr": "103410304",
  "scale": "MLS203",
  "homeOnInit": false,
  "isRackSystem": true
}
```

### Jog API — discuss before implementing

WFS exposes `start_cont_motion(channel, direction)` /
`stop_motion(channel)`. ImSwitch's `PositionerManager` base has no jog
API today. Two options:

1. **Add jog to the base.** Two new abstract-ish methods (with default
   no-op implementations so existing managers don't break):
   ```python
   def jog_start(self, axis: str, sign: int): pass
   def jog_stop(self, axis: str): pass
   ```
   This unlocks jog for any stage that supports it (Standa, PI, SmarAct
   all do). Worth doing — but a separate PR before the MLS203 port so
   the API change can be reviewed independently.

2. **Manager-only method.** Add `jog_start`/`jog_stop` only on
   `KinesisStageManager`. Controllers that want jog must check
   `isinstance(positioner, KinesisStageManager)`. Pragmatic but locks
   the feature to one stage.

**Recommendation:** do option 1 as a precursor PR. Keep option 2 as a
fallback if review of the base-class change drags.

### Things to drop

- `key_press_event` / `key_release_event` / `add_interwidget_comm` —
  the controller layer should bind WASD keys to `jog_start`/`jog_stop`,
  not the driver.
- `update_widget` returning `(x, y)` — `position` property covers it.

**Estimated effort:** medium (2–3 days incl. the jog base-class
change).

---

## #5 — Thorlabs scientific camera (TSI SDK) + Thorcam cleanup

**Source:** [module_thorlabcam.py](../WidefieldStarss/src/WFS/module_thorlabcam.py)
**Target driver:** `imswitch/imcontrol/model/interfaces/thorcamera_tsi.py`
**Target manager:** `imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py`

### Cleanup: misnamed `ThorcamManager.py`

[ThorcamManager.py](imswitch/imcontrol/model/managers/detectors/ThorcamManager.py)
is an orphaned duplicate of [AVManager.py](imswitch/imcontrol/model/managers/detectors/AVManager.py)
(identical docstring, same Allied Vision GXIPY backend, **zero
references** anywhere in the codebase or setup JSONs — confirmed by
grep). It pretends to be a Thorlabs driver but isn't.

**Action:** delete `ThorcamManager.py` outright as part of this PR.
Remove any leftover import from `detectors/__init__.py` if present.
No deprecation shim needed since nothing references it.

### Replacement: real TSI-SDK driver

The WFS `ThorCMOS` class wraps the genuine Thorlabs Scientific Camera
SDK (`thorlabs_tsi_sdk.tl_camera.TLCameraSDK`) — Zelux / Kiralux /
Quantalux family. ImSwitch has no manager for this today.

### Driver split

- `interfaces/thorcamera_tsi.py` — thin wrapper around `TLCameraSDK`.
  Exposes:
  - `open(serial=None)` (auto-pick first if `None`)
  - `set_exposure_us`, `set_gain`, `set_roi`, `set_frame_rate`
  - `set_trigger_mode(software|hardware|bulb)`,
    `set_trigger_polarity(active_high|active_low)`
  - `arm(buffer_size)`, `disarm()`, `issue_software_trigger()`
  - `get_pending_frame()` → ndarray or None
  - `dispose()`
- **Throw away** the WFS-side `LiveThread`, `AcquisitionThread`,
  `DataList` ring buffer. ImSwitch's `DetectorManager` already runs
  the acquisition loop and buffers frames.
- **Keep** the DLL-path bootstrapping (`os.add_dll_directory`,
  `PATH` manipulation). Move it into the driver's `__init__` and make
  the DLL search path configurable via setup file (default to a
  `dlls/64_lib` next to the manager).

### Manager: `ThorCamTSIManager`

Follow [HamamatsuManager.py](imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py)
as the reference shape — Hamamatsu is the closest existing analogue
(scientific camera, hardware trigger support, ROI control). Expose
`DetectorNumberParameter` / `DetectorListParameter` for:

- exposure (µs)
- gain
- frame rate (Hz, with `is_frame_rate_control_enabled` toggle)
- ROI (x0, y0, x1, y1)
- trigger mode (`Software` / `Hardware` / `Bulb`) — list parameter
- trigger polarity (`ActiveHigh` / `ActiveLow`) — list parameter

### `managerProperties` schema

```json
{
  "cameraSerial": null,
  "dllLocation": "dlls/64_lib",
  "defaults": {
    "exposure_us": 50000,
    "gain": 0,
    "operation_mode": "software",
    "trigger_polarity": "active_high"
  }
}
```

### Mock

Add `MockThorTSICamera` to `thorcamera_tsi.py` that returns synthetic
2448×2048 uint16 frames (random or a gradient). Wire it as the fallback
when `TLCameraSDK()` raises ImportError or `discover_available_cameras`
returns empty — same pattern as Standa.

### Verification

- Headless: `example_no_hardware.json` gets a `thorTSI` detector
  entry that loads the mock; `imswitch/imcontrol/_test/ui/test_liveview.py`
  exercises the mock-frame path.
- With hardware: smoke test live view, then a 10-frame hardware-trigger
  acquisition driven by an external pulse source (or by #6 once it
  lands).

**Estimated effort:** medium-large (3–5 days). Most of the work is the
DLL/SDK bring-up and parameter mapping, not the ImSwitch glue.

---

## Cross-cutting tasks

1. **CHANGELOG / ROADMAP.md update** — note the new managers per port.
2. **Setup JSON examples** — add one demo profile per port under
   `imswitch/_data/user_defaults/imcontrol_setups/` so users have a
   copy-paste starting point.
3. **CI** — extend the headless smoke test to import every new manager
   via its mock backend.
4. **`pylablib` dependency** — already present? Verify in
   `pyproject.toml`; add if missing (used by #1, #2, #4).
5. **`thorlabs_tsi_sdk` dependency** — add as an optional extra
   (`pip install imswitch[thortsi]`) so users without the SDK don't see
   ImportError at startup.

## Suggested merge order

1. (precursor) `PositionerManager` jog API extension — own PR.
2. `KinesisRotatorManager` (#1) — smallest, validates pattern.
3. `ElliptecRotatorManager` + shared bus (#2).
4. `JenaPiezoZManager` (#3).
5. `KinesisStageManager` (#4) — depends on jog API from step 1.
6. `ThorcamManager` deletion + `ThorCamTSIManager` (#5).

Each step is a self-contained PR. Steps 2–5 are independent and can
happen in parallel once step 1 is in.

## Deferred (not in this plan)

- **#6 Arduino/Teensy trigger.** See open question below — we want to
  decide whether to position this as a lightweight pulse-sequencer
  alternative to PulseStreamer/NI before designing the manager
  interface. Custom Teensy firmware
  (`/Users/lenny/PycharmProjects/WidefieldStarss/teensy/arduino_code_teensy4p1_v3.txt`)
  means the protocol is fully under our control — we can extend it as
  needed rather than being locked to a vendor wire format.
- **#7 PAX1000 polarimeter.** Needs a category decision (new
  `InstrumentManager` base vs. script-level helper).
