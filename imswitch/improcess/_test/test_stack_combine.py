"""Stack/Combine: rank-general stacking + concatenation of results."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.combine import (
    StackCombineProcessor,
    combine_compatibility,
    concatenate_results,
    stack_results,
)
from imswitch.improcess.view.StackCombineDialog import StackCombineDialog


def _image(shape, labels, name="img", scales=None):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=list(labels),
        axis_scales=list(scales) if scales else None,
        scale_unit="um" if scales else "px",
    )


# -- stack: new leading axis, rank-general ---------------------------------

def test_stack_two_2d_images_makes_a_3d_stack():
    a = _image((8, 10), ["Y", "X"], "a", scales=[0.5, 0.5])
    b = _image((8, 10), ["Y", "X"], "b", scales=[0.5, 0.5])

    out = stack_results([a, b], axis_label="Z", name="My stack")

    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["Z", "Y", "X"]
    assert out.axis_scales == [1.0, 0.5, 0.5]
    assert out.scale_unit == "um"
    assert out.name == "My stack"
    np.testing.assert_array_equal(out.data[0], a.data)
    np.testing.assert_array_equal(out.data[1], b.data)
    assert out.metadata["source_results"] == ["a", "b"]


def test_stack_two_3d_stacks_makes_a_4d_stack():
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((3, 8, 10), ["Z", "Y", "X"], "b")

    out = stack_results([a, b], axis_label="T")

    assert out.data.shape == (2, 3, 8, 10)
    assert out.axis_labels == ["T", "Z", "Y", "X"]


def test_stack_with_c_label_gets_channels_view_mode():
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 10), ["Y", "X"], "b")

    out = stack_results([a, b], axis_label="C")

    assert out.axis_labels == ["C", "Y", "X"]
    assert any(mode.name == "Channels" for mode in out.view_modes)


def test_stack_order_matches_input_order():
    a = _image((4, 4), ["Y", "X"], "a")
    b = _image((4, 4), ["Y", "X"], "b")
    b.data[:] = 7.0

    out = stack_results([b, a], axis_label="Z")

    np.testing.assert_array_equal(out.data[0], b.data)
    assert out.metadata["source_results"] == ["b", "a"]


# -- concatenate: append along an existing axis ------------------------------

def test_concatenate_grows_the_join_axis():
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((5, 8, 10), ["Z", "Y", "X"], "b")

    out = concatenate_results([a, b], join_axis=0)

    assert out.data.shape == (8, 8, 10)
    assert out.axis_labels == ["Z", "Y", "X"]
    np.testing.assert_array_equal(out.data[:3], a.data)
    np.testing.assert_array_equal(out.data[3:], b.data)
    assert out.metadata["join_axis"] == "Z"


def test_concatenate_rejects_mismatch_outside_join_axis():
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((3, 9, 10), ["Z", "Y", "X"], "b")

    ok, reason = combine_compatibility([a, b], mode="concatenate", join_axis=0)
    assert not ok
    assert "'b'" in reason
    with pytest.raises(ValueError):
        concatenate_results([a, b], join_axis=0)


# -- compatibility reasons ----------------------------------------------------

def test_stack_rejects_shape_mismatch_with_reason():
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 12), ["Y", "X"], "b")

    ok, reason = combine_compatibility([a, b], mode="stack")
    assert not ok
    assert "(8, 12)" in reason and "(8, 10)" in reason
    with pytest.raises(ValueError):
        stack_results([a, b])


def test_fewer_than_two_results_is_rejected():
    a = _image((8, 10), ["Y", "X"], "a")
    ok, reason = combine_compatibility([a])
    assert not ok
    assert "two" in reason
    ok, reason = combine_compatibility([])
    assert not ok


def test_mismatched_labels_and_scales_are_rejected():
    a = _image((8, 10), ["Y", "X"], "a", scales=[0.5, 0.5])
    ok, _ = combine_compatibility([a, _image((8, 10), ["T", "X"], "b", scales=[0.5, 0.5])])
    assert not ok
    ok, _ = combine_compatibility([a, _image((8, 10), ["Y", "X"], "b", scales=[0.9, 0.9])])
    assert not ok


# -- processor contract --------------------------------------------------------

def test_stack_combine_is_registered():
    assert "stack-combine" in available_processor_ids()


def test_processor_apply_uses_params_results():
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 10), ["Y", "X"], "b")

    out = StackCombineProcessor().apply(a, {"results": [a, b], "axis_label": "Z"})
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["Z", "Y", "X"]

    out = StackCombineProcessor().apply(
        a, {"results": [a, b], "mode": "concatenate", "join_axis": 0}
    )
    assert out.data.shape == (16, 10)


def test_processor_rejects_single_result():
    a = _image((8, 10), ["Y", "X"], "a")
    with pytest.raises(ValueError):
        StackCombineProcessor().apply(a, {})


# -- dialog ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_dialog_default_params_stack_z(qapp):
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 10), ["Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    params = dialog.selected_params()
    assert params["mode"] == "stack"
    assert params["axis_label"] == "Z"
    assert params["results"] == [a, b]
    assert params["name"] == "Stacked"
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_dialog_reorders_inputs(qapp):
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 10), ["Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    dialog.inputList.setCurrentRow(1)
    dialog._move_selected(-1)

    assert dialog.selected_params()["results"] == [b, a]


def test_dialog_concatenate_mode_returns_join_axis(qapp):
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((5, 8, 10), ["Z", "Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    dialog.modeCombo.setCurrentIndex(1)  # concatenate
    params = dialog.selected_params()

    assert params["mode"] == "concatenate"
    assert params["join_axis"] == 0
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_dialog_disables_ok_and_shows_reason_when_incompatible(qapp):
    a = _image((8, 10), ["Y", "X"], "a")
    b = _image((8, 12), ["Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert dialog.statusLabel.text()  # the reason is shown, not a silent no
