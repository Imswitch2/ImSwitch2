# Hardware Manager Audit — Consolidated Findings

All 43 manager files audited (2026-05-13). **80+ instant fixes applied**, **20+ moderate proposals open**, **6 hard issues open**.

Classification legend:
- **instant** — single-method fix, applied directly
- **moderate** — one-method replacement or small refactor, proposed not implemented
- **hard** — cross-file change, new class, new dependency, or architectural rework

---

## TOP PRIORITY

### [HARD] Replace or vendor the `lantz` dependency

**The problem.** `lantz` (used by `RS232Driver`, `LantzLaserManager`, `lantzlasers.py`, and indirectly by anything importing them) was last released in 2018 and has not kept up with the Python ecosystem. To get fresh installs to work today we already had to pin **three** transitive shims:

| Pinned | Reason |
|---|---|
| `setuptools<80` | lantz imports `pkg_resources`, dropped in setuptools 80 |
| `pyvisa<1.12` | lantz does `import visa`, shim removed in pyvisa 1.12 |
| Eager `from .lantzlasers import LantzLaser` in `interfaces/__init__.py` | lantz import side-effects (COM init, vendor DLLs) are load-bearing for DCAM camera open |

Each of those is a workaround that blocks future upgrades of the Python ecosystem. The next break is just a matter of time.

**The fix.** Long-term: stop depending on lantz. Two paths:
1. **Vendor only the parts we use.** `RS232Driver.py` uses `lantz.messagebased.MessageBasedDriver`. That's a thin wrapper around `pyvisa` we could replace with ~40 lines of direct pyvisa code. Laser drivers that go through `LantzLaser` are similarly small.
2. **Migrate to a maintained framework** (e.g. `python-microscopy` / `microscope` package, which ImSwitch already depends on via the `microscope` extra).

**Scope.** This affects: `RS232Driver.py`, `lantzlasers.py`, `LantzLaserManager`, every laser/RS232 driver that goes through `MessageBasedDriver`. Estimated 1–2 days of focused work; requires hardware testing for at least one real laser to validate.

**Why now?** The lantz workaround pins (`setuptools<80`, `pyvisa<1.12`) will increasingly conflict with other packages. Best to do this before another required dependency forces us to break the pins.

---

## Other Hard Issues

### [ISSUE] PulseStreamerManager — global stdout redirection
Lines 30-31 redirect `sys.stdout` globally with no restore, breaking other parts of the application. Should use `contextlib.redirect_stdout` to wrap only the `PulseStreamer()` instantiation.

### [ISSUE] BSC203StageManager — no None-checking after failed init
If hardware init fails, `self.dev` is None but ~10 methods (`homeAll`, `homing`, `move`, `setPosition`, `move_relative_mm`, `setJogPars`, `jog`) call `self.dev.*` without guards → AttributeError on every interaction. Needs systematic None-checking or a mock-device pattern.

### [ISSUE] LeicaDMIManager — missing `super().__init__()` call
Class never calls the `PositionerManager` base `__init__`, so `self._position` is never set. Lines 46 and 57 access it → AttributeError on first use. Needs an `initialPosition` dict (default or device-queried).

### [ISSUE] PyCoboltManager — add mock/fallback driver
No way to test the Cobolt laser stack without real hardware. Should accept `port="mock"` and provide a `MockCoboltLaser` that simulates the command/response protocol. Hardware-control code; needs domain review.

### [ISSUE] Improve cross-manager exception-context surfacing
Most managers catch the real-camera failure and silently fall to a mock with `self.__logger.warning(f'Failed: {e}')`. When the mock then fails (as the `Q_ is None` bug demonstrated), the original exception is buried. Add `exc_info=True` or chain exceptions explicitly so the real underlying error surfaces in the log. Affects every manager that wraps `_getCameraObj` / `_getDriver` / `_getDevice` in try/except.

---

## Cross-Cutting Moderate Proposals

These same patterns recur across many files. One follow-up PR per pattern would clear them in bulk.

### Pattern A — Dead-code parameter check in `setParameter()`
9 detector managers (BaslerManager, ThorcamManager, PhotometricsManager, GXPIPYManager, TISManager, AVManager, JetsonCamManager, PiCamManager, ESP32CamManager) duplicate this block after `super().setParameter(name, value)`:

```python
if name not in self._DetectorManager__parameters:
    raise AttributeError(f'Non-existent parameter "{name}" specified')
```

`super().setParameter` already validates the name at `DetectorManager.py:129-130`, so the inner block is unreachable. Remove from all 9 files.

### Pattern B — Hardware library imported at module level
Several managers still load their vendor library at module top, risking ImportError on startup when the library isn't installed. Convert to lazy imports inside `__init__`:
- Done already: `Cobolt0601LaserManager`, `Cobolt0601NewLaserManager`, `ESP32CamManager`, `ESP32Manager`, `GRBLManager`, `SQUIDManager`, `PulseStreamerManager`, `HamamatsuSLMdviManager`
- Still TODO: `BSC203StageManager` (line 3 `from thorlabs_apt_device.devices.bsc import BSC`), `PyCoboltManager` (line 1 `import serial`)

### Pattern C — Missing mock/fallback in laser managers
4 laser managers raise on missing hardware instead of degrading to a mock:
- `CoolLEDLaserManager` — wrap RS232 lookup in try/except
- `PulseStreamerLaserManager` — wrap `pulseStreamerManager` lookup in try/except
- `PyMicroscopeLaserManager` — wrap driver `importlib`/`getattr` chain
- `ESP32LEDLaserManager` — wrap RS232 lookup

All follow the same template: set `self._isMock = True` on failure, then early-return in `setEnabled()` / `setValue()`.

