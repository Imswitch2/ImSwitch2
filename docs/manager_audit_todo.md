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
- [x] `imswitch/imcontrol/model/managers/lasers/LantzLaserManager.py` — no issues found
- [x] `imswitch/imcontrol/model/managers/lasers/AAAOTFLaserManager.py` — 2 instant fixes applied (added logger; print → logger)
- [x] `imswitch/imcontrol/model/managers/lasers/MPBLaserManager.py` — 1 instant fix applied (getValue returns numeric value)
- [x] `imswitch/imcontrol/model/managers/lasers/CoolLEDLaserManager.py` � 1 instant fix applied (None comparison style); 1 moderate proposal (mock mode)
- [x] `imswitch/imcontrol/model/managers/lasers/PulseStreamerLaserManager.py` � 1 instant fix applied (docstring formatting); 2 moderate proposals (mock mode, setValue guard)
- [x] `imswitch/imcontrol/model/managers/lasers/PyMicroscopeLaserManager.py` — 3 moderate proposals (mock mode, driver validation, division by zero)
- [x] `imswitch/imcontrol/model/managers/lasers/ESP32LEDLaserManager.py` — 2 instant fixes applied (typo, boolean multiplication); 1 moderate proposal (mock mode)
- [x] `imswitch/imcontrol/model/managers/lasers/LEDMatrixManager.py` — 4 instant fixes applied (typos, docstring errors, missing pass)
- [x] `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py` — 5 instant fixes (typos); 3 moderate; 1 hard

## Positioner Managers

- [x] `imswitch/imcontrol/model/managers/positioners/NidaqPositionerManager.py` — 2 instant fixes (style, validation); 1 moderate
- [x] `imswitch/imcontrol/model/managers/positioners/PIStageManager.py` — 5 instant fixes (exception handling, dead code)
- [x] `imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py` — 2 instant fixes; 2 moderate proposals; 1 hard issue
- [x] `imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager.py` — 3 instant fixes applied (logger)
- [x] `imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager2.py` — 4 instant fixes applied (critical syntax error fixed, logger added)
- [x] `imswitch/imcontrol/model/managers/positioners/LeicaDMIManager.py` — 4 instant fixes applied (bare except, logger); 1 hard
- [x] `imswitch/imcontrol/model/managers/positioners/MHXYStageManager.py` — 1 instant fix applied (exception handling); 1 moderate
- [x] `imswitch/imcontrol/model/managers/positioners/SQUIDStageManager.py` — 3 instant fixes applied (dead code, logger order, print); 2 moderate
- [x] `imswitch/imcontrol/model/managers/positioners/SmarACTPositionerManager.py` — 5 instant fixes applied (logging config, logger, duplicate method, dead code); 2 moderate
- [x] `imswitch/imcontrol/model/managers/positioners/MockPositionerManager.py` — 2 instant fixes applied (axis parameter usage)

## Rotator Managers

- [x] `imswitch/imcontrol/model/managers/rotators/StandaRotatorManager.py` — 2 instant fixes applied (typo, redundant method); 1 moderate

## RS232 / Board Managers

- [x] `imswitch/imcontrol/model/managers/rs232/RS232Manager.py` — 1 instant fix applied (exception logging); 1 moderate
- [x] `imswitch/imcontrol/model/managers/rs232/ESP32Manager.py` — 4 instant fixes applied (lazy import, bare excepts); 1 moderate
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

### LantzLaserManager — 2026-05-13

No issues found. This base class for Lantz-based lasers is clean and follows good patterns:
- The `LantzLaser` import at line 2 is an interface wrapper (not a hardware library), which lazily loads actual hardware drivers with proper try/except handling
- Mock/fallback logic is properly delegated to the `LantzLaser` interface (lantzlasers.py)
- No bare except blocks
- No hard-coded paths, magic numbers, or platform-specific assumptions
- No Python errors or deprecated API calls

### AAAOTFLaserManager — 2026-05-13

**Instant fixes applied**
- Line 4 — Added `from imswitch.imcommon.model import initLogger` to enable proper logging
- Line 27 — Added `self.__logger = initLogger(self, instanceName=name)` to initialize logger in `__init__`
- Line 67 — Changed `print(f"creating lut for {laserInfo} from calib failed due to: {e}")` to `self.__logger.error(f"Creating LUT for {name} from calib failed due to: {e}")` for proper error logging instead of print statement

