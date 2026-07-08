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
        reconstructionController=reconstruction
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
