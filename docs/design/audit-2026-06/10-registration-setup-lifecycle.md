# Registration, Setup Validation, and Lifecycle Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Scope:** imcontrol widget/controller registration, bundled setup files,
device-plugin validation, server startup, and manager/controller lifecycle.

## Summary

The manager plugin system is in better shape than the GUI registration layer:
device managers can be resolved through a registry, but widgets and controllers
still depend on static export maps and exact naming conventions. Setup parsing
tests now catch dataclass schema drift, but they do not catch stale widget keys,
invalid layout references, missing controller/widget exports, or server
lifecycle problems.

Concrete scan results:

- Default dock layout has 42 widget keys.
- Two default layout keys, `Et` and `WellPlate`, do not have matching widget or
  controller exports in the lazy registration maps.
- One bundled setup, `example_sted.json`, still enables stale widget key
  `Rotation`.
- `validate-setup` checks device manager resolution and managerProperties
  schemas, but not GUI widget/controller availability or layout validity.

## Findings

### [P1] The optional server path appears blocked before Pyro startup

**Sites:**

- `imswitch/imcontrol/controller/server/ImSwitchServer.py:27-45`
- `imswitch/imcontrol/controller/server/ImSwitchServer.py:47-49`
- `imswitch/imcontrol/controller/ImConMainController.py:198-205`
- `imswitch/imcontrol/controller/ImConMainController.py:381-383`

**Evidence:**

`ImSwitchServer.run()` calls `uvicorn.run(app)` before configuring and serving
Pyro. Under normal uvicorn behavior that call blocks, so the Pyro setup below it
is not reached until uvicorn exits. The uvicorn call also ignores
`pyroServerInfo.host` and `pyroServerInfo.port`. `stop()` calls
`self._daemon.shutdown()`, but `_daemon` is not assigned in this class. The main
controller starts a `Thread` for the server, but `closeEvent()` only closes
controllers and the master controller; it does not explicitly stop or wait the
server thread.

**Impact:**

Any setup that enables `pyroServerInfo.active` risks a hanging or partially
started server path. Shutdown can also leave a server thread running or raise
late in `stop()`.

**Next fix:**

Split FastAPI and Pyro lifecycle explicitly:

- Use a configured uvicorn `Server` object or move FastAPI serving to its own
  controlled thread.
- Store the Pyro daemon/server handle that `stop()` must shut down.
- Add `ImSwitchServer.stop()` idempotency.
- In `ImConMainController.closeEvent()`, stop and wait the server thread before
  closing hardware managers.
- Add a no-hardware lifecycle test that starts a fake server object and asserts
  `run -> stop -> wait` sequencing.

### [P1] Widget/controller registration is static and already out of sync

**Sites:**

- `imswitch/imcontrol/view/widgets/__init__.py:10-58`
- `imswitch/imcontrol/controller/controllers/__init__.py:11-59`
- `imswitch/imcontrol/view/ImConMainView.py:215-219`
- `imswitch/imcontrol/view/ImConMainView.py:317-318`
- `imswitch/imcontrol/view/guitools/ViewSetupInfo.py:117-118`
- `imswitch/imcontrol/controller/controllers/WellPlateController.py:7-12`

**Evidence:**

Widgets and controllers are imported through static lazy-export maps. The main
view then constructs components by `getattr(widgets, f'{widgetKey}Widget')`,
with a special case only for `Scan`. The default dock layout includes
`WellPlate` and `Et`, but neither key has a matching export in the widget or
controller map. `WellPlateWidget.py` and `WellPlateController.py` exist, so this
is a registration drift, not a missing implementation. `ViewSetupInfo` documents
that `availableWidgets` may be `true` to enable all widgets, which would include
the default keys and hit these stale registrations.

**Impact:**

New UI features still require source edits in multiple places. A setup cannot
provide an external widget/controller the way it can provide an external device
manager. More immediately, `availableWidgets: true` or enabling `WellPlate`
will fail during GUI construction despite the files existing.

**Next fix:**

Add a UI component registry that owns:

- widget key
- widget class
- controller class
- default dock title/placement
- setup prerequisites

Then make the lazy maps and default dock layout generated from the registry, or
replace them entirely. Add a unit test asserting every default layout key has a
registered widget/controller and every bundled setup `availableWidgets` key is
registered.

### [P1] Bundled setup validation misses stale widget keys

**Sites:**

