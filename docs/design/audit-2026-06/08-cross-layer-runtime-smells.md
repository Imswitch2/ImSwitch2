# Cross-Layer and Runtime-Smell Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Scope:** `imswitch/imcontrol`, `imswitch/imcommon`, and `imswitch/improcess`
runtime Python files, excluding test directories.

## Summary

The setup-mode and manager/controller cleanups removed several of the sharpest
setup-specific compromises, but the broader runtime still has dependency
direction and timing smells that will matter for smart microscopy, headless
workflows, and third-party setup extensions.

Static pass:

- 541 runtime Python files scanned.
- No `eval`, `exec`, `os.system`, or direct `subprocess` shell calls were found
  in the scanned runtime files.
- 10 mutable default arguments remain.
- 93 `print(...)` calls remain in runtime code.
- 100 `time.sleep(...)` calls remain in runtime code.
- 163 bare or pass-only exception handlers were counted. This is a smell metric,
  not a severity claim for every site; the highest-risk hardware/controller
  examples are called out in the manager/controller audit.
- 45 functions are 120 lines or longer.

## Findings

### [P1] Model code still depends on controller-owned types

**Sites:**

- `imswitch/imcontrol/model/WidgetStatePersistence.py:210`
- `imswitch/imcontrol/model/WidgetStatePersistence.py:323`
- `imswitch/imcontrol/model/WidgetStatePersistence.py:485`
- `imswitch/imcontrol/model/workflows/smart_mode_workflow.py:35`
- `imswitch/imcontrol/model/managers/lasers/NidaqLaserManager.py:6`

**Evidence:**

`WidgetStatePersistence` lazy-imports `ComponentStateApplyMode` from
`controller.basecontrollers`. `smart_mode_workflow.py` type-checks against
`SmartMicroscopyModeService` and result classes that live in the controller
package. `NidaqLaserManager` imports `CommunicationChannel` from the controller
package; that import appears unused.

**Impact:**

The model/workflow layer is supposed to be the reusable headless surface for
future tiling, timelapse, and event-triggered workflows. Importing controller
symbols from that layer makes extraction difficult and keeps controller code in
the dependency cone of model-only tests or scripts.

**Next fix:**

Move `ComponentStateApplyMode` and smart-mode result/protocol types to a model
or `imcommon` contract module. Keep controller implementations behind those
protocols. Remove the unused `NidaqLaserManager` controller import.

### [P1] Widgets still execute model logic and filesystem discovery

**Sites:**

- `imswitch/imcontrol/view/widgets/EtSnoutyWidget.py:9`
- `imswitch/imcontrol/view/widgets/EtSnoutyWidget.py:25-42`
- `imswitch/imcontrol/view/widgets/SegmentationParamsWidget.py:90`
- `imswitch/imcontrol/view/widgets/SegmentationParamsWidget.py:262`
- `imswitch/imcontrol/view/widgets/ShortcutEditorDialog.py:378`
- `imswitch/imcontrol/view/guitools/ViewSetupInfo.py:4`

**Evidence:**

`EtSnoutyWidget` imports model path helpers, creates pipeline directories, and
lists pipeline files in the widget constructor. `SegmentationParamsWidget`
imports and instantiates the workflow `Segmenter` and runs segmentation during
preview updates. `ShortcutEditorDialog` loads setup information through
`configfiletools`.

**Impact:**

The view layer is doing business logic and disk discovery. That makes the same
behavior harder to reuse in headless workflows and harder to test without Qt.
It also makes widgets less portable across setups because a widget constructor
can touch disk or instantiate algorithms.

**Next fix:**

Move discovery and algorithm execution behind controller/model services. Widgets
should receive display data and emit parameter changes; they should not own
setup file loading, pipeline directory creation, or segmentation execution.

### [P1] UI/controller paths use sleeps for readiness and synchronization

**Sites:**

- `imswitch/imcontrol/controller/controllers/RecordingController.py:107`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:145`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:165`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:201`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:266`
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:582`
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:663`
- `imswitch/imcontrol/controller/controllers/EtSnoutyController.py:723`
- `imswitch/imcontrol/controller/controllers/BFTimelapseController.py:66`
- `imswitch/imcontrol/controller/controllers/BFTimelapseController.py:68`

**Evidence:**

`RecordingController` sleeps after folder creation and after starting recording
before it triggers scans. `EtSnoutyController` sleeps after enabling a laser and
before reading detector frames or mask previews. `BFTimelapseController` sleeps
around transmitted-light shutter toggling with comments saying the delay still
needs to be tested.

**Impact:**

Fixed sleeps are fragile hardware contracts. They freeze or delay controller
paths, slow tests, and encode device timing assumptions without a ready signal.
For smart microscopy this is especially risky: event-triggered workflows need
deterministic "armed", "frame ready", and "mode applied" states.

**Next fix:**

Replace controller sleeps with manager signals, explicit futures, or small
state machines driven by `QTimer`. Recording should expose an "armed" or
"ready for scan trigger" signal. Detector frame preview should use a public
detector readiness/frame API. Lamp/laser settling should be setup-configured
and applied by a manager or mode applier rather than hardcoded in controllers.

### [P1] Mutable default arguments remain in runtime code

**Sites:**

