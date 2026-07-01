"""Tests for stack/channel split processors."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.channel_split import ChannelSplitProcessor
from imswitch.improcess.processors.stack_split import StackSplitProcessor


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


def test_stack_split_registered():
    assert "stack-split" in available_processor_ids()


def test_channel_split_registered():
    assert "channel-split" in available_processor_ids()


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
