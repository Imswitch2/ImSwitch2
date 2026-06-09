import numpy as np
import pytest

from imswitch.improcess.analysis.roi_stats import compute_roi_stats


def test_compute_roi_stats_full_image_ignores_nan():
    image = np.array([[1, 2, np.nan], [4, 5, 6]], dtype=float)

    stats = compute_roi_stats(image)

    assert stats.area_pixels == 6
    assert stats.finite_pixels == 5
    assert stats.mean == pytest.approx(3.6)
    assert stats.median == pytest.approx(4.0)
    assert stats.minimum == pytest.approx(1.0)
    assert stats.maximum == pytest.approx(6.0)
    assert stats.total == pytest.approx(18.0)


def test_compute_roi_stats_rectangle_clips_to_image():
    image = np.arange(25, dtype=float).reshape(5, 5)

    stats = compute_roi_stats(image, roi=(-2, 3, 1, 4))

    expected = image[0:3, 1:4]
    assert stats.area_pixels == expected.size
    assert stats.mean == pytest.approx(float(expected.mean()))
    assert stats.total == pytest.approx(float(expected.sum()))


def test_compute_roi_stats_rejects_empty_roi():
    image = np.ones((5, 5), dtype=float)

    with pytest.raises(ValueError, match="ROI is empty"):
        compute_roi_stats(image, roi=(1, 1, 2, 3))