- `imswitch/imcontrol/model/interfaces/ESP32Client.py:220`
- `imswitch/imcontrol/model/interfaces/restapicamera.py:54`
- `imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:265`
- `imswitch/imcontrol/view/widgets/SLMsWidget.py:215`
- `imswitch/imcontrol/view/widgets/SLMsWidget.py:250`
- `imswitch/imcontrol/view/widgets/SLMsWidget.py:488`
- `imswitch/imcontrol/view/widgets/SLMsWidget.py:1398`

**Evidence:**

Runtime APIs and widgets use `{}` or `[]` as function defaults.

**Impact:**

If any of those defaults are mutated, state leaks between calls and between
devices/widgets. Even when currently not mutated, these signatures invite hidden
shared state in future edits.

**Next fix:**

Use `None` defaults and instantiate lists/dicts inside the method. The
interface clients (`ESP32Client`, `restapicamera`) and `GalvoScanDesigner` are
the easiest first pass.

### [P2] `restapicamera.py` looks stale and under-owned

**Sites:**

- `imswitch/imcontrol/model/interfaces/restapicamera.py:11`
- `imswitch/imcontrol/model/interfaces/restapicamera.py:34`
- `imswitch/imcontrol/model/interfaces/restapicamera.py:54`
- `imswitch/imcontrol/model/interfaces/restapicamera.py:72`

**Evidence:**

The file has a typo-like `self. base_uri` assignment, a bare `except` in
connectivity probing, a mutable default payload, and direct `print` calls in
setters.

**Impact:**

This is probably a low-traffic interface, which makes it more dangerous: stale
device code tends to break only when someone tries to revive a setup. It should
either be refreshed and tested or marked experimental/legacy explicitly.

**Next fix:**

Replace the bare catch, remove mutable defaults, convert prints to logging, and
add a small interface-level test with a fake HTTP server or mocked `requests`.

### [P2] Runtime stdout prints hide diagnostics from the ImSwitch log

**Sites:**

- `imswitch/imcontrol/controller/controllers/ScanControllerMoNaLISA.py:202`
- `imswitch/imcontrol/controller/controllers/ScanControllerMoNaLISA.py:220`
- `imswitch/imcontrol/controller/controllers/ScanControllerMoNaLISA.py:245`
- `imswitch/imcontrol/controller/controllers/BeadRecController.py:234`
- `imswitch/imcontrol/controller/controllers/BeadRecController.py:342`
- `imswitch/imcontrol/controller/controllers/BeadRecController.py:755`
- `imswitch/imcontrol/controller/controllers/RotationScanController.py:254`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:273`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:280`

**Evidence:**

Controllers and hardware interfaces still print status and failure messages to
stdout instead of using `initLogger` or a widget status channel.

**Impact:**

In GUI runs, stdout is easy to miss. In hardware workflows, missed diagnostics
slow down debugging and make failure reports harder to reconstruct.

**Next fix:**

Route controller messages through the existing logger or explicit UI status
signals. Keep `print` only in scripts, command-line tools, or guarded demos.

### [P2] `improcess` still imports `imcontrol`

**Sites:**

- `imswitch/improcess/controller/ImProcessMainController.py:54`
- `imswitch/improcess/controller/ImProcessMainController.py:232`
- `imswitch/improcess/model/processing_config.py:9`
- `imswitch/improcess/model/processing_config.py:10`
- `imswitch/improcess/view/WatcherFrame.py:3`

**Evidence:**

`improcess` loads widget-state persistence and setup parsing through
`imcontrol`, and its view imports `imcontrol.view.guitools`.

**Impact:**

`improcess` cannot stand alone as a processing package. The reverse dependency
also keeps common setup/config concepts trapped under `imcontrol`.

**Next fix:**

Move shared setup metadata, persistence contracts, and GUI helpers needed by
both applications into `imcommon`, then inject concrete control-specific
services where needed.

### [P2] Several widgets and controllers are still too large to evolve safely

**Largest functions found:**

- `TriggerScopePLSRMulticolorWidget.__init__`: 314 LOC
- `ImProcessMainView.__init__`: 307 LOC
- `TriggerScopeLSXYRWidget.__init__`: 307 LOC
- `ScanWidgetAdvanced.initControls`: 266 LOC
- `TriggerScopeGalvoDetectionWidget.__init__`: 265 LOC
- `ScanControllerAdvanced.getParameters`: 244 LOC
- `RecordingManager._record`: 194 LOC
- `ImConMainController.__init__`: 180 LOC

**Impact:**

Large constructors and parameter builders make it expensive to add new scan
variants or workflow modes without regressions. They also hide smaller
duplicated patterns because the code has no local substructure to reuse.

**Next fix:**

Split only along stable seams: UI section builders, scan-parameter serializers,
recording lifecycle services, and main-controller boot phases. Avoid cosmetic
extraction until the public manager and workflow contracts are stable.

## Suggested sequencing

1. Move shared state/mode protocol types out of controller modules.
2. Remove easy mutable defaults and unused cross-layer imports.
3. Convert controller/interface `print` calls to logging or status signals.
4. Replace readiness sleeps in recording, Snouty frame preview, and BF timelapse
   with explicit manager/controller readiness contracts.
5. Move widget-owned filesystem discovery and segmentation execution into
   controller/model services.
6. Break `improcess` -> `imcontrol` imports by moving shared contracts to
   `imcommon`.
