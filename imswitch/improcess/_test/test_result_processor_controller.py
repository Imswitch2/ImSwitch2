from types import SimpleNamespace

from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _Logger:
    def __init__(self):
        self.exceptions = []

    def exception(self, *args):
        self.exceptions.append(args)


class _Widget:
    def __init__(self, processor):
        self.processor = processor
        self.status = []
        self.current_results = []

    def setStatusText(self, text):
        self.status.append(text)

    def setCurrentResult(self, result):
        self.current_results.append(result)


class _Processor:
    id = "fake"

    def apply(self, input_result, params):
        return SimpleNamespace(name=f"{input_result.name}_processed")


class _FailingProcessor:
    id = "bad"

    def apply(self, input_result, params):
        raise RuntimeError("boom")


def _bound_controller(processor):
    comm = SimpleNamespace(
        sigResultProduced=_Signal(),
        sigCurrentResultChanged=_Signal(),
    )
    widget = _Widget(processor)
    controller = SimpleNamespace(
        _widget=widget,
        _commChannel=comm,
        _logger=_Logger(),
    )
    return ResultProcessorController.runProcessor.__get__(controller), controller


def test_result_processor_controller_emits_processor_output():
    run_processor, controller = _bound_controller(_Processor())
    source = SimpleNamespace(name="source")

    run_processor(source, {"x": 1})

    produced = controller._commChannel.sigResultProduced.emitted
    current = controller._commChannel.sigCurrentResultChanged.emitted
    assert produced[0][0].name == "source_processed"
    assert produced[0][1] == "source_processed"
    assert current[0][0].name == "source_processed"
    assert controller._widget.current_results[0].name == "source_processed"
    assert controller._widget.status == ["Created source_processed."]


def test_result_processor_controller_reports_processor_failure():
    run_processor, controller = _bound_controller(_FailingProcessor())

    run_processor(SimpleNamespace(name="source"), {})

    assert controller._commChannel.sigResultProduced.emitted == []
    assert controller._commChannel.sigCurrentResultChanged.emitted == []
    assert controller._widget.status == ["boom"]
    assert controller._logger.exceptions
