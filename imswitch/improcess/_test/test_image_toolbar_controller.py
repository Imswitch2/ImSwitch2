"""Tests for the ImProcess image toolbar first slice."""

import os
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.controller.ImageToolbarController import ImageToolbarController
from imswitch.improcess.model.luts import IMAGE_LUTS
from imswitch.improcess.model.result import ViewMode
from imswitch.improcess.view.ChannelControlsDialog import ChannelControlsDialog
from imswitch.improcess.view.ContrastBrightnessDialog import ContrastBrightnessDialog
from imswitch.improcess.view.StackSubsetDialog import StackSubsetDialog


class _Signal:
    def __init__(self):
        self._slots = []
        self.emitted = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        self.emitted.append(args)
        for slot in list(self._slots):
            slot(*args)


class _View:
    def __init__(self):
        self.sigImageAutoContrastRequested = _Signal()
        self.sigImageResetContrastRequested = _Signal()
        self.sigImageContrastDialogRequested = _Signal()
        self.sigImageChannelControlsRequested = _Signal()
        self.sigImageLutChanged = _Signal()
        self.sigImageResetViewRequested = _Signal()
        self.sigImageDuplicateRequested = _Signal()
        self.sigImageCropSubstackRequested = _Signal()
        self.sigImageMaxProjectionRequested = _Signal()
        self.sigImageSplitStackRequested = _Signal()
        self.sigImageSplitChannelsRequested = _Signal()
        self.sigImageMergeChannelsRequested = _Signal()
        self.sigImageMakeCompositeRequested = _Signal()
        self.sigImageMakeRgbRequested = _Signal()
        self.enabled_states = []
        self.action_enabled = {}
        self.lut_enabled = []
        self.lut_values = []
        self.reconstructionWidget = SimpleNamespace(resetView=lambda: None)

    def setImageActionsEnabled(self, enabled):
        self.enabled_states.append(bool(enabled))

    def setImageActionEnabled(self, action_id, enabled):
        self.action_enabled[action_id] = bool(enabled)

    def setImageLutEnabled(self, enabled):
        self.lut_enabled.append(bool(enabled))

    def setImageLutValue(self, lut_id):
        self.lut_values.append(lut_id)


class _Result:
    def __init__(self, data):
        self.name = "source"
        self.data = data
        self.axis_labels = ["Z", "Y", "X"][-data.ndim:]
        self.axis_scales = [1.0] * data.ndim
        self.scale_unit = "px"
        self.view_modes = [ViewMode("Standard", tuple(range(data.ndim)))]
        self.display_levels = None
        self.display_colormap = "grayclip"

    def setDispLevels(self, levels):
        self.display_levels = levels

    def setDisplayColormap(self, colormap):
        self.display_colormap = str(colormap)

    def getDisplayColormap(self):
        return self.display_colormap


class _ReconstructionController:
    def __init__(self, data):
        self.result = _Result(data) if data is not None else None
        self.selected_results = [self.result] if self.result is not None else []
        self.layer_states = []
        self.layer_visibility = {}
        self.layer_luts = {}
        self.levels = None
        self.level_range = None
        self.colormap = "grayclip"

    def getActiveResult(self):
        return self.result

    def getSelectedResults(self):
        return [
            (getattr(result, "name", f"result_{index}"), result)
            for index, result in enumerate(self.selected_results)
        ]

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

    def getActiveImageColormap(self):
        return self.colormap

    def setActiveImageColormap(self, colormap):
        self.colormap = str(colormap)
        self.result.setDisplayColormap(colormap)

    def getDisplayLayerStates(self):
        return list(self.layer_states)

    def setDisplayLayerVisible(self, layer_id, visible):
        self.layer_visibility[layer_id] = bool(visible)

    def setDisplayLayerColormap(self, layer_id, colormap):
        self.layer_luts[layer_id] = str(colormap)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _controller(data):
    comm = SimpleNamespace(
        sigCurrentResultChanged=_Signal(),
        sigResultProduced=_Signal(),
    )
    view = _View()
    recon = _ReconstructionController(data)
    controller = ImageToolbarController(comm, view, recon)
    return controller, view, recon


