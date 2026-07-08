"""Tests for stack/channel split processors."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.channel_split import ChannelSplitProcessor
from imswitch.improcess.processors.stack_split import StackSplitProcessor
from imswitch.improcess.processors.stack_subset import StackSubsetProcessor


def _result(data, labels):
    return ArrayProcessingResult(
        name="source",
        data=np.asarray(data, dtype=np.float32),
        axis_labels=list(labels),
    )


class _ShapeOnlyArray:
    shape = (3, 2, 4)
    ndim = 3

    def __array__(self, dtype=None):
        raise AssertionError("shape-only enablement should not materialize data")


class _LazyArray:
    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.ndim = self._data.ndim
        self.dtype = self._data.dtype
        self.backend = "test-lazy"
        self.keys = []

    def __getitem__(self, key):
        self.keys.append(key)
        return self._data[key]

    def __array__(self, dtype=None):
        raise AssertionError("lazy subset apply should not materialize data")


def test_stack_split_registered():
    assert "stack-split" in available_processor_ids()


def test_channel_split_registered():
    assert "channel-split" in available_processor_ids()


def test_stack_subset_registered():
    assert "stack-subset" in available_processor_ids()


def test_stack_subset_preserves_rank_and_uses_view_by_default():
    data = np.arange(2 * 4 * 5, dtype=np.float32).reshape(2, 4, 5)
    result = _result(data, ["Z", "Y", "X"])
    result.axis_scales = [2.0, 0.5, 0.25]

    subset = StackSubsetProcessor().apply(
        result,
        {"ranges": [{"axis": "Z", "start": 1, "stop": 2}]},
    )

    assert subset.name == "source (subset)"
    assert subset.data.shape == (1, 4, 5)
    assert subset.axis_labels == ["Z", "Y", "X"]
    assert subset.axis_scales == [2.0, 0.5, 0.25]
    assert np.shares_memory(subset.data, data)
    np.testing.assert_array_equal(subset.data[0], data[1])


def test_stack_subset_step_updates_axis_scale_and_can_copy():
    data = np.arange(5 * 4, dtype=np.float32).reshape(5, 4)
    result = _result(data, ["Z", "Y"])
    result.axis_scales = [1.5, 0.2]

    subset = StackSubsetProcessor().apply(
        result,
        {"ranges": [{"axis": 0, "start": 0, "stop": 5, "step": 2}], "copy": True},
    )

    assert subset.data.shape == (3, 4)
    assert subset.axis_scales == [3.0, 0.2]
    assert not np.shares_memory(subset.data, data)
    np.testing.assert_array_equal(subset.data, data[0:5:2])


def test_stack_subset_crops_multiple_dimensions():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    result = _result(data, ["T", "Y", "X"])
    result.axis_scales = [1.0, 0.5, 0.2]

    subset = StackSubsetProcessor().apply(
        result,
        {
            "ranges": [
                {"axis": "T", "start": 1, "stop": 3},
                {"axis": "X", "start": 0, "stop": 5, "step": 2},
            ]
        },
    )

    assert subset.data.shape == (2, 4, 3)
    assert subset.axis_labels == ["T", "Y", "X"]
    assert subset.axis_scales == [1.0, 0.5, 0.4]
    np.testing.assert_array_equal(subset.data, data[1:3, :, 0:5:2])


def test_stack_subset_validates_ranges():
    result = _result(np.zeros((2, 3, 4), dtype=np.float32), ["Z", "Y", "X"])

    with pytest.raises(ValueError, match="Invalid subset range"):
        StackSubsetProcessor().apply(
            result,
            {"ranges": [{"axis": "Z", "start": 2, "stop": 2}]},
        )


def test_stack_subset_enablement_uses_shape_metadata_without_materializing():
    result = ArrayProcessingResult(
        name="lazy",
        data=_ShapeOnlyArray(),
        axis_labels=["C", "Y", "X"],
    )

    assert StackSubsetProcessor().applies_to(result)


def test_stack_subset_defers_non_numpy_array_slicing_until_read():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    lazy = _LazyArray(data)
    result = ArrayProcessingResult(
        name="lazy",
        data=lazy,
        axis_labels=["Z", "Y", "X"],
    )

    subset = StackSubsetProcessor().apply(
        result,
        {"ranges": [{"axis": "Z", "start": 1, "stop": 3}]},
    )

    assert lazy.keys == []
    assert subset.data.shape == (2, 4, 5)
    assert subset.data.backend == "test-lazy-subset"
    np.testing.assert_array_equal(subset.data[1], data[2])
    assert lazy.keys == [(2, slice(0, 4, 1), slice(0, 5, 1))]


def test_stack_subset_defers_multi_axis_lazy_slicing_until_read():
    data = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
    lazy = _LazyArray(data)
    result = ArrayProcessingResult(
        name="lazy",
        data=lazy,
        axis_labels=["Z", "Y", "X"],
    )

    subset = StackSubsetProcessor().apply(
        result,
        {
            "ranges": [
                {"axis": "Z", "start": 0, "stop": 4, "step": 2},
                {"axis": "Y", "start": 1, "stop": 5},
            ]
        },
    )

    assert lazy.keys == []
    assert subset.data.shape == (2, 4, 6)
    np.testing.assert_array_equal(subset.data[1, 2], data[2, 3])
    assert lazy.keys == [(2, 3, slice(0, 6, 1))]


def test_stack_split_prefers_z_axis_and_removes_label():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    result = _result(data, ["Z", "Y", "X"])

    output = StackSplitProcessor().apply(result, {"axis": "Auto"})

    assert len(output.results) == 2
    assert output.results[0].name == "source (Z 0)"
    assert output.results[0].axis_labels == ["Y", "X"]
    np.testing.assert_array_equal(output.results[1].data, data[1])


def test_stack_split_can_use_explicit_time_axis():
    data = np.arange(2 * 3 * 4 * 5, dtype=np.float32).reshape(2, 3, 4, 5)
    result = _result(data, ["T", "Z", "Y", "X"])

    output = StackSplitProcessor().apply(result, {"axis": "T"})

    assert len(output.results) == 2
    assert output.results[0].axis_labels == ["Z", "Y", "X"]
    np.testing.assert_array_equal(output.results[0].data, data[0])


def test_channel_split_requires_channel_like_axis():
    result = _result(np.zeros((2, 3, 4), dtype=np.float32), ["Z", "Y", "X"])

    assert not ChannelSplitProcessor().applies_to(result)
    with pytest.raises(ValueError, match="No matching split axis"):
        ChannelSplitProcessor().apply(result, {"axis": "Auto"})


def test_channel_split_enablement_uses_shape_metadata_without_materializing():
    result = ArrayProcessingResult(
        name="lazy",
        data=_ShapeOnlyArray(),
        axis_labels=["C", "Y", "X"],
    )

    assert ChannelSplitProcessor().applies_to(result)


def test_channel_split_uses_c_axis():
    data = np.arange(3 * 2 * 4, dtype=np.float32).reshape(3, 2, 4)
    result = _result(data, ["C", "Y", "X"])

    output = ChannelSplitProcessor().apply(result, {"axis": "Auto"})

    assert len(output.results) == 3
    assert output.results[2].name == "source (C 2)"
    assert output.results[2].axis_labels == ["Y", "X"]
    np.testing.assert_array_equal(output.results[2].data, data[2])
