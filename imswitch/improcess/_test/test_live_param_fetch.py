"""Tests for live reconstruction parameter lookup."""

from unittest.mock import MagicMock

from imswitch.improcess.controller.LiveModeController import LiveModeController
from imswitch.improcess.controller.MemoryLiveController import MemoryLiveController


class _Main:
    def __init__(self, widget=None):
        self._widget = widget


class _Widget:
    def __init__(self):
        self.parTree = MagicMock()
        self.parTree.get_values.return_value = {"legacy": "values"}
        self.parTree.get_param_dict.return_value = {"legacy": "dict"}

    def getReconstructionParams(self):
        return {"from": "view"}


class _LegacyWidget:
    def __init__(self):
        self.parTree = MagicMock()
        self.parTree.get_values.return_value = {"legacy": "values"}
        self.parTree.get_param_dict.return_value = {"legacy": "dict"}


def _controller(controller_class, widget):
    controller = controller_class.__new__(controller_class)
    controller._mainController = _Main(widget)
    controller._logger = MagicMock()
    return controller


def test_live_mode_uses_view_reconstruction_params():
    controller = _controller(LiveModeController, _Widget())

    assert controller._getReconstructorParams() == {"from": "view"}


def test_memory_live_uses_view_reconstruction_params():
    controller = _controller(MemoryLiveController, _Widget())

    assert controller._getReconstructorParams() == {"from": "view"}


def test_live_param_lookup_falls_back_to_param_widget_values():
    controller = _controller(LiveModeController, _LegacyWidget())

    assert controller._getReconstructorParams() == {"legacy": "values"}


def test_live_param_lookup_falls_back_to_empty_dict_without_widget():
    controller = _controller(LiveModeController, None)

    assert controller._getReconstructorParams() == {}
