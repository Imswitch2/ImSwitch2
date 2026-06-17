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

Status: `[todo]`
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

### P03 - Advanced Scan Sequence Builder Delta

Status: `[todo]`
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

### P04 - SLMs Setup-Mode Sync And Pattern Deltas

Status: `[todo]`
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

### P05 - Hardware Manager Bugfix Deltas

Status: `[todo]`
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

### P06 - Scan Compatibility For BeadRec And Recording Edges

Status: `[todo]`
Depends on: P03
Automation: automation-deferred; do not select this package in automated runs
until the user explicitly re-enables it.
Source commits: none for now. Do not apply the originally identified
recording/BeadRec commits (`33eaa07c`, `77195838`, `4ca7a58d`) in this package.
Primary source files:

- `imswitch/imcontrol/controller/controllers/BeadRecController.py`
- `imswitch/imcontrol/controller/controllers/RecordingController.py`
- `imswitch/imcontrol/model/managers/RecordingManager.py`
- scan parameter consumers touched by advanced scan length semantics

Task summary:
Placeholder for future scan/recording compatibility work caused by advanced
scan length/sequence changes. Keep it as todo, but do not implement it from the
`testalab_scanDev` commits for now.

Notes:
Deferred because the ImSwitch2 recording manager changed too much for a direct
port from `testalab_scanDev`. When this is revisited, audit the current
recording/BeadRec design first and make a fresh plan instead of applying the
old commits.

### P07 - Config Editor Templates And Docs

Status: `[todo]`
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
