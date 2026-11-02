from types import SimpleNamespace
from unittest.mock import MagicMock

from imswitch.improcess.controller.ProcessingSessionController import (
    ProcessingSessionController,
)
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)


def test_session_open_in_viewer_routes_by_dataset_kind():
    assert (
        ProcessingSessionController._viewer_plugin_id(SimpleNamespace(kind="scan"))
        == "view-only"
    )
    assert (
        ProcessingSessionController._viewer_plugin_id(
            SimpleNamespace(kind="time-lapse")
        )
        == "time-lapse"
    )


def test_session_open_in_viewer_does_not_change_active_reconstructor():
    dataset = SimpleNamespace(
        id="dataset-1",
        label="lapse",
        kind="time-lapse",
        raw_deleted=False,
    )
    session = SimpleNamespace(dataset_by_id=lambda dataset_id: dataset)
    active = object()
    opener = MagicMock(return_value="started")
    data_obj = object()

    controller = ProcessingSessionController.__new__(ProcessingSessionController)
    controller._session = session
    controller._selectedDatasetId = dataset.id
    controller._main = SimpleNamespace(
        _activeReconstructor=active,
        reconstructorManager=SimpleNamespace(openInspection=opener),
    )
    controller._widget = SimpleNamespace(
        currentDatasetId=lambda: dataset.id,
        setStatus=MagicMock(),
    )
    controller._logger = MagicMock()
    controller._viewer_data_obj = MagicMock(return_value=data_obj)

    controller.openSelectedInViewer()

    opener.assert_called_once_with("time-lapse", data_obj)
    assert controller._main._activeReconstructor is active


def test_open_inspection_uses_plugin_defaults_without_installing_plugin(monkeypatch):
    active = object()
    result = SimpleNamespace(name="inspection")
    data_obj = object()
    plugin = SimpleNamespace(
        id="view-only",
        result_role="inspection",
        execution_policy="inline",
        default_params=lambda: {"viewer": "default"},
        validate_source=MagicMock(),
    )

    from imswitch.improcess.reconstructors import registry as registry_module
    from imswitch.improcess.reconstructors import run as run_module

    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: SimpleNamespace(
            get_reconstructor=lambda _plugin_id, raise_on_missing=True: plugin
        ),
    )
    run_reconstruction = MagicMock(return_value=SimpleNamespace(result=result))
    monkeypatch.setattr(run_module, "run_reconstruction", run_reconstruction)

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._main = SimpleNamespace(_activeReconstructor=active)
    controller._publishPluginResult = MagicMock()

    outcome = controller.openInspection("view-only", data_obj)

    assert outcome == "ready"
    plugin.validate_source.assert_called_once_with(data_obj)
    run_reconstruction.assert_called_once_with(
        plugin, data_obj, {"viewer": "default"}
    )
    controller._publishPluginResult.assert_called_once_with(
        result, "inspection", reconstructor=plugin
    )
    assert result._session_backing_data_obj is data_obj
    assert controller._main._activeReconstructor is active


def test_worker_inspection_receives_its_own_default_params(monkeypatch):
    active = object()
    data_obj = object()
    plugin = SimpleNamespace(
        id="time-lapse",
        result_role="inspection",
        execution_policy="worker",
        default_params=lambda: {"time_axis": "planned"},
        validate_source=MagicMock(),
    )

    from imswitch.improcess.reconstructors import registry as registry_module

    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: SimpleNamespace(
            get_reconstructor=lambda _plugin_id, raise_on_missing=True: plugin
        ),
    )

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._main = SimpleNamespace(_activeReconstructor=active)
    controller._start_worker_reconstruction = MagicMock(return_value=True)

    outcome = controller.openInspection("time-lapse", data_obj)

    assert outcome == "started"
    controller._start_worker_reconstruction.assert_called_once_with(
        plugin,
        [data_obj],
        False,
        params_override={"time_axis": "planned"},
    )
    assert controller._main._activeReconstructor is active


def test_open_inspection_autoloads_missing_viewer_without_activating(monkeypatch):
    active = object()
    data_obj = object()
    result = SimpleNamespace(name="inspection")
    plugin = SimpleNamespace(
        id="time-lapse",
        result_role="inspection",
        execution_policy="inline",
        default_params=lambda: {"time_axis": "planned"},
        validate_source=MagicMock(),
    )

    class _Registry:
        def __init__(self):
            self.plugin = None

        def get_reconstructor(self, plugin_id, raise_on_missing=True):
            if self.plugin is not None and self.plugin.id == plugin_id:
                return self.plugin
            if raise_on_missing:
                raise KeyError(plugin_id)
            return None

        def reconstructors(self):
            return [self.plugin] if self.plugin is not None else []

        def unregister_reconstructor(self, plugin_id):
            previous = self.plugin
            if previous is not None and previous.id == plugin_id:
                self.plugin = None
            return previous

    registry = _Registry()

    from imswitch.improcess import reconstructors as reconstructors_module
    from imswitch.improcess.reconstructors import registry as registry_module
    from imswitch.improcess.reconstructors import run as run_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

    def _register(target_registry, plugin_id):
        assert target_registry is registry
        assert plugin_id == "time-lapse"
        target_registry.plugin = plugin
        return plugin

    register = MagicMock(side_effect=_register)
    monkeypatch.setattr(
        reconstructors_module, "register_reconstructor_by_id", register
    )
    monkeypatch.setattr(
        reconstructors_module,
        "available_reconstructor_specs",
        lambda: [
            ("time-lapse", "Time Lapse", "Inspect a time lapse"),
            ("other", "Other", "Another reconstructor"),
        ],
    )
    monkeypatch.setattr(
        run_module,
        "run_reconstruction",
        MagicMock(return_value=SimpleNamespace(result=result)),
    )

    available = MagicMock()
    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._main = SimpleNamespace(_activeReconstructor=active)
    controller._widget = SimpleNamespace(setAvailableReconstructors=available)
    controller._logger = MagicMock()
    controller._publishReconstructorChoices = MagicMock()
    controller._publishPluginResult = MagicMock()

    outcome = controller.openInspection("time-lapse", data_obj)

    assert outcome == "ready"
    register.assert_called_once_with(registry, "time-lapse")
    controller._publishReconstructorChoices.assert_called_once_with()
    available.assert_called_once_with(
        [("other", "Other", "Another reconstructor")]
    )
    assert controller._main._activeReconstructor is active


def test_open_inspection_reports_viewer_autoload_failure(monkeypatch):
    active = object()
    registry = SimpleNamespace(
        get_reconstructor=lambda _plugin_id, raise_on_missing=True: None
    )

    from imswitch.improcess import reconstructors as reconstructors_module
    from imswitch.improcess.reconstructors import registry as registry_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    monkeypatch.setattr(
        reconstructors_module,
        "register_reconstructor_by_id",
        MagicMock(side_effect=ImportError("optional dependency missing")),
    )

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._main = SimpleNamespace(_activeReconstructor=active)

    import pytest

    with pytest.raises(RuntimeError, match="Could not load the Time Lapse viewer"):
        controller.openInspection("time-lapse", object())

    assert controller._main._activeReconstructor is active