def test_toolbar_actions_enable_only_for_image_results():
    _controller(np.zeros((3, 4, 5), dtype=np.float32))
    _, view, _ = _controller(None)

    assert view.enabled_states[-1] is False


def test_lut_choices_include_hot():
    assert ("hot", "Hot") in IMAGE_LUTS


def test_projection_action_disabled_for_2d_images():
    _controller_2d, view_2d, _recon_2d = _controller(np.zeros((3, 4), dtype=np.float32))
    _controller_3d, view_3d, _recon_3d = _controller(np.zeros((2, 3, 4), dtype=np.float32))

    assert view_2d.action_enabled["max-projection"] is False
    assert view_2d.action_enabled["crop-substack"] is True
    assert view_3d.action_enabled["max-projection"] is True
    assert view_3d.action_enabled["channels"] is False
    assert view_3d.action_enabled["crop-substack"] is True
    assert view_3d.action_enabled["split-stack"] is True
    assert view_3d.action_enabled["split-channels"] is False
    assert view_3d.action_enabled["merge-channels"] is False
    assert view_3d.action_enabled["make-composite"] is False
    assert view_3d.action_enabled["make-rgb"] is False


def test_auto_contrast_sets_display_levels_and_result_metadata():
    controller, _view, recon = _controller(np.arange(100, dtype=np.float32).reshape(10, 10))

    controller.autoContrast(saturated_percent=0.0)

    assert recon.levels == (0.0, 99.0)
    assert recon.result.display_levels == (0.0, 99.0)


def test_lut_change_updates_active_result_colormap():
    controller, view, recon = _controller(np.zeros((3, 4), dtype=np.float32))

    controller.setLut("green")

    assert recon.colormap == "green"
    assert recon.result.display_colormap == "green"
    assert view.lut_values[-1] == "grayclip"


def test_channel_controls_dialog_emits_visibility_and_lut(qapp):
    dialog = ChannelControlsDialog(lut_choices=IMAGE_LUTS)
    visible = []
    luts = []
    dialog.sigLayerVisibilityChanged.connect(
        lambda layer_id, state: visible.append((layer_id, state))
    )
    dialog.sigLayerLutChanged.connect(
        lambda layer_id, lut_id: luts.append((layer_id, lut_id))
    )
    dialog.setLayerStates(
        [
            {
                "id": "C_0",
                "name": "Channel 0",
                "visible": True,
                "colormap": "grayclip",
                "metadata": {"component": "C_0"},
            }
        ]
    )

    dialog.table.cellWidget(0, 0).setChecked(False)
    lut_combo = dialog.table.cellWidget(0, 2)
    lut_combo.setCurrentIndex(lut_combo.findData("hot"))
    lut_combo.activated.emit(lut_combo.currentIndex())

    assert visible == [("C_0", False)]
    assert luts == [("C_0", "hot")]


def test_channel_controls_enabled_and_updates_reconstruction_controller():
    controller, view, recon = _controller(np.zeros((3, 4), dtype=np.float32))
    recon.layer_states = [
        {
            "id": "C_0",
            "name": "Channel 0",
            "visible": True,
            "colormap": "grayclip",
            "metadata": {"component": "C_0"},
        }
    ]

    controller.currentResultChanged(recon.result)
    controller.setChannelLayerVisible("C_0", False)
    controller.setChannelLayerLut("C_0", "hot")

    assert view.action_enabled["channels"] is True
    assert recon.layer_visibility == {"C_0": False}
    assert recon.layer_luts == {"C_0": "hot"}


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


