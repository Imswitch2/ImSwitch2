# testalab_scanDev Port Backlog

Source branch: `upstream/testalab_scanDev`
Source URL: https://github.com/ImSwitch/ImSwitch/commits/testalab_scanDev/
Source head audited: `3f14744e62ab5caaa05734bffe5c8c064900fedd`
Source base used for rough sizing: `upstream/master...upstream/testalab_scanDev`
Initial target head audited: `main` at `d3a699c1f9aba15de65e9824a78f046366078ef1`
Initial audit date: 2026-06-17

## Rough Audit

The source branch contains 261 commits after the merge-base with
`upstream/master`. The full diff is large:

- 213 files changed
- 39,917 insertions
- 1,210 deletions

Main clusters:

- setup modes for hardware state snapshots
- advanced scan sequence-builder UI, controller, manager, and TTL designer deltas
- SLMs widget/controller and CGH pattern designer support
- flip-mirror widget/manager support
- hardware manager deltas for TIS, Cobolt, Serial DAC Z, PI/Piezo, PMT, Swabian,
  Leica stand, and related templates
- config editor/templates and TIS docs
- several already-ported or unrelated legacy extras

Components already present in the target tree at audit time include:

- `ScanControllerAdvanced.py`
- `ScanWidgetAdvanced.py`
- `ScanManagerAdvanced.py`
- `AdvancedScanTTLCycleDesigner.py`
- `SLMsController.py`
- `SLMsWidget.py`
- `BFTimelapseController.py` / `BFTimelapseWidget.py`
- `LeicaStandController.py` / `LeicaStandWidget.py`
- `Cobolt0601NewLaserManager.py`
- `PMTManager.py`
- `SwabianTimeTaggerManager.py`

Components apparently missing in the target tree at audit time include:

- `SetupModeController.py`
- `SetupModesController.py`
- `SetupModesWidget.py`
- `FlipMirrorController.py`
- `FlipMirrorWidget.py`
- `FlipMirrorsManager.py`
- `model/managers/flipMirrors/ThorlabsMFF.py`
- `model/managers/flipMirrors/ThorlabsMFF_mock.py`
- `SerialDacZManager.py`

## Automation Workflow

Each automation run must do exactly one package from the queue.

1. Read this file.
2. Pick the first package whose status is `[todo]`, whose dependencies are
   `[done]` or not listed, and that is not marked automation-deferred.
3. Change its status to `[in_progress]` before editing.
4. Compare the source branch against target code with source-aware commands,
   for example:
   - `git show upstream/testalab_scanDev:path/to/file.py`
   - `git diff main upstream/testalab_scanDev -- path/to/file.py`
   - `git log --oneline upstream/master..upstream/testalab_scanDev -- path`
5. Port behavior into ImSwitch2 patterns. Do not copy stale code blindly.
6. Add or update focused no-hardware tests when practical.
7. Run the narrowest useful checks:
   - `python -m compileall -q <changed-python-files>`
   - `python -m pytest <focused-tests> -q`
   - `ruff check <changed-python-files>` when available
   - `git diff --check -- <changed-files>`
8. Mark the package `[done]` with a short implementation note and checks run.
   If blocked, mark `[blocked]` with the exact blocker and stop.
9. Commit only the package changes. Do not push or open a PR unless the user
   later asks for that.

Safety rules:

- Do not run hardware, move stages, enable lasers, start DAQ tasks, or poll real
  devices.
- Prefer mocks, source-level tests, and pure logic tests.
- Preserve ImSwitch2 architectural changes that post-date ImSwitch1.
- Avoid vendoring binary SDK files or large third-party library copies unless a
  package explicitly requires and justifies them.
- Leave unrelated user changes alone.

## Package Queue

### P01 - Setup Modes Core

Status: `[done]`
Depends on: none
Source commits: `f5c19149`, `f212b706`, `143165b1`
Primary source files:

