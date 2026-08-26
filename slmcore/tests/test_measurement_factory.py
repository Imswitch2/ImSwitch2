import numpy as np
import pytest

from slmcore.measurement import create_image_measurement


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