### MPBLaserManager — 2026-05-13

**Instant fixes applied**
- Line 59-60 — Fixed `getValue()` to parse and return numeric value instead of raw string. The method now splits the RS232 response format (e.g., 'D >100') and extracts the numeric value, converting it to float for proper use in calculations and comparisons.

### CoolLEDLaserManager � 2026-05-13

**Instant fixes applied**
- Line 25-27 � Changed `!= None` to `is not None` for PEP 8 compliance. Python style guide recommends using `is not None` instead of `!= None` for None comparisons.

**Moderate proposals**
- Line 16-30 � Add mock/fallback mode with try/except wrapper around RS232 manager initialization
  ```python
  # current
  def __init__(self, laserInfo, name, **lowLevelManagers):
      self.__logger = initLogger(self, instanceName=name)
      self._rs232manager = lowLevelManagers['rs232sManager'][
          laserInfo.managerProperties['rs232device']
      ]
      self.__channel_index = laserInfo.managerProperties['channel_index']
  
  # proposed
  def __init__(self, laserInfo, name, **lowLevelManagers):
      self.__logger = initLogger(self, instanceName=name)
      self._isMock = False
      try:
          self._rs232manager = lowLevelManagers['rs232sManager'][
              laserInfo.managerProperties['rs232device']
          ]
          self.__channel_index = laserInfo.managerProperties['channel_index']
      except Exception as e:
          self._isMock = True
          self.__logger.warning(f'CoolLED not available, entering mock mode: {e}')
      # Then add early returns in setEnabled() and setValue() if self._isMock
  ```

### PulseStreamerLaserManager — 2026-05-13

**Instant fixes applied**
- Line 11 — Fixed docstring formatting: changed `"analogChannel"` (malformed with mismatched quotes/backticks) to proper RST format ``analogChannel``

**Moderate proposals**
- Lines 14-21 — Add mock/fallback mode with try/except wrapper around pulseStreamerManager initialization
  ```python
  # current
  def __init__(self, laserInfo, name, **lowLevelManagers):
      self._logger = initLogger(self, instanceName=name)
      self._pulseStreamerManager = lowLevelManagers["pulseStreamerManager"]
  
  # proposed
  def __init__(self, laserInfo, name, **lowLevelManagers):
      self._logger = initLogger(self, instanceName=name)
      self._isMock = False
      try:
          self._pulseStreamerManager = lowLevelManagers["pulseStreamerManager"]
      except Exception as e:
          self._isMock = True
          self._logger.warning(f'PulseStreamer not available, entering mock mode: {e}')
      # Then add early returns in setEnabled() and setValue() if self._isMock
  ```

- Lines 28-34 — Add guard in setValue to prevent errors when analog control is not available (binary-only lasers)
  ```python
  # current
  def setValue(self, voltage):
      """Sets the output voltage of the analog channel selected by the manager."""
      self._pulseStreamerManager.setAnalog(
          channel=self._analogChannels, voltage=voltage,
          min_val=self.valueRangeMin, max_val=self.valueRangeMax
      )
  
  # proposed
  def setValue(self, voltage):
      """Sets the output voltage of the analog channel selected by the manager."""
      if self._analogChannels is None:
          self._logger.warning(f'setValue called on binary-only laser {self.name}')
          return
      self._pulseStreamerManager.setAnalog(
          channel=self._analogChannels, voltage=voltage,
          min_val=self.valueRangeMin, max_val=self.valueRangeMax
      )
  ```

### PyMicroscopeLaserManager — 2026-05-13

