"""Phase-3 standard image operations (ImageJ parity)."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import result_kind
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.background import subtract_background
from imswitch.improcess.processors.filters import FilterProcessor, apply_filter
from imswitch.improcess.processors.image_calculator import (
    ImageCalculatorProcessor,
    calculate_results,
)
from imswitch.improcess.processors.label_morphology import (
    LabelMorphologyProcessor,
    apply_morphology,
)
from imswitch.improcess.processors.math_ops import MathProcessor, apply_math
from imswitch.improcess.processors.scale_type import (
    ConvertTypeProcessor,
    ResizeProcessor,
    convert_type,
)
from imswitch.improcess.processors.transform import (
    TransformProcessor,
    apply_transform,
)
from imswitch.improcess.view.ImageCalculatorDialog import ImageCalculatorDialog


def _image(shape, labels, name="img", scales=None, data=None):
    if data is None:
        data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=list(labels),
        axis_scales=list(scales) if scales else None,
        scale_unit="um" if scales else "px",
    )


def test_all_new_processors_are_registered():
    ids = available_processor_ids()
    for processor_id in (
        "image-calculator",
        "math",
        "filter",
        "transform",
        "resize",
        "convert-type",
        "subtract-background",
        "label-morphology",
    ):
        assert processor_id in ids


# -- image calculator ---------------------------------------------------------

def test_calculator_subtract_produces_float_difference():
    a = _image((6, 8), ["Y", "X"], "a")
    b = _image((6, 8), ["Y", "X"], "b")
    b.data = np.ones_like(a.data)

    out = calculate_results(a, b, operation="subtract")

    assert out.data.dtype == np.float32
    np.testing.assert_array_equal(out.data, a.data - 1.0)
    assert out.name == "a subtract b"
    assert out.metadata["calculator_operation"] == "subtract"


def test_calculator_divide_by_zero_yields_zero_not_inf():
    a = _image((4, 4), ["Y", "X"], "a")
    b = _image((4, 4), ["Y", "X"], "b", data=np.zeros((4, 4), np.float32))

    out = calculate_results(a, b, operation="divide")

    assert np.all(np.isfinite(out.data))
    np.testing.assert_array_equal(out.data, np.zeros((4, 4), np.float32))


def test_calculator_rejects_incompatible_and_wrong_arity():
    a = _image((6, 8), ["Y", "X"], "a")
    with pytest.raises(ValueError):
        calculate_results(a, _image((6, 9), ["Y", "X"], "b"))
    with pytest.raises(ValueError):
        calculate_results(a, _image((6, 8), ["Y", "X"], "b"), operation="modulo")
    with pytest.raises(ValueError):
        ImageCalculatorProcessor().apply(a, {"results": [a]})


def test_calculator_processor_applies_via_params():
    a = _image((6, 8), ["Y", "X"], "a")
    b = _image((6, 8), ["Y", "X"], "b")

    out = ImageCalculatorProcessor().apply(a, {"results": [a, b], "operation": "average"})
    np.testing.assert_allclose(out.data, (a.data + b.data) / 2.0)


# -- math ---------------------------------------------------------------------

def test_math_constant_and_unary_operations():
    data = np.array([[1.0, 4.0], [9.0, 16.0]], np.float32)

    np.testing.assert_allclose(apply_math(data, "add", 2), data + 2)
    np.testing.assert_allclose(apply_math(data, "multiply", 3), data * 3)
    np.testing.assert_allclose(apply_math(data, "square-root"), np.sqrt(data))
    # Range-preserving inversion: min and max swap places.
    inverted = apply_math(data, "invert")
    assert inverted.max() == data.max() and inverted.min() == data.min()
    np.testing.assert_allclose(inverted, (16.0 + 1.0) - data)
    # Log maps non-positive pixels to zero.
    with_zero = np.array([[0.0, np.e]], np.float32)
    np.testing.assert_allclose(apply_math(with_zero, "log"), [[0.0, 1.0]], atol=1e-6)
    with pytest.raises(ValueError):
        apply_math(data, "divide", 0)
    with pytest.raises(ValueError):
        apply_math(data, "modulo")


def test_math_gamma_preserves_range():
    data = np.array([[0.0, 50.0, 100.0]], np.float32)
    out = apply_math(data, "gamma", 2.0)
    assert out.min() == 0.0 and out.max() == 100.0
    assert out[0, 1] < 50.0  # gamma > 1 darkens midtones


def test_math_processor_applies_and_propagates_metadata():
    result = _image((4, 4), ["Y", "X"], scales=[0.5, 0.5])
    out = MathProcessor().apply(result, {"operation": "add", "value": 5})
    np.testing.assert_allclose(out.data, result.data + 5)
    assert out.axis_scales == [0.5, 0.5] and out.scale_unit == "um"


# -- filters --------------------------------------------------------------------

def test_gaussian_filter_only_touches_the_spatial_plane():
    stack = np.zeros((2, 9, 9), np.float32)
    stack[0, 4, 4] = 1.0

    out = apply_filter(stack, "gaussian", radius=1.0, spatial_axes=(1, 2))

    # Plane 1 stays empty: no bleed across the leading (Z) axis.
    assert np.all(out[1] == 0)
    assert 0 < out[0, 4, 4] < 1.0
    np.testing.assert_allclose(out[0].sum(), 1.0, rtol=1e-3)


def test_median_filter_removes_hot_pixel():
    image = np.ones((7, 7), np.float32)
    image[3, 3] = 100.0
    out = apply_filter(image, "median", radius=1.0)
    assert out[3, 3] == 1.0


def test_unsharp_increases_edge_contrast():
    image = np.zeros((8, 8), np.float32)
    image[:, 4:] = 10.0
    out = apply_filter(image, "unsharp", radius=1.0, amount=0.6)
    assert np.ptp(out) > np.ptp(image)


def test_filter_processor_uses_axis_labels_for_spatial_plane():
    stack = np.zeros((2, 9, 9), np.float32)
    stack[0, 4, 4] = 1.0
    result = _image((2, 9, 9), ["T", "Y", "X"], data=stack)
    out = FilterProcessor().apply(result, {"method": "gaussian", "radius": 1.0})
    assert np.all(np.asarray(out.data)[1] == 0)


# -- transform ---------------------------------------------------------------------

def test_rotate_90_cw_matches_expectation():
    image = np.array([[1, 2], [3, 4]], np.float32)
    np.testing.assert_array_equal(
        apply_transform(image, "rotate-90-cw"), [[3, 1], [4, 2]]
    )
    np.testing.assert_array_equal(
        apply_transform(image, "rotate-90-ccw"), [[2, 4], [1, 3]]
    )
    np.testing.assert_array_equal(
        apply_transform(image, "flip-horizontal"), [[2, 1], [4, 3]]
    )
    np.testing.assert_array_equal(
        apply_transform(image, "flip-vertical"), [[3, 4], [1, 2]]
    )


def test_rotation_swaps_spatial_extents_and_scales():
    result = _image((2, 4, 6), ["Z", "Y", "X"], scales=[1.0, 0.2, 0.4])
    out = TransformProcessor().apply(result, {"operation": "rotate-90-cw"})
    assert np.asarray(out.data).shape == (2, 6, 4)
    assert out.axis_labels == ["Z", "Y", "X"]
    assert out.axis_scales == [1.0, 0.4, 0.2]  # Y/X pitches travel with extents


# -- resize + type conversion --------------------------------------------------------

def test_resize_halves_spatial_plane_and_doubles_pixel_pitch():
    result = _image((2, 8, 8), ["Z", "Y", "X"], scales=[1.0, 0.5, 0.5])
    out = ResizeProcessor().apply(result, {"factor": 0.5, "interpolation": "bilinear"})
    assert np.asarray(out.data).shape == (2, 4, 4)
    assert out.axis_scales == [1.0, 1.0, 1.0]  # half the pixels, double the pitch


def test_convert_type_rescales_to_integer_range():
    data = np.array([[0.0, 0.5, 1.0]], np.float32)
    out = convert_type(data, "8-bit", rescale=True)
    assert out.dtype == np.uint8
    np.testing.assert_array_equal(out, [[0, 127, 255]])
    clipped = convert_type(np.array([[-5.0, 300.0]]), "8-bit", rescale=False)
    np.testing.assert_array_equal(clipped, [[0, 255]])
    assert convert_type(data, "32-bit float").dtype == np.float32
    with pytest.raises(ValueError):
        convert_type(data, "64-bit")


def test_convert_type_processor_applies():
    result = _image((4, 4), ["Y", "X"])
    out = ConvertTypeProcessor().apply(result, {"type": "16-bit", "rescale": True})
    assert np.asarray(out.data).dtype == np.uint16
    assert np.asarray(out.data).max() == np.iinfo(np.uint16).max


# -- background subtraction ------------------------------------------------------------

def test_rolling_ball_removes_smooth_background():
    yy, xx = np.mgrid[0:32, 0:32].astype(np.float32)
    background = 0.05 * (yy + xx)
    image = background.copy()
    image[16, 16] += 50.0  # a bright point on a smooth ramp

    subtracted, estimated = subtract_background(image, radius=5.0)

    # The peak survives; the ramp is mostly gone away from the peak.
    assert subtracted[16, 16] > 40.0
    corner_residual = abs(subtracted[2, 2]) + abs(subtracted[30, 30])
    assert corner_residual < 1.0
    assert estimated.shape == image.shape


# -- label morphology --------------------------------------------------------------------

def _blob_labels():
    labels = np.zeros((16, 16), np.int32)
    labels[2:8, 2:8] = 1
    labels[4, 4] = 0  # a hole
    labels[10:14, 10:14] = 2
    return labels


def test_fill_holes_and_relabels():
    out = apply_morphology(_blob_labels(), "fill-holes")
    assert out[4, 4] > 0
    assert set(np.unique(out)) == {0, 1, 2}


def test_erode_shrinks_objects():
    out = apply_morphology(_blob_labels(), "erode", radius=1)
    assert (out > 0).sum() < (_blob_labels() > 0).sum()


def test_watershed_splits_touching_objects():
    labels = np.zeros((16, 32), np.int32)
    # Two circles fused into one connected blob.
    yy, xx = np.mgrid[0:16, 0:32]
    labels[((yy - 8) ** 2 + (xx - 10) ** 2) <= 36] = 1
    labels[((yy - 8) ** 2 + (xx - 21) ** 2) <= 36] = 1
    assert apply_morphology(labels, "fill-holes").max() == 1  # currently one object

    out = apply_morphology(labels, "watershed-split", radius=3)
    assert out.max() >= 2  # split into (at least) the two circles


def test_morphology_gates_on_labels_kind():
    processor = LabelMorphologyProcessor()
    image = _image((8, 8), ["Y", "X"])
    assert not processor.accepts(image)  # kind "image" is rejected

    class _Labels(ArrayProcessingResult):
        kind = "labels"

    labels = _Labels(name="l", data=_blob_labels(), axis_labels=["Y", "X"])
    assert processor.accepts(labels)
    out = processor.apply(labels, {"operation": "fill-holes"})
    assert result_kind(out) == "labels"
    assert out.display_layers()[0].kind == "labels"


# -- calculator dialog ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_calculator_dialog_defaults_and_params(qapp):
    a = _image((6, 8), ["Y", "X"], "a")
    b = _image((6, 8), ["Y", "X"], "b")
    dialog = ImageCalculatorDialog([a, b], active_result=b)

    params = dialog.selected_params()
    assert params["results"][0] is b  # active result preselected as A
    assert params["operation"] == "add"
    assert params["float32"] is True
    assert dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_calculator_dialog_disables_ok_with_reason_for_mismatch(qapp):
    a = _image((6, 8), ["Y", "X"], "a")
    b = _image((6, 9), ["Y", "X"], "b")
    dialog = ImageCalculatorDialog([a, b])

    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert dialog.statusLabel.text()
