"""Test that projection tool produces results via sigRunRequested."""

from types import SimpleNamespace

from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController


class _FakeWidget:
    """Minimal fake widget with sigRunRequested signal."""
    sigRunRequested = object()
    
    def __init__(self):
        self.current_results = []
        self.has_signal = True
    
    def setCurrentResult(self, result):
        self.current_results.append(result)


class _FakeView:
    def __init__(self, widget):
        self.projection_widget = widget
    
    def getRuntimeAnalysisWidget(self, tool_id):
        if tool_id == "projection":
            return self.projection_widget
        return None


class _FakeResultProcessorController:
    def __init__(self, widget):
        self.widget = widget
        self.run_calls = []


class _FakeFactory:
    def __init__(self):
        self.created_controllers = []
    
    def createController(self, controller_cls, widget):
        from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController
        if controller_cls is ResultProcessorController:
            ctrl = _FakeResultProcessorController(widget)
            self.created_controllers.append(ctrl)
            return ctrl
        return object()


def test_projection_tool_has_sigRunRequested():
    """Projection tool should be wired as a result-processor with sigRunRequested."""
    from imswitch.improcess.model.runtime_tools import runtime_result_processor_ids
    
    ids = runtime_result_processor_ids()
    
    assert "projection" in ids


def test_projection_widget_is_wired_on_load():
    """Loading projection tool should wire a ResultProcessorController to it."""
    widget = _FakeWidget()
    view = _FakeView(widget)
    factory = _FakeFactory()
    current_result = SimpleNamespace(name="test_result", data=[[1, 2], [3, 4]])
    
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = view
    controller._ImProcessMainController__factory = factory
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
    controller._resultProcessorControllers = {}
    controller.mainViewController = SimpleNamespace(
        reconstructionController=SimpleNamespace(
            getActiveResult=lambda: current_result
        )
    )
    
    controller._wire_runtime_result_processor("projection")
    
    assert len(factory.created_controllers) == 1
    assert factory.created_controllers[0].widget is widget
    assert widget.current_results == [current_result]


def test_projection_spec_is_result_processor_kind():
    """Projection spec should have result-processor widget_kind."""
    from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
    
    specs = runtime_analysis_tool_specs()
    projection_spec = specs.get("projection")
    
    assert projection_spec is not None
    assert projection_spec.widget_kind == "result-processor"
    assert projection_spec.processor_id == "projection"
