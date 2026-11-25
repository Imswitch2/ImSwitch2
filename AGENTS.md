# AGENTS.md

Dense orientation for coding agents. Facts only; every backticked path and
name below is checked to exist by `imswitch/imcommon/_test/test_agents_md.py`.
Humans: start at `docs/index.rst` (built at imswitch2.readthedocs.io).
Package detail: `imswitch/imcontrol/AGENTS.md`, `imswitch/improcess/AGENTS.md`,
`imswitch/imscripting/AGENTS.md`.

## What this is

ImSwitch2: PyQt5 + napari desktop app that runs custom microscopes. Hardware is
described by one JSON *setup file*; `MultiManager` builds one manager per device
from its `managerName`. Python >= 3.10 (CI: 3.10), PyPI name `imswitch2`,
import name `imswitch`, entry `python -m imswitch`.

## Where to find what

| Path | What |
|---|---|
| `imswitch/__main__.py` | startup: args, `modules.json`, module load, `launchApp` |
| `imswitch/imcommon/` | shared library, not a module: `framework/` (Qt shim), `model/api.py` (`APIExport`, `generateAPI`), `model/dirtools.py` (user folder, defaults sync), `model/WidgetStatePersistence.py`, `controller/ModuleCommunicationChannel.py` |
| `imswitch/imcontrol/` | acquisition: managers, controllers, widgets, scan, recording, config editor -> `imswitch/imcontrol/AGENTS.md` |
| `imswitch/improcess/` | processing: reconstructors, processors, results, workflows, live -> `imswitch/improcess/AGENTS.md` |
| `imswitch/imscripting/` | script editor/runner, script scope -> `imswitch/imscripting/AGENTS.md` |
| `imswitch/pluginapi/` | stable import surface for out-of-tree device plugins |
| `imswitch/_data/user_defaults/` | shipped setups, scripts, tutorials, `config/modules.json`; copied to `~/ImSwitchConfig` |
| `examples/plugins/` | pip-installable device plugin packages (own CI job) |
| `tools/` | generators and checkers (table below) |
| `docs/` | Sphinx site; `docs/architecture.rst` is the developer map |
| `.github/workflows/ci.yml` | lint, test lanes, plugin tests, package build |

## Layering, as practised

- A *module* is a package with `__imswitch_module__ = True` and
  `getMainViewAndController`; found by `getAvailableModules`.
- Between modules: `ModuleCommunicationChannel` signals at runtime. Direct
  imports across modules are rare, lazy and for shared helpers only:
  `imswitch/imcontrol/model/acquisition_result.py`,
  `imswitch/imcontrol/model/plugins/validation.py`,
  `imswitch/imcontrol/view/configeditor/editor.py` (into improcess),
  `imswitch/improcess/model/processing_config.py` (into imcontrol). Do not add more.
- MVP inside a module: widgets are passive (render, emit signals); controllers
  hold logic; managers own hardware. Controllers reach each other only via
  `CommunicationChannel` (per module) or service objects on it
  (`scanWorkflow`); not by importing each other.
- Use `Signal`, `Thread`, `Worker`, `Timer`, `Mutex` from
  `imswitch.imcommon.framework` in application code. New coordination logic
  is written Qt-free and tested in isolation (examples:
  `imswitch/imcontrol/model/managers/_scan_execution.py`,
  `imswitch/imcontrol/model/managers/_acquisition_leases.py`,
  `imswitch/imcontrol/model/scan_cloak.py`).
- Script/remote API = controller methods decorated `@APIExport`;
  `@APIExport(runOnUIThread=True)` for anything touching Qt objects.

## Red zone

CI has no hardware: it never exercises timing, emission, motion or DAQ output.
Every PR is reviewed by a maintainer who tests it on their own microscope before
merge (`docs/current-state-and-known-issues.rst`). Red-zone paths (each glob is
tested to match at least one file):