**Moderate proposals**
- Lines 16-26 — Add mock/fallback mode with try/except wrapper around hardware initialization
  ```python
  # current
  def __init__(self, laserInfo, name, **_lowLevelManager) -> None:
      self.__logger = initLogger(self, instanceName=name)
      self.__port = laserInfo.managerProperties["digitalPorts"]
      self.__driver = str(laserInfo.managerProperties["pyMicroscopeDriver"])
      driver = self.__driver.split(".")
      package = importlib.import_module(
          pythontools.joinModulePath("microscope.lights", driver[0])
      )
      self.__laser = getattr(package, driver[1])(self.__port)
  
  # proposed
  def __init__(self, laserInfo, name, **_lowLevelManager) -> None:
      self.__logger = initLogger(self, instanceName=name)
      self.__port = laserInfo.managerProperties["digitalPorts"]
      self.__driver = str(laserInfo.managerProperties["pyMicroscopeDriver"])
      self._isMock = False
      try:
          driver = self.__driver.split(".")
          package = importlib.import_module(
              pythontools.joinModulePath("microscope.lights", driver[0])
          )
          self.__laser = getattr(package, driver[1])(self.__port)
      except Exception as e:
          self._isMock = True
          self.__logger.warning(f'PyMicroscope laser not available, mock mode: {e}')
      # Then add early returns in setEnabled() and setValue() if self._isMock
  ```

- Lines 20-24 — Validate driver string format before splitting and accessing indices
  ```python
  # current
  driver = self.__driver.split(".")
  package = importlib.import_module(
      pythontools.joinModulePath("microscope.lights", driver[0])
  )
  self.__laser = getattr(package, driver[1])(self.__port)
  
  # proposed
  driver = self.__driver.split(".")
  if len(driver) != 2:
      raise ValueError(f"Invalid driver format: {self.__driver}. Expected 'module.class'")
  package = importlib.import_module(
      pythontools.joinModulePath("microscope.lights", driver[0])
  )
  self.__laser = getattr(package, driver[1])(self.__port)
  ```

- Line 37 — Add guard against division by zero if maxPower is 0 or invalid
  ```python
  # current
  def setValue(self, value) -> None:
      self.__laser.power = float(value) / self.__maxPower
  
  # proposed
  def setValue(self, value) -> None:
      if self.__maxPower == 0:
          self.__logger.error(f"Cannot set power: maxPower is 0")
          return
      self.__laser.power = float(value) / self.__maxPower
  ```

### ESP32LEDLaserManager — 2026-05-13

**Instant fixes applied**
- Line 5 — Fixed typo in docstring: "LAsers" → "Lasers"
- Line 31 — Made boolean multiplication explicit: changed `self.power*self.enabled` to `self.power if self.enabled else 0` for clarity

**Moderate proposals**
- Lines 16-20 — Add mock/fallback mode with try/except wrapper around rs232manager initialization
  ```python
  # current
  def __init__(self, laserInfo, name, **lowLevelManagers):
      super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)
      self._rs232manager = lowLevelManagers['rs232sManager'][
          laserInfo.managerProperties['rs232device']
      ]
  
  # proposed
  def __init__(self, laserInfo, name, **lowLevelManagers):
      super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)
      self.__logger = initLogger(self, instanceName=name)
      self._isMock = False
      try:
          self._rs232manager = lowLevelManagers['rs232sManager'][
              laserInfo.managerProperties['rs232device']
          ]
      except Exception as e:
          self._isMock = True
          self.__logger.warning(f'ESP32 LED not available, mock mode: {e}')
      # Then add early returns in setEnabled() and setValue() if self._isMock
  ```

### LEDMatrixManager — 2026-05-13

**Instant fixes applied**
- Line 7 — Fixed typo in docstring: "LEDMatrixs" → "LED matrices"
- Line 97 — Fixed incorrect docstring for freqRangeMax property: "minimum" → "maximum"
- Line 116 — Fixed typo in docstring: "wether" → "whether"
- Line 125 — Added missing `pass` statement to setModulationDutyCycle method body


### PyCoboltManager — 2026-05-13

**Instant fixes applied**
- Line 48 — Fixed typo in error message: "accesible" → "accessible"
- Line 99 — Fixed typo in docstring: "probler" → "proper"
- Line 228 — Fixed typo in docstring: "laset" → "laser"
- Line 253 — Fixed typo in log message: "responce recieved" → "response received"
- Line 578 — Fixed typo in method name: "get_modualtion_tec_setpoint" → "get_modulation_tec_setpoint"

**Moderate proposals**
- Lines 1-3 — Lazy import serial library to avoid ImportError on startup
  ```python
  # current
  import serial
  from serial.tools import list_ports
  from serial.serialutil import SerialException
  
  # proposed
  # Move imports inside __init__ or connect() methods with try/except:
  try:
      import serial
      from serial.tools import list_ports
      from serial.serialutil import SerialException
  except ImportError:
      raise ImportError("pyserial required for Cobolt laser support")
  ```

