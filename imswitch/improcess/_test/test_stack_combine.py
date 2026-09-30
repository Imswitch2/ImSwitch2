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


# -- singleton axes and units are not differences in the pixels ---------------

def _legacy(name="legacy", shape=(8, 10), pitch_nm=30.0, value=0.0):
    """A result of the MoNaLISA reconstructor: six axes, four of them 1, nm."""
    data = np.full((1, 1, 1, 1, *shape), value, dtype=np.float32)
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=["Dataset", "Base", "T", "Z", "Y", "X"],
        axis_scales=[1.0, 1.0, 1.0, 1.0, pitch_nm, pitch_nm],
        scale_unit="nm",
    )


def _plain(name="plain", shape=(8, 10), pitch_um=0.03, value=1.0):
    """A result of the lattice reconstructor: the image and nothing else, um."""
    data = np.full(shape, value, dtype=np.float32)
    return ArrayProcessingResult(
        name=name, data=data, axis_labels=["Y", "X"],
        axis_scales=[pitch_um, pitch_um], scale_unit="um",
    )


def test_singleton_axes_do_not_keep_the_same_picture_apart():
    """(1, 1, 1, 1, Y, X) in nm and (Y, X) in um are one image twice over."""
    legacy, plain = _legacy(), _plain()

    ok, reason = combine_compatibility([legacy, plain], mode="stack", new_axis_label="C")
    assert ok, reason
    out = stack_results([legacy, plain], axis_label="C")
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["C", "Y", "X"]
    assert out.scale_unit == "nm"
    assert out.axis_scales == pytest.approx([1.0, 30.0, 30.0])
    np.testing.assert_array_equal(out.data[0], 0.0)
    np.testing.assert_array_equal(out.data[1], 1.0)

    # The other way round the output takes the first input's unit.
    out = stack_results([plain, legacy], axis_label="C")
    assert out.data.shape == (2, 8, 10)
    assert out.scale_unit == "um"
    assert out.axis_scales == pytest.approx([1.0, 0.03, 0.03])
    np.testing.assert_array_equal(out.data[0], 1.0)


def test_inputs_that_agree_axis_for_axis_keep_their_axes():
    """Two legacy results stack as they always did: nothing is squeezed."""
    out = stack_results([_legacy("a"), _legacy("b")], axis_label="C")
    assert out.data.shape == (2, 1, 1, 1, 1, 8, 10)
    assert out.axis_labels == ["C", "Dataset", "Base", "T", "Z", "Y", "X"]


def test_concatenating_along_a_singleton_axis_grows_it():
    """A legacy result's T axis, one plane long, takes a plain image as its
    second plane; the axes the plain image lacks are read as singletons."""
    legacy, plain = _legacy(), _plain()

    ok, reason = combine_compatibility([legacy, plain], mode="concatenate", join_axis=2)
    assert ok, reason
    out = concatenate_results([legacy, plain], join_axis=2)
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["T", "Y", "X"]
    assert out.axis_scales == pytest.approx([1.0, 30.0, 30.0])
    assert out.metadata["join_axis"] == "T"
    np.testing.assert_array_equal(out.data[0], 0.0)
    np.testing.assert_array_equal(out.data[1], 1.0)

    # A plain first input has no T axis to concatenate along; the join axis
    # is the first input's, so that stays Y.
    out = concatenate_results([plain, legacy], join_axis=0)
    assert out.data.shape == (16, 10)
    assert out.scale_unit == "um"


def test_singleton_tolerance_does_not_forgive_real_differences():
    legacy = _legacy()
    ok, reason = combine_compatibility([legacy, _plain(shape=(8, 12))], mode="stack")
    assert not ok
    assert "(8, 12)" in reason and "(1, 1, 1, 1, 8, 10)" in reason

    wrong_labels = _plain()
    wrong_labels.axis_labels = ["X", "Y"]
    ok, reason = combine_compatibility([legacy, wrong_labels], mode="stack")
    assert not ok
    assert "axes" in reason

    ok, reason = combine_compatibility([legacy, _plain(pitch_um=0.04)], mode="stack")
    assert not ok
    assert "scale" in reason and "um" in reason and "nm" in reason

    pixels = _plain()
    pixels.scale_unit = "px"
    ok, reason = combine_compatibility([legacy, pixels], mode="stack")
    assert not ok
    assert "unit" in reason


def test_a_squeezed_singleton_axis_no_longer_collides_with_the_new_label():
    """The plain image has no Z, so stacking along a new Z is fine even
    though the legacy input carries a Z axis of length 1."""
    ok, reason = combine_compatibility([_legacy(), _plain()], mode="stack", new_axis_label="Z")
    assert ok, reason
    out = stack_results([_legacy(), _plain()], axis_label="Z")
    assert out.axis_labels == ["Z", "Y", "X"]


def test_singleton_alignment_does_not_materialize_lazy_data():
    a = ArrayProcessingResult(
        name="a", data=_ShapeOnlyArray((1, 1, 8, 10)),
        axis_labels=["T", "Z", "Y", "X"],
    )
    b = ArrayProcessingResult(
        name="b", data=_ShapeOnlyArray((8, 10)), axis_labels=["Y", "X"]
    )
    ok, reason = combine_compatibility([a, b], mode="stack", new_axis_label="C")
    assert ok, reason
    ok, reason = combine_compatibility([a, b], mode="concatenate", join_axis=0)
    assert ok, reason


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

    dialog.inputWidget.inputList.setCurrentRow(1)
    dialog.inputWidget._move_selected(-1)

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


def test_dialog_can_switch_existing_axis_collision_to_concatenate(qapp):
    a = _image((1, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((1, 8, 10), ["Z", "Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    dialog.axisLabelCombo.setCurrentText("Z")

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert not dialog.switchToConcatenateButton.isHidden()

    dialog.switchToConcatenateButton.click()
    params = dialog.selected_params()

    assert params["mode"] == "concatenate"
    assert params["join_axis"] == 0
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_dialog_hides_existing_axis_redirect_when_concatenate_would_fail(qapp):
    a = _image((1, 8, 10), ["Z", "Y", "X"], "a")
    b = _image((1, 9, 10), ["Z", "Y", "X"], "b")
    dialog = StackCombineDialog([a, b])

    dialog.axisLabelCombo.setCurrentText("Z")

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert dialog.switchToConcatenateButton.isHidden()


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
    dialog.inputWidget.inputList.item(0).setCheckState(QtCore.Qt.Unchecked)
    dialog.inputWidget.inputList.item(1).setCheckState(QtCore.Qt.Checked)

    params = dialog.selected_params()
    assert [getattr(r, "name", "") for r in params["results"]] == [
        "multi_mean",
        "plain",
    ]
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    out = StackCombineProcessor().apply(params["results"][0], params)
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["Z", "Y", "X"]
