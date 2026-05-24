# Hardware Manager Audit — Open Issues

**Last audited:** 2026-05

All 43 manager files audited (2026-05-13). **100+ fixes applied** (instant refactors, mock/fallback modes, lazy imports, validation checks). The following issues remain open.

Classification legend:
- **moderate** — one-method replacement or small refactor, ready to implement
- **hard** — cross-file change, new class, new dependency, or architectural rework

---

## ✅ RESOLVED (2026-05-14)

### [HARD] Replace or vendor the `lantz` dependency — **RESOLVED in commit 59907138**

Lantz dependency fully removed. RS232Driver rewritten as self-contained pyvisa wrapper. Cobolt drivers vendored with direct ASCII protocol implementation. All pyvisa/setuptools pins lifted.

### Pattern A — Dead-code parameter check in setParameter() — **RESOLVED in commit ca1a60be**

Removed unreachable parameter validation from 9 detector managers.

### Pattern C — Missing mock/fallback in laser managers — **RESOLVED**

All laser managers now degrade gracefully when hardware unavailable:
- CoolLEDLaserManager (commit b30741fb)
- PulseStreamerLaserManager (commit d1f3d1c5)
- PyMicroscopeLaserManager (commit e03f1a58)
- ESP32LEDLaserManager (commit 53277e17)

### Pattern D — Missing mock fallback for RS232-backed positioners — **RESOLVED**

- MHXYStageManager (commit 315ffb1b)
- SQUIDStageManager (commit f3d53b51)

### Pattern E — Hard-coded NI-DAQ device name — **RESOLVED in commit 2453aaf4**

APDManager and PMTManager now accept configurable device name via managerProperties.

### Pattern B (partial) — Hardware library lazy imports — **PARTIALLY RESOLVED**

Completed lazy imports:
- PyCoboltManager (commit 0a84883d)
- BSC203StageManager (commit cea53f7c)

---

## Open Hard Issues

### [HARD] PulseStreamerManager — global stdout redirection
Lines 30-31 redirect `sys.stdout` globally with no restore, breaking other parts of the application. Should use `contextlib.redirect_stdout` to wrap only the `PulseStreamer()` instantiation.

**File:** `imswitch/imcontrol/model/managers/PulseStreamerManager.py:30-31`

### [HARD] BSC203StageManager — no None-checking after failed init
If hardware init fails, `self.dev` is None but ~10 methods (`homeAll`, `homing`, `move`, `setPosition`, `move_relative_mm`, `setJogPars`, `jog`) call `self.dev.*` without guards → AttributeError on every interaction. Needs systematic None-checking or a mock-device pattern.

**File:** `imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py`

### [HARD] LeicaDMIManager — missing `super().__init__()` call
Class never calls the `PositionerManager` base `__init__`, so `self._position` is never set. Lines 46 and 57 access it → AttributeError on first use. Needs an `initialPosition` dict (default or device-queried).

**File:** `imswitch/imcontrol/model/managers/positioners/LeicaDMIManager.py:9-16`

### [HARD] PyCoboltManager — add mock/fallback driver
No way to test the Cobolt laser stack without real hardware. Should accept `port="mock"` and provide a `MockCoboltLaser` that simulates the command/response protocol. Hardware-control code; needs domain review.

**File:** `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py`

### [HARD] Improve cross-manager exception-context surfacing
Most managers catch the real-camera failure and silently fall to a mock with `self.__logger.warning(f'Failed: {e}')`. When the mock then fails, the original exception is buried. Add `exc_info=True` or chain exceptions explicitly so the real underlying error surfaces in the log. Affects every manager that wraps `_getCameraObj` / `_getDriver` / `_getDevice` in try/except.

---

## Open Moderate Issues

### Pattern F — Overly broad `except Exception:`
- `StandaRotatorManager`, `RS232Manager` — narrow to the actually-possible exceptions (`ImportError`, `OSError`, `AttributeError`, `serial.SerialException`) so programming errors fail fast.

### Unique file-specific issues

- **PhotometricsManager** (lines 150-208) — Trigger source value mapping is inconsistent between `_setTriggerSource()` and `_updatePropertiesFromCamera()`: writing `'External "start-trigger"'` writes 2048 but reading 2048 maps to `'External "frame-trigger"'`. Needs hardware-doc verification before fixing.

---

## Historical Reference

All 43 manager files audited 2026-05-13. Over 100 fixes applied in May 2026 including:
- Instant fixes: unreachable code removal, uninitialized variable fixes, syntax errors, logger usage, bare except narrowing
- Mock/fallback modes: 4 laser managers, 2 positioner managers, 3 board managers
- Lazy imports: 8+ managers converted to avoid import-time crashes
- Validation/safety: PyMicroscopeLaserManager, PyCoboltManager, NidaqPositionerManager
- Configuration: NI-DAQ device names now configurable
- Architecture: lantz dependency removed (commit 59907138)

See commit history (May 2026) for detailed per-file changes.