- Lines 177, 189 — Fix boolean logic error (OR should be AND)
  ```python
  # current (line 177)
  if not "-08-" in self.modelnumber or not "-06-" in self.modelnumber:
  
  # proposed
  if not "-08-" in self.modelnumber and not "-06-" in self.modelnumber:
  ```

- Lines 32, 63, 94, 107, 120, 124, 594 — Replace bare except blocks with specific exceptions
  ```python
  # current (example from line 63)
  except:
      pass
  
  # proposed
  except (serial.SerialException, RuntimeError) as e:
      logger.debug(f"Failed to connect to {port.device}: {e}")
  ```

**Hard issues**
- **[ISSUE] Add mock/fallback mode for Cobolt laser driver**
  - Currently no way to test without real hardware connected
  - Add mock mode when port/serialnumber is "mock" or "simulation"
  - Mock should simulate basic command/response protocol
  - Requires adding MockCoboltLaser class and integration throughout
  - Red-zone: hardware control code, requires hardware expert review

### NidaqPositionerManager — 2026-05-13

**Instant fixes applied**
- Line 37 — Removed trailing comma from method signature `resetToCurrent(self,)` → `resetToCurrent(self)`
- Line 38 — Fixed spacing: `setPosition(...,0)` → `setPosition(..., 0)` for consistency
- Lines 40-43 — Added axis validation in `get_abs` method to prevent KeyError with clear error message

**Moderate proposals**
- Lines 28, 31, 38 — Fix inconsistent axis parameter usage across methods
  ```python
  # current (line 28)
  def move(self, dist, axis):
      self.setPosition(self._position[self.axes[0]] + dist, axis)
  
  # current (line 30-31)
  def setPosition(self, position, axis):
      self._position[self.axes[0]] = position
      # axis parameter is ignored, always uses self.axes[0]
  
  # proposed - either use the axis parameter consistently:
  def move(self, dist, axis):
      self.setPosition(self._position[axis] + dist, axis)
  
  def setPosition(self, position, axis):
      self._position[axis] = position
      self._nidaqManager.setAnalog(target=self.name, ...)
  
  # OR remove axis parameter since only one axis is supported:
  def move(self, dist):
      self.setPosition(self._position[self.axes[0]] + dist)
  
  def setPosition(self, position):
      self._position[self.axes[0]] = position
      self._nidaqManager.setAnalog(target=self.name, ...)
  ```

### PIStageManager — 2026-05-13

**Instant fixes applied**
- Line 5 — Removed unused import `from serial.serialutil import SerialException` (dead import)
- Line 67 — Fixed bare `except:` → `except Exception as e:` and added exception details to log message
- Lines 90-91, 94-95 — Added debug logging to silent exception handlers in `_resolve_usb_description` cleanup
- Line 157 — Added exception details to log message: `{e}` 
- Line 174 — Changed second `if axis == 'Y':` to `elif axis == 'Y':` for correct control flow
- Lines 261-310 — Removed large commented-out dead code block (obsolete move_to methods)

### BSC203StageManager — 2026-05-13

**Instant fixes applied**
- Line 6 — Removed unused import `import time` (dead import)
- Line 27-28 — Fixed error message from "NanoMax motorized stage" to "BSC203 motorized stage" and removed commented-out `#self.initialize()` call

**Moderate proposals**
- Line 3 — Lazy-load hardware library to avoid ImportError on startup
  ```python
  # current
  from thorlabs_apt_device.devices.bsc import BSC
  
  # proposed
  # Remove module-level import, add inside __init__:
  try:
      from thorlabs_apt_device.devices.bsc import BSC
      self.dev = BSC(...)
  except ImportError as e:
      self.__logger.warning(f'thorlabs_apt_device not installed: {e}')
      self.dev = None
  except SerialException:
      ...
  ```

- Line 22-23 — Read serial port from managerProperties instead of hard-coded 'COM9'
  ```python
  # current
  home = False
  port = 'COM9'
  
  # proposed
  manager_properties = positionerInfo.managerProperties
  home = manager_properties.get('home', False)
  port = manager_properties.get('port', 'COM9')  # fallback to COM9 if not specified
  ```