def test_duplicate_publishes_array_result():
    controller, _view, recon = _controller(np.arange(6, dtype=np.float32).reshape(2, 3))

    controller.duplicateResult()

    produced = controller._commChannel.sigResultProduced.emitted
    current = controller._commChannel.sigCurrentResultChanged.emitted
    assert len(produced) == 1
    duplicate = produced[0][0]
    assert duplicate.name == "source (duplicate)"
    assert duplicate.axis_labels == recon.result.axis_labels
    np.testing.assert_array_equal(duplicate.data, recon.result.data)
    recon.result.data[0, 0] = -1.0
    assert duplicate.data[0, 0] != recon.result.data[0, 0]
    assert current[0][0] is duplicate


def test_max_projection_publishes_projection_result():
    data = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    controller, _view, _recon = _controller(data)

    controller.maxProjection()

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    result = produced[0][0]
    assert result.name == "source (max Z-projection)"
    assert result.axis_labels == ["Y", "X"]
    np.testing.assert_array_equal(result.data, data.max(axis=0))


def test_crop_substack_dialog_converts_first_last_to_processor_ranges(qapp):
    result = _Result(np.zeros((3, 4, 5), dtype=np.float32))
    dialog = StackSubsetDialog(result)
    dialog._rows[0].firstSpin.setValue(2)
    dialog._rows[0].lastSpin.setValue(3)
    dialog._rows[2].stepSpin.setValue(2)

    assert dialog.selected_params() == {
        "ranges": [
            {"axis": 0, "start": 1, "stop": 3, "step": 1},
            {"axis": 2, "start": 0, "stop": 5, "step": 2},
        ],
        "copy": False,
    }


def test_crop_substack_publishes_subset_result(monkeypatch):
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    toolbar_module = importlib.import_module(
        "imswitch.improcess.controller.ImageToolbarController"
    )
    monkeypatch.setattr(
        toolbar_module.StackSubsetDialog,
        "get_params",
        staticmethod(
            lambda _result, parent=None, napari_viewer=None: {
                "ranges": [{"axis": "Z", "start": 1, "stop": 3}],
                "copy": False,
            }
        ),
    )
    controller, _view, _recon = _controller(data)

    controller.cropSubstack()

    produced = controller._commChannel.sigResultProduced.emitted
    current = controller._commChannel.sigCurrentResultChanged.emitted
    assert len(produced) == 1
    subset = produced[0][0]
    assert subset.name == "source (subset)"
    assert subset.axis_labels == ["Z", "Y", "X"]
    assert subset.data.shape == (2, 4, 5)
    np.testing.assert_array_equal(subset.data, data[1:3])
    assert current[0][0] is subset


def test_split_stack_publishes_each_plane():
    data = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    controller, _view, _recon = _controller(data)

    controller.splitStack()

    produced = controller._commChannel.sigResultProduced.emitted
    current = controller._commChannel.sigCurrentResultChanged.emitted
    assert [args[0].name for args in produced] == ["source (Z 0)", "source (Z 1)"]
    np.testing.assert_array_equal(produced[1][0].data, data[1])
    assert current[0][0] is produced[-1][0]


def test_split_channels_enabled_and_publishes_channels():
    data = np.arange(12, dtype=np.float32).reshape(3, 2, 2)
    controller, view, recon = _controller(data)
    recon.result.axis_labels = ["C", "Y", "X"]
    controller.currentResultChanged(recon.result)

    assert view.action_enabled["split-channels"] is True
    controller.splitChannels()

    produced = controller._commChannel.sigResultProduced.emitted
    assert [args[0].name for args in produced] == [
        "source (C 0)",
        "source (C 1)",
        "source (C 2)",
    ]
    np.testing.assert_array_equal(produced[2][0].data, data[2])


def test_make_composite_enabled_and_publishes_composite_result():
    data = np.arange(12, dtype=np.float32).reshape(3, 2, 2)
    controller, view, recon = _controller(data)
    recon.result.axis_labels = ["C", "Y", "X"]
    controller.currentResultChanged(recon.result)

    assert view.action_enabled["make-composite"] is True
    controller.makeComposite()

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    composite = produced[0][0]
    layers = composite.display_layers()
    assert composite.name == "source (composite)"
    assert [layer.colormap for layer in layers] == ["red", "green", "blue"]


