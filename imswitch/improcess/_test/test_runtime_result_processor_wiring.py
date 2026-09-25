from types import SimpleNamespace

from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
from imswitch.improcess.controller.runtime_result_processors import (
    runtime_result_processor_ids,
)


def test_runtime_result_processor_ids_track_generic_processor_widgets():
    ids = runtime_result_processor_ids()

    assert "drift-correct" in ids
    assert "denoise" in ids
    assert "projection" in ids
    assert "roi-manager" not in ids


class _Widget:
    sigRunRequested = object()

    def __init__(self):
        self.current_results = []

    def setCurrentResult(self, result):
        self.current_results.append(result)


class _Factory:
    def __init__(self):
        self.created = []

    def createController(self, controller_cls, widget):
        self.created.append((controller_cls, widget))
        return object()


def _main_controller_with_current_result(result, widget):
    controller = ImProcessMainController.__new__(ImProcessMainController)
    factory = _Factory()
    view = SimpleNamespace(getRuntimeAnalysisWidget=lambda _processor_id: widget)
    reconstruction = SimpleNamespace(getActiveResult=lambda: result)
    controller.mainViewController = SimpleNamespace(
        reconstructionController=reconstruction,
        graphController=None,
    )
    controller._resultProcessorControllers = {}
    controller._ImProcessMainController__factory = factory
    controller._ImProcessMainController__mainView = view
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None
    )
    return controller, factory


def test_runtime_result_processor_widget_is_seeded_with_active_result():
    current_result = object()
    widget = _Widget()
    controller, factory = _main_controller_with_current_result(current_result, widget)

    controller._wire_runtime_result_processor("smlm-render")

    assert factory.created
    assert widget.current_results == [current_result]


def test_runtime_result_processor_widget_is_reseeded_when_already_wired():
    current_result = object()
    widget = _Widget()
    controller, factory = _main_controller_with_current_result(current_result, widget)
    controller._resultProcessorControllers["smlm-render"] = object()

    controller._wire_runtime_result_processor("smlm-render")

    assert factory.created == []
    assert widget.current_results == [current_result]


def test_runtime_graph_widget_gets_graph_controller():
    widget = SimpleNamespace()
    controller, factory = _main_controller_with_current_result(object(), widget)

    controller._wire_runtime_result_processor("graph")

    assert factory.created
    assert factory.created[0][0].__name__ == "GraphController"
    assert factory.created[0][1] is widget
    assert controller.mainViewController.graphController is not None


def test_startup_wires_producing_panels_without_saved_layout():
    """A fresh profile has no saved layout, so the persistence adapter hook
    never fires; __init__ must wire producing panels unconditionally or their
    run buttons stay dead until the first layout save/restore cycle."""
    import inspect

    source = inspect.getsource(ImProcessMainController.__init__)

    assert "self._wire_runtime_result_processors()" in source


_PLUGIN_SOURCE = """
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.array_result import ArrayProcessingResult

class InvertProcessor(Processor):
    name = "Invert"
    id = "user.invert"
    category = "User"
    kinds = ("image",)
    @property
    def applies_to(self):
        return lambda result: getattr(result.data, "ndim", 0) >= 2
    def make_param_widget(self, parent):
        return None
    def apply(self, result, params):
        return ArrayProcessingResult(
            name=result.name, data=result.data, axis_labels=list(result.axis_labels)
        )
"""


def test_reload_user_plugins_rediscovers_and_refreshes(tmp_path, monkeypatch):
    """The 'Reload plugins' handler re-scans the folder: a newly dropped plugin
    appears in the enumeration and registry, and the tool combo is refreshed."""
    import imswitch.improcess.plugins.user_plugins as up
    import imswitch.improcess.processors as processors
    import imswitch.improcess.reconstructors.registry as registry_module
    from imswitch.improcess.reconstructors.registry import PluginRegistry

    plugin_dir = tmp_path / "improcess_plugins"
    plugin_dir.mkdir()
    (plugin_dir / "invert.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(up.dirtools.UserFileDirs, "Root", str(tmp_path))

    registry = PluginRegistry()
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    processors.clear_user_plugins()

    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    refreshed = []
    controller._refresh_runtime_processor_choices = lambda: refreshed.append("processors")
    controller._refresh_reconstructor_choices = lambda: refreshed.append("reconstructors")

    try:
        controller._reload_user_plugins()

        assert "user.invert" in processors.available_processor_ids()
        assert registry.get_processor("user.invert", raise_on_missing=False) is not None
        # Both runtime menus are refreshed: the tool combo and Load reconstructor.
        assert refreshed == ["processors", "reconstructors"]
    finally:
        processors.clear_user_plugins()