**Hard issues**
- **[ISSUE] BSC203StageManager methods crash when device initialization fails**
  - Methods like `homeAll()`, `homing()`, `move()`, `setPosition()`, etc. call `self.dev.*` without checking if `self.dev` is None
  - If initialization fails (line 28), all subsequent method calls will raise AttributeError
  - Need systematic None-checking or mock device pattern across all 10+ methods
  - Affects: `homeAll()`, `homing()`, `move()`, `setPosition()`, `move_relative_mm()`, `setJogPars()`, `jog()`

### PiezoconceptZManager — 2026-05-13

**Instant fixes applied**
- Line 1 — Removed unused import `import time` (dead import)
- Line 1, 23 — Added logger import and initialization in `__init__` (was missing)
- Line 40 — Replaced `print(f"Set position to: {value}")` with `self.__logger.debug()` for proper logging
- Line 60 — Replaced `print(f"PiezoZManager get abs error: {e}")` with `self.__logger.warning()` for proper error reporting

### PiezoconceptZManager2 — 2026-05-13

**Instant fixes applied**
- Line 1 — Removed unused import `import time` (dead import)
- Line 1, 23 — Added logger import and initialization in `__init__` (was missing)
- Line 31-32 — **CRITICAL**: Fixed syntax error with mismatched parentheses `except Exception as e:(` → `except Exception as e:` and `print(...))` → `self.__logger.warning(...)`
- Line 67 — Replaced `print(f"PiezoZManager get abs error: {e}")` with `self.__logger.warning()` for proper error reporting

### LeicaDMIManager — 2026-05-13

**Instant fixes applied**
- Line 13 — Replaced bare `except:` with `except KeyError:` to make exception handling specific and debuggable
- Line 31 — Replaced `print(f"creating lut for {positionerInfo} from calib failed due to: {e}")` with `self.__logger.warning()` for proper error reporting
- Line 34 — Replaced `print(self._rs232Manager.query(cmd))` with `self.__logger.info(f"DMI stand serial no: {self._rs232Manager.query(cmd)}")` for proper logging
- Line 43 — Replaced `print('Warning: Step bigger than 500nm.')` with `self.__logger.warning()` for proper warning logging

**Hard issues**
- **[ISSUE] LeicaDMIManager missing base class initialization**
  - Class does not call `super().__init__()` which is required by PositionerManager abstract base class
  - This causes `self._position` to never be initialized (base class sets this in __init__)
  - Lines 46 and 57 use `self._position` which will raise AttributeError on first use
  - Requires understanding what `initialPosition` dict should contain for this device
  - May need to query device for current position or use a default value

### MHXYStageManager — 2026-05-13

**Instant fixes applied**
- Line 29-32 — Wrapped serial number query in try/except to prevent startup crash if device not responding. Changed to use f-string for consistent logging format.

**Moderate proposals**
- Lines 26-28 — Add try/except with mock fallback for RS232 manager initialization to prevent crash when hardware is unavailable
  ```python
  # current
  self._rs232Manager = lowLevelManagers['rs232sManager'][
      positionerInfo.managerProperties['rs232device']
  ]
  
  # proposed
  try:
      self._rs232Manager = lowLevelManagers['rs232sManager'][
          positionerInfo.managerProperties['rs232device']
      ]
  except (KeyError, Exception):
      self.__logger.error(f'Failed to access MHXYStage RS232 connection, loading mock.')
      from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
      self._rs232Manager = MockRS232Driver(
          name=positionerInfo.managerProperties.get('rs232device', 'mock'),
          settings={'port': 'Mock'}
      )
  ```

### SQUIDStageManager — 2026-05-13

**Instant fixes applied**
- Line 7 — Removed unused SPEED=1000 constant (dead code, never referenced in the file)
- Line 10 — Moved logger initialization before super().__init__() call to ensure logger is available if errors occur during initialization
- Line 26 — Replaced print('Wrong axis...') with self.__logger.error() for proper error reporting

