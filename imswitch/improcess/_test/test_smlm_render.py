"""Phase 3 tests: pure-numpy SMLM rendering + the render processor."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_render import (
    FWHM_TO_SIGMA,
    RenderGrid,
    render_xy,
    render_xyz,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import (
    empty_localizations,
    localizations_from_columns,
)
from imswitch.improcess.processors.smlm_render import SmlmRenderProcessor


# -- render_xy --------------------------------------------------------------


def test_render_xy_histogram_counts_land_in_right_bins():
    x = np.array([0.0, 0.0, 100.0])
    y = np.array([0.0, 0.0, 100.0])
    image, grid = render_xy(x, y, pixel_size_nm=10.0, render_type="histogram")
    assert isinstance(grid, RenderGrid)
    assert image.dtype == np.float32
    # Two localizations at the origin bin, one at the far corner.
    assert image[0, 0] == pytest.approx(2.0)
    assert image[-1, -1] == pytest.approx(1.0)
    assert image.sum() == pytest.approx(3.0)


def test_render_xy_histogram_weights():
    x = np.array([0.0, 0.0])
    y = np.array([0.0, 0.0])
    image, _ = render_xy(
        x, y, pixel_size_nm=10.0, render_type="histogram", weights=np.array([3.0, 4.0])
    )
    assert image.sum() == pytest.approx(7.0)


def test_render_xy_grid_scale_and_shape():
    x = np.array([0.0, 50.0])
    y = np.array([0.0, 30.0])
    image, grid = render_xy(x, y, pixel_size_nm=5.0)
    assert grid.pixel_size_nm == (5.0, 5.0)
    assert grid.shape == image.shape
    # width covers 50 nm / 5 = 10 bins; height 30/5 = 6.
    assert image.shape == (6, 10)


def test_render_xy_gaussian_is_mass_preserving_and_smooth():
    # Single centred emitter, generous canvas so the kernel isn't clipped.
    x = np.array([500.0])
    y = np.array([500.0])
    image, _ = render_xy(
        x, y, pixel_size_nm=10.0, render_type="fixed_gaussian", fwhm_nm=60.0,
        bounds=(0.0, 1000.0, 0.0, 1000.0),
    )
    assert image.sum() == pytest.approx(1.0, rel=1e-2)  # unit mass per point
    # Smooth: the peak has non-zero neighbours (not a single spike).
    peak = np.unravel_index(np.argmax(image), image.shape)
    assert image[peak] > 0
    assert image[peak[0], peak[1] + 1] > 0


def test_render_xy_gaussian_sigma_matches_fwhm():
    x = np.array([500.0])
    y = np.array([500.0])
    fwhm_nm = 100.0
    pixel = 5.0
    image, _ = render_xy(
        x, y, pixel_size_nm=pixel, render_type="fixed_gaussian", fwhm_nm=fwhm_nm,
        bounds=(0.0, 1000.0, 0.0, 1000.0),
    )
    # Estimate sigma from the second moment of the row through the peak.
    row = image.sum(axis=0)
    coords = np.arange(row.size) * pixel
    centre = np.sum(coords * row) / np.sum(row)
    sigma_est = np.sqrt(np.sum((coords - centre) ** 2 * row) / np.sum(row))
    expected_sigma = fwhm_nm / FWHM_TO_SIGMA
    assert sigma_est == pytest.approx(expected_sigma, rel=0.1)


def test_render_xy_empty_is_blank():
    image, grid = render_xy(np.array([]), np.array([]), pixel_size_nm=10.0)
    assert image.sum() == 0.0
    assert image.shape == grid.shape


def test_render_xy_rejects_bad_pixel_size():
    with pytest.raises(ValueError):
        render_xy(np.array([1.0]), np.array([1.0]), pixel_size_nm=0.0)


def test_render_xy_unknown_type():
    with pytest.raises(ValueError):
        render_xy(np.array([1.0]), np.array([1.0]), pixel_size_nm=10.0, render_type="x")


# -- render_xyz -------------------------------------------------------------


def test_render_xyz_histogram_shape_and_binning():
    x = np.array([0.0, 100.0])
    y = np.array([0.0, 50.0])
    z = np.array([0.0, 200.0])
    volume, grid = render_xyz(
        x, y, z, pixel_size_nm=10.0, z_pixel_size_nm=20.0, render_type="histogram"
    )
    assert volume.ndim == 3
    assert grid.pixel_size_nm == (20.0, 10.0, 10.0)
    # depth 200/20=10, height 50/10=5, width 100/10=10
    assert volume.shape == (10, 5, 10)
    assert volume.sum() == pytest.approx(2.0)


def test_render_xyz_gaussian_mass_preserving():
    x = np.array([500.0])
    y = np.array([500.0])
    z = np.array([500.0])
    volume, _ = render_xyz(
        x, y, z, pixel_size_nm=20.0, z_pixel_size_nm=20.0,
        render_type="fixed_gaussian", fwhm_nm=80.0,
        bounds=(0.0, 1000.0, 0.0, 1000.0, 0.0, 1000.0),
    )
    assert volume.sum() == pytest.approx(1.0, rel=2e-2)


# -- processor --------------------------------------------------------------


def _result(count=50, with_z=False, pixel_size_nm=100.0):
    rng = np.random.default_rng(1)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 5000, count),
        "y_nm": rng.uniform(0, 5000, count),
        "photons": rng.uniform(500, 1500, count),
    }
    if with_z:
        columns["z_nm"] = rng.uniform(-500, 500, count)
    locs = localizations_from_columns(columns)
    dims = "3D" if with_z else "2D"
    return LocalizationResult("locs", locs, pixel_size_nm=pixel_size_nm, dims=dims)


def test_render_processor_applies_only_to_localization_result():
    processor = SmlmRenderProcessor()
    assert processor.applies_to(_result())
    other = ArrayProcessingResult("img", np.zeros((4, 4)), ["Y", "X"])
    assert not processor.applies_to(other)


def test_render_processor_produces_image_result_with_nm_scale():
    processor = SmlmRenderProcessor()
    result = _result()
    params = {"render_type": "histogram", "pixel_size_nm": 10.0, "fwhm_nm": 20.0}
    out = processor.apply(result, params)
    assert isinstance(out, ArrayProcessingResult)
    assert out.data.ndim == 2
    assert out.axis_labels == ["Y", "X"]
    assert out.scale_unit == "nm"
    assert out.axis_scales == [10.0, 10.0]
    assert np.asarray(out.data).sum() == pytest.approx(result.count)


def test_render_processor_3d_volume():
    processor = SmlmRenderProcessor()
    result = _result(with_z=True)
    params = {
        "render_type": "histogram", "pixel_size_nm": 20.0,
        "render_3d": True, "z_pixel_size_nm": 40.0,
    }
    out = processor.apply(result, params)
    assert out.data.ndim == 3
    assert out.axis_labels == ["Z", "Y", "X"]
    assert out.axis_scales[0] == 40.0


def test_render_processor_2d_result_ignores_3d_flag():
    processor = SmlmRenderProcessor()
    result = _result(with_z=False)
    out = processor.apply(result, {"render_3d": True, "pixel_size_nm": 10.0})
    assert out.data.ndim == 2  # 2D source can't render a volume


def test_render_processor_empty_localizations():
    processor = SmlmRenderProcessor()
    result = LocalizationResult("empty", empty_localizations(0), pixel_size_nm=100.0)
    out = processor.apply(result, {"pixel_size_nm": 10.0})
    assert np.asarray(out.data).sum() == 0.0


def test_render_processor_rejects_wrong_input():
    processor = SmlmRenderProcessor()
    other = ArrayProcessingResult("img", np.zeros((4, 4)), ["Y", "X"])
    with pytest.raises(TypeError):
        processor.apply(other, {})
