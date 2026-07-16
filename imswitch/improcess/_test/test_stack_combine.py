"""Stack/Combine: rank-general stacking + concatenation of results."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.combine import (
    StackCombineProcessor,
    combine_compatibility,
    concatenate_results,
    default_stack_axis_label,
    stack_results,
)
from imswitch.improcess.view.StackCombineDialog import (
    StackCombineDialog,
    expand_input_choices,
)


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


def test_mismatched_scale_unit_is_rejected_in_both_modes():
    """Same-shaped px + um inputs must not silently become one calibrated stack."""
    a = _image((8, 10), ["Y", "X"], "a", scales=[0.5, 0.5])  # um
    b = _image((8, 10), ["Y", "X"], "b", scales=[0.5, 0.5])
    b.scale_unit = "px"  # same numeric scales, different physical unit

    ok, reason = combine_compatibility([a, b], mode="stack")
    assert not ok
    assert "unit" in reason
    ok, reason = combine_compatibility([a, b], mode="concatenate", join_axis=0)
    assert not ok
    assert "unit" in reason


def test_new_axis_label_colliding_with_existing_labels_is_rejected():
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((3, 8, 10), ["Z", "Y", "X"], "b")

    ok, reason = combine_compatibility([a, b], mode="stack", new_axis_label="Z")
    assert not ok
    assert "'Z'" in reason
    with pytest.raises(ValueError):
        stack_results([a, b], axis_label="Z")
    # A non-colliding label still works.
    assert stack_results([a, b], axis_label="T").axis_labels == ["T", "Z", "Y", "X"]


def test_default_stack_axis_label_skips_collisions():
    assert default_stack_axis_label(["Y", "X"]) == "Z"
    assert default_stack_axis_label(["Z", "Y", "X"]) == "T"
    assert default_stack_axis_label(["T", "Z", "Y", "X"]) == "C"
    assert default_stack_axis_label(["C", "T", "Z", "Y", "X"]) == "S"


class _ShapeOnlyArray:
    """Lazy stand-in: shape metadata only, materialization is an error."""

    def __init__(self, shape):
        self.shape = tuple(shape)
        self.ndim = len(shape)

    def __array__(self, dtype=None):
        raise AssertionError("compatibility checks must not materialize data")


def test_compatibility_check_does_not_materialize_lazy_data():
    a = ArrayProcessingResult(
        name="a", data=_ShapeOnlyArray((3, 8, 10)), axis_labels=["Z", "Y", "X"]
    )
    b = ArrayProcessingResult(
        name="b", data=_ShapeOnlyArray((3, 8, 10)), axis_labels=["Z", "Y", "X"]
    )

    ok, _ = combine_compatibility([a, b], mode="stack", new_axis_label="T")
    assert ok
    ok, _ = combine_compatibility([a, b], mode="concatenate", join_axis=0)
    assert ok


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


def test_dialog_default_axis_label_avoids_collision(qapp):
    a = _image((3, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((3, 8, 10), ["Z", "Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    assert dialog.selected_params()["axis_label"] == "T"
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()

    # Forcing the colliding label surfaces the reason and disables OK.
    dialog.axisLabelCombo.setCurrentText("Z")
    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert "'Z'" in dialog.statusLabel.text()


class _MultiLayerResult(ArrayProcessingResult):
    """Result whose canonical data groups two heterogeneous components."""

    def display_layers(self):
        return [
            DisplayLayerSpec(
                name="mean",
                data=np.zeros((8, 10), np.float32),
                axis_labels=["Y", "X"],
                component="mean",
            ),
            DisplayLayerSpec(
                name="source",
                data=np.zeros((8, 10), np.float32),
                axis_labels=["Y", "X"],
                component="source",
                role="context",
            ),
        ]


def test_multilayer_results_are_expanded_into_components(qapp):
    multi = _MultiLayerResult(
        name="multi",
        data=np.zeros((2, 8, 10), np.float32),
        axis_labels=["C", "Y", "X"],
    )
    plain = _image((8, 10), ["Y", "X"], "plain")

    inputs = expand_input_choices([multi, plain])

    labels = [label for label, _result, _checked in inputs]
    assert labels == ["multi", "multi › mean", "plain"]  # context layer excluded
    checked = [checked for _label, _result, checked in inputs]
    assert checked == [True, False, True]  # whole results checked, components not


def test_dialog_combines_checked_component_with_plain_result(qapp):
    multi = _MultiLayerResult(
        name="multi",
        data=np.zeros((2, 8, 10), np.float32),
        axis_labels=["C", "Y", "X"],
    )
    plain = _image((8, 10), ["Y", "X"], "plain")
    dialog = StackCombineDialog([multi, plain])

    # Uncheck the (C, Y, X) whole result, check its (Y, X) mean component.
    from qtpy import QtCore
    dialog.inputList.item(0).setCheckState(QtCore.Qt.Unchecked)
    dialog.inputList.item(1).setCheckState(QtCore.Qt.Checked)

    params = dialog.selected_params()
    assert [getattr(r, "name", "") for r in params["results"]] == [
        "multi_mean",
        "plain",
    ]
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    out = StackCombineProcessor().apply(params["results"][0], params)
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["Z", "Y", "X"]