**Moderate proposals**
- Lines 14-16 — Add try/except with mock fallback for RS232 manager initialization to prevent crash when hardware is unavailable
  ```python
  # current
  self._rs232manager = lowLevelManagers['rs232sManager'][
      positionerInfo.managerProperties['rs232device']
  ]
  
  # proposed
  try:
      self._rs232manager = lowLevelManagers['rs232sManager'][
          positionerInfo.managerProperties['rs232device']
      ]
  except (KeyError, Exception):
      self.__logger.error('Failed to access SQUID stage RS232 connection, loading mock.')
      from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
      self._rs232manager = MockRS232Driver(
          name=positionerInfo.managerProperties.get('rs232device', 'mock'),
          settings={'port': 'Mock'}
      )
  ```

- Lines 33-35 — Add exception handling to closeEvent() to prevent crash if device is disconnected
  ```python
  # current
  def closeEvent(self):
      self._rs232manager._squid.close()
  
  # proposed
  def closeEvent(self):
      try:
          self._rs232manager._squid.close()
      except Exception as e:
          self.__logger.warning(f"Error closing SQUID stage: {e}")
  ```

### SmarACTPositionerManager — 2026-05-13

**Instant fixes applied**
- Line 5 — Removed `logging.basicConfig(level=logging.DEBUG)` which was setting global logging configuration and affecting all modules
- Line 12 — Removed `print('Could not import smaract interface!')` statement in import error handler (improper error reporting)
- Lines 68-70 — Replaced custom `logging.getLogger(name)` with ImSwitch's `initLogger(self, instanceName=name)` and moved before `super().__init__()` for consistency with other managers
- Lines 82-92 — Removed duplicate `ExitIfError` method definition (identical method was already defined at lines 281-291)
- Line 214 — Removed dead code `self.axis_lookup_table.items()` which had no effect (result not assigned or used)

**Moderate proposals**
- Lines 8-11 — Add mock fallback for hardware library import instead of re-raising ImportError
  ```python
  # current
  try:
      from imswitch.imcontrol.model.interfaces.SmarACT import *
  except ImportError:
      raise
  
  # proposed
  try:
      from imswitch.imcontrol.model.interfaces.SmarACT import *
      HARDWARE_AVAILABLE = True
  except ImportError:
      HARDWARE_AVAILABLE = False
      # Define mock constants and functions
      SA_OK = 0
      SA_STOPPED_STATUS = 0
      SA_HOLDING_STATUS = 1
      # ... (define other needed constants and mock functions)
  ```

- Lines 80-93 — Add try/except with mock fallback in `__setup_connection_and_buffers()` to allow manager to initialize when hardware is not available
  ```python
  # current
  def __setup_connection_and_buffers(self):
      """ Internal use only. Connect to the device and set up a buffer to receive replies.
      """
      self.mcsHandle = ct.c_ulong()
      self.outBuffer = ct.create_string_buffer(17)
      self.ioBufferSize = ct.c_ulong(18)
      self.ExitIfError(
          SA_FindSystems("", self.outBuffer, self.ioBufferSize)
      )
      # ... rest of connection code
  
  # proposed
  def __setup_connection_and_buffers(self):
      """ Internal use only. Connect to the device and set up a buffer to receive replies.
      """
      try:
          self.mcsHandle = ct.c_ulong()
          self.outBuffer = ct.create_string_buffer(17)
          self.ioBufferSize = ct.c_ulong(18)
          self.ExitIfError(
              SA_FindSystems("", self.outBuffer, self.ioBufferSize)
          )
          # ... rest of connection code
          self._mock = False
      except Exception as e:
          self.__logger__.warning(f"Failed to connect to SmarACT hardware: {e}. Using mock mode.")
          self._mock = True
          self.mcsHandle = None
  ```

### MockPositionerManager — 2026-05-13

**Instant fixes applied**
- Line 23 — Changed `self._position[self.axes[0]]` to `self._position[axis]` in `move()` method to use the axis parameter instead of ignoring it
- Line 26 — Changed `self._position[self.axes[0]]` to `self._position[axis]` in `setPosition()` method to use the axis parameter instead of ignoring it. This makes both methods consistent with the base class contract and allows proper KeyError if an invalid axis is passed.

### StandaRotatorManager — 2026-05-13

**Instant fixes applied**
- Line 29-31 — Removed redundant `position()` method that shadowed the base class property. The base class already defines `position` as a `@property` (RotatorManager.py line 28-30), so this method was dead code that would never be called.
- Line 63 — Fixed typo in warning message: "availalbe" → "available"