<!-- redzone-globs:start -->
```
imswitch/imcontrol/model/managers/NidaqManager.py
imswitch/imcontrol/model/managers/TriggerScopeManager.py
imswitch/imcontrol/model/managers/ScanManager*.py
imswitch/imcontrol/model/managers/_scan_execution.py
imswitch/imcontrol/model/managers/_acquisition_leases.py
imswitch/imcontrol/model/managers/lasers/**/*.py
imswitch/imcontrol/model/managers/positioners/**/*.py
imswitch/imcontrol/model/managers/rotators/**/*.py
imswitch/imcontrol/model/managers/stands/**/*.py
imswitch/imcontrol/model/managers/detectors/**/*.py
imswitch/imcontrol/model/managers/pulsegen/**/*.py
imswitch/imcontrol/model/managers/rs232/**/*.py
imswitch/imcontrol/model/signaldesigners/**/*.py
imswitch/imcontrol/model/interfaces/**/*.py
imswitch/imcontrol/model/lantzdrivers/**/*.py
imswitch/imcontrol/model/devices/lifecycle*.py
imswitch/imcontrol/model/laser_power.py
imswitch/imcontrol/controller/MasterController.py
imswitch/imcontrol/controller/controllers/LaserController.py
imswitch/imcontrol/controller/controllers/ScanController*.py
imswitch/imcontrol/controller/controllers/TriggerScope*.py
imswitch/imcontrol/controller/controllers/_triggerscope_scan_*.py
```
<!-- redzone-globs:end -->

Rules when touching them:
1. Do not change timing constants, voltage/power ranges or defaults, safe
   states or the finalize order in `MasterController` unless the task asks;
   values come from a maintainer.
2. Startup restore (`ComponentStateApplyMode` `STARTUP_RESTORE`) must never start
   emission or motion (`docs/state-persistence.rst`).
3. Tests use mocks or `nidaq.simulation: true` only; never open a real device.
4. In the PR, tick the red-zone box of
   `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`, name the paths and
   say what a rig test should check.

## To add X, touch these (guard test in brackets)

Test paths are under `imswitch/imcontrol/_test/unit/` unless prefixed.

**Device manager (existing kind)** - traced on 959ded43
1. `imswitch/imcontrol/model/managers/<family>/<Name>Manager.py`, subclass the
   family base; open hardware via `_installBackend` (see Mock rules).
2. `python tools/extract_manager_schemas.py --write`; commit
   `imswitch/imcontrol/model/configeditor/schemas/` [`test_configeditor_schemas_match_source.py`,
   which also pins the manager count].
3. Snapshot: `python tools/extract_manager_schemas.py --report --json > imswitch/imcontrol/_test/unit/configeditor_extraction_snapshot.json`
   [`test_configeditor_extraction_tree.py`].
4. Either register in `imswitch/imcontrol/model/plugins/builtins.py` (then bump the
   `registered` count) or add it to `_UNREGISTERED_CORE_MANAGERS` [`test_setup_metadata.py`].
5. A card in `docs/devices/<family>.rst`, or the name in `UNDOCUMENTED`
   [`test_devices_docs_drift.py`].
6. Optional template: `imswitch/imcontrol/view/configeditor/builtin_templates/<family>/`.

**New device kind** - traced on 9b488ad2, 8d8b6819: the above, plus a
`SetupInfo` field (`imswitch/imcontrol/model/SetupInfo.py`), a group
`MultiManager` subclass in `imswitch/imcontrol/model/managers/`, construction and
finalize in `MasterController`, `MULTIMANAGER_BACKED_KINDS` in
`imswitch/imcontrol/model/plugins/manifest.py`, `imswitch/imcontrol/model/plugins/setup_metadata.py`,
`imswitch/imcontrol/model/configeditor/kinds.py` + `catalog.py`, status kind in
`imswitch/imcontrol/model/devices/status.py`/`supervisor.py` [`test_setup_metadata.py`,
`test_configeditor_type_authority.py`, `test_master_controller_shutdown.py`].

**Controller + widget (panel `<Key>`)** - traced on 3f5eb7c6, 9a506576, 8d8b6819
1. `imswitch/imcontrol/view/widgets/<Key>Widget.py` + entry in `_WIDGET_MODULES`.
2. `imswitch/imcontrol/controller/controllers/<Key>Controller.py` + entry in `_CONTROLLER_MODULES`.
3. `imswitch/imcontrol/view/ImConMainView.py`: `_DOCK_DISPLAY_NAMES` and
   `_DEFAULT_RIGHT_DOCK_INFOS` or `_DEFAULT_LEFT_DOCK_INFOS` (a key in neither
   default table nor `widgetLayout` is never built) [`test_example_setups_schema.py`].
4. The config editor offers it via `_DOCK_DISPLAY_NAMES` (parsed by regex); its
   group comes from `_WIDGET_GROUP_MAP` in
   `imswitch/imcontrol/view/configeditor/editor.py`, else "Other".
5. Exported methods: see "Script API action".

