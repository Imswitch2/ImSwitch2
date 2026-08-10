"""The generic processor panel adapts to the processor's declared arity.

A single-input processor gets an "Apply to" scope so one operation can sweep
several reconstructions; a multi-input one gets the shared picker, so
Merge channels / Stack/Combine / Image calculator opened from *Load tool* can
actually see more than the current result — they used to raise on every run.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import _all_processor_classes
from imswitch.improcess.view.ResultProcessorWidget import (
    SCOPE_ALL,
    SCOPE_SELECTED,
    ResultProcessorWidget,
)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _image(name, shape=(4, 5), labels=("Y", "X")):
    return ArrayProcessingResult(
        name=name, data=np.zeros(shape, np.float32), axis_labels=list(labels)
    )


def _panel(processor_id):
    return ResultProcessorWidget(_all_processor_classes()[processor_id]())


def test_multi_input_panel_shows_the_picker(qapp):
    panel = _panel("channel-merge")

    assert panel.isMultiInput() is True
    assert panel.inputWidget is not None
    assert panel.inputCombo is None


def test_single_input_panel_shows_the_input_and_scope_combos(qapp):
    panel = _panel("denoise")

    assert panel.isMultiInput() is False
    assert panel.inputWidget is None
    assert panel.scopeCombo.count() == 3


def test_multi_input_panel_runs_on_the_checked_results(qapp):
    panel = _panel("channel-merge")
    a, b = _image("a"), _image("b")
    emitted = []
    panel.sigRunRequested.connect(lambda inputs, params: emitted.append(inputs))

    panel.setAvailableResults([("a", a), ("b", b)])
    assert panel.runButton.isEnabled()
    panel.runButton.click()

    assert emitted == [[a, b]]


def test_multi_input_panel_prechecks_the_selection(qapp):
    panel = _panel("channel-merge")
    a, b, c = _image("a"), _image("b"), _image("c")

    panel.setAvailableResults([("a", a), ("b", b), ("c", c)], [("a", a), ("c", c)])

    assert panel.selectedInputs() == [a, c]


def test_multi_input_panel_explains_an_impossible_run(qapp):
    panel = _panel("channel-merge")
    a = _image("a")

    panel.setAvailableResults([("a", a)])

    assert panel.runButton.isEnabled() is False
    assert "at least two" in panel.statusLabel.text()


def test_multi_input_panel_leaves_out_inputs_the_processor_rejects(qapp):
    """A localization/table result is not something merge channels can take,
    so it must not appear as a checkable channel."""
    panel = _panel("channel-merge")
    image = _image("image")
    table = _image("table")
    table.kind = "table"

    panel.setAvailableResults([("image", image), ("table", table)])

    assert panel.inputWidget.checked_results() == [image]


def test_scope_selected_runs_once_per_selected_result(qapp):
    panel = _panel("denoise")
    a, b, c = _image("a"), _image("b"), _image("c")
    emitted = []
    panel.sigRunRequested.connect(lambda inputs, params: emitted.append(inputs))

    panel.setCurrentResult(a)
    panel.setAvailableResults([("a", a), ("b", b), ("c", c)], [("a", a), ("c", c)])
    panel.scopeCombo.setCurrentIndex(panel.scopeCombo.findData(SCOPE_SELECTED))
    panel.runButton.click()

    assert emitted == [[a, c]]
    assert "2 results" in panel.statusLabel.text()


def test_scope_all_runs_over_every_loaded_result(qapp):
    panel = _panel("denoise")
    a, b = _image("a"), _image("b")

    panel.setCurrentResult(a)
    panel.setAvailableResults([("a", a), ("b", b)], [("a", a)])
    panel.scopeCombo.setCurrentIndex(panel.scopeCombo.findData(SCOPE_ALL))

    assert panel.selectedInputs() == [a, b]


def test_current_scope_still_runs_on_the_chosen_component(qapp):
    panel = _panel("denoise")
    a, b = _image("a"), _image("b")

    panel.setCurrentResult(a)
    panel.setAvailableResults([("a", a), ("b", b)], [("a", a), ("b", b)])

    assert panel.selectedInputs() == [a]