def test_make_rgb_enabled_and_publishes_rgb_result():
    data = np.arange(3 * 2 * 2, dtype=np.float32).reshape(3, 2, 2)
    controller, view, recon = _controller(data)
    recon.result.axis_labels = ["C", "Y", "X"]
    controller.currentResultChanged(recon.result)

    assert view.action_enabled["make-rgb"] is True
    controller.makeRgb()

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    rgb = produced[0][0]
    assert rgb.name == "source (RGB)"
    assert rgb.data.shape == (2, 2, 3)
    assert rgb.display_layers()[0].rgb is True


def test_make_rgb_opens_channel_picker_for_more_than_three_channels(monkeypatch):
    data = np.zeros((5, 2, 2), dtype=np.float32)
    data[4] = np.array([[0, 10], [20, 30]], dtype=np.float32)
    controller, view, recon = _controller(data)
    recon.result.axis_labels = ["C", "Y", "X"]
    controller.currentResultChanged(recon.result)

    toolbar_module = importlib.import_module(
        "imswitch.improcess.controller.ImageToolbarController"
    )
    monkeypatch.setattr(
        toolbar_module.ChannelPickerDialog,
        "get_channels",
        staticmethod(lambda _result, _axis, parent=None: [4, 0, 0]),
    )

    controller.makeRgb()

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    rgb = produced[0][0]
    assert rgb.params["channels"] == [4, 0, 0]
    assert rgb.data[..., 0].max() == 255


def test_make_rgb_cancelled_picker_does_not_publish(monkeypatch):
    data = np.zeros((5, 2, 2), dtype=np.float32)
    controller, view, recon = _controller(data)
    recon.result.axis_labels = ["C", "Y", "X"]
    controller.currentResultChanged(recon.result)

    toolbar_module = importlib.import_module(
        "imswitch.improcess.controller.ImageToolbarController"
    )
    monkeypatch.setattr(
        toolbar_module.ChannelPickerDialog,
        "get_channels",
        staticmethod(lambda *args, **kwargs: None),
    )

    controller.makeRgb()

    assert controller._commChannel.sigResultProduced.emitted == []


def test_make_rgb_uses_composite_display_levels():
    from imswitch.improcess.processors.make_composite import MakeCompositeProcessor

    data = np.zeros((3, 2, 2), dtype=np.float32)
    data[0] = np.array([[0, 10], [20, 30]], dtype=np.float32)
    source = _Result(data)
    source.axis_labels = ["C", "Y", "X"]
    composite = MakeCompositeProcessor().apply(source, {"axis": "Auto"})
    composite.setDisplayLayerLevels("C_0", (0.0, 100.0))

    controller, view, recon = _controller(data)
    recon.result = composite
    controller.currentResultChanged(composite)

    controller.makeRgb()

    produced = controller._commChannel.sigResultProduced.emitted
    rgb = produced[0][0]
    expected_red = np.clip(data[0] / 100.0 * 255.0, 0, 255).astype(np.uint8)
    np.testing.assert_array_equal(rgb.data[..., 0], expected_red)


def test_merge_channels_enabled_for_selected_compatible_results_and_publishes_stack():
    data = np.arange(2 * 2, dtype=np.float32).reshape(2, 2)
    controller, view, recon = _controller(data)
    second = _Result(data + 10)
    second.name = "second"
    recon.selected_results = [recon.result, second]
    controller.currentResultChanged(recon.result)

    assert view.action_enabled["merge-channels"] is True
    controller.mergeChannels()

    produced = controller._commChannel.sigResultProduced.emitted
    assert len(produced) == 1
    merged = produced[0][0]
    assert merged.name == "Merged channels"
    assert merged.axis_labels == ["C", "Y", "X"]
    np.testing.assert_array_equal(merged.data[1], second.data)
