"""Tests for channel/color processors."""

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.make_composite import (
    CompositeResult,
    MakeCompositeProcessor,
)


def _result(data, labels):
    return ArrayProcessingResult(
        name="source",
        data=np.asarray(data, dtype=np.float32),
        axis_labels=list(labels),
    )


def test_make_composite_registered():
    assert "make-composite" in available_processor_ids()


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
