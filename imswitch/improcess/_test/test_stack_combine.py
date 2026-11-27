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


# =============================================================================
# Padding, appending and the offers that go with them
# =============================================================================

from imswitch.improcess.processors.combine import (  # noqa: E402
    CombineMismatch,
    align_results,
    combine_results,
    preferred_candidate,
)


def _filled(shape, labels, name, value, scales=None, dtype=np.float32):
    return ArrayProcessingResult(
        name=name,
        data=np.full(shape, value, dtype=dtype),
        axis_labels=list(labels),
        axis_scales=list(scales) if scales else None,
        scale_unit="um" if scales else "px",
    )


def _ok(results, **kwargs):
    ok, reason = combine_compatibility(results, **kwargs)
    assert ok, reason


def _refused(results, **kwargs):
    ok, reason = combine_compatibility(results, **kwargs)
    assert not ok
    return reason


# -- padding -------------------------------------------------------------------

def test_a_size_mismatch_is_still_refused_by_default_and_the_message_offers_padding():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((9, 12), ["Y", "X"], "b", 2)
    reason = _refused([a, b], mode="stack")
    assert "(9, 12)" in reason and "pad" in reason
    with pytest.raises(CombineMismatch) as raised:
        stack_results([a, b])
    assert raised.value.kind == "size"


def test_pad_centres_the_smaller_input_with_zeros_and_records_it():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((9, 12), ["Y", "X"], "b", 2)

    out = stack_results([a, b], pad="pad")

    assert out.data.shape == (2, 9, 12)
    # 8 -> 9: nothing before, one after; 10 -> 12: one on each side.
    expected = np.zeros((9, 12), np.float32)
    expected[0:8, 1:11] = 1
    np.testing.assert_array_equal(out.data[0], expected)
    np.testing.assert_array_equal(out.data[1], 2)
    padding = out.metadata["padding"]
    assert padding["anchor"] == "centre" and padding["value"] == "0"
    assert padding["inputs"] == [{
        "input": "a", "original_shape": [8, 10], "padded_shape": [9, 12],
        "before": {"X": 1}, "after": {"Y": 1, "X": 1},
    }]
    assert padding["fraction"] == pytest.approx(1 - (8 * 10 + 9 * 12) / (2 * 9 * 12), abs=1e-5)
    assert out.axis_labels == ["Z", "Y", "X"]


def test_pad_at_the_origin_keeps_the_input_in_its_corner():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((9, 12), ["Y", "X"], "b", 2)

    out = stack_results([a, b], pad="pad", pad_anchor="origin")

    expected = np.zeros((9, 12), np.float32)
    expected[0:8, 0:10] = 1
    np.testing.assert_array_equal(out.data[0], expected)
    padding = out.metadata["padding"]
    assert padding["inputs"][0]["before"] == {}
    assert padding["inputs"][0]["after"] == {"Y": 1, "X": 2}
    # Nothing was added before the first input, so its pixels did not move.
    assert padding["first_input_grid_preserved"] is True
    # Centre padding of the first input moves them.
    centred = stack_results([a, b], pad="pad")
    assert centred.metadata["padding"]["first_input_grid_preserved"] is False


def test_pad_values_zero_nan_and_edge():
    a, b = _filled((2, 2), ["Y", "X"], "a", 5), _filled((4, 4), ["Y", "X"], "b", 1)
    a.data[:] = [[1, 2], [3, 4]]

    nan = stack_results([a, b], pad="pad", pad_anchor="origin", pad_value="nan")
    assert np.isnan(nan.data[0][2:]).all() and np.isnan(nan.data[0][:, 2:]).all()
    np.testing.assert_array_equal(nan.data[0][:2, :2], [[1, 2], [3, 4]])

    edge = stack_results([a, b], pad="pad", pad_anchor="origin", pad_value="edge")
    np.testing.assert_array_equal(
        edge.data[0], [[1, 2, 2, 2], [3, 4, 4, 4], [3, 4, 4, 4], [3, 4, 4, 4]]
    )


