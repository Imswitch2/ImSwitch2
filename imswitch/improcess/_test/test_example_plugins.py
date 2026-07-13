"""The shipped example drop-in plugins load and apply correctly.

Guards examples/improcess_plugins/ so the reference plugins we tell users to
copy never drift from the Processor contract.
"""

from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model import ArrayProcessingResult, result_kind
from imswitch.improcess.plugins.user_plugins import discover_processor_plugins
from imswitch.improcess.processors.base import Processor

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "improcess_plugins"


def _image(shape=(6, 8)):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(name="img", data=data, axis_labels=["Y", "X"])


def test_examples_directory_exists():
    assert _EXAMPLES_DIR.is_dir()
    assert (_EXAMPLES_DIR / "invert.py").exists()
    assert (_EXAMPLES_DIR / "gaussian_blur.py").exists()


def test_example_plugins_are_discoverable_processors():
    classes, errors = discover_processor_plugins(str(_EXAMPLES_DIR))

    assert errors == []
    assert set(classes) == {"example.invert", "example.gaussian-blur"}
    for processor_cls in classes.values():
        assert issubclass(processor_cls, Processor)
        assert processor_cls.kinds == ("image",)


def test_invert_example_applies():
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))
    processor = classes["example.invert"]()
    source = _image()

    out = processor.apply(source, {})

    assert result_kind(out) == "image"
    assert out.data.shape == source.data.shape
    np.testing.assert_array_equal(out.data, source.data.max() - source.data)
    # Gating: accepts an image, rejects a non-image (table) kind.
    assert processor.accepts(source)


def test_gaussian_blur_example_applies():
    pytest.importorskip("scipy")
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))
    processor = classes["example.gaussian-blur"]()
    source = _image((16, 16))

    out = processor.apply(source, {"sigma": 2.0})

    assert out.data.shape == source.data.shape
    assert out.data.dtype == source.data.dtype
    # Blurring reduces the dynamic range of a ramp image.
    assert np.ptp(out.data) < np.ptp(source.data)


def test_example_plugins_reject_non_image_kinds():
    """The example image processors must not offer themselves on a table."""
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))

    class _Table(ArrayProcessingResult):
        kind = "table"

    table = _Table(name="t", data=np.zeros((2, 3), np.float32), axis_labels=["ROI", "M"])
    for processor_cls in classes.values():
        assert not processor_cls().accepts(table)
