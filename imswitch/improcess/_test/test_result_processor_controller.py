from types import SimpleNamespace

import numpy as np

from imswitch.improcess.controller.ResultProcessorController import ResultProcessorController
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.base import ProcessorOutput


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
        return ArrayProcessingResult(
            name=f"{input_result.name}_processed",
            data=np.array([[1]], dtype=np.float32),
            axis_labels=["Y", "X"],
        )


class _FailingProcessor:
    id = "bad"

    def apply(self, input_result, params):
        raise RuntimeError("boom")


class _MultiInputProcessor:
    """Declares the multi-input arity, so its inputs arrive in params."""

    id = "multi"
    min_inputs = 2
    max_inputs = None

    def apply(self, input_result, params):
        names = "+".join(r.name for r in params["results"])
        return ArrayProcessingResult(
            name=names,
            data=np.array([[1]], dtype=np.float32),
            axis_labels=["Y", "X"],
        )


class _PickyProcessor:
    """Fails on one named input and succeeds on the rest."""

    id = "picky"

    def apply(self, input_result, params):
        if input_result.name == "bad":
            raise RuntimeError("no good")
        return ArrayProcessingResult(
            name=f"{input_result.name}_processed",
            data=np.array([[1]], dtype=np.float32),
            axis_labels=["Y", "X"],
        )


class _MultiOutputProcessor:
    id = "many"

    def apply(self, input_result, params):
        return ProcessorOutput(
            [
                ArrayProcessingResult(
                    name=f"{input_result.name}_a",
                    data=np.array([[1]], dtype=np.float32),
                    axis_labels=["Y", "X"],
                ),
                ArrayProcessingResult(
                    name=f"{input_result.name}_b",
                    data=np.array([[2]], dtype=np.float32),
                    axis_labels=["Y", "X"],
                ),
            ]
        )


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


def test_result_processor_controller_publishes_multi_output():
    run_processor, controller = _bound_controller(_MultiOutputProcessor())

    run_processor(SimpleNamespace(name="source"), {})

    produced = controller._commChannel.sigResultProduced.emitted
    current = controller._commChannel.sigCurrentResultChanged.emitted
    assert [args[1] for args in produced] == ["source_a", "source_b"]
    assert current[0][0].name == "source_b"
    assert controller._widget.current_results[0].name == "source_b"
    assert controller._widget.status == ["Created 2 results."]


def test_batch_runs_once_per_input():
    """Applying one processor to several reconstructions is a loop, not a
    special processor — this is what the panel's "Apply to" scope produces."""
    run_processor, controller = _bound_controller(_Processor())
    inputs = [SimpleNamespace(name="one"), SimpleNamespace(name="two")]

    run_processor(inputs, {})

    produced = controller._commChannel.sigResultProduced.emitted
    assert [args[1] for args in produced] == ["one_processed", "two_processed"]
    assert controller._widget.status == ["Created 2 results."]


def test_one_bad_input_does_not_throw_away_the_rest_of_a_batch():
    run_processor, controller = _bound_controller(_PickyProcessor())
    inputs = [
        SimpleNamespace(name="good"),
        SimpleNamespace(name="bad"),
        SimpleNamespace(name="also-good"),
    ]

    run_processor(inputs, {})

    produced = controller._commChannel.sigResultProduced.emitted
    assert [args[1] for args in produced] == ["good_processed", "also-good_processed"]
    assert "1 failed" in controller._widget.status[0]
    assert "no good" in controller._widget.status[0]


def test_multi_input_processor_receives_every_input_in_params():
    run_processor, controller = _bound_controller(_MultiInputProcessor())
    inputs = [SimpleNamespace(name="a"), SimpleNamespace(name="b")]

    run_processor(inputs, {})

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    assert produced[0][1] == "a+b"


def test_a_bare_result_is_still_accepted():
    """Custom panels (Segmentation, PSF, Colocalization) emit one result, not
    a list; the arity work must not break their contract."""
    run_processor, controller = _bound_controller(_Processor())

    run_processor(SimpleNamespace(name="source"), {})

    assert controller._commChannel.sigResultProduced.emitted[0][1] == "source_processed"
