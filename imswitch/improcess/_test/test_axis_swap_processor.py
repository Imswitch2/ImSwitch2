"""Swap / Relabel Axes: the array order, the labels and the scales."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.axis_swap import AxisSwapProcessor, parse_labels, parse_order, parse_scales
from imswitch.improcess.processors.axis_swap.processor import ProcessorInputError


def _result(shape, labels, scales=None):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    result = ArrayProcessingResult(name="source", data=data, axis_labels=list(labels))
    if scales is not None:
        result.axis_scales = list(scales)
    return result


def _apply(result, **params):
    return AxisSwapProcessor().apply(result, {**AxisSwapProcessor.default_params(), **params})


def test_registered_and_does_nothing_by_default():
    assert "axis-swap" in available_processor_ids()
    source = _result((2, 3, 4), "ZYX", [2.0, 0.5, 0.25])
    out = _apply(source)
    assert out.axis_labels == ["Z", "Y", "X"] and out.axis_scales == [2.0, 0.5, 0.25]
    np.testing.assert_array_equal(out.data, source.data)


def test_swapping_two_axes_moves_the_data_with_its_labels_and_scales():
    source = _result((2, 3, 4, 5), "TZYX", [10.0, 2.0, 0.5, 0.25])
    out = _apply(source, order="Z,T")
    assert out.data.shape == (3, 2, 4, 5)
    assert out.axis_labels == ["Z", "T", "Y", "X"]
    assert out.axis_scales == [2.0, 10.0, 0.5, 0.25]
    np.testing.assert_array_equal(out.data[1, 0], source.data[0, 1])     # the same data, stored the other way
    assert np.shares_memory(out.data, source.data)                        # a view, not a copy
    assert out.metadata["permutation"] == [1, 0, 2, 3]


def test_x_becomes_y_when_the_labels_stay_in_place():
    source = _result((3, 4), "YX", [0.5, 0.25])
    out = _apply(source, order="X,Y", keep_labels=True)
    assert out.data.shape == (4, 3)
    assert out.axis_labels == ["Y", "X"] and out.axis_scales == [0.5, 0.25]
    np.testing.assert_array_equal(out.data, source.data.T)


def test_a_full_new_order_by_label_or_index():
    source = _result((2, 3, 4), "ZYX")
    by_label = _apply(source, order="Z,X,Y")
    by_index = _apply(source, order="0,2,1")
    assert by_label.axis_labels == by_index.axis_labels == ["Z", "X", "Y"]
    np.testing.assert_array_equal(by_label.data, np.transpose(source.data, (0, 2, 1)))
    np.testing.assert_array_equal(by_index.data, by_label.data)


def test_relabelling_leaves_the_array_alone():
    source = _result((2, 3, 4), "ZYX", [2.0, 0.5, 0.25])
    full = _apply(source, labels="T,Y,X")
    pairs = _apply(source, labels="Z=T")
    assert full.axis_labels == pairs.axis_labels == ["T", "Y", "X"]
    assert full.axis_scales == [2.0, 0.5, 0.25]
    assert full.data is source.data
    swapped_names = _apply(source, labels="X=Y, Y=X")                    # pairs read the labels before renaming
    assert swapped_names.axis_labels == ["Z", "X", "Y"]


def test_reorder_then_relabel():
    source = _result((2, 3, 4), "XYZ")
    out = _apply(source, order="Z,Y,X", labels="Z=T")
    assert out.data.shape == (4, 3, 2) and out.axis_labels == ["T", "Y", "X"]


def test_a_lazy_array_is_read_and_reordered():
    class Lazy:
        shape, ndim, dtype = (2, 3, 4), 3, np.dtype(np.float32)

        def __array__(self, dtype=None, copy=None):
            return np.arange(24, dtype=np.float32).reshape(self.shape)

    source = ArrayProcessingResult(name="lazy", data=Lazy(), axis_labels=["Z", "Y", "X"])
    out = _apply(source, order="Y,X")
    assert out.data.shape == (2, 4, 3)


@pytest.mark.parametrize("params, message", [
    ({"order": "Q,Z"}, "no axis 'Q'"),
    ({"order": "Z,Z"}, "names an axis twice"),
    ({"order": "T,Z,Y"}, "two axes to swap, or all 4"),
    ({"order": "7,0"}, "outside the 4 axes"),
    ({"labels": "A,B"}, "one label per axis"),
    ({"labels": "Z=T"}, "same label"),
    ({"labels": "Z=Q,Y"}, "not both"),
    ({"scales": "Q=1"}, "no axis 'Q'"),
    ({"scales": "Z=abc"}, "not a number"),
    ({"scales": "Z=0"}, "greater than zero"),
    ({"scales": "Z=-1"}, "greater than zero"),
    ({"scales": "Z=nan"}, "greater than zero"),
    ({"scales": "Z=1, Z=2"}, "set twice"),
    ({"scales": "Z=1, 2"}, "not both"),
    ({"scales": "1,2"}, "one value per axis"),
])
def test_what_cannot_be_done_is_refused_with_the_reason(params, message):
    with pytest.raises(ProcessorInputError, match=message):
        _apply(_result((2, 3, 4, 5), "TZYX"), **params)


def test_parsers():
    assert parse_order("", "ZYX") == (0, 1, 2)
    assert parse_order("x, y", "ZYX") == (0, 2, 1)
    assert parse_order(["Y", "X"], "ZYX") == (0, 2, 1)
    assert parse_labels("", "ZYX") == ["Z", "Y", "X"]
    assert parse_labels("z=T", "ZYX") == ["T", "Y", "X"]


def test_the_form_hands_over_what_was_typed(qtbot):
    from qtpy import QtWidgets

    widget = AxisSwapProcessor().make_param_widget(None)
    qtbot.addWidget(widget)
    assert widget.get_values() == AxisSwapProcessor.default_params()
    edits = widget.findChildren(QtWidgets.QLineEdit)
    edits[0].setText(" Z,T ")
    widget.findChild(QtWidgets.QCheckBox).setChecked(True)
    assert widget.get_values() == {"order": "Z,T", "keep_labels": True, "labels": "", "scales": ""}
    edits[2].setText(" Z=0.25 ")
    assert widget.get_values()["scales"] == "Z=0.25"
    widget.set_values({"scales": "T=2"})
    assert edits[2].text() == "T=2"


def test_scales_set_the_spacing_by_pairs_or_full_list_and_leave_the_array_alone():
    source = _result((2, 3, 4), "ZYX", [2.0, 0.5, 0.25])
    one = _apply(source, scales="Z=0.25")
    assert one.axis_scales == [0.25, 0.5, 0.25] and one.data is source.data
    assert source.axis_scales == [2.0, 0.5, 0.25]                          # the source is not changed
    assert _apply(source, scales="Z=0.25, x=0.1").axis_scales == [0.25, 0.5, 0.1]
    assert _apply(source, scales="1, 2, 3").axis_scales == [1.0, 2.0, 3.0]
    assert _apply(source, scales="0=9").axis_scales == [9.0, 0.5, 0.25]


def test_scales_name_axes_after_reordering_and_relabelling():
    source = _result((2, 3, 4), "ZYX", [2.0, 0.5, 0.25])
    out = _apply(source, order="Y,X", labels="Z=T", scales="T=7, X=0.1")
    assert out.axis_labels == ["T", "X", "Y"] and out.axis_scales == [7.0, 0.1, 0.5]  # X moved to the middle, with its scale
    assert _apply(source, labels="Z=T", scales="T=7").axis_scales == [7.0, 0.5, 0.25]
    with pytest.raises(ProcessorInputError, match="no axis 'Z'"):
        _apply(source, labels="Z=T", scales="Z=7")                          # Z is called T by then


def test_rescaling_the_image_plane_is_a_new_grid_but_rescaling_z_is_not():
    source = _result((2, 3, 4), "ZYX", [2.0, 0.5, 0.25])
    assert _apply(source, scales="Z=1").coordinate_space_uid == source.coordinate_space_uid
    assert _apply(source, scales="X=1").coordinate_space_uid != source.coordinate_space_uid


def test_parse_scales():
    assert parse_scales("", "ZYX", [1, 2, 3]) == [1.0, 2.0, 3.0]
    assert parse_scales("z=5", "ZYX", [1, 2, 3]) == [5.0, 2.0, 3.0]
    assert parse_scales(["1", "2", "3"], "ZYX", [9, 9, 9]) == [1.0, 2.0, 3.0]
