"""Tests for the ImProcess image toolbar first slice."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.controller.ImageToolbarController import ImageToolbarController
from imswitch.improcess.view.ContrastBrightnessDialog import ContrastBrightnessDialog


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _View:
    def __init__(self):
        self.sigImageAutoContrastRequested = _Signal()
        self.sigImageResetContrastRequested = _Signal()
        self.sigImageContrastDialogRequested = _Signal()
        self.sigImageResetViewRequested = _Signal()
        self.enabled_states = []
        self.reconstructionWidget = SimpleNamespace(resetView=lambda: None)

    def setImageActionsEnabled(self, enabled):
        self.enabled_states.append(bool(enabled))


class _Result:
    def __init__(self, data):
        self.data = data
        self.display_levels = None

    def setDispLevels(self, levels):
        self.display_levels = levels


class _ReconstructionController:
    def __init__(self, data):
        self.result = _Result(data) if data is not None else None
        self.levels = None
        self.level_range = None

    def getActiveResult(self):
        return self.result

    def getActiveImage(self):
        return self.result.data

    def getActiveImageCurrentView(self):
        return self.result.data[-1] if self.result.data.ndim > 2 else self.result.data

    def getActiveImageDisplayLevels(self):
        return self.levels

    def setActiveImageDisplayLevels(self, minimum, maximum):
        self.levels = (minimum, maximum)
        self.result.setDispLevels(self.levels)

    def setActiveImageDisplayLevelsRange(self, minimum, maximum):
        self.level_range = (minimum, maximum)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _controller(data):
    comm = SimpleNamespace(sigCurrentResultChanged=_Signal())
    view = _View()
    recon = _ReconstructionController(data)
    controller = ImageToolbarController(comm, view, recon)
    return controller, view, recon


def test_toolbar_actions_enable_only_for_image_results():
    _controller(np.zeros((3, 4), dtype=np.float32))
    _, view, _ = _controller(None)

    assert view.enabled_states[-1] is False


def test_auto_contrast_sets_display_levels_and_result_metadata():
    controller, _view, recon = _controller(np.arange(100, dtype=np.float32).reshape(10, 10))

    controller.autoContrast(saturated_percent=0.0)

    assert recon.levels == (0.0, 99.0)
    assert recon.result.display_levels == (0.0, 99.0)


def test_reset_contrast_sets_range_and_levels():
    controller, _view, recon = _controller(np.array([[np.nan, -2.0], [3.0, np.inf]]))

    controller.resetContrast()

    assert recon.level_range == (-2.0, 3.0)
    assert recon.levels == (-2.0, 3.0)


def test_contrast_dialog_emits_level_changes(qapp):
    dialog = ContrastBrightnessDialog()
    emitted = []
    dialog.sigLevelsChanged.connect(lambda minimum, maximum: emitted.append((minimum, maximum)))

    dialog.setDataRange(0.0, 10.0)
    dialog.setLevels(2.0, 8.0)
    dialog.minSpin.setValue(3.0)

    assert emitted[-1] == (3.0, 8.0)
