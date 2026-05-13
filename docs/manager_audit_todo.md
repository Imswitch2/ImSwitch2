# Hardware Manager Audit — TODO

Each entry is one file to review. An agent picks the **first unchecked item**, audits it,
and marks it `[x]` with a one-line result note.

Classification legend (for issues found):
- **instant** — single-method fix, no other files touched → fix directly
- **moderate** — one method replacement or small refactor → propose fix, don't implement
- **hard** — cross-file change, new class, new dependency, or architectural rework → open GitHub issue draft

---

## Detector Managers

- [x] `imswitch/imcontrol/model/managers/detectors/APDManager.py` — 2 instant fixes applied (bare except → logged); 1 moderate proposal (hard-coded device name)
- [x] `imswitch/imcontrol/model/managers/detectors/PMTManager.py` — 2 instant fixes applied (bare except → logged); 1 moderate proposal (hard-coded device name)
- [x] `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py` — fixed: `_getCameraObj` now early-exits on `cameraId="mock"` before touching DCAM DLL
- [x] `imswitch/imcontrol/model/managers/detectors/BaslerManager.py` — 2 instant fixes applied (bare except → logged; wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/ThorcamManager.py` — 2 instant fixes applied (bare except → logged; wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py` — 2 instant fixes applied (attribute name bug; bare except → logged); 1 moderate proposal (trigger mapping)
- [x] `imswitch/imcontrol/model/managers/detectors/GXPIPYManager.py` — 2 instant fixes applied (bare except → logged; wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/TISManager.py` — 1 instant fix applied (uninitialized variable); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py` — 3 instant fixes applied (bare except → logged)
- [x] `imswitch/imcontrol/model/managers/detectors/AVManager.py` — 1 instant fix applied (wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/JetsonCamManager.py` — 1 instant fix applied (wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/PiCamManager.py` — 1 instant fix applied (wrong attribute name); 1 moderate proposal (dead code)
- [x] `imswitch/imcontrol/model/managers/detectors/ESP32CamManager.py` — 3 instant fixes applied (module-level import; wrong attribute name; misleading log); 1 moderate proposal (dead code)

## Laser Managers

- [x] `imswitch/imcontrol/model/managers/lasers/NidaqLaserManager.py` — 3 instant fixes applied (bare except → logged; print → logger; wrong logger args)
- [x] `imswitch/imcontrol/model/managers/lasers/Cobolt0601LaserManager.py` — 2 instant fixes applied (lazy import; print → logger)
- [x] `imswitch/imcontrol/model/managers/lasers/Cobolt0601NewLaserManager.py` — 5 instant fixes applied (lazy imports; mock assignment bug; spacing/style)
- [x] `imswitch/imcontrol/model/managers/lasers/CoboltLaserManager.py` — no issues found
- [ ] `imswitch/imcontrol/model/managers/lasers/LantzLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/AAAOTFLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/MPBLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/CoolLEDLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/PulseStreamerLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/PyMicroscopeLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/ESP32LEDLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/LEDMatrixManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py`

## Positioner Managers

- [ ] `imswitch/imcontrol/model/managers/positioners/NidaqPositionerManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/PIStageManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager2.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/LeicaDMIManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/MHXYStageManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/SQUIDStageManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/SmarACTPositionerManager.py`
- [ ] `imswitch/imcontrol/model/managers/positioners/MockPositionerManager.py`

## Rotator Managers

- [ ] `imswitch/imcontrol/model/managers/rotators/StandaRotatorManager.py`

## RS232 / Board Managers

- [ ] `imswitch/imcontrol/model/managers/rs232/RS232Manager.py`
- [ ] `imswitch/imcontrol/model/managers/rs232/ESP32Manager.py`
- [ ] `imswitch/imcontrol/model/managers/rs232/GRBLManager.py`
- [ ] `imswitch/imcontrol/model/managers/rs232/SQUIDManager.py`

## SLM Managers

- [ ] `imswitch/imcontrol/model/managers/slms/HamamatsuSLMdviManager.py`
- [ ] `imswitch/imcontrol/model/managers/slms/HamamatsuSLMusbManager.py`

## Infrastructure Managers (audit for robustness only)

- [ ] `imswitch/imcontrol/model/managers/NidaqManager.py`
- [ ] `imswitch/imcontrol/model/managers/RecordingManager.py`
- [ ] `imswitch/imcontrol/model/managers/PulseStreamerManager.py`

---

## Audit Details

### PMTManager — 2026-05-13

**Instant fixes applied**
- Line 75-76 — Bare `except Exception: pass` in `__del__` replaced with logged exception. Silent failures during thread cleanup prevented debugging; now logs "Failed to clean up scan thread: {e}".
- Line 511-512 — Bare `except Exception: pass` in `close()` method (ScanWorker class) replaced with logged exception. Silent failures when closing NI-DAQ input task now log "Failed to close input task: {e}".

**Moderate proposals**
- Line 31 — Hard-coded NI-DAQ device name "Dev1" should be configurable
  ```python
  # current
  if isinstance(self._channel, int):
      self._channel = f"Dev1/ai{self._channel}"
  
  # proposed
  device_name = detectorInfo.managerProperties.get("deviceName", "Dev1")
  if isinstance(self._channel, int):
      self._channel = f"{device_name}/ai{self._channel}"
  ```
  Rationale: Systems with multiple NI-DAQ devices or custom device names (e.g., "PXI1Slot3") will fail. Making this configurable maintains backward compatibility while supporting non-standard setups.

### BaslerManager — 2026-05-13

**Instant fixes applied**
- Line 99-100 — Bare `except:` in `getChunk()` replaced with logged exception. Silent failures when retrieving camera chunks prevented debugging; now logs "Failed to get chunk from camera: {e}" before returning None.
- Line 86 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists.

**Moderate proposals**
- Line 74-75 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.

### ThorcamManager — 2026-05-13

**Instant fixes applied**
- Line 92-93 — Bare `except:` in `getChunk()` replaced with logged exception. Silent failures when retrieving camera chunks prevented debugging; now logs "Failed to get chunk from camera: {e}" before returning None.
- Line 81 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists.

**Moderate proposals**
- Line 69-70 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.

### PhotometricsManager — 2026-05-13

**Instant fixes applied**
- Line 28 — Fixed attribute name inconsistency: changed `self.scanLineTime` to `self.__scanLineTime` to match usage in lines 108, 171, and 187. The code was setting a public attribute but accessing a private (name-mangled) attribute, which would cause AttributeError when `crop()` is called before `_setReadoutPort()`.
- Line 89-90 — Bare `except RuntimeError: pass` in `getChunk()` replaced with logged exception. Silent failures when polling frames prevented debugging; now logs "Failed to get chunk from camera: {e}" before returning partial frame list.

**Moderate proposals**
- Lines 203-208 — Fix inconsistent trigger source value mappings between `_setTriggerSource()` and `_updatePropertiesFromCamera()`
  ```python
  # current in _setTriggerSource (lines 150-160)
  'Internal trigger' -> 1792
  'External "start-trigger"' -> 2048
  'External "frame-trigger"' -> 2560
  
  # current in _updatePropertiesFromCamera (lines 203-208)
  1792 -> 'Internal trigger'
  2304 -> 'External "start-trigger"'
  2048 -> 'External "frame-trigger"'
  
  # proposed: Make mappings consistent (need to verify correct values with hardware docs)
  # Option A: Fix _updatePropertiesFromCamera to use 2048 for start-trigger and 2560 for frame-trigger
  # Option B: Fix _setTriggerSource to use 2304 for start-trigger
  ```
  Rationale: The mismatch causes incorrect trigger source display after setting it. When user sets 'External "start-trigger"' (writes 2048), reading back shows 'External "frame-trigger"' (reads 2048). Need hardware documentation to determine correct values.

### GXPIPYManager — 2026-05-13

**Instant fixes applied**
- Line 123-124 — Bare `except:` in `getChunk()` replaced with logged exception. Silent failures when retrieving camera chunks prevented debugging; now logs "Failed to get chunk from camera: {e}" before returning None.
- Line 96 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists.

**Moderate proposals**
- Line 84-85 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.

### TISManager — 2026-05-13

**Instant fixes applied**
- Line 26 — Initialize `self.__image = None` in `__init__()` to prevent AttributeError. The `getLatestFrame()` method (lines 58-61) returns `self.__image` but this variable was never initialized. If `getLatestFrame()` is called when `self._adjustingParameters` is True (e.g., during a camera action), it would return an uninitialized variable causing AttributeError.

**Moderate proposals**
- Line 71-72 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.

### SwabianTimeTaggerManager — 2026-05-13

**Instant fixes applied**
- Line 140-141 — Bare `except Exception: pass` in `__del__()` replaced with logged exception. Silent failures when stopping acquisition during cleanup prevented debugging; now logs "Failed to stop acquisition during cleanup: {e}".
- Line 147-148 — Bare `except Exception: pass` in `__del__()` replaced with logged exception. Silent failures when cleaning up TimeTagger objects prevented debugging; now logs "Failed to clean up TimeTagger objects: {e}".
- Line 382-383 — Bare `except Exception: pass` in `stopAcquisition()` replaced with logged exception. Silent failures when stopping scan thread prevented debugging; now logs "Failed to stop scan thread: {e}".

### AVManager — 2026-05-13

**Instant fixes applied**
- Line 89 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists in `getParameter()`.

**Moderate proposals**
- Line 77-78 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.


### JetsonCamManager — 2026-05-13

**Instant fixes applied**
- Line 84 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists in `getParameter()`.

**Moderate proposals**
- Line 72-73 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.


### PiCamManager — 2026-05-13

**Instant fixes applied**
- Line 87 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists in `getParameter()`.

**Moderate proposals**
- Line 75-76 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.


### ESP32CamManager — 2026-05-13

**Instant fixes applied**
- Line 4 — Removed module-level import of `CameraESP32Cam` hardware library. This import is redundant (already lazily imported in line 164) and risks ImportError on startup if the library is not installed.
- Line 89 — Fixed incorrect attribute reference `self._parameters` (which doesn't exist) to `self.parameters` (the property from base class). This bug would have caused AttributeError with wrong message when checking if parameter exists in `getParameter()`.
- Line 168 — Fixed misleading log message that said "Failed to initialize PiCamera" when it should say "ESP32Camera".

**Moderate proposals**
- Line 77-78 — Remove unreachable dead code in `setParameter()` method
  ```python
  # current
  super().setParameter(name, value)
  
  if name not in self._DetectorManager__parameters:
      raise AttributeError(f'Non-existent parameter "{name}" specified')
  
  value = self._camera.setPropertyValue(name, value)
  
  # proposed
  super().setParameter(name, value)
  value = self._camera.setPropertyValue(name, value)
  ```
  Rationale: The `super().setParameter()` call already validates the parameter name and raises AttributeError if it doesn't exist (DetectorManager.py line 129-130), so the subsequent check is unreachable dead code that adds confusion.


### NidaqLaserManager — 2026-05-13

**Instant fixes applied**
- Line 40 — Changed bare `except:` to `except Exception as e:` and improved error message to include exception details. Bare except blocks catch all exceptions including KeyboardInterrupt and SystemExit, making debugging impossible.
- Line 31 — Replaced `print()` with `self.__logger.warning()` for consistent logging throughout the codebase. Print statements don't respect the logging configuration and make it harder to track issues in production.
- Line 58 — Fixed incorrect argument order in `self.__logger.error(e, "Error trying to set value to laser.")`. The error message should come first, not the exception object. Changed to f-string format: `self.__logger.error(f"Error trying to set value to laser: {e}")`.

### Cobolt0601LaserManager — 2026-05-13

**Instant fixes applied**
- Line 1 — Removed module-level `from lantz import Q_` import and moved to lazy import inside `__init__` method. Module-level hardware library imports risk ImportError on startup if the library is not installed, preventing the entire application from starting even when this specific laser is not used.
- Line 19-24 — Added lazy import of `Q_` from lantz with try/except in `__init__`, storing as `self._Q` for use throughout the class. Import failures are logged before re-raising.
- Line 35 — Replaced `print(f'Laser turning {enabled}')` with `self.__logger.debug(f'Laser turning {enabled}')`. Print statements bypass the logging system and cannot be controlled or filtered in production environments.
- Lines 41, 43 — Updated references from `Q_` to `self._Q` to use the lazily-imported instance stored in `__init__`.

### Cobolt0601NewLaserManager — 2026-05-13

**Instant fixes applied**
- Lines 2-3 — Removed module-level hardware imports `from .PyCoboltManager import list_lasers` and `from .PyCoboltManager import Cobolt06`. Module-level hardware library imports risk ImportError on startup if the library is not installed, preventing the entire application from starting even when this specific laser is not used.
- Lines 20-26 — Added lazy import of `Cobolt06` from PyCoboltManager with try/except in `__init__`, storing as `self._Cobolt06` for instantiation. Import failures are logged before re-raising.
- Line 38 — Updated `self._laser = Cobolt06(port=self._port)` to `self._laser = self._Cobolt06(port=self._port)` to use the lazily-imported class reference.
- Lines 68-69 — Fixed critical bug where mock laser was created in local variable `laser` but never assigned to `self._laser`. Changed `laser = driver(self._port)` and `laser.initialize()` to `self._laser = driver(self._port)` and `self._laser.initialize()`. Without this fix, any code path using the mock would fail with AttributeError when trying to access `self._laser` attributes.
- Line 93 — Fixed spacing inconsistency in comparison `if power ==0:` to `if power == 0:` for code consistency.
- Line 107 — Changed non-Pythonic comparison `if active == False:` to `if not active:` following Python style guidelines.
- Lines 149-153 — Added lazy import of `list_lasers` in `getAllDeviceNames` method with try/except. Returns empty list on import failure instead of crashing.

### CoboltLaserManager — 2026-05-13

No issues found. This is a simple backwards compatibility alias that inherits from `Cobolt0601LaserManager` with no additional implementation. All robustness concerns are addressed in the parent class (which was audited separately).

