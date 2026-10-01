"""Bead PSF analysis: detection, pre-filtering, fits, selection, bead correction."""

import math

import numpy as np
import pytest

from imswitch.improcess.analysis.bead_psf import (
    FWHM_FACTOR,
    STATUS_BRIGHT,
    STATUS_CROWDED,
    STATUS_SATURATED,
    BeadPSFParams,
    Selection,
    analyze_beads,
    average_psf,
    bead_sigma_nm,
    correct_fwhm_for_bead,
    fit_gaussian_nd,
    focal_surface,
    select_beads,
    summarize,
    theoretical_fwhm_nm,
)


def _positions(n, shape, margin, min_dist, rng):
    points = []
    for _attempt in range(100_000):
        if len(points) == n:
            break
        p = np.array([rng.uniform(margin, s - margin) for s in shape])
        if all(np.linalg.norm(p - q) > min_dist for q in points):
            points.append(p)
    assert len(points) == n, "test field too small for the requested beads"
    return np.array(points)


def _render(shape, centers, sigmas, amps, background):
    grids = np.indices(shape, dtype=float)
    image = np.full(shape, background, dtype=float)
    for c, a in zip(centers, amps):
        image += a * np.exp(-0.5 * sum(((g - ci) / s) ** 2 for g, ci, s in zip(grids, c, sigmas)))
    return image


def _field_2d(sigma_px=2.0, n=50, shape=(400, 400)):
    """50 beads; 0-2 are 3x too bright, 3-4 unresolved doublets (4 px), 5-6 close pairs (9 px)."""
    rng = np.random.default_rng(1)
    centers = _positions(n, shape, 20, 40, rng)
    amps = np.full(n, 1000.0)
    amps[:3] = 3000.0
    partners = np.vstack([centers[3:5] + [0.0, 4.0], centers[5:7] + [0.0, 9.0]])
    image = _render(shape, np.vstack([centers, partners]), (sigma_px, sigma_px),
                    np.concatenate([amps, [1000.0] * 4]), 100.0)
    return rng.poisson(image).astype(np.uint16), centers


PX = 65.0
TRUTH = FWHM_FACTOR * 2.0 * PX


def _params(**kw):
    return BeadPSFParams(pixel_size=(PX, PX), expected_fwhm_nm=(300.0,), **kw)


def test_theory_and_bead_sigma():
    lateral, axial = theoretical_fwhm_nm(1.4, 520, 1.515)
    assert lateral == pytest.approx(189.4, abs=0.5)
    assert axial == pytest.approx(488.9, abs=1.0)
    assert bead_sigma_nm(100, "volume") == pytest.approx(100 / math.sqrt(20))
    assert bead_sigma_nm(100, "shell") == pytest.approx(100 / math.sqrt(12))


def test_fit_gaussian_recovers_anisotropic_spot():
    rng = np.random.default_rng(0)
    image = rng.poisson(_render((25, 25), [(12.3, 11.7)], (2.0, 2.6), [500.0], 50.0)).astype(float)
    fit = fit_gaussian_nd(image)
    assert fit.center == pytest.approx([12.3, 11.7], abs=0.1)
    assert fit.sigma == pytest.approx([2.0, 2.6], rel=0.05)
    assert fit.r2 > 0.95


def test_2d_field_statuses_and_statistics():
    image, centers = _field_2d()
    analysis = analyze_beads(image, _params())
    mask = select_beads(analysis, Selection())
    summary = summarize(analysis, mask, Selection())
    assert summary["stats"]["fwhm_lat"]["median"] == pytest.approx(TRUTH, rel=0.03)
    assert summary["status_counts"][STATUS_BRIGHT] >= 3
    assert summary["status_counts"][STATUS_CROWDED] >= 4
    assert summary["n_selected"] >= 40
    # unresolved doublets come through as one elongated blob and fail the ellipticity cut
    for bead, chosen in zip(analysis.beads, mask):
        for c in centers[3:5] + [0.0, 2.0]:
            if abs(bead["y_det"] - c[0]) < 3 and abs(bead["x_det"] - c[1]) < 5:
                assert not chosen


def test_selection_range_and_average():
    image, _ = _field_2d()
    analysis = analyze_beads(image, _params())
    everything = select_beads(analysis, Selection(fwhm_lat_range=(0, 1e9), max_ellipticity=10, min_r2=0))
    narrow = select_beads(analysis, Selection(fwhm_lat_range=(300.0, 310.0)))
    assert narrow.sum() < everything.sum()
    averaged = average_psf(image, analysis, select_beads(analysis, Selection()))
    assert averaged.fwhm["fwhm_x"] == pytest.approx(TRUTH, rel=0.03)
    assert averaged.fwhm["fwhm_y"] == pytest.approx(TRUTH, rel=0.03)