### Pattern D — Missing mock fallback for RS232-backed positioners
- `MHXYStageManager`, `SQUIDStageManager` — wrap RS232 lookup in try/except and fall back to `MockRS232Driver` like `RS232Manager` already does.

### Pattern E — Hard-coded NI-DAQ device name `"Dev1"`
- `APDManager`, `PMTManager` — make device name configurable via `detectorInfo.managerProperties["deviceName"]`, defaulting to `"Dev1"`.

### Pattern F — Overly broad `except Exception:`
- `StandaRotatorManager`, `RS232Manager` — narrow to the actually-possible exceptions (`ImportError`, `OSError`, `AttributeError`, `serial.SerialException`) so programming errors fail fast.

---

## Unique File-Specific Moderate Proposals

These don't fit any cross-cutting pattern.

- **PhotometricsManager** (lines 150-208) — Trigger source value mapping is inconsistent between `_setTriggerSource()` and `_updatePropertiesFromCamera()`: writing `'External "start-trigger"'` writes 2048 but reading 2048 maps to `'External "frame-trigger"'`. Needs hardware-doc verification before fixing.
- **PyCoboltManager** (lines 177, 189) — `if not "-08-" in self.modelnumber or not "-06-" in self.modelnumber:` — the `or` should almost certainly be `and` (model number is always one or the other, never both, so the OR is always true).
- **PyMicroscopeLaserManager** (line 37) — `setValue()` divides by `self.__maxPower` without checking for zero.
- **PyMicroscopeLaserManager** (line 22) — `self.__driver.split(".")` then `driver[0]`/`driver[1]` without validating the split produced 2 elements.
- **NidaqPositionerManager** (lines 28, 31, 38) — `axis` parameter is accepted but ignored; methods always operate on `self.axes[0]`. Either honour `axis` or drop the parameter.

---

## File-by-File Audit Status

All checked. The text after each entry summarizes what was applied/proposed; full details are in the per-file commits.

### Detectors (13/13)
- [x] APDManager — 2 instant; 1 moderate (Pattern E)
- [x] PMTManager — 2 instant; 1 moderate (Pattern E)
- [x] HamamatsuManager — `_getCameraObj` early-exits on `cameraId="mock"`
- [x] BaslerManager — 2 instant; 1 moderate (Pattern A)
- [x] ThorcamManager — 2 instant; 1 moderate (Pattern A)
- [x] PhotometricsManager — 2 instant; 1 unique moderate (trigger mapping)
- [x] GXPIPYManager — 2 instant; 1 moderate (Pattern A)
- [x] TISManager — 1 instant; 1 moderate (Pattern A)
- [x] SwabianTimeTaggerManager — 3 instant (no further proposals)
- [x] AVManager — 1 instant; 1 moderate (Pattern A)
- [x] JetsonCamManager — 1 instant; 1 moderate (Pattern A)
- [x] PiCamManager — 1 instant; 1 moderate (Pattern A)
- [x] ESP32CamManager — 3 instant; 1 moderate (Pattern A)

### Lasers (13/13)
- [x] NidaqLaserManager — 3 instant
- [x] Cobolt0601LaserManager — 2 instant (Pattern B)
- [x] Cobolt0601NewLaserManager — 5 instant (Pattern B; critical mock-assignment bug)
- [x] CoboltLaserManager — clean
- [x] LantzLaserManager — clean (but see TOP PRIORITY)
- [x] AAAOTFLaserManager — 2 instant
- [x] MPBLaserManager — 1 instant (numeric parsing)
- [x] CoolLEDLaserManager — 1 instant; 1 moderate (Pattern C)
- [x] PulseStreamerLaserManager — 1 instant; 2 moderate (Pattern C + setValue guard)
- [x] PyMicroscopeLaserManager — 3 unique moderates (driver validation, /0, mock)
- [x] ESP32LEDLaserManager — 2 instant; 1 moderate (Pattern C)
- [x] LEDMatrixManager — 4 instant
- [x] PyCoboltManager — 5 instant; 3 moderate (Pattern B + F + boolean-logic bug); 1 hard

### Positioners (10/10)
- [x] NidaqPositionerManager — 2 instant; 1 unique moderate (axis usage)
- [x] PIStageManager — 5 instant
- [x] BSC203StageManager — 2 instant; 2 moderate (Pattern B + hard-coded port); 1 hard
- [x] PiezoconceptZManager — 3 instant
- [x] PiezoconceptZManager2 — 4 instant (critical syntax error fixed)
- [x] LeicaDMIManager — 4 instant; 1 hard
- [x] MHXYStageManager — 1 instant; 1 moderate (Pattern D)
- [x] SQUIDStageManager — 3 instant; 2 moderate (Pattern D + closeEvent guard)
- [x] SmarACTPositionerManager — 5 instant; 2 moderate (mock fallback for ctypes lib)
- [x] MockPositionerManager — 2 instant

### Rotators (1/1)
- [x] StandaRotatorManager — 2 instant; 1 moderate (Pattern F)

### RS232 / boards (4/4)
- [x] RS232Manager — 1 instant; 1 moderate (Pattern F)
- [x] ESP32Manager — 4 instant; 1 moderate (mock when hardware unreachable)
- [x] GRBLManager — 3 instant; 1 moderate (mock fallback for board)
- [x] SQUIDManager — 2 instant; 1 moderate (mock fallback for device)

### SLMs (2/2)
- [x] HamamatsuSLMdviManager — 4 instant
- [x] HamamatsuSLMusbManager — 6 instant (Windows-only path fixed, multiple uninit-var bugs)

### Infrastructure (3/3)
- [x] NidaqManager — 3 instant
- [x] RecordingManager — 3 instant
- [x] PulseStreamerManager — 5 instant; 1 hard (global stdout redirect)
