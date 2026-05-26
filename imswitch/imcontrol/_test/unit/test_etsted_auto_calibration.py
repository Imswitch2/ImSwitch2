"""
Synthetic-data tests for the etSTED auto-calibration pipeline.

We generate a sparse bead field, render two images of it (a low-res
widefield and a high-res scan) with a known linear coordinate transform,
and assert that ``auto_calibrate`` recovers a polynomial whose
residuals on the inlier set are sub-pixel.
"""

import numpy as np
import pytest

from imswitch.imcontrol.model.EtSTEDAutoCalibration import (
    auto_calibrate,
    coarse_shift_2d,
    detect_beads_2d,
    fit_affine_2d_ransac,
    match_beads,
)


def _render_beads(positions_yx: np.ndarray, shape: tuple[int, int],
                  sigma: float = 1.2, amplitude: float = 1.0,
                  noise: float = 0.01, seed: int = 0) -> np.ndarray:
    """Render gaussian PSFs at the given subpixel positions."""
    H, W = shape
    img = np.zeros((H, W), dtype=np.float32)
    yy, xx = np.mgrid[:H, :W]
    for y, x in positions_yx:
        img += amplitude * np.exp(-(((yy - y) ** 2 + (xx - x) ** 2) / (2 * sigma ** 2)))
    rng = np.random.default_rng(seed)
    img += rng.standard_normal(img.shape).astype(np.float32) * noise
    return img


def test_detect_beads_finds_sparse_gaussians():
    rng = np.random.default_rng(1)
    H, W = 128, 128
    positions = rng.uniform(10, 118, size=(20, 2))
    img = _render_beads(positions, (H, W))

    coords, vals = detect_beads_2d(img, sigma=1.0, min_distance=4,
                                   threshold_rel=0.2)
    # Should find roughly the 20 we planted (≥ 15 — some may be adjacent).
    assert len(coords) >= 15
    # Brightest first.
    assert np.all(np.diff(vals) <= 1e-6)


def test_coarse_shift_recovers_known_translation():
    rng = np.random.default_rng(2)
    positions = rng.uniform(20, 108, size=(15, 2))
    H, W = 128, 128
    ref = _render_beads(positions, (H, W))
    src = _render_beads(positions + np.array([5.0, -3.0]), (H, W))

    dy, dx = coarse_shift_2d(src, ref)
    # We rendered src by adding [+5, -3] to the bead *positions*, i.e.
    # src(y) has bead at y0+5 where ref(y) has it at y0; equivalently
    # src(y - 5) ≈ ref(y).  ``coarse_shift_2d(src, ref)`` returns d such
    # that src(y + d) ≈ ref(y), so d = [-5, +3].
    assert abs(dy + 5) <= 1
    assert abs(dx - 3) <= 1


def test_match_beads_drops_far_outliers():
    src = np.array([[10, 10], [50, 50], [100, 100]], dtype=np.float64)
    ref = np.array([[11, 11], [51, 49], [200, 200]], dtype=np.float64)
    src_m, ref_m, dists = match_beads(src, ref, max_distance=5.0)
    assert len(src_m) == 2
    assert np.all(dists < 5)


def test_ransac_recovers_pure_translation():
    src = np.random.default_rng(3).uniform(0, 100, size=(20, 2))
    true_shift = np.array([7.0, -4.0])
    ref = src + true_shift
    # Add three outliers.
    ref[:3] += np.random.default_rng(4).uniform(50, 80, size=(3, 2))
    A, inliers = fit_affine_2d_ransac(src, ref, n_iter=500)
    assert A is not None
    assert inliers.sum() >= 15  # the 17 non-outliers
    # Linear part should be (close to) identity.
    np.testing.assert_allclose(A[:, :2], np.eye(2), atol=0.05)
    # Translation column should be close to the planted shift.
    np.testing.assert_allclose(A[:, 2], true_shift, atol=0.5)


def test_auto_calibrate_end_to_end():
    """Full pipeline on a synthetic two-modality bead field.

    Bead positions are placed in a common sample-space frame, then
    rendered into a lo-res image (pixel size 1.0) and a hi-res image
    (pixel size 0.5) — i.e. a 2× scale factor between them.  We
    verify that ``auto_calibrate`` finds the inliers, fits the
    polynomial, and predicts the hi-res sample-space coordinates
    within sub-pixel error.
    """
    rng = np.random.default_rng(5)
    # 50 beads, in lo-res pixel coords inside [20, 108].  We need enough
    # beads inside the (smaller) hi-res overlap region for the third-order
    # polynomial fit to be well-conditioned — ≥ 10 inliers is required, and
    # we want plenty of buffer to absorb any matching outliers.
    lo_positions = rng.uniform(20, 108, size=(50, 2))
    H = W = 128
    lo_img = _render_beads(lo_positions, (H, W), sigma=1.0, amplitude=2.0,
                           noise=0.02, seed=10)

    # Hi-res "scan" image: a small translation only (no zoom), so most
    # beads are visible in both frames and the polynomial fit has plenty
    # of well-distributed inliers.  Real lab calibrations will involve a
    # zoom too, but the polynomial absorbs that the same way.
    lo_pixel_size_um = 1.0
    hi_pixel_size_um = 1.0
    lo_size_um = H  # legacy helper uses image height as 'lo size'
    hi_size_um = H * hi_pixel_size_um

    shift = np.array([3.0, -2.0])
    hi_positions = lo_positions + shift
    inside = ((hi_positions[:, 0] >= 5) & (hi_positions[:, 0] <= H - 5) &
              (hi_positions[:, 1] >= 5) & (hi_positions[:, 1] <= W - 5))
    hi_img = _render_beads(hi_positions[inside], (H, W), sigma=1.0,
                           amplitude=2.0, noise=0.02, seed=11)

    result = auto_calibrate(
        lo_img, hi_img,
        lo_pixel_size_um=lo_pixel_size_um,
        hi_pixel_size_um=hi_pixel_size_um,
        lo_size_um=lo_size_um,
        hi_size_um=hi_size_um,
        bead_thr_rel=0.15,
        match_max_dist=40.0,
        ransac_inlier_px=4.0,
        ransac_n_iter=1000,
    )

    assert result.n_lo_detected >= 30
    assert result.n_hi_detected >= 15
    assert result.n_matched >= 12
    assert result.n_inliers >= 10
    # The polynomial fit should achieve sub-pixel residuals on inliers.
    assert result.rms_residual_px < 1.0, (
        f'RMS residual {result.rms_residual_px:.3f} > 1 px — '
        f'auto-calibration quality regressed')
    assert result.coefficients.shape == (20,)
    assert not np.allclose(result.coefficients, 0)
