"""Tests for channel/color processors."""

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.channel_merge import (
    ChannelMergeProcessor,
    can_merge_results,
)
from imswitch.improcess.processors.make_composite import (
    CompositeResult,
    MakeCompositeProcessor,
)
from imswitch.improcess.processors.make_rgb import MakeRGBProcessor, RGBResult


def _result(data, labels):
    return ArrayProcessingResult(
        name="source",
        data=np.asarray(data, dtype=np.float32),
        axis_labels=list(labels),
    )


def test_make_composite_registered():
    assert "make-composite" in available_processor_ids()


def test_make_rgb_registered():
    assert "make-rgb" in available_processor_ids()


def test_channel_merge_registered():
    assert "channel-merge" in available_processor_ids()


def test_make_composite_creates_colored_display_layers_from_c_axis():
    data = np.arange(3 * 2 * 4, dtype=np.float32).reshape(3, 2, 4)
    result = _result(data, ["C", "Y", "X"])

    composite = MakeCompositeProcessor().apply(result, {"axis": "Auto"})
    layers = composite.display_layers()

    assert isinstance(composite, CompositeResult)
    assert composite.name == "source (composite)"
    assert [layer.metadata["component"] for layer in layers] == ["C_0", "C_1", "C_2"]
    assert [layer.colormap for layer in layers] == ["red", "green", "blue"]
    assert [layer.axis_labels for layer in layers] == [["Y", "X"]] * 3
    np.testing.assert_array_equal(layers[2].data, data[2])


def test_make_composite_preserves_per_layer_lut_overrides():
    data = np.arange(3 * 2 * 4, dtype=np.float32).reshape(3, 2, 4)
    result = _result(data, ["C", "Y", "X"])

    composite = MakeCompositeProcessor().apply(result, {"axis": "Auto"})
    composite.setDisplayLayerColormap("C_1", "magenta")
    composite.setDisplayLayerLevels("C_1", (2.0, 5.0))

    layers = composite.applyDisplayLayerSettings(composite.display_layers())
    assert layers[1].colormap == "magenta"
    assert layers[1].display_levels == (2.0, 5.0)


def test_make_composite_uses_base_axis():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    result = _result(data, ["Base", "Y", "X"])

    composite = MakeCompositeProcessor().apply(result, {"axis": "Auto"})

    assert composite.channel_axis == 0
    assert len(composite.display_layers()) == 2


def test_make_rgb_creates_channel_last_uint8_result():
    data = np.zeros((3, 2, 2), dtype=np.float32)
    data[0] = np.array([[0, 1], [2, 3]], dtype=np.float32)
    data[1] = np.array([[10, 20], [30, 40]], dtype=np.float32)
    data[2] = np.array([[5, 5], [10, 15]], dtype=np.float32)
    result = _result(data, ["C", "Y", "X"])

    rgb = MakeRGBProcessor().apply(result, {"axis": "Auto"})

    assert isinstance(rgb, RGBResult)
    assert rgb.name == "source (RGB)"
    assert rgb.data.dtype == np.uint8
    assert rgb.data.shape == (2, 2, 3)
    assert rgb.axis_labels == ["Y", "X", "RGB"]
    assert rgb.data[..., 0].max() == 255
    assert rgb.data[..., 1].max() == 255
    layers = rgb.display_layers()
    assert len(layers) == 1
    assert layers[0].rgb is True
    assert layers[0].axis_labels == ["Y", "X"]


def test_make_rgb_fills_missing_blue_channel_with_zero():
    data = np.arange(2 * 2 * 3, dtype=np.float32).reshape(2, 2, 3)
    result = _result(data, ["C", "Y", "X"])

    rgb = MakeRGBProcessor().apply(result, {"axis": "Auto"})

    assert np.all(rgb.data[..., 2] == 0)


def test_channel_merge_combines_compatible_results_on_c_axis():
    red = _result(np.full((2, 3), 1.0, dtype=np.float32), ["Y", "X"])
    green = _result(np.full((2, 3), 2.0, dtype=np.float32), ["Y", "X"])
    green.name = "green"

    merged = ChannelMergeProcessor().apply(
        red,
        {"results": [red, green], "name": "merged"},
    )

    assert merged.name == "merged"
    assert merged.axis_labels == ["C", "Y", "X"]
    assert merged.data.shape == (2, 2, 3)
    assert merged.metadata["source_results"] == ["source", "green"]
    np.testing.assert_array_equal(merged.data[1], green.data)


def test_channel_merge_compatibility_uses_shape_and_labels():
    first = _result(np.zeros((2, 3), dtype=np.float32), ["Y", "X"])
    second = _result(np.zeros((2, 3), dtype=np.float32), ["Y", "X"])
    mismatched = _result(np.zeros((2, 3), dtype=np.float32), ["Z", "X"])
    scaled = _result(np.zeros((2, 3), dtype=np.float32), ["Y", "X"])
    scaled.axis_scales = [2.0, 1.0]

    assert can_merge_results([first, second])
    assert not can_merge_results([first, mismatched])
    assert not can_merge_results([first, scaled])