**Scan frontend (cloak)** - traced on 380b8ddd: entry in `SCAN_CLOAKS`;
`ScanController<Cloak>` subclassing `ScanCloakController` and the backend
controller; `ScanWidget<Cloak>` subclassing `ScanCloakPanel`; both lazy maps; a
shipped mock setup; rows in `MOCK_SETUPS`, `EDITS`, `PREPARE_RUN` of
`test_scan_cloak_contract.py`; history regen (shipped file). A new *backend*
`scanWidgetType` must follow `docs/scan-lifecycle.rst` "Contract for new scan controllers".

**Processor / reconstructor** - see `imswitch/improcess/AGENTS.md` (traced on
f1ae1ec1, 8f54d646, d091f6bc).

**Script API action** - traced on e1dcb887
1. `@APIExport` method with a docstring on a controller (`runOnUIThread=True` if it touches Qt).
2. `PYTHONPATH=$PWD python tools/generateapidocs.py`; commit `docs/api/`
   [`imswitch/imscripting/_test/test_scripting_api_docs.py`].
3. `docs/scripting.rst`; optional tutorial (next item).

**Shipped setup, script or tutorial** (`imswitch/_data/user_defaults/`)
1. Tutorials: header lines `Mock setup: <file>.json` and `It simulates:`; row in
   `imswitch/_data/user_defaults/scripts/tutorial/README.md`
   [`imswitch/imscripting/_test/test_shipped_tutorials.py` runs each one].
2. Setups must parse and name known widgets [`test_example_setups_schema.py`].
3. Any add/change/move/delete: `python tools/update_user_defaults_history.py`
   [`imswitch/imcommon/_test/test_user_defaults_sync.py`].

**New config key**
- `managerProperties` key: read it in the manager, then steps 2, 3, 5 of Device manager.
- Top-level/section field: `SetupInfo.py` dataclass (unknown top-level keys
  land in `_catchAll`, e.g. `processing`), `docs/setupinfo-reference.rst`, editor
  template under `imswitch/imcontrol/view/configeditor/builtin_templates/sections/`.

## Generated files

| File(s) | Regenerate | Guard |
|---|---|---|
| `imswitch/imcontrol/model/configeditor/schemas/` | `python tools/extract_manager_schemas.py --write` (`--check`) | `test_configeditor_schemas_match_source.py` |
| `imswitch/imcontrol/_test/unit/configeditor_extraction_snapshot.json` | `--report --json >` it | `test_configeditor_extraction_tree.py` |
| `docs/api/*.rst` | `PYTHONPATH=$PWD python tools/generateapidocs.py` (`--check`) | `test_scripting_api_docs.py` |
| `imswitch/_data/user_defaults_history.json` | `python tools/update_user_defaults_history.py` (needs full history: `git fetch --unshallow`) | `test_user_defaults_sync.py` |
| `examples/improcess_workflows/python_step_*.yaml` | `python tools/make_python_recipe_workflows.py` (`--check`) | `imswitch/improcess/_test/test_python_step_recipes.py` |
| `docs/images/auto/` | `python tools/screenshot_widgets.py` | none |

Hand-kept mirrors with guards: `IMPROCESS_RECONSTRUCTOR_FALLBACK` /
`IMPROCESS_PROCESSOR_FALLBACK` [`test_config_editor_plugin_fallback.py`],
`docs/devices/*.rst` cards [`test_devices_docs_drift.py`], `_UNREGISTERED_CORE_MANAGERS`.
Run every tool with a throwaway `HOME` (imports sync the user folder).

## Tests

```
HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -p pytestqt.plugin -p pytest_timeout -p no:cacheprovider --timeout=180 -q <paths>
```
CI adds `-p xdist.plugin -n auto`. Lanes (`.github/workflows/ci.yml`; a test in a
new directory runs in CI only once a lane lists it):

| Lane | Paths |
|---|---|
| unit | `imswitch/test_no_hardware_profile.py`, `imswitch/imcontrol/_test/unit`, `imswitch/imcontrol/controller/_test` |
| improcess | `imswitch/improcess/_test` (ignores `test_snouty.py`) |
| core | `imswitch/imcommon/_test`, `imswitch/imscripting/_test` |
| plugins | `examples/plugins/*/tests` |
| not in CI | `imswitch/imcontrol/_test/ui`, `imswitch/test_no_hardware_ui_smoke.py`, `-m glviewer` |