def test_nan_padding_refuses_integer_data_in_the_check():
    a = _filled((8, 10), ["Y", "X"], "a", 1, dtype=np.uint16)
    b = _filled((9, 12), ["Y", "X"], "b", 1, dtype=np.uint16)
    reason = _refused([a, b], mode="stack", pad="pad", pad_value="nan")
    assert "floating" in reason and "uint16" in reason
    _ok([a, b], mode="stack", pad="pad", pad_value="0")
    out = stack_results([a, b], pad="pad", pad_value="edge")
    assert out.data.dtype == np.uint16


def test_padding_limit_refuses_a_small_image_in_a_big_one_unless_allowed():
    big, small = _filled((64, 64), ["Y", "X"], "big", 1), _filled((16, 16), ["Y", "X"], "small", 1)
    reason = _refused([big, small], mode="stack", pad="pad")
    assert "'small'" in reason and "4.0x" in reason and "2x limit" in reason
    with pytest.raises(CombineMismatch) as raised:
        stack_results([big, small], pad="pad")
    assert raised.value.kind == "pad_limit"
    # Exactly the limit is fine, and the limit is a parameter; 0 lifts it.
    _ok([big, _filled((32, 32), ["Y", "X"], "half", 1)], mode="stack", pad="pad")
    _ok([big, small], mode="stack", pad="pad", pad_max_ratio=4)
    assert stack_results([big, small], pad="pad", pad_max_ratio=0).data.shape == (2, 64, 64)


def test_padding_does_not_forgive_scales_labels_or_units():
    a = _filled((8, 10), ["Y", "X"], "a", 1, scales=[0.5, 0.5])
    b = _filled((9, 12), ["Y", "X"], "b", 1, scales=[0.4, 0.4])
    reason = _refused([a, b], mode="stack", pad="pad")
    assert "scales" in reason and "Scale/Resize" in reason and "not resampling" in reason

    wrong = _filled((9, 12), ["X", "Y"], "b", 1, scales=[0.5, 0.5])
    reason = _refused([a, wrong], mode="stack", pad="pad")
    assert "axes" in reason and "Swap / Relabel Axes" in reason

    px = _filled((9, 12), ["Y", "X"], "b", 1)
    assert "unit" in _refused([a, px], mode="stack", pad="pad")


def test_only_the_plane_is_padded_never_the_z_t_or_c_count():
    a = _filled((3, 8, 10), ["Z", "Y", "X"], "a", 1)
    b = _filled((4, 8, 10), ["Z", "Y", "X"], "b", 1)
    reason = _refused([a, b], mode="stack", new_axis_label="T", pad="pad")
    assert "only the last two" in reason
    # ...which concatenating takes care of.
    assert concatenate_results([a, b], join_axis=0).data.shape == (7, 8, 10)


def test_concatenate_pads_the_plane_but_not_the_join_axis():
    a = _filled((3, 8, 10), ["Z", "Y", "X"], "a", 1)
    b = _filled((2, 9, 10), ["Z", "Y", "X"], "b", 2)
    assert "outside the join axis" in _refused([a, b], mode="concatenate", join_axis=0)

    out = concatenate_results([a, b], join_axis=0, pad="pad", pad_anchor="origin")

    assert out.data.shape == (5, 9, 10)
    assert out.metadata["padding"]["inputs"][0]["after"] == {"Y": 1}
    np.testing.assert_array_equal(out.data[:3, :8], 1)
    np.testing.assert_array_equal(out.data[:3, 8:], 0)
    np.testing.assert_array_equal(out.data[3:], 2)


def test_three_inputs_are_padded_to_the_largest_per_axis():
    shapes = [(8, 12), (10, 10), (9, 11)]
    results = [_filled(s, ["Y", "X"], f"r{i}", i) for i, s in enumerate(shapes)]
    out = stack_results(results, pad="pad")
    assert out.data.shape == (3, 10, 12)


def test_unpadded_results_carry_no_padding_metadata():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((8, 10), ["Y", "X"], "b", 2)
    out = stack_results([a, b], pad="pad")
    assert "padding" not in out.metadata
    assert out.metadata == stack_results([a, b]).metadata


def test_legacy_singleton_inputs_can_be_padded_too():
    out = stack_results([_legacy(), _plain(shape=(9, 12))], axis_label="C", pad="pad")
    assert out.data.shape == (2, 9, 12)
    assert out.axis_labels == ["C", "Y", "X"]


# -- appending (auto) ------------------------------------------------------------