- `imswitch/imcontrol/controller/SetupModeController.py`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py`
- `imswitch/imcontrol/view/widgets/SetupModesWidget.py`
- `imswitch/imcontrol/_test/unit/test_setup_modes.py`
- integration deltas in `CommunicationChannel.py`, `MasterController.py`,
  `basecontrollers.py`, `SetupInfo.py`, `widgets/__init__.py`, and
  `controllers/__init__.py`

Task summary:
Port the setup-mode data model, controller, widget, and no-hardware tests so
hardware state snapshots can be saved, edited, applied, renamed, duplicated, and
deleted.

Notes:
Keep this package focused on the setup-mode backbone. Do not also port all
device-specific setup-mode adapters unless they are needed for tests to pass.

Done notes:
Ported the setup-mode backend, compact setup-mode widget/controller, lazy
controller/widget exports, `SetupModes` dock metadata, API wiring, and the scan
setup-mode adapter while preserving ImSwitch2 `ScanLifecycleMixin`,
`WorkflowFacadeController`, and widget-state persistence.

Checks run:

- `git diff --check`
- `python -m compileall -q imswitch/imcontrol/controller/SetupModeController.py imswitch/imcontrol/controller/controllers/SetupModesController.py imswitch/imcontrol/view/widgets/SetupModesWidget.py imswitch/imcontrol/controller/basecontrollers.py imswitch/imcontrol/controller/ImConMainController.py imswitch/imcontrol/view/ImConMainView.py imswitch/imcontrol/view/guitools/ViewSetupInfo.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest imswitch/imcontrol/_test/unit/test_setup_modes.py -q`
- `ruff check imswitch/imcontrol/controller/SetupModeController.py imswitch/imcontrol/controller/controllers/SetupModesController.py imswitch/imcontrol/view/widgets/SetupModesWidget.py imswitch/imcontrol/controller/basecontrollers.py imswitch/imcontrol/controller/ImConMainController.py imswitch/imcontrol/view/ImConMainView.py imswitch/imcontrol/view/guitools/ViewSetupInfo.py imswitch/imcontrol/_test/unit/test_setup_modes.py`

### P02 - Flip Mirror Support

Status: `[done]`
Depends on: P01 only if setup modes are touched during integration
Source commit: `de6a3ec7`
Primary source files:

- `imswitch/imcontrol/controller/controllers/FlipMirrorController.py`
- `imswitch/imcontrol/view/widgets/FlipMirrorWidget.py`
- `imswitch/imcontrol/model/managers/FlipMirrorsManager.py`
- `imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF.py`
- `imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF_mock.py`
- related setup/template/widget registration files

Task summary:
Port flip-mirror widget, manager aggregation, and Thorlabs MFF mock-safe manager
support.

Notes:
Keep physical-device calls behind existing manager patterns and cover registration
or mock behavior without real hardware.

Done notes:
Ported the flip-mirror controller, widget, manager aggregation, Thorlabs MFF
hardware manager, and mock manager. Added `flipMirrors` setup schema support,
master-controller construction, lazy controller/widget exports, dock metadata,
and no-hardware tests for mock loading, state changes, reset, finalization, and
lazy exports. The `ThorlabsMFF_mock` manager name remains loadable through a
class alias for `MultiManager`.

Checks run:

- `git diff --check`
- `python -m compileall -q docs/agent_tasks/testalab_scanDev_port_backlog.md imswitch/imcontrol/controller/controllers/FlipMirrorController.py imswitch/imcontrol/view/widgets/FlipMirrorWidget.py imswitch/imcontrol/model/managers/FlipMirrorsManager.py imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF.py imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF_mock.py imswitch/imcontrol/model/SetupInfo.py imswitch/imcontrol/model/__init__.py imswitch/imcontrol/model/managers/__init__.py imswitch/imcontrol/controller/MasterController.py imswitch/imcontrol/controller/controllers/__init__.py imswitch/imcontrol/view/widgets/__init__.py imswitch/imcontrol/view/ImConMainView.py imswitch/imcontrol/view/guitools/ViewSetupInfo.py imswitch/imcontrol/_test/unit/test_flip_mirrors.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest imswitch/imcontrol/_test/unit/test_flip_mirrors.py -q`
- `ruff check imswitch/imcontrol/controller/controllers/FlipMirrorController.py imswitch/imcontrol/view/widgets/FlipMirrorWidget.py imswitch/imcontrol/model/managers/FlipMirrorsManager.py imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF.py imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF_mock.py imswitch/imcontrol/model/SetupInfo.py imswitch/imcontrol/model/__init__.py imswitch/imcontrol/model/managers/__init__.py imswitch/imcontrol/controller/MasterController.py imswitch/imcontrol/controller/controllers/__init__.py imswitch/imcontrol/view/widgets/__init__.py imswitch/imcontrol/view/ImConMainView.py imswitch/imcontrol/view/guitools/ViewSetupInfo.py imswitch/imcontrol/_test/unit/test_flip_mirrors.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest imswitch/imcontrol/_test/unit/test_setup_modes.py imswitch/imcontrol/_test/unit/test_flip_mirrors.py -q`

### P03 - Advanced Scan Sequence Builder Delta

Status: `[done]`
Depends on: none
Source commits: `1dcbbe2a`, `740a72f0`, `e73aafdd`, `e43f63b5`,
`814736db`, `47b09e2f`, `db9ae85a`, `7158509e`, `eb7a241e`, `3f14744e`
Primary source files:

- `imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py`
- `imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py`
- `imswitch/imcontrol/model/managers/ScanManagerAdvanced.py`
- `imswitch/imcontrol/model/signaldesigners/AdvancedScanTTLCycleDesigner.py`
- related scan setup/templates/tests

Task summary:
Port the missing sequence-builder behavior into the already-present ImSwitch2
advanced scan files.

Acceptance points:

- line-program device checkbox only changes visibility, matching `3f14744e`
- loading scanning parameters handles saved values correctly
- graph time-unit selection is preserved
- dead-time label and device tooltips are present where compatible
- persistent sequence-builder device selection works
- start-delay semantics, including negative values, are preserved where safe

Done notes:
Ported the advanced scan sequence-builder UI delta into the existing ImSwitch2
advanced scan widget while keeping the deferred BeadRec controls out. Added
line-program visibility-only toggling, timing/sequence modes, lock-with device
state, persistent checkable sequence device menus, negative start-delay
sequence rows, dead-time labeling/tooltips, graph time-unit replay, scan curve
plotting, saved sequence-builder state restore, and TTL filtering for scanning
positioners. Preserved ImSwitch2 JSON/INI scan parameter loading and widget
state persistence.

Checks run:

- `git diff --check -- docs/agent_tasks/testalab_scanDev_port_backlog.md imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py imswitch/imcontrol/model/managers/ScanManagerAdvanced.py imswitch/imcontrol/model/signaldesigners/AdvancedScanTTLCycleDesigner.py imswitch/imcontrol/_test/unit/test_scan_advanced_sequence_builder.py`
- `python -m compileall -q docs/agent_tasks/testalab_scanDev_port_backlog.md imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py imswitch/imcontrol/model/managers/ScanManagerAdvanced.py imswitch/imcontrol/model/signaldesigners/AdvancedScanTTLCycleDesigner.py imswitch/imcontrol/_test/unit/test_scan_advanced_sequence_builder.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest imswitch/imcontrol/_test/unit/test_scan_advanced_sequence_builder.py imswitch/imcontrol/_test/unit/test_widget_responsiveness_contract.py imswitch/imcontrol/_test/unit/test_scan_lifecycle.py -q`
- `ruff check imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py imswitch/imcontrol/controller/controllers/ScanControllerAdvanced.py imswitch/imcontrol/model/managers/ScanManagerAdvanced.py imswitch/imcontrol/model/signaldesigners/AdvancedScanTTLCycleDesigner.py imswitch/imcontrol/_test/unit/test_scan_advanced_sequence_builder.py`

### P04 - SLMs Setup-Mode Sync And Pattern Deltas

Status: `[done]`
Depends on: P01
Source commits: `b785064f`, `f212b706`, `143165b1`
Primary source files:

- `imswitch/imcontrol/view/widgets/SLMsWidget.py`
- `imswitch/imcontrol/controller/controllers/SLMsController.py`
- `imswitch/imcontrol/controller/patterndesigners/`
- related SLM manager/templates/tests

Task summary:
Port missing SLMs widget/controller behavior, especially setup-mode mirroring and
combobox synchronization.

Notes:
The target tree already has SLMs files. Diff source and target carefully and
avoid regressing ImSwitch2-specific responsiveness fixes.

Done notes:
Ported SLMs setup-mode synchronization including config combobox sync between
widget and SetupModeController, startup config persistence to setup file,
config inspector dialog with metadata and full dump, config comparison before
overwrite, duplicate/reload/open-folder operations, separated user actions
(activated) from programmatic changes (currentIndexChanged) to prevent loops,
enhanced UI with tooltips and dynamic button states, and aberration pattern
fixes (coma/spherical linear/quadratic term removal). All changes preserve
ImSwitch2 architectural patterns.

Checks run:

- `git diff --check`
- `python -m compileall -q imswitch/imcontrol/controller/controllers/SLMsController.py imswitch/imcontrol/view/widgets/SLMsWidget.py`
- `ruff check imswitch/imcontrol/controller/controllers/SLMsController.py imswitch/imcontrol/view/widgets/SLMsWidget.py`

### P05 - Hardware Manager Bugfix Deltas

Status: `[done]`
Depends on: none
Source commits: `34c2ee68`, `04ae8d1a`, `7619ade3`, `c18c4a67`
Primary source files:

- `imswitch/imcontrol/model/managers/positioners/PositionerManager.py`
- `imswitch/imcontrol/model/managers/positioners/SerialDacZManager.py`
- `imswitch/imcontrol/model/managers/detectors/TISManager.py`
- `imswitch/imcontrol/model/interfaces/tiscamera.py`
- `imswitch/imcontrol/model/interfaces/tiscamera_mock.py`

Task summary:
Port no-hardware-safe manager bugfixes and add focused tests where possible.

Acceptance points:

- generalized `get_abs(axis)` behavior is preserved for focus-lock callers
- TIS close/set-parameter fixes are present without forwarding synthetic params
- Serial DAC Z manager is ported or explicitly marked blocked with dependency
  details

Out of scope:

- `Cobolt0601NewLaserManager.py`, Cobolt mock/driver deltas, and source commit
  `80457490` are intentionally not part of this package.

Done notes:
Ported 3 commits (04ae8d1a skipped as Cobolt laser-related). Created new
SerialDacZManager.py (227 lines) with virtual Z positioner via Serial DAC,
MicroPython REPL interface, voltage clamping, and safe shutdown. Fixed TIS
camera resource leaks by implementing proper close() in TISManager.py and
tiscamera.py including critical ic_ic.close_library() call, idempotent closing,
and destructor cleanup. Generalized positioner get_abs(axis) signature in
PiezoconceptZManager.py and PiezoconceptZManager2.py. Enhanced
FocusLockController.py with axis resolution logic (prefers Z, falls back to 0),
abstraction methods getPositionerAbs() and movePositioner(). Added optional
positionerAxis field to FocusLockInfo in SetupInfo.py. All acceptance points
satisfied.

Checks run:

- `python -m compileall -q <all 7 modified files>`
- `git diff --check`
- `ruff check <all 7 modified files>`

### P06 - Scan Compatibility For BeadRec And Recording Edges

Status: `[done]`
Depends on: P03
Source commits audited: `33eaa07c`, `77195838`, `4ca7a58d`, `d8bf778a`,
`1590188d`
Primary source files:

- `imswitch/imcontrol/controller/controllers/BeadRecController.py`
- `imswitch/imcontrol/controller/controllers/RecordingController.py`
- `imswitch/imcontrol/model/managers/RecordingManager.py`
- scan parameter consumers touched by advanced scan length semantics

Task summary:
Audit current ImSwitch2 BeadRec/recording behavior against the scan-length,
soft timelapse stop, joystick, per-detector camera TTL, and advanced-scan
BeadRec hookup ideas from `testalab_scanDev`.

Done notes:
Audited P06 as a verification package rather than a direct port. The old
BeadRec scan-length patch (`33eaa07c`) only removed stale `+1` dimensions; the
current ImSwitch2 implementation already enforces exact reconstruction sizes
through `BeadAcquisitionConfig`, `create_reconstruction_buffer()`, and
`reconstruction_image()`. The soft scan-timelapse stop (`77195838`) and
joystick disable/restore fix (`4ca7a58d`) are already present in the current
RecordingController and PositionerController. The per-detector `numCamTTL`
recording logic from `d8bf778a` is present in newer RecordingManager form.
The WIP direct AdvancedScan/BeadRec signal bridge from `1590188d` is superseded
by the current scan-source/CommunicationChannel contract and
`getFramesPerScanPixel()` behavior. No code was ported from these old commits.

Checks run:

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytestqt.plugin imswitch/imcontrol/_test/unit/test_recording.py imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py imswitch/imcontrol/_test/unit/test_bead_rec_controller_contract.py -q`

