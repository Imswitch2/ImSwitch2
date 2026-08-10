"""Merge channels picker: which results become the channels, in what order."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.view.MergeChannelsDialog import MergeChannelsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _image(name, shape=(8, 10), labels=("Y", "X"), scales=None, unit="px"):
    return ArrayProcessingResult(
        name=name,
        data=np.zeros(shape, dtype=np.float32),
        axis_labels=list(labels),
        axis_scales=list(scales) if scales else [1.0] * len(shape),
        scale_unit=unit,
    )


class _MultiLayerResult(ArrayProcessingResult):
    """A result whose images are display layers, as a composite's are."""

    def display_layers(self):
        return [
            DisplayLayerSpec(
                name=f"{self.name}_plane{index}",
                data=self.data[index],
                axis_labels=["Y", "X"],
                display_levels=(0.0, 1.0),
                axis_scales=[1.0, 1.0],
                scale_unit="px",
                colormap="gray",
                metadata={"component": f"plane{index}"},
            )
            for index in range(self.data.shape[0])
        ]


def test_dialog_defaults_to_every_result_as_a_channel(qapp):
    a, b = _image("a"), _image("b")
    dialog = MergeChannelsDialog([a, b])

    params = dialog.selected_params()

    assert params["results"] == [a, b]
    assert params["axis_label"] == "C"
    assert params["name"] == "Merged channels"
    assert params["composite"] is True
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_dialog_prechecks_only_the_preselected_results(qapp):
    """Every loaded result is offered; the reconstruction-list selection just
    decides what starts checked."""
    a, b, c = _image("a"), _image("b"), _image("c")
    dialog = MergeChannelsDialog([a, b, c], preselected=[a, c])

    assert dialog.selected_params()["results"] == [a, c]


def test_dialog_order_decides_channel_order(qapp):
    a, b = _image("a"), _image("b")
    dialog = MergeChannelsDialog([a, b])

    dialog.inputWidget.inputList.setCurrentRow(1)
    dialog.inputWidget._move_selected(-1)

    assert dialog.selected_params()["results"] == [b, a]


def test_dialog_shows_the_reason_instead_of_a_dead_ok(qapp):
    a = _image("a", scales=[0.065, 0.065], unit="um")
    mismatched = _image("mismatched", scales=[0.13, 0.13], unit="um")
    dialog = MergeChannelsDialog([a, mismatched])

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert "mismatched" in dialog.statusLabel.text()
    assert "scales" in dialog.statusLabel.text()


def test_dialog_refuses_a_single_channel(qapp):
    dialog = MergeChannelsDialog([_image("a"), _image("b")])

    dialog.inputWidget.inputList.item(1).setCheckState(QtCore.Qt.Unchecked)

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert "at least two" in dialog.statusLabel.text()


def test_a_lone_stack_is_pointed_at_split_stack(qapp):
    """The planes of a stack are images too, but a merge takes whole results
    — say which operation turns them into inputs."""
    stack = _image("recD", shape=(2, 8, 10), labels=("Z", "Y", "X"))
    dialog = MergeChannelsDialog([stack])

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert "Split stack" in dialog.statusLabel.text()


def test_a_lone_channel_stack_is_pointed_at_make_composite(qapp):
    stack = _image("recE", shape=(2, 8, 10), labels=("C", "Y", "X"))
    dialog = MergeChannelsDialog([stack])

    assert "Make composite" in dialog.statusLabel.text()


def test_two_layers_of_one_result_can_be_merged(qapp):
    """Merging within a single reconstruction: its display layers are listed
    individually, so two channels of one result are pickable inputs."""
    multi = _MultiLayerResult(
        name="multi",
        data=np.stack(
            [np.full((8, 10), 2.0, np.float32), np.full((8, 10), 9.0, np.float32)]
        ),
        axis_labels=["C", "Y", "X"],
    )
    dialog = MergeChannelsDialog([multi])

    dialog.inputWidget.inputList.item(0).setCheckState(QtCore.Qt.Unchecked)
    for row in (1, 2):
        dialog.inputWidget.inputList.item(row).setCheckState(QtCore.Qt.Checked)

    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert [r.name for r in dialog.selected_params()["results"]] == [
        "multi_plane0",
        "multi_plane1",
    ]


def test_composite_can_be_turned_off(qapp):
    dialog = MergeChannelsDialog([_image("a"), _image("b")])

    dialog.compositeCheck.setChecked(False)

    assert dialog.selected_params()["composite"] is False


def test_multilayer_results_offer_their_components_as_channels(qapp):
    """A channel of one reconstruction can be merged with a whole other one."""
    multi = _MultiLayerResult(
        name="multi",
        data=np.zeros((2, 8, 10), np.float32),
        axis_labels=["C", "Y", "X"],
    )
    plain = _image("plain")
    dialog = MergeChannelsDialog([multi, plain])

    # Uncheck the (C, Y, X) whole result, check its (Y, X) component instead.
    dialog.inputWidget.inputList.item(0).setCheckState(QtCore.Qt.Unchecked)
    dialog.inputWidget.inputList.item(1).setCheckState(QtCore.Qt.Checked)

    params = dialog.selected_params()
    assert [getattr(r, "name", "") for r in params["results"]] == [
        "multi_plane0",
        "plain",
    ]
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