def test_auto_appends_a_plane_to_a_stack_in_either_selection_order():
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1, scales=[2.0, 0.5, 0.5])
    plane = _filled((8, 10), ["Y", "X"], "plane", 9, scales=[0.5, 0.5])

    after = combine_results([stack, plane], mode="auto")
    assert after.data.shape == (4, 8, 10)
    assert after.axis_labels == ["Z", "Y", "X"]
    assert after.axis_scales == pytest.approx([2.0, 0.5, 0.5])
    np.testing.assert_array_equal(after.data[:3], 1)
    np.testing.assert_array_equal(after.data[3], 9)
    assert after.metadata["mode"] == "concatenate"
    assert after.metadata["join_axis"] == "Z"
    assert after.metadata["appended"] == ["plane"]
    assert after.metadata["requested_mode"] == "auto"

    # The plane first: it leads the output, and the labels and scales are
    # the stack's, not the first selected input's.
    before = combine_results([plane, stack], mode="auto")
    assert before.data.shape == (4, 8, 10)
    assert before.axis_labels == ["Z", "Y", "X"]
    assert before.axis_scales == pytest.approx([2.0, 0.5, 0.5])
    np.testing.assert_array_equal(before.data[0], 9)
    np.testing.assert_array_equal(before.data[1:], 1)


def test_auto_appends_a_stack_to_a_4d_stack():
    four = _filled((2, 3, 8, 10), ["T", "Z", "Y", "X"], "four", 1)
    three = _filled((3, 8, 10), ["Z", "Y", "X"], "three", 7)
    for order, where in (([four, three], 2), ([three, four], 0)):
        out = combine_results(order, mode="auto", axis_label="C")
        assert out.data.shape == (3, 3, 8, 10)
        assert out.axis_labels == ["T", "Z", "Y", "X"]
        np.testing.assert_array_equal(out.data[where], 7)


def test_auto_takes_several_planes_and_one_stack_in_selection_order():
    stack = _filled((2, 4, 5), ["Z", "Y", "X"], "stack", 1)
    first = _filled((4, 5), ["Y", "X"], "p1", 8)
    second = _filled((4, 5), ["Y", "X"], "p2", 9)
    out = combine_results([first, stack, second], mode="auto")
    assert out.data.shape == (4, 4, 5)
    assert [float(plane[0, 0]) for plane in out.data] == [8, 1, 1, 9]
    assert out.metadata["appended"] == ["p1", "p2"]
    assert out.metadata["source_results"] == ["p1", "stack", "p2"]


def test_auto_stacks_equal_rank_inputs_exactly_as_stack_does():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((8, 10), ["Y", "X"], "b", 2)
    auto = combine_results([a, b], mode="auto", axis_label="C")
    plain = stack_results([a, b], axis_label="C")
    np.testing.assert_array_equal(auto.data, plain.data)
    assert auto.axis_labels == plain.axis_labels
    assert auto.metadata["mode"] == "stack"
    # ...and equal-rank 3D stacks stack onto a new 4D axis, they are not joined.
    s1, s2 = (_filled((3, 8, 10), ["Z", "Y", "X"], n, 1) for n in "ab")
    assert combine_results([s1, s2], mode="auto", axis_label="T").data.shape == (2, 3, 8, 10)


def test_auto_keeps_the_singleton_tolerance_of_stack():
    out = combine_results([_legacy(), _plain()], mode="auto", axis_label="C")
    assert out.data.shape == (2, 8, 10)
    assert out.axis_labels == ["C", "Y", "X"]


def test_auto_pads_and_appends_together():
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((7, 9), ["Y", "X"], "plane", 9)
    reason = _refused([stack, plane], mode="auto")
    assert "(7, 9)" in reason
    out = combine_results([stack, plane], mode="auto", pad="pad", pad_anchor="origin")
    assert out.data.shape == (4, 8, 10)
    assert out.metadata["padding"]["inputs"][0]["original_shape"] == [1, 7, 9]
    assert out.metadata["padding"]["inputs"][0]["after"] == {"Y": 1, "X": 1}
    np.testing.assert_array_equal(out.data[3, :7, :9], 9)
    np.testing.assert_array_equal(out.data[3, 7:], 0)


