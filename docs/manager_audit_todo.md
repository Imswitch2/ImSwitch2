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
- [ ] `imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/GXPIPYManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/TISManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/AVManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/JetsonCamManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/PiCamManager.py`
- [ ] `imswitch/imcontrol/model/managers/detectors/ESP32CamManager.py`

## Laser Managers

- [ ] `imswitch/imcontrol/model/managers/lasers/NidaqLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/Cobolt0601LaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/Cobolt0601NewLaserManager.py`
- [ ] `imswitch/imcontrol/model/managers/lasers/CoboltLaserManager.py`
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