def test_saturated_bead_flagged():
    image = np.clip(_render((100, 100), [(50, 50)], (2.0, 2.0), [70000.0], 100.0), 0, 65535).astype(np.uint16)
    analysis = analyze_beads(image, _params())
    assert [b["status"] for b in analysis.beads] == [STATUS_SATURATED]


def test_pixel_unit_analysis_skips_physical_extras():
    image, _ = _field_2d()
    analysis = analyze_beads(image, BeadPSFParams(pixel_size=(1.0, 1.0), unit="px", bead_diameter_nm=100.0))
    summary = summarize(analysis, select_beads(analysis, Selection()), Selection())
    assert summary["stats"]["fwhm_lat"]["median"] == pytest.approx(FWHM_FACTOR * 2.0, rel=0.03)
    assert "fwhm_lat_corr" not in summary["stats"]
    assert any("px" in w for w in summary["warnings"])


@pytest.mark.parametrize("mode", ["separable", "full"])
def test_3d_widths(mode):
    rng = np.random.default_rng(2)
    shape, sigma, px = (41, 200, 200), (3.0, 2.0, 2.0), (200.0, 65.0, 65.0)
    lateral = _positions(15, shape[1:], 20, 40, rng)
    centers = np.column_stack([rng.uniform(17, 23, len(lateral)), lateral])
    image = rng.poisson(_render(shape, centers, sigma, np.full(len(lateral), 800.0), 100.0)).astype(np.uint16)
    params = BeadPSFParams(pixel_size=px, expected_fwhm_nm=(300.0, 1400.0), fit_mode_3d=mode)
    analysis = analyze_beads(image, params)
    summary = summarize(analysis, select_beads(analysis, Selection()), Selection())
    assert summary["stats"]["fwhm_lat"]["median"] == pytest.approx(FWHM_FACTOR * 2.0 * 65, rel=0.04)
    assert summary["stats"]["fwhm_z"]["median"] == pytest.approx(FWHM_FACTOR * 3.0 * 200, rel=0.05)
    assert summary["n_selected"] >= 10


def test_focal_surface_recovers_tilt():
    rng = np.random.default_rng(4)
    shape, sigma, px = (41, 300, 300), (3.0, 2.0, 2.0), (100.0, 65.0, 65.0)
    lateral = _positions(16, shape[1:], 20, 40, rng)
    z = 20 + lateral[:, 1] * px[2] * 0.020 / px[0]  # 20 mrad along x
    image = rng.poisson(_render(shape, np.column_stack([z, lateral]), sigma, np.full(16, 800.0), 100.0))
    params = BeadPSFParams(pixel_size=px, expected_fwhm_nm=(300.0, 700.0))
    analysis = analyze_beads(image.astype(np.uint16), params)
    surface = focal_surface(analysis, select_beads(analysis, Selection()))
    assert surface["tilt_x_mrad"] == pytest.approx(20.0, abs=2.0)
    assert abs(surface["tilt_y_mrad"]) < 2.0


def test_bead_size_correction_on_rendered_sphere():
    """Projected volume-labelled sphere (d = 200 nm) blurred by a Gaussian PSF
    (sigma 100 nm): the corrected FWHM recovers the PSF, while subtracting d
    in quadrature over-corrects by more than 10 %."""
    from scipy import ndimage

    step, d, s_psf, n = 10.0, 200.0, 100.0, 161
    y, x = np.indices((n, n), dtype=float)
    r = np.hypot(y - n // 2, x - n // 2) * step
    sphere = np.sqrt(np.clip((d / 2) ** 2 - r**2, 0, None))
    fit = fit_gaussian_nd(ndimage.gaussian_filter(sphere, s_psf / step))
    measured = FWHM_FACTOR * float(fit.sigma.mean()) * step
    assert correct_fwhm_for_bead(measured, d) == pytest.approx(FWHM_FACTOR * s_psf, rel=0.02)
    assert math.sqrt(measured**2 - d**2) < 0.9 * FWHM_FACTOR * s_psf


def test_large_bead_warns():
    image, _ = _field_2d()
    analysis = analyze_beads(image, _params(bead_diameter_nm=500.0))
    summary = summarize(analysis, select_beads(analysis, Selection()), Selection())
    assert any("correction" in w for w in summary["warnings"])
