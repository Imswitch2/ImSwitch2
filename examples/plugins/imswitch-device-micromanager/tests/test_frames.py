"""Frame normalisation (``_frames``): dtype from PixelType, shape checks, colour refused. Pure numpy, no MMCore."""
import numpy as np
import pytest

from imswitch_device_micromanager._frames import (
    FrameFormatError,
    UnsupportedPixelTypeError,
    dtype_for_bytes_per_pixel,
    dtype_for_pixel_type,
    dtype_for_pixel_type_property,
    frame_bytes,
    normalize_frame,
)


def _md(pixel_type, h, w):
    return {"PixelType": pixel_type, "Height": str(h), "Width": str(w)}


@pytest.mark.parametrize("pixel_type, dtype", [("GRAY8", np.uint8), ("GRAY16", np.uint16)])
def test_dtype_comes_from_the_pixel_type_tag(pixel_type, dtype):
    assert dtype_for_pixel_type(pixel_type) == np.dtype(dtype)
    img = np.zeros((4, 6), dtype)
    out = normalize_frame(img, _md(pixel_type, 4, 6))
    assert out.shape == (4, 6) and out.dtype == np.dtype(dtype)


def test_single_component_third_axis_is_squeezed():
    out = normalize_frame(np.zeros((4, 6, 1), np.uint16), _md("GRAY16", 4, 6))
    assert out.shape == (4, 6)


@pytest.mark.parametrize("pixel_type", ["RGB32", "RGB64", "GRAY32", "32bit", ""])
def test_colour_and_unknown_pixel_types_are_refused(pixel_type):
    with pytest.raises(UnsupportedPixelTypeError):
        dtype_for_pixel_type(pixel_type)


def test_four_component_frame_is_refused():
    with pytest.raises(UnsupportedPixelTypeError, match="mono"):
        normalize_frame(np.zeros((4, 6, 3), np.uint8), _md("RGB32", 4, 6))


def test_array_contradicting_its_metadata_is_not_converted_silently():
    with pytest.raises(FrameFormatError, match="uint16"):
        normalize_frame(np.zeros((4, 6), np.uint8), _md("GRAY16", 4, 6))
    with pytest.raises(FrameFormatError, match="Width"):
        normalize_frame(np.zeros((4, 6), np.uint16), _md("GRAY16", 4, 7))
    with pytest.raises(FrameFormatError, match="PixelType"):
        normalize_frame(np.zeros((4, 6), np.uint16), {})


def test_bytes_per_pixel_before_the_first_frame():
    assert dtype_for_bytes_per_pixel(1) == np.uint8
    assert dtype_for_bytes_per_pixel(2) == np.uint16
    with pytest.raises(UnsupportedPixelTypeError):
        dtype_for_bytes_per_pixel(4, n_components=4)
    with pytest.raises(UnsupportedPixelTypeError):
        dtype_for_bytes_per_pixel(8)


def test_frame_bytes():
    assert frame_bytes((512, 512), np.uint16) == 512 * 512 * 2
    assert frame_bytes((3, 5), "uint8") == 15


def test_real_metadata_from_mmcore_round_trips(adapter_dir):
    from pymmcore_plus import CMMCorePlus

    core = CMMCorePlus(adapter_paths=[adapter_dir])
    core.loadSystemConfiguration("MMConfig_demo.cfg")
    try:
        core.snapImage()
        core.startSequenceAcquisition("Camera", 1, 0, True)
        from mm_helpers import wait_until
        wait_until(lambda: core.getRemainingImageCount(), message="one frame")
        img, md = core.popNextImageAndMD()
        out = normalize_frame(img, md)
        assert out.shape == (int(md["Height"]), int(md["Width"]))
        assert out.dtype == dtype_for_pixel_type(md["PixelType"])
    finally:
        core.unloadAllDevices()


@pytest.mark.parametrize("text, dtype", [
    ("8bit", np.uint8), ("16bit", np.uint16), ("8-bit", np.uint8), (" 16 bit ", np.uint16),
    ("GRAY8", np.uint8), ("gray16", np.uint16),
])
def test_a_pixel_type_property_value_selects_a_dtype(text, dtype):
    assert dtype_for_pixel_type_property(text) == np.dtype(dtype)


@pytest.mark.parametrize("text", ["32bitRGB", "64bitRGB", "32bit", "12bit", "Mono", "", "RGB32"])
def test_a_pixel_type_value_that_cannot_be_predicted_is_none(text):
    assert dtype_for_pixel_type_property(text) is None
