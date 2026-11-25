# imswitch/imcontrol — agent notes

Root rules, red zone and "to add X" checklists: `AGENTS.md` (repository root).
Developer prose: `docs/architecture.rst`, `docs/scan-lifecycle.rst`,
`docs/state-persistence.rst`, `docs/adding-device-support.rst`.
Paths below are relative to this package.

## Map

| Path | What |
|---|---|
| `__init__.py` | `getMainViewAndController`: options, setup picker, `ViewSetupInfo`, view, controller |
| `model/SetupInfo.py` | setup-file dataclasses (`dataclasses_json`); unknown top-level keys -> `_catchAll` |
| `view/guitools/ViewSetupInfo.py` | view fields: `availableWidgets`, `widgetLayout` |
| `model/Options.py` | per-computer options (`~/ImSwitchConfig/config/imcontrol_options.json`): setup file, recordings folder, memory |
| `controller/MasterController.py` | builds every manager from the setup; low-level managers first; finalize order in `closeEvent` |
| `model/managers/MultiManager.py` | per-device instantiation by `managerName` |
| `model/managers/<family>/` | `detectors`, `lasers`, `positioners`, `rotators`, `rs232`, `slms`, `stands`, `flipMirrors`, `instruments`, `pulsegen`; base class per family beside the concrete ones |
| `model/plugins/` | `DevicePluginRegistry` (`registry.py`), `builtins.py`, entry-point discovery, `manifest.py` kinds, `validate-setup` CLI (`__main__.py`) |
| `model/interfaces/`, `model/lantzdrivers/` | vendor drivers and their `*_mock.py` |
| `model/devices/` | device status, graph, lifecycle (reconnect), acquisition gate |
| `model/resources.py` | `ResourceRegistry`: reservations and command admission |
| `model/measurement/` | instruments, run controls, run engine (shared types in `imswitch/imcommon/model/measurement_run/`) |
| `model/signaldesigners/` | scan waveforms (`ScanDesigner`) and TTL patterns (`TTLCycleDesigner`) |
| `model/managers/ScanManager*.py` | one scan family each (`Base`, `Advanced`, `TriggerScope`) |
| `model/managers/_scan_execution.py` | `ScanExecutionCoordinator`: run/iteration tokens, SCAN lease, finish barrier |
| `model/managers/_acquisition_leases.py` | per-detector lease refcounts, FAULTED quarantine |
| `model/managers/mockscan/` | software NI-DAQ scan simulation (`nidaq.simulation: true`) |
| `model/scan_cloak.py` | `SCAN_CLOAKS`, `resolve_scan_info`: backend vs simpler frontend |
| `model/managers/RecordingManager.py` | recording worker, writer thread, storers (Zarr/HDF5/TIFF) |
| `model/configeditor/` | schema extraction, catalogs, checked-in `schemas/` |
| `view/configeditor/editor.py` | the Config Studio (standalone-capable PyQt5 script) |
| `controller/CommunicationChannel.py` | intra-module signal bus + `scanWorkflow` service; `signals()` is the script contract |
| `controller/basecontrollers.py` | `ImConWidgetController`, `LiveUpdatedController`, `SuperScanController`, `StatefulComponentMixin`, `ImConWidgetControllerFactory` |
| `controller/controllers/`, `view/widgets/` | one `<Key>Controller` per `<Key>Widget`; lazy maps `_CONTROLLER_MODULES`, `_WIDGET_MODULES` |
| `controller/ImConMainController.py` | builds controllers, API-only controllers (`WorkflowFacadeController`, `ReservationController`, `InstrumentsController`), the API |
| `controller/server/ImSwitchServer.py` | REST + Pyro5 server when `pyroServerInfo.active` |
| `_test/unit/`, `controller/_test/` | unit lane; `_test/ui/` needs a display |

## How panels are built

`ImConMainView` creates a dock + widget for each key of `availableWidgets` that is
in `_DEFAULT_RIGHT_DOCK_INFOS` / `_DEFAULT_LEFT_DOCK_INFOS` (or in the setup's
`widgetLayout`); `Image` and `Log` are built explicitly. `ImConMainController`
then makes `<Key>Controller` for each built widget. Exception `Scan`: classes are
`ScanWidget` / `ScanController` + `resolve_scan_info(scan).frontend_type`, i.e.
the cloak if `scan.cloak` is set, else `scanWidgetType`. The scan *manager* follows
the backend `scanWidgetType`. The API exists only for the widgets a setup loads.

## Device status and lifecycle (`model/devices/`)

- `DeviceManagerStatusMixin` (`status.py`): passive, cached status; reading it
  never touches hardware. Managers record `_setConnected`, `_setMockActive`,
  `_setAbsent`, `_setConnectionError`.
- New managers open hardware with `_installBackend(open_real, make_mock, ...)` ->
  `BackendHolder`: configured mock; transient (absent until connected); real;
  on failure an *absent* backend (`DeviceNotConnectedError` on every use) or,
  only with `useMockOnFailure`, a mock with the error recorded. Declare the
  backend attribute with `backend_attribute` so a reconnect needs no bookkeeping.
- Panels grey out rows with `device_usable` / `not_connected_reason`.
- `DeviceSupervisor` (`supervisor.py`): read-only device graph and status.
  `DeviceLifecycleService` (`lifecycle_service.py`): connect/disconnect/reconnect,
  serialized per device, via `_lifecycleSafeState` / `_lifecycleReinitialise`.
- `acquisition_gate.py`: scans and recordings hold an acquisition ticket for
  their whole duration; a reconnect is refused while one is held, and vice versa.
- Design notes: `docs/design/plans/device-reconnect-2.0.md`.

## Scan and acquisition invariants

- Only the coordinator calls the NI-DAQ manager's `runScan`. New scan entry
  points arm through `getSharedScanExecutionCoordinator(...)`: `arm` for NI-DAQ,
  `armWithStarter` for autonomous firmware such as TriggerScope.
- Completion runs once per iteration; `scanDone` is published after the finish
  barrier (`finishScan` acknowledgements, 5 s deadline). Details and the
  contract for new scan controllers: `docs/scan-lifecycle.rst`.
- Detectors join acquisition only by holding a lease (`LeasePurpose`); a detector
  whose stop failed is quarantined (`DetectorFaultedError`).
- Mutating commands on reservable devices pass `ResourceRegistry.admit`;
  scripts reserve through `api.imcontrol.reserve(...)`.

## Widget state

Controllers that persist settings mix in `StatefulComponentMixin` and implement
`getComponentState`, `applyComponentState`, `describeComponentState`,
`getComponentStateHazards`. Contract: `docs/state-persistence.rst`.

## Tests here

- Real stacks are cheap: `MasterController(setupInfo, CommunicationChannel(main, setupInfo), moduleCommChannel)`
  on a shipped mock setup, controllers via `ImConWidgetControllerFactory`, widgets
  with `optionsBasic` (`_test/__init__.py`). Skip `ImageWidget` (no GL headless).
- Config-editor guards: `test_configeditor_schemas_match_source.py`,
  `test_configeditor_extraction_tree.py`, `test_config_editor_plugin_fallback.py`,
  `test_devices_docs_drift.py`, `test_setup_metadata.py`.
- Bundled setups: `test_example_setups_schema.py` (every widget key resolves to both lazy maps).