- `imswitch/_data/user_defaults/imcontrol_setups/example_sted.json:233-244`
- `imswitch/imcontrol/_test/unit/test_example_setups_schema.py:14-21`
- `imswitch/imcontrol/model/plugins/validation.py:215-264`

**Evidence:**

`example_sted.json` includes `"Rotation"` in `availableWidgets`, but the current
registered key is `Rotator` and there is no `RotationWidget` or
`RotationController`. The schema regression test only parses each setup as
`ViewSetupInfo`; it does not instantiate the view or check registry validity.
The plugin validation utility iterates only device sections and manager names.

**Impact:**

The previous schema fixes prevent dataclass `KeyError`s, but a setup can still
parse successfully and fail at GUI construction. This is exactly the kind of
configuration drift that makes bundled examples untrustworthy.

**Next fix:**

Extend setup validation with:

- `availableWidgets` and `widgetLayout` keys must resolve through the UI
  component registry.
- `Scan` variants must resolve to registered scan widget/controller pairs.
- `availableWidgets: true` must be tested against the full default layout.
- Bundled setup tests should run these structural checks without launching Qt.

### [P1] MasterController shutdown skips bespoke managers with threads/finalizers

**Sites:**

- `imswitch/imcontrol/controller/MasterController.py:34-37`
- `imswitch/imcontrol/controller/MasterController.py:47-49`
- `imswitch/imcontrol/controller/MasterController.py:117-121`
- `imswitch/imcontrol/model/managers/TriggerScopeManager.py:75-82`
- `imswitch/imcontrol/model/managers/TriggerScopeManager.py:90-92`
- `imswitch/imcontrol/model/managers/TriggerScopeManager.py:139-142`
- `imswitch/imcontrol/model/managers/pulsegen/TeensyPulseManager.py:305-312`

**Evidence:**

`MasterController.closeEvent()` stops recording, then finalizes only attributes
that are `MultiManager` instances. `pulseGeneratorManager` and
`triggerScopeManager` are bespoke managers, not `MultiManager` instances.
`TeensyPulseManager` has a real `finalize()` method. `TriggerScopeManager`
starts a serial-monitor thread and currently relies on `__del__` for `quit()` /
`wait()`, while `closeMonitor()` only calls `quit()` and does not wait.

**Impact:**

Hardware/service managers outside `MultiManager` can leak threads or leave
connections open on shutdown. Relying on `__del__` is especially fragile in Qt
object graphs because object destruction order is not a lifecycle contract.

**Next fix:**

Give all managers, including bespoke managers, a common lifecycle interface
(`finalize()` or `close()`) and make `MasterController` call it explicitly for
every manager attribute it owns. `TriggerScopeManager` should implement
idempotent `finalize()` that stops and waits its monitor thread.

### [P2] UI component extensibility lags behind device-plugin extensibility

**Sites:**

- `imswitch/imcontrol/model/managers/MultiManager.py:15-24`
- `imswitch/imcontrol/model/managers/MultiManager.py:42-65`
- `imswitch/imcontrol/view/widgets/__init__.py:10-58`
- `imswitch/imcontrol/controller/controllers/__init__.py:11-59`

**Evidence:**

`MultiManager` maps device groups to plugin kinds and resolves manager classes
through the device plugin registry before falling back to legacy imports. The
UI layer has no equivalent registry. It uses static maps and naming conventions.

**Impact:**

External device support can be added without editing core manager code, but
external workflow/UI support cannot. This matters for future ET variants,
tiling workflows, and setup-specific smart microscopy panels.

**Next fix:**

Treat UI/workflow contributions as a separate registry from device managers.
Do not let arbitrary plugins inject GUI code implicitly into normal runs, but
allow setup files to reference explicitly installed and registered component
keys with clear diagnostics when missing.

## Suggested sequencing

1. Fix the server lifecycle path or disable `pyroServerInfo.active` with a clear
   diagnostic until the path is tested.
2. Add structural setup validation for `availableWidgets`, `widgetLayout`, and
   scan widget variants.
3. Register `WellPlate` or remove it from the default layout; remove or migrate
   the stale `Et` default key.
4. Fix `example_sted.json` to use `Rotator` or remove the stale `Rotation`
   widget entry.
5. Add a common manager lifecycle/finalization pass and stop relying on
   `__del__` for threaded managers.
6. Design a UI/workflow component registry after the device-manager registry
   pattern, with explicit setup references and diagnostics.