- `pip install -e ".[test]"` has no vendor packages (pyvisa, nidaqmx): import them lazily/guarded.
- Root `conftest.py` stubs napari/vispy/matplotlib when missing. Fixtures:
  `optionsBasic` and setup JSON constants in `imswitch/imcontrol/_test/__init__.py`;
  real `MasterController` on a shipped mock setup (`CloakRig` in `test_scan_cloak_contract.py`).
- Headless containers have no OpenGL: napari image widgets cannot be built; build controllers without `Image`.
- Unit lane is ~5000 tests, minutes serially; run the files you touched, then the lane.
- CI runs only for PRs against `main`; a stacked PR gets none. `ruff check imswitch/`
  (critical rules only, `pyproject.toml`). Docs: `python -m sphinx -b html -W docs <out>`
  (Read the Docs uses `fail_on_warning`).
- Known order-dependent/flaky in serial full runs: some
  `test_scan_cloak_contract.py[SimpleMoNaLISA]` cases, thread-timing cases in
  `imswitch/improcess/_test/test_processor_runner.py`, `test_dock_panel_sizing.py`;
  `test_scripting_api_docs.py` fails where its subprocess cannot import colorama.
  Re-run on the base commit before calling a failure pre-existing.
- Markers (`pyproject.toml`): `nohardware`, `hardware`, `ui`, `glviewer` (deselected
  by default), `redzone` (declared, used by no test; CI selects by path, not marker).

## Threading

Prose and rationale: `docs/architecture.rst` (Threading; Device status and absent devices).

- Never on the GUI thread: device I/O that can block, waveform design of large
  scans, disk drains, processor/reconstructor runs. Freezes shipped from each:
  88efbe8b (scan design), 21016c91 (recording stop), 5a0b3769 (processors).
- Pattern: `Worker` subclass + `Thread()`, `worker.moveToThread(thread)`, connect
  signals, `thread.start()`; results come back by signal (example
  `imswitch/imcontrol/controller/controllers/FFTController.py`; ImProcess runs use
  `ProcessorRunner` in `imswitch/improcess/controller/processor_runner.py`).
- Script threads reach Qt only through `@APIExport(runOnUIThread=True)` methods.
- An exception escaping a slot aborts PyQt when no excepthook is installed (as
  under pytest); the app installs `installExceptHook`. Catch in slots you test.
- A QObject manager's attributes cannot be read before `QObject.__init__` ran;
  status code reads `self.__dict__` for that reason (`imswitch/imcontrol/model/devices/status.py`).

## Mock and simulation

- No silent fallback. A real device that fails to open becomes *absent*
  (`DeviceNotConnectedError` on use, shown in Hardware status) unless the setup
  opts into a mock (`useMockOnFailure`); either way the failure is recorded with
  `_setConnectionError`. Mock is selected explicitly per device (per-manager
  property, e.g. `cameraListIndex: "mock"` for `AVManager`) or `nidaq.simulation: true`.
  A missing `nidaqmx` is a degraded real state, not simulation (`docs/mock-infrastructure.rst`).
- Run without hardware: `python -m imswitch`, pick a shipped mock setup
  (`example_mock.json`, `example_no_hardware.json`, `*mock_scan*.json`).
  Simulated scans: `imswitch/imcontrol/model/managers/mockscan/`.

## Conventions

- Commits: subject says what now happens, area first (`Scan: ...`) or
  conventional (`fix(slm): ...`, `docs: ...`); body says why and what was wrong;
  one logical change per commit (see `git log`).
- Changelog: a bullet under *Unreleased* in `docs/changelog.rst` for every
  user-visible change, in its section (New Features, Behaviour Changes, Bug
  Fixes, Developer / Internal).
- Docs: one home per fact; a new page needs a toctree entry in `docs/index.rst`
  or the RTD build fails.
- PRs use `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md` (risk, red zone, agent metadata).

## Pitfalls

- Importing `imswitch` writes `~/ImSwitchConfig`; always a throwaway `HOME`.
- `tools/generateapidocs.py` without `PYTHONPATH=$PWD` may import another checkout.
- Shallow clone + `tools/update_user_defaults_history.py` silently drops entries.
- A new processor/reconstructor id missing from the editor fallback list fails `test_config_editor_plugin_fallback.py`.
- A new managerProperties key fails three tests until schemas, snapshot and card are updated.
- `scanWidgetType` `PointScan`/`MoNaLISA` are retired (`retiredScanWidgetTypeError`); the scan widget class is `ScanWidget` + `resolve_scan_info(scan).frontend_type`.