def test_auto_asks_when_the_labels_do_not_say_which_axis():
    four = _filled((2, 2, 8, 10), ["Z", "Z", "Y", "X"], "four", 1)
    three = _filled((2, 8, 10), ["Z", "Y", "X"], "three", 7)
    with pytest.raises(CombineMismatch) as raised:
        align_results([four, three], mode="auto")
    assert raised.value.kind == "ambiguous"
    assert raised.value.candidates == ((0, "Z"), (1, "Z"))
    assert preferred_candidate([(0, "T"), (1, "Z")]) == (1, "Z")
    assert preferred_candidate([(2, "C"), (3, "S")]) == (2, "C")
    # The answer settles it, as an index when the label cannot.
    out = combine_results([four, three], mode="auto", append_axis="1")
    assert out.data.shape == (2, 3, 8, 10)
    with pytest.raises(CombineMismatch):
        align_results([four, three], mode="auto", append_axis="Q")


def test_append_axis_must_be_one_the_inputs_can_join_along():
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    assert combine_results([stack, plane], mode="auto", append_axis="Z").data.shape == (4, 8, 10)
    assert combine_results([stack, plane], mode="auto", append_axis="0").data.shape == (4, 8, 10)
    with pytest.raises(CombineMismatch) as raised:
        align_results([stack, plane], mode="auto", append_axis="Y")
    assert "cannot be appended along 'Y'" in str(raised.value)


def test_auto_refuses_what_is_not_one_missing_axis_and_says_why():
    four = _filled((2, 3, 8, 10), ["T", "Z", "Y", "X"], "four", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    reason = _refused([four, plane], mode="auto")
    assert "lacks 2 axes" in reason

    three_z = _filled((3, 8, 10), ["Z", "Y", "X"], "z", 1)
    three_t = _filled((2, 8, 10), ["T", "Y", "X"], "t", 1)
    assert "different axes" in _refused([four, three_z, three_t], mode="auto")

    relabelled = _filled((8, 10), ["X", "Y"], "swapped", 1)
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    reason = _refused([stack, relabelled], mode="auto")
    assert "Swap / Relabel Axes" in reason


def test_auto_does_not_forgive_scales_units_or_sizes_of_a_plane_by_default():
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1, scales=[1.0, 0.5, 0.5])
    wrong = _filled((8, 10), ["Y", "X"], "plane", 9, scales=[0.4, 0.4])
    assert "Scale/Resize" in _refused([stack, wrong], mode="auto", pad="pad")
    px = _filled((8, 10), ["Y", "X"], "plane", 9)
    assert "unit" in _refused([stack, px], mode="auto")


def test_stack_and_concatenate_modes_do_not_append_on_their_own():
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    assert not combine_compatibility([stack, plane], mode="stack", new_axis_label="T")[0]
    assert combine_compatibility([stack, plane], mode="concatenate", join_axis=0)[0]


def test_composite_and_rgb_results_are_combined_as_before(qapp):
    from imswitch.improcess.processors.make_composite.processor import MakeCompositeProcessor

    base = ArrayProcessingResult(
        name="c", data=np.ones((2, 8, 10), np.float32), axis_labels=["C", "Y", "X"]
    )
    composite = MakeCompositeProcessor().apply(base, {})
    out = stack_results([composite, composite], axis_label="T")
    assert out.data.shape == (2, *np.asarray(composite.data).shape)


# -- compatibility ---------------------------------------------------------------

def test_default_params_reproduce_todays_behaviour():
    defaults = StackCombineProcessor.default_params()
    assert defaults["mode"] == "stack" and defaults["pad"] == "none"
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((8, 12), ["Y", "X"], "b", 1)
    with pytest.raises(ValueError):
        StackCombineProcessor().apply(a, {"results": [a, b], "mode": "stack"})
    with pytest.raises(ValueError):
        StackCombineProcessor().apply(a, {**defaults, "results": [a, b]})


def test_apply_takes_the_new_parameters_as_a_workflow_would_write_them():
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((9, 12), ["Y", "X"], "b", 1)
    out = StackCombineProcessor().apply(
        a, {"results": [a, b], "mode": "stack", "pad": True, "pad_value": 0, "axis_label": "C"}
    )
    assert out.data.shape == (2, 9, 12)
    out = StackCombineProcessor().apply(
        a, {"results": [a, b], "pad": "pad", "pad_value": float("nan"), "pad_anchor": "origin"}
    )
    assert np.isnan(out.data[0][8:]).all()
    with pytest.raises(ValueError):
        StackCombineProcessor().apply(a, {"results": [a, b], "mode": "sideways"})