### P07 - Config Editor Templates And Docs

Status: `[done]`
Depends on: P02, P03, P05
Source commits: `d1cf26e3`, `df586807`
Primary source files:

- `utility_scripts/imswitch_config_editor.py`
- `utility_scripts/builtin_templates/`
- `docs/TISCamera.rst`
- `docs/api/api.imcontrol.rst`
- `docs/use-cases.rst`
- setup-info reference docs if the target tree uses them

Task summary:
Port only the template and documentation updates that correspond to features
successfully ported in earlier packages.

Notes:
Do not reintroduce stale templates for features that were skipped or blocked.

Done notes:
Config editor and all 16 builtin templates from d1cf26e3 were already present in
the repository from prior porting work and have been enhanced beyond the source
commit. Added missing TISCamera.rst documentation (43 lines) from df586807 with
installation instructions for TIS camera drivers and TISGrabber C DLL on Windows.
All templates validated as proper JSON. The docs/api/api.imcontrol.rst and
docs/use-cases.rst files were not modified in the source commits and require no
updates.

Checks run:

- `python -m compileall -q utility_scripts/imswitch_config_editor.py`
- `find utility_scripts/builtin_templates -name "*.json" | xargs -n1 python -m json.tool > /dev/null`
- `git diff --check`

### P08 - Optional Branch Leftovers Audit

Status: `[todo]`
Depends on: P01, P02, P03, P04, P05, P06, P07
Source commits: remaining commits in `upstream/master..upstream/testalab_scanDev`
Primary source areas:

- EtMonalisa/EtSTED deltas
- imreconstruct denoising files
- vendored `pipython`
- binary SLM SDK files
- one-off scripts such as `ai_nidaq_tests.py` and `test-galvoscandesigner.py`

Task summary:
Audit branch leftovers and either slice them into new explicit backlog packages
or mark them out of scope for this scanDev port.

Notes:
This package should not make broad code changes. It should update this backlog
with any newly discovered package definitions and source refs.