**Moderate proposals**
- Line 61 — Replace overly broad `except Exception:` with more specific exception handling
  ```python
  # current
  try:
      from imswitch.imcontrol.model.interfaces.standamotor import StandaMotor
      motor = StandaMotor(device_id, lib_loc, steps_per_turn, microsteps_per_step)
      self.__logger.info(f'Initialized Standa motor {device_id}')
  except Exception:
      self.__logger.warning(f'Failed to initialize Standa motor {device_id}, loading mocker')
  
  # proposed
  try:
      from imswitch.imcontrol.model.interfaces.standamotor import StandaMotor
      motor = StandaMotor(device_id, lib_loc, steps_per_turn, microsteps_per_step)
      self.__logger.info(f'Initialized Standa motor {device_id}')
  except (ImportError, OSError, RuntimeError) as e:
      self.__logger.warning(f'Failed to initialize Standa motor {device_id}: {e}, loading mocker')
  ```
  This prevents catching programming errors like AttributeError, TypeError, NameError which should fail fast for debugging.

### RS232Manager — 2026-05-13

**Instant fixes applied**
- Line 56-57 — Added exception details to warning message. Changed `except Exception:` to `except Exception as e:` and updated warning message to include the actual error: `f'Failed to initialize RS232 port {port}: {e}. Initializing mock RS232 port'`. This provides better debugging information when RS232 initialization fails.

**Moderate proposals**
- Line 56 — Replace overly broad `except Exception:` with more specific exception handling
  ```python
  # current
  try:
      from imswitch.imcontrol.model.interfaces.RS232Driver import generateDriverClass
      DriverClass = generateDriverClass(settings)
      rs232port = DriverClass(port)
      rs232port.initialize()
      return rs232port
  except Exception as e:
      self.__logger.warning(f'Failed to initialize RS232 port {port}: {e}. Initializing mock RS232 port')
  
  # proposed
  try:
      from imswitch.imcontrol.model.interfaces.RS232Driver import generateDriverClass
      DriverClass = generateDriverClass(settings)
      rs232port = DriverClass(port)
      rs232port.initialize()
      return rs232port
  except (ImportError, OSError, AttributeError, serial.SerialException) as e:
      self.__logger.warning(f'Failed to initialize RS232 port {port}: {e}. Initializing mock RS232 port')
  ```
  This prevents catching programming errors like NameError, TypeError, KeyError which should fail fast for debugging.

### ESP32Manager — 2026-05-13

**Instant fixes applied**
- Line 1 — Removed module-level hardware import `import uc2rest as uc2`. Moved to lazy import inside `__init__` at line 29 within try/except block. This prevents ImportError on startup if the UC2-REST library is not installed.
- Lines 14, 19, 24 — Replaced three bare `except:` clauses with `except KeyError:`. Bare except catches all exceptions including SystemExit and KeyboardInterrupt, making debugging impossible. KeyError is the specific exception when accessing missing dictionary keys.
- Lines 28-34 — Added try/except wrapper around UC2Client initialization to catch ImportError and warn user. Sets `self._esp32 = None` when library is not available instead of crashing.

**Moderate proposals**
- Lines 28-34 — Add proper mock fallback for when hardware connection fails (not just import failure)
  ```python
  # current
  try:
      import uc2rest as uc2
      self._esp32 = uc2.UC2Client(host=self._host, port=80, identity=self._identity, 
                                  serialport=self._serialport, baudrate=115200)
  except ImportError:
      self.__logger.warning('uc2rest library not installed. Install with: pip install UC2-REST')
      self._esp32 = None
  
  # proposed
  try:
      import uc2rest as uc2
      self._esp32 = uc2.UC2Client(host=self._host, port=80, identity=self._identity, 
                                  serialport=self._serialport, baudrate=115200)
  except ImportError:
      self.__logger.warning('uc2rest library not installed. Install with: pip install UC2-REST')
      self._esp32 = None
  except Exception as e:
      self.__logger.warning(f'Failed to initialize ESP32 device: {e}. Using None fallback.')
      self._esp32 = None
  ```
  This catches hardware connection failures (not just missing library) and provides a graceful fallback. However, the manager needs to be refactored to handle `self._esp32 = None` in all methods that use it.