def test_check_inputs_accepts_what_an_option_could_combine_and_refuses_the_rest():
    processor = StackCombineProcessor()
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    small = _filled((7, 9), ["Y", "X"], "small", 9)
    assert processor.check_inputs([stack, plane])[0]
    assert processor.check_inputs([stack, small])[0]
    assert processor.check_inputs([plane, small])[0]

    assert not processor.check_inputs([plane])[0]
    ok, reason = processor.check_inputs(
        [plane, _filled((8, 10), ["Y", "X"], "scaled", 1, scales=[0.5, 0.5])]
    )
    assert not ok and "unit" in reason
    ok, reason = processor.check_inputs([stack, _filled((8, 10), ["X", "Y"], "swapped", 1)])
    assert not ok and "Swap / Relabel Axes" in reason
    # An ambiguous append is a choice the parameters make.
    four = _filled((2, 2, 8, 10), ["Z", "Z", "Y", "X"], "four", 1)
    assert processor.check_inputs([four, stack])[0]


def test_check_does_not_read_lazy_data_when_padding_or_appending():
    lazy_stack = ArrayProcessingResult(
        name="stack", data=_ShapeOnlyArray((3, 8, 10)), axis_labels=["Z", "Y", "X"]
    )
    lazy_plane = ArrayProcessingResult(
        name="plane", data=_ShapeOnlyArray((7, 9)), axis_labels=["Y", "X"]
    )
    _ok([lazy_stack, lazy_plane], mode="auto", pad="pad", pad_value="nan")
    ok, _ = StackCombineProcessor().check_inputs([lazy_stack, lazy_plane])
    assert ok
    description = align_results([lazy_stack, lazy_plane], mode="auto", pad="pad").describe()
    assert "Appends 'plane' to the Z axis" in description
    assert "(4, 8, 10)" in description


def test_param_widget_round_trips_the_parameters(qapp):
    processor = StackCombineProcessor()
    widget = processor.make_param_widget(None)
    assert widget.get_values() == processor.default_params()
    values = {
        "mode": "auto", "join_axis": 2, "name": "n", "axis_label": "T", "append_axis": "Z",
        "pad": "pad", "pad_anchor": "origin", "pad_value": "edge", "pad_max_ratio": 3.5,
    }
    widget.set_values(values)
    assert widget.get_values() == values


# -- the dialog ------------------------------------------------------------------

def _ok_enabled(dialog):
    return dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def _run(dialog):
    params = dialog.selected_params()
    results = params["results"]
    return StackCombineProcessor().apply(results[0], params)


def test_dialog_defaults_to_automatic_and_says_what_it_will_do(qapp):
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((8, 10), ["Y", "X"], "b", 2)
    dialog = StackCombineDialog([a, b])
    assert dialog.modeCombo.currentData() == "auto"
    assert "Stacks 2 inputs along a new Z axis" in dialog.planLabel.text()
    assert "(2, 8, 10)" in dialog.planLabel.text()
    assert dialog.selected_params()["mode"] == "stack"
    assert not dialog.offerPadButton.isVisibleTo(dialog)


def test_dialog_offers_to_pad_a_size_mismatch_and_shows_the_shape(qapp):
    a, b = _filled((8, 10), ["Y", "X"], "a", 1), _filled((9, 12), ["Y", "X"], "b", 2)
    dialog = StackCombineDialog([a, b])

    assert not _ok_enabled(dialog)
    assert "(9, 12)" in dialog.statusLabel.text()
    assert dialog.offerPadButton.isVisibleTo(dialog)
    assert "(2, 9, 12)" in dialog.offerPadButton.text()
    assert not dialog.offerAppendButton.isVisibleTo(dialog)

    dialog.offerPadButton.click()

    assert _ok_enabled(dialog)
    assert dialog.padCheck.isChecked()
    assert "Pads 'a' (8, 10) -> (9, 12)" in dialog.planLabel.text()
    assert dialog.statusLabel.text() == ""
    assert not dialog.offerPadButton.isVisibleTo(dialog)
    params = dialog.selected_params()
    assert params["pad"] == "pad" and params["pad_anchor"] == "centre"
    assert params["pad_value"] == "0" and params["pad_max_ratio"] == 2.0
    assert _run(dialog).data.shape == (2, 9, 12)

    # The plan follows the options live.
    dialog.padAnchorCombo.setCurrentIndex(dialog.padAnchorCombo.findData("origin"))
    assert "origin" in dialog.planLabel.text()
    assert dialog.selected_params()["pad_anchor"] == "origin"
    dialog.padCheck.setChecked(False)
    assert not _ok_enabled(dialog)
    assert "pad_anchor" not in dialog.selected_params()


