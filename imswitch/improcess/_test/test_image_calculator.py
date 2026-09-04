"""Image calculator: what it will combine, and what it still refuses.

Written after a rig report: applying an ROI mask to the image it was drawn on
was refused because one array was ``(1, 233, 233)`` and the other
``(233, 233)`` — the same picture, and something numpy lines up without
ambiguity. The calculator had been reusing the *stacking* compatibility check,
which is stricter for a good reason that does not apply to arithmetic.
"""

import numpy as np
import pytest

from imswitch.improcess.processors.combine import elementwise_compatibility
from imswitch.improcess.processors.image_calculator.processor import (
    calculate_results,
)

# --------------------------------------------------------------------------
# rig feedback: applying an ROI mask to the image it was drawn on
# --------------------------------------------------------------------------

def _result(shape, labels, scales=None, unit="px", name="r", kind="image"):
    from imswitch.improcess.model.result import ProcessingResult

    class _R(ProcessingResult):
        def save(self, path, fmt="tiff"):
            pass

    made = _R(
        name=name,
        data=np.zeros(shape, dtype=np.float32),
        axis_labels=list(labels),
        axis_scales=list(scales or [1.0] * len(shape)),
        scale_unit=unit,
    )
    made.kind = kind
    return made


def test_a_2d_mask_can_be_applied_to_a_1_by_n_by_n_image():
    """The reported case: (1, 233, 233) and (233, 233) are the same picture,
    and numpy lines them up without ambiguity."""
    ok, reason = elementwise_compatibility([
        _result((1, 233, 233), "ZYX", name="image"),
        _result((233, 233), "YX", name="mask"),
    ])
    assert ok, reason


def test_a_mask_broadcasts_across_a_stack():
    """One mask applied to every slice is what a mask on a stack means."""
    ok, _reason = elementwise_compatibility([
        _result((5, 233, 233), "ZYX"), _result((233, 233), "YX"),
    ])
    assert ok


def test_the_arithmetic_agrees_with_what_the_gate_allowed():
    """If the offer said yes, the run must not then say no."""
    image = _result((1, 8, 8), "ZYX", name="image")
    image.data = np.full((1, 8, 8), 7.0, dtype=np.float32)
    mask = _result((8, 8), "YX", name="mask", kind="labels")
    mask.data = np.zeros((8, 8), dtype=np.float32)
    mask.data[2:5, 2:5] = 1.0

    out = calculate_results(image, mask, operation="multiply")
    assert out.data.shape == (1, 8, 8)
    assert out.data[0, 3, 3] == pytest.approx(7.0)
    assert out.data[0, 0, 0] == pytest.approx(0.0)


def test_a_label_image_is_offered_to_the_calculator():
    """Multiplying by a label image is what a mask is; the kinds gate exists
    to keep a metrics table away, not a mask."""
    from imswitch.improcess.processors.image_calculator.processor import (
        ImageCalculatorProcessor,
    )

    processor = ImageCalculatorProcessor()
    assert processor.accepts(_result((8, 8), "YX", kind="labels"))
    assert processor.accepts(_result((8, 8), "YX", kind="image"))


# -- and what stays refused -------------------------------------------------

def test_a_row_and_a_column_are_not_an_image():
    """numpy would happily broadcast them into something neither contains."""
    ok, reason = elementwise_compatibility([
        _result((233, 1), "YX"), _result((1, 233), "YX"),
    ])
    assert not ok
    assert "lined up" in reason


def test_genuinely_different_shapes_are_still_refused():
    ok, reason = elementwise_compatibility([
        _result((233, 233), "YX"), _result((100, 100), "YX"),
    ])
    assert not ok
    assert "(100, 100)" in reason


def test_different_planes_are_still_refused():
    ok, reason = elementwise_compatibility([
        _result((233, 233), "YX"), _result((233, 233), "YZ"),
    ])
    assert not ok
    assert "axis" in reason


def test_incompatible_units_are_still_refused():
    """A nm result and a um one are not comparable however well they line up."""
    ok, reason = elementwise_compatibility([
        _result((233, 233), "YX", unit="nm"),
        _result((233, 233), "YX", unit="um"),
    ])
    assert not ok
    assert "unit" in reason


def test_mismatched_pixel_scales_are_still_refused():
    ok, reason = elementwise_compatibility([
        _result((233, 233), "YX", scales=[0.1, 0.1]),
        _result((233, 233), "YX", scales=[0.2, 0.2]),
    ])
    assert not ok
    assert "scale" in reason


def test_stacking_stays_strict():
    """Building one array is a different question from arithmetic on two."""
    from imswitch.improcess.processors.combine import combine_compatibility

    inputs = [_result((1, 233, 233), "ZYX"), _result((233, 233), "YX")]
    ok, reason = combine_compatibility(inputs, mode="stack")
    assert not ok
    assert "does not match" in reason
