"""Bulk-publish safety valve: many results/layers need confirmation."""

import os
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.view.bulk_confirm import (
    BULK_PUBLISH_THRESHOLD,
    bulk_publish_description,
)


def _image(name="img"):
    return ArrayProcessingResult(
        name=name,
        data=np.zeros((4, 4), np.float32),
        axis_labels=["Y", "X"],
    )


class _ManyLayerResult(ArrayProcessingResult):
    def __init__(self, layer_count, **kwargs):
        super().__init__(**kwargs)
        self._layer_count = layer_count

    def display_layers(self):
        return [object()] * self._layer_count


# -- pure description logic ---------------------------------------------------

def test_no_warning_at_or_under_threshold():
    results = [_image(f"r{i}") for i in range(BULK_PUBLISH_THRESHOLD)]
    assert bulk_publish_description(results) is None
    assert bulk_publish_description([]) is None


def test_warns_for_many_results():
    results = [_image(f"r{i}") for i in range(BULK_PUBLISH_THRESHOLD + 1)]
    message = bulk_publish_description(results)
    assert message is not None
    assert str(BULK_PUBLISH_THRESHOLD + 1) in message


def test_warns_for_one_result_with_many_display_layers():
    composite = _ManyLayerResult(
        50, name="big composite", data=np.zeros((4, 4), np.float32), axis_labels=["Y", "X"]
    )
    message = bulk_publish_description([composite])
    assert message is not None
    assert "50" in message and "big composite" in message


def test_few_layers_pass_without_warning():
    composite = _ManyLayerResult(
        3, name="small", data=np.zeros((4, 4), np.float32), axis_labels=["Y", "X"]
    )
    assert bulk_publish_description([composite]) is None


def test_broken_display_layers_never_blocks():
    class _Broken(ArrayProcessingResult):
        def display_layers(self):
            raise RuntimeError("boom")

    result = _Broken(name="b", data=np.zeros((4, 4), np.float32), axis_labels=["Y", "X"])
    assert bulk_publish_description([result]) is None


def test_threshold_parameter_is_respected():
    results = [_image(f"r{i}") for i in range(5)]
    assert bulk_publish_description(results, threshold=4) is not None
    assert bulk_publish_description(results, threshold=5) is None


# -- publish paths honor the guard -----------------------------------------------

def test_toolbar_publish_blocks_when_declined(monkeypatch):
    import imswitch.improcess.controller.ImageToolbarController as module

    controller = module.ImageToolbarController.__new__(module.ImageToolbarController)
    emitted = []
    controller._view = object()
    controller._commChannel = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda *a: emitted.append(("produced", a))),
        sigCurrentResultChanged=SimpleNamespace(emit=lambda *a: emitted.append(("current", a))),
    )

    monkeypatch.setattr(module, "confirm_bulk_publish", lambda parent, results: False)
    controller._publishResults([_image(), _image()])
    assert emitted == []

    monkeypatch.setattr(module, "confirm_bulk_publish", lambda parent, results: True)
    controller._publishResults([_image()])
    assert [kind for kind, _args in emitted] == ["produced", "current"]


def test_generic_panel_blocks_when_declined(monkeypatch):
    import imswitch.improcess.controller.ResultProcessorController as module

    controller = module.ResultProcessorController.__new__(module.ResultProcessorController)
    emitted = []
    statuses = []
    result = _image("out")
    controller._widget = SimpleNamespace(
        processor=SimpleNamespace(
            id="fake", apply=lambda _result, _params: result
        ),
        setStatusText=statuses.append,
        setCurrentResult=lambda _result: None,
    )
    controller._commChannel = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda *a: emitted.append(("produced", a))),
        sigCurrentResultChanged=SimpleNamespace(emit=lambda *a: emitted.append(("current", a))),
    )
    controller._logger = SimpleNamespace(exception=lambda *a, **k: None)

    monkeypatch.setattr(module, "confirm_bulk_publish", lambda parent, results: False)
    controller.runProcessor(_image("in"), {})
    assert emitted == []
    assert any("Cancelled" in status for status in statuses)

    monkeypatch.setattr(module, "confirm_bulk_publish", lambda parent, results: True)
    controller.runProcessor(_image("in"), {})
    assert [kind for kind, _args in emitted] == ["produced", "current"]