def test_dialog_warns_about_heavy_padding_and_refuses_extreme_padding(qapp):
    big, small = _filled((64, 64), ["Y", "X"], "big", 1), _filled((40, 40), ["Y", "X"], "small", 1)
    dialog = StackCombineDialog([big, small])
    dialog.offerPadButton.click()
    assert _ok_enabled(dialog)
    assert "Warning" in dialog.warningLabel.text() and "%" in dialog.warningLabel.text()

    tiny = _filled((16, 16), ["Y", "X"], "tiny", 1)
    dialog = StackCombineDialog([big, tiny])
    assert not dialog.offerPadButton.isVisibleTo(dialog)  # past the limit: no one-click offer
    dialog.padCheck.setChecked(True)
    assert not _ok_enabled(dialog)
    assert "Allow padding beyond 2x" in dialog.statusLabel.text()
    dialog.padLargeCheck.setChecked(True)
    assert _ok_enabled(dialog)
    assert dialog.selected_params()["pad_max_ratio"] == 0.0


def test_dialog_appends_a_plane_to_a_stack_by_default(qapp):
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    dialog = StackCombineDialog([stack, plane])

    assert _ok_enabled(dialog)
    assert "Appends 'plane' to the Z axis of 'stack'" in dialog.planLabel.text()
    assert "(4, 8, 10)" in dialog.planLabel.text()
    params = dialog.selected_params()
    assert params["mode"] == "auto" and params["append_axis"] == "Z"
    assert "axis_label" not in params
    assert dialog.nameEdit.text() == "Concatenated"
    assert _run(dialog).data.shape == (4, 8, 10)

    # Selection order: move the plane first.
    dialog.inputWidget.inputList.setCurrentRow(1)
    dialog.inputWidget._move_selected(-1)
    assert dialog.selected_params()["results"] == [plane, stack]
    out = _run(dialog)
    np.testing.assert_array_equal(out.data[0], 9)
    assert out.axis_labels == ["Z", "Y", "X"]


def test_dialog_offers_to_append_when_stack_mode_is_chosen(qapp):
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((8, 10), ["Y", "X"], "plane", 9)
    dialog = StackCombineDialog([stack, plane])
    dialog.modeCombo.setCurrentIndex(dialog.modeCombo.findData("stack"))

    assert not _ok_enabled(dialog)
    assert dialog.offerAppendButton.isVisibleTo(dialog)
    assert dialog.offerAppendButton.text().startswith("Append to axis Z")
    assert "(4, 8, 10)" in dialog.offerAppendButton.text()
    dialog.offerAppendButton.click()
    assert _ok_enabled(dialog)
    assert dialog.modeCombo.currentData() == "auto"


def test_dialog_offers_to_append_and_pad_together(qapp):
    stack = _filled((3, 8, 10), ["Z", "Y", "X"], "stack", 1)
    plane = _filled((7, 9), ["Y", "X"], "plane", 9)
    dialog = StackCombineDialog([stack, plane])

    # Automatic already appends, so padding is the only thing missing.
    assert not _ok_enabled(dialog)
    assert dialog.offerPadButton.isVisibleTo(dialog)
    assert "(4, 8, 10)" in dialog.offerPadButton.text()
    assert not dialog.offerBothButton.isVisibleTo(dialog)

    # From an explicit Stack both are missing, and one click gives both.
    dialog.modeCombo.setCurrentIndex(dialog.modeCombo.findData("stack"))
    assert not dialog.offerPadButton.isVisibleTo(dialog)
    assert not dialog.offerAppendButton.isVisibleTo(dialog)
    assert dialog.offerBothButton.isVisibleTo(dialog)
    assert "Append to axis Z and pad to common size" in dialog.offerBothButton.text()
    assert "(4, 8, 10)" in dialog.offerBothButton.text()
    dialog.offerBothButton.click()
    assert _ok_enabled(dialog)
    out = _run(dialog)
    assert out.data.shape == (4, 8, 10)
    assert "padding" in out.metadata


