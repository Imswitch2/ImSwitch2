# Manager/Controller Architecture Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Scope:** `imswitch/imcontrol/model/managers/**/*.py` and `imswitch/imcontrol/controller/controllers/**/*.py`

## Summary

The do-first fixes from `AUDIT-2026-06.md` are implemented, but the manager and
controller layers still carry several real compromises. The largest remaining
theme is not a single bug; it is missing public ownership boundaries. Controllers
still reach into manager internals in a few important paths, some managers hide
hardware initialization failures behind mock fallback, and several scan/setup
controllers combine UI plumbing, persistence, validation, hardware commands, and
legacy compatibility in one class.

Static pass:

- 149 Python files scanned: 98 manager files and 51 controller files.
- Bare `except:` remains in 4 files: `PyCoboltManager.py`,
  `HamamatsuManager.py`, `LeicaDMIManager_mock.py`, `ScanControllerMoNaLISA.py`.
- Biggest controller hotspots by size: `SLMsController` (~1300 LOC),
  `SetupModesController` (~1050 LOC), `ScanControllerAdvanced` (~1040 LOC),
  `SettingsController` (~900 LOC), `BeadRecController` (~900 LOC),
  `EventTriggeredBaseController` (~810 LOC), `EtSnoutyController` (~680 LOC).
- Biggest manager hotspots by size: `SwabianTimeTaggerManager`,
  `Cobolt0601NewLaserManager`, `NidaqManager`, `HamamatsuManager`,
  `APDManager`, `DetectorManager`, `RecordingManager`.

## Findings

### [P1] SetupStatus remains a rig-specific mode switcher

**Sites:**

- `imswitch/imcontrol/controller/controllers/SetupStatusController.py:14-91`
- `imswitch/imcontrol/controller/controllers/SetupStatusController.py:184-201`
- `imswitch/imcontrol/controller/controllers/SetupStatusController.py:229-237`

**Evidence:**

`SetupStatusController` still hardcodes `COM6`, `COM5`, `COM11`, camera layer
names (`Orca`, `WidefieldCamera`), six setup configuration dictionaries, and
hotkeys. It also constructs `APTDevice_Motor` directly in the controller.

**Impact:**

This cannot generalize to another Snouty-like setup without editing controller
source. It also duplicates the newer setup-mode/smart-mode machinery and remains
the live consumer of `sigSetConfig`.

**Next fix:**

Move the remaining responsibilities into setup-mode components:

- Flip mirrors: use `FlipMirrorController` as a `StatefulComponentMixin`
  participant.
- Detection layers: express as view/layer component state or a small public
  layer-visibility component.
- Rotation stage: migrate to `PositionerController`/positioner manager control
  if it is a KDC/general-purpose motor driver, not rotator.
- Remove `sigSetConfig` once external Snouty setup modes are migrated.

### [P1] StandManager leaks its private sub-manager and still says Leica

**Sites:**

- `imswitch/imcontrol/model/managers/StandManager.py:6-27`
- `imswitch/imcontrol/controller/controllers/EtMonalisaController.py:89-96`

**Evidence:**

`StandManager` loads a concrete stand manager through importlib, catches all
exceptions, logs `Failed to load LeicaDMIManager` regardless of the configured
stand, then loads `Mock{managerName}`. `EtMonalisaController` directly calls
`self._master.standManager._subManager.setFLUO()` / `setCS()` /
`setILshutter(1)`.

**Impact:**

An Olympus or other stand manager would either need matching private method names
or more controller edits. A real initialization error can silently fall back to a
mock manager, which is unsafe for hardware-critical mode switching.

**Next fix:**

Add public `StandManager` methods for the capabilities controllers need
(`setMode`, `setTransmittedLightShutter`, or a generic component-state API) and
make mock fallback explicit via configuration. Controller code should never call
`._subManager`.

### [P1] EtSnouty still bypasses detector manager internals

**Sites:**

- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:215`
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:661`
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:721`

**Evidence:**

EtSnouty uses `self._master.detectorsManager._subManagers[self.detectorFast]`
to get frames and build masks.

**Impact:**

This bypasses detector manager lifecycle and any future acquisition routing. It
also keeps EtSnouty divergent from the newer `EventTriggeredControllerBase`
pattern, so each smart microscopy feature risks being implemented twice.

**Next fix:**

Add public detector manager APIs for the needed operations (`getLatestFrame`,
named detector access, mask-preview frame acquisition), then route EtSnouty
through those. After that, reassess whether EtSnouty can inherit
`EventTriggeredControllerBase` or at least share more of its event/session
service layer.

### [P1] Controllers patch manager internals because public APIs are missing

**Sites:**

- `imswitch/imcontrol/controller/controllers/BSC203Controller.py:145-149`
- `imswitch/imcontrol/controller/controllers/LaserController.py:37`

**Evidence:**

`BSC203Controller` writes directly to `self._stageManager._position` to keep the
generic position widget in sync. `LaserController` reads
`lManager._laserInfo.managerProperties` to infer whether a calibration CSV is
present and then changes UI units/ranges.

**Impact:**

These are small but clear boundary leaks. They make future manager
implementations depend on private fields that are not part of the contract.

**Next fix:**

Add public manager APIs:

- `PositionerManager.updateTrackedPosition(axis, value)` or
  `syncPositionFromHardware()`.
- `LaserManager.displayRange()` / `usesCalibrationLookup()` /
  `calibrationMetadata()`.

### [P1] Hardcoded fallback ports remain inside hardware managers

**Sites:**

- `imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py:19`
- `imswitch/imcontrol/model/managers/positioners/SerialDacZManager.py:40`

**Evidence:**

`BSC203StageManager` defaults to `COM9`; `SerialDacZManager` defaults to
`COM13`.

**Impact:**

This is the same class of issue as the removed setup-specific laser names:
hardware identity should be in setup JSON, not in manager code. Defaults can
cause a wrong serial device to be opened on another rig.

**Next fix:**

Require `managerProperties.port` for hardware managers that open serial devices.
If examples need convenience defaults, put them in example setup files, not
runtime manager code. A mock mode can remain explicit.

### [P1] Bare exception handlers remain in hardware/control paths

**Sites:**

- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:29`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:71`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:102`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:115`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:128`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:132`
- `imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py:627`
- `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py:181`
- `imswitch/imcontrol/model/managers/stands/LeicaDMIManager_mock.py:9`
- `imswitch/imcontrol/controller/controllers/ScanControllerMoNaLISA.py:364`

**Evidence:**

The remaining bare catches are all in hardware-adjacent code or scan parameter
calculation. Several return `False`, `0`, or silently pass.

**Impact:**

They catch `KeyboardInterrupt`/`SystemExit` and hide hardware communication or
parameter errors. This makes failures look like disconnected devices or default
values.

**Next fix:**

Replace with specific exception types and log context. The first easy targets
are `HamamatsuManager.getParameter`, `ScanControllerMoNaLISA.updatePixels`, and
the Cobolt serial discovery loop.

### [P2] ScanControllerAdvanced is too much scan schema in one controller

**Sites:**

- `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py:393`
- `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py:642`

**Evidence:**

`getParameters()` is roughly 244 LOC and `setParameters()` roughly 137 LOC. They
translate UI state into analog/digital scan dictionaries, advanced TTL line
programs, line-step devices, lock-master state, and compatibility defaults. Many
individual widget setter failures are swallowed with `except Exception: pass`.

**Impact:**

Adding a new scan workflow or mode means editing a fragile controller method
instead of extending a schema/service. Tests exist for some sequence-builder
logic, but the controller still owns too much mutable dict assembly.

**Next fix:**

Extract a scan-parameter schema/serializer service that can round-trip UI state
to `ScanInfo`-compatible dicts and validate missing devices before hardware
starts. The controller should mostly bind widgets to that service.

### [P2] SetupModesController is a service, backend, UI, and persistence layer

**Site:**

- `imswitch/imcontrol/controller/controllers/SetupModesController.py:14`

**Evidence:**

The class is over 1000 LOC and owns mode application, hazard/preflight logic,
state bundle I/O, UI interaction, and smart microscopy role editing. It has
improved substantially, but it is now core infrastructure and should not stay as
one controller.

**Impact:**

The more workflows depend on mode switching, the harder it becomes to reason
about hazards and persistence because the logic is tied to a UI controller.

**Next fix:**

Move non-UI logic into a model/service layer:

- `SetupModeRepository` for load/save/list.
- `SetupModeApplier` for applying component state and rollback.
- `SmartMicroscopyModeService` already exists; keep controllers as consumers.

### [P2] Mock fallback is often implicit

**Sites:**

- `imswitch/imcontrol/model/managers/StandManager.py:16-25`
- `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py:386-395`
- `imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py:22-34`

**Evidence:**

Some managers fall back to mock or `dev=None` on import/init failure. That is
useful for development, but in hardware-critical paths the distinction between
"no hardware configured" and "hardware failed to initialize" must be explicit.

**Impact:**

Real experiments can continue with a mock or disabled manager while UI still
appears available, unless every caller checks carefully.

**Next fix:**

Add a common manager capability/status contract:

- `isMock`
- `isConnected`
- `initializationError`
- `hardwareCritical`

Then controllers/setup modes can block or warn consistently.

## Suggested sequencing

1. Decommission `SetupStatusController` and `sigSetConfig` after external Snouty
   setup modes are migrated.
2. Add public manager APIs for `StandManager`, detector access, position
   tracking, and laser calibration metadata.
3. Remove manager-level hardcoded serial-port defaults.
4. Replace remaining bare exceptions in PyCobolt, Hamamatsu, Leica mock, and
   MoNaLISA scan code.
5. Split scan/setup controller services after the public manager API pass, so
   the extracted services can use stable contracts.
