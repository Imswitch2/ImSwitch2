import numpy as np
import pytest

from slmcore.core.measurement import ImageMeasurement,create_image_measurement


def test_create_image_measurement_normalizes_rgb_to_2d_float():
    rgb = np.zeros((3,4,3),dtype=np.uint8)
    rgb[...,0] = 3
    measurement = create_image_measurement(
        rgb,source="detector",detector="cam",metadata={"round":1},
    )

    assert measurement.image.shape == (3,4)
    assert measurement.image.dtype == np.float64
    assert np.allclose(measurement.image,1.0)
    assert measurement.detector == "cam"
    assert measurement.metadata["round"] == 1


def test_create_image_measurement_rejects_non_image_shape():
    with pytest.raises(ValueError,match="must be 2D"):
        create_image_measurement(np.zeros((4,)),source="file")


def test_loaded_external_measurement_requires_no_live_detector_identity():
    measurement = create_image_measurement(
        np.arange(12, dtype=np.float64).reshape(3, 4),
        source="loaded_file",
        metadata={"path": "offline/example.tif"},
    )

    assert measurement.source == "loaded_file"
    assert measurement.detector is None
    assert measurement.metadata["path"] == "offline/example.tif"
    np.testing.assert_array_equal(
        measurement.image, np.arange(12, dtype=np.float64).reshape(3, 4),
    )


def test_measurement_pixel_size_is_optional_and_validated():
    measurement = create_image_measurement(
        np.zeros((3,3)),source="file",pixel_size_um=0.123,
    )
    assert measurement.pixel_size_um == pytest.approx(0.123)
    assert measurement.detector is None
    assert ImageMeasurement(np.zeros((2,2))).pixel_size_um is None
    for value in (0,-1,float("nan"),float("inf")):
        with pytest.raises(ValueError,match="positive finite"):
            ImageMeasurement(np.zeros((2,2)),pixel_size_um=value)