def test_dialog_asks_which_axis_when_the_labels_do_not_say(qapp):
    four = _filled((2, 2, 8, 10), ["Z", "Z", "Y", "X"], "four", 1)
    three = _filled((2, 8, 10), ["Z", "Y", "X"], "three", 7)
    dialog = StackCombineDialog([four, three])

    assert dialog.appendAxisCombo.isVisibleTo(dialog)
    assert dialog.appendAxisCombo.count() == 2
    assert "do not say which axis" in dialog.planLabel.text()
    assert _ok_enabled(dialog)  # pre-filled with the best guess
    first = dialog.selected_params()
    assert first["mode"] == "auto" and first["append_axis"] == "0"
    dialog.appendAxisCombo.setCurrentIndex(1)
    assert dialog.selected_params()["append_axis"] == "1"
    assert _run(dialog).data.shape == (2, 3, 8, 10)


def test_dialog_prefers_z_over_t_as_the_guess(qapp):
    four = _filled((2, 3, 8, 10), ["T", "Z", "Y", "X"], "four", 1)
    three = _filled((3, 8, 10), ["Z", "Y", "X"], "three", 7)
    dialog = StackCombineDialog([four, three])
    # Unambiguous by labels: no question is asked.
    assert not dialog.appendAxisCombo.isVisibleTo(dialog)
    assert dialog.selected_params()["append_axis"] == "T"


def test_dialog_scale_mismatch_points_to_scale_resize_and_offers_nothing(qapp):
    a = _filled((8, 10), ["Y", "X"], "a", 1, scales=[0.5, 0.5])
    b = _filled((9, 12), ["Y", "X"], "b", 1, scales=[0.4, 0.4])
    dialog = StackCombineDialog([a, b])

    assert not _ok_enabled(dialog)
    text = dialog.statusLabel.text()
    assert "pixel scales" in text and "Scale/Resize" in text
    for button in (dialog.offerPadButton, dialog.offerAppendButton, dialog.offerBothButton):
        assert not button.isVisibleTo(dialog)
    dialog.padCheck.setChecked(True)
    assert not _ok_enabled(dialog)
    assert "Scale/Resize" in dialog.statusLabel.text()


def test_dialog_label_mismatch_points_to_swap_relabel_axes(qapp):
    a = _filled((8, 10), ["Y", "X"], "a", 1)
    b = _filled((8, 10), ["T", "X"], "b", 1)
    dialog = StackCombineDialog([a, b])
    assert not _ok_enabled(dialog)
    assert "Swap / Relabel Axes" in dialog.statusLabel.text()
    assert not dialog.offerPadButton.isVisibleTo(dialog)


def test_dialog_shows_the_fundamental_problem_instead_of_offering_to_pad(qapp):
    # The size differs too, but padding would not make these combinable.
    a = _filled((8, 10), ["Y", "X"], "a", 1, scales=[0.5, 0.5])
    b = _filled((9, 12), ["Y", "X"], "b", 1, scales=[0.4, 0.4])
    dialog = StackCombineDialog([a, b])
    assert "scales" in dialog.statusLabel.text()
    assert not dialog.offerPadButton.isVisibleTo(dialog)


def test_dialog_explicit_modes_still_return_their_old_params(qapp):
    a = _filled((3, 8, 10), ["Z", "Y", "X"], "a", 1)
    b = _filled((5, 8, 10), ["Z", "Y", "X"], "b", 1)
    dialog = StackCombineDialog([a, b])
    dialog.modeCombo.setCurrentIndex(dialog.modeCombo.findData("stack"))
    params = dialog.selected_params()
    assert params["mode"] == "stack" and params["axis_label"] == "T"
    dialog.modeCombo.setCurrentIndex(dialog.modeCombo.findData("concatenate"))
    params = dialog.selected_params()
    assert params["mode"] == "concatenate" and params["join_axis"] == 0
    assert "pad" not in params and "append_axis" not in params
    assert _run(dialog).data.shape == (8, 8, 10)
