"""Tests for live reconstruction parameter lookup."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from imswitch.improcess.controller.LiveModeController import LiveModeController
from imswitch.improcess.controller.MemoryLiveController import MemoryLiveController
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)


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


def test_monalisa_reconstruct_uses_legacy_path_by_default():
    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {
        "reconstruction_method": "MoNaLISA",
    }
    controller._main = SimpleNamespace(
        _activeReconstructor=SimpleNamespace(id="monalisa"),
        monalisaController=MagicMock(),
    )
    controller._reconstruct_with_plugin = MagicMock()
    data_objs = [object()]

    controller.reconstruct(data_objs, consolidate=False)

    controller._main.monalisaController.runLegacyReconstruct.assert_called_once_with(
        data_objs, False
    )
    controller._reconstruct_with_plugin.assert_not_called()


def test_monalisa_fast_gauss_selector_uses_plugin_path():
    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
    }
    controller._main = SimpleNamespace(
        _activeReconstructor=SimpleNamespace(id="monalisa"),
        monalisaController=MagicMock(),
    )
    controller._reconstruct_with_plugin = MagicMock()
    data_objs = [object()]

    controller.reconstruct(data_objs, consolidate=False)

    controller._reconstruct_with_plugin.assert_called_once_with(data_objs, False)
    controller._main.monalisaController.runLegacyReconstruct.assert_not_called()


def test_monalisa_ism_selector_uses_plugin_path():
    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {
        "reconstruction_method": "ISM reassignment",
    }
    controller._main = SimpleNamespace(
        _activeReconstructor=SimpleNamespace(id="monalisa"),
        monalisaController=MagicMock(),
    )
    controller._reconstruct_with_plugin = MagicMock()
    data_objs = [object()]

    controller.reconstruct(data_objs, consolidate=False)

    controller._reconstruct_with_plugin.assert_called_once_with(data_objs, False)
    controller._main.monalisaController.runLegacyReconstruct.assert_not_called()


def test_monalisa_plugin_path_injects_scan_params():
    result = SimpleNamespace(name="fast-result", output_pixel_size_nm=None)
    reconstructor = SimpleNamespace(
        id="monalisa",
        name="MoNaLISA",
        process=MagicMock(return_value=result),
    )
    data_obj = SimpleNamespace(name="input")
    scan_params = {"steps": ["10", "10", "1", "1"]}

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._logger = MagicMock()
    controller._widget = MagicMock()
    controller._widget.getReconstructionParams.return_value = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
    }
    controller._widget.parTree = object()
    controller._commChannel = MagicMock()
    controller._main = SimpleNamespace(
        _activeReconstructor=reconstructor,
        monalisaController=SimpleNamespace(_scanParDict=scan_params),
        wfsBatchController=MagicMock(),
    )

    controller._reconstruct_with_plugin([data_obj], consolidate=False)

    reconstructor.process.assert_called_once()
    assert reconstructor.process.call_args.args[0] is data_obj
    params = reconstructor.process.call_args.args[1]
    assert params["reconstruction_method"] == "Fast Gauss MoNaLISA"
    assert params["scan_params"] == scan_params
    assert params["scan_params"] is not scan_params
