import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("napari")

from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget  # noqa: E402


def test_plane_affine_is_embedded_in_trailing_axes_of_3d_layer():
    plane = np.array(
        [[1.0, 0.2, 4.0], [-0.1, 0.9, -3.0], [0.0, 0.0, 1.0]]
    )

    result = ImageWidget._embedPlaneAffine(plane, 3)

    expected = np.eye(4)
    expected[1:3, 1:3] = plane[:2, :2]
    expected[1:3, 3] = plane[:2, 2]
    np.testing.assert_allclose(result, expected)


def test_plane_affine_is_unchanged_for_2d_layer():
    plane = np.array(
        [[1.0, 0.2, 4.0], [-0.1, 0.9, -3.0], [0.0, 0.0, 1.0]]
    )
    np.testing.assert_allclose(ImageWidget._embedPlaneAffine(plane, 2), plane)
