"""
Fully-automatic coordinate-transform calibration for etSTED / etMonalisa.

Replaces the manual "click corresponding beads in two napari layers"
workflow with a four-stage pipeline:

  1. **Bead detection** in each image independently — Gaussian smooth
     followed by a maximum-filter local-peak search.  Single-scale DoG
     equivalent, works well on sparse bead samples.

  2. **Coarse shift** between modalities — FFT phase correlation of the
     two images.  Bootstraps the matcher so beads in overlapping
     regions land within ``match_max_distance`` of each other.

  3. **Robust point-set matching** — RANSAC over candidate 2-D affine
     transforms.  Each iteration picks three random putative pairs
     (the minimum for a 6-parameter affine), fits a candidate, scores
     by inlier count.  Outlier rejection without needing pre-labelled
     correspondences.

  4. **Polynomial refinement** — feed the inlier pairs to
     :meth:`EtSTEDTransformService.calibrate` for the existing
     third-order polynomial fit, producing the same coefficient format
     ``loadTransform`` already consumes.

Architecture mirrors ``Mini_Recon/core/multicolor.py`` (3-D affine
descriptor-based registration), trimmed to 2-D and chained to the
polynomial fit step at the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .EtSTEDTransformService import EtSTEDTransformService


# Defaults — sane starting points; the widget will expose overrides.
DEFAULT_BEAD_SIGMA       = 1.5   # Gaussian pre-filter σ (px)
DEFAULT_BEAD_MIN_DIST    = 6     # minimum inter-bead spacing (px)
DEFAULT_BEAD_THR_REL     = 0.20  # peak threshold as fraction of smoothed max
DEFAULT_MATCH_MAX_DIST   = 25.0  # max NN distance (px, in HI-res coords)
DEFAULT_RANSAC_N_ITER    = 2000
DEFAULT_RANSAC_INLIER_PX = 3.0
# Third-order polynomial has 20 parameters; each inlier contributes 2 residuals
# (one per output axis), so we need ≥ 10 inliers for the LM solver to even run,
# and substantially more for a stable, well-conditioned fit.
MIN_INLIERS_FOR_POLY     = 10
HARD_MIN_INLIERS         = 10    # below this, calibrate() can't run at all


@dataclass
class AutoCalibrationResult:
    """Outcome of :func:`auto_calibrate`.

    Attributes
    ----------
    coefficients : (20,) ndarray
        Polynomial coefficients in the same format as the existing
        ``transform_pipelines/*.csv`` files.
    lo_coords_px : (N, 2) ndarray
        Low-resolution bead pixel coordinates (y, x) of the matched
        inlier set, in the lo-res image's pixel coordinate system.
    hi_coords_px : (N, 2) ndarray
        Matching high-resolution bead pixel coordinates (y, x).
    coarse_shift_px : (2,) ndarray
        Initial (dy, dx) shift between lo and hi images, in lo-res
        pixels.  Reported for QC; absorbed into the affine.
    n_lo_detected : int
        Total bead candidates before matching.
    n_hi_detected : int
    n_matched : int
        After NN matching with ``match_max_distance`` cutoff.
    n_inliers : int
        After RANSAC consensus.
    rms_residual_px : float
        Root-mean-squared affine residual on the inlier set, in HI-res px.
    warnings : list[str]
        Soft warnings raised during the run; empty on a healthy fit.
    """

    coefficients: np.ndarray
    lo_coords_px: np.ndarray
    hi_coords_px: np.ndarray
    coarse_shift_px: np.ndarray
    n_lo_detected: int
    n_hi_detected: int
    n_matched: int
    n_inliers: int
    rms_residual_px: float
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — bead detection
# ─────────────────────────────────────────────────────────────────────────────


def detect_beads_2d(
    image: np.ndarray,
    sigma: float = DEFAULT_BEAD_SIGMA,
    min_distance: int = DEFAULT_BEAD_MIN_DIST,
    threshold_rel: float = DEFAULT_BEAD_THR_REL,
) -> tuple[np.ndarray, np.ndarray]:
    """Detect bead-like point sources in a 2-D image.

    Strategy: Gaussian pre-filter (suppresses camera noise), then locate
    voxels that are the strict maximum in a square neighbourhood of radius
    ``min_distance`` and exceed ``threshold_rel × smoothed_max``.

    Parameters
    ----------
    image : (H, W) array
    sigma : Gaussian σ (px) applied before peak search
    min_distance : minimum spacing between accepted peaks (px)
    threshold_rel : discard peaks below this fraction of the smoothed maximum

    Returns
    -------
    coords : (N, 2) int array  [y, x], sorted by descending intensity
    values : (N,)  float array of smoothed intensities at each peak
    """
    from scipy.ndimage import gaussian_filter, maximum_filter

    if image.ndim != 2:
        raise ValueError(f'detect_beads_2d expects 2-D input; got shape {image.shape}')

    smoothed = gaussian_filter(image.astype(np.float32), sigma=sigma)
    footprint = 2 * int(min_distance) + 1
    is_local_max = maximum_filter(smoothed, size=footprint) == smoothed
    thr = float(threshold_rel) * float(smoothed.max())
    mask = is_local_max & (smoothed > thr)

    coords = np.argwhere(mask)  # (N, 2) in [y, x]
    if len(coords) == 0:
        return np.empty((0, 2), dtype=np.intp), np.empty(0, dtype=np.float32)

    values = smoothed[mask]
    order = np.argsort(values)[::-1]
    return coords[order].astype(np.intp), values[order]


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — coarse shift
# ─────────────────────────────────────────────────────────────────────────────


def coarse_shift_2d(src: np.ndarray, ref: np.ndarray,
                    pre_blur_sigma: float = 2.0) -> np.ndarray:
    """FFT cross-correlation 2-D shift estimate.

    Returns the (dy, dx) shift d such that ``src(x + d) ≈ ref(x)`` — i.e.
    add d to coordinates in *src* to land in the *ref* frame.  This is only
    used as an initial guess for the matcher, so an integer-pixel,
    intensity-biased result is fine.

    Uses plain cross-correlation rather than phase correlation.  Phase
    correlation is the principled choice for extended textures but
    collapses on very sparse bead images — every Fourier coefficient gets
    normalized to unit magnitude, the phase spectrum becomes noise-
    dominated, and the cross-correlation peak vanishes at the origin.
    Plain cross-correlation is robust on sparse images; the additional
    Gaussian pre-blur (sigma=2 px by default) further smooths each bead
    into a broader blob so the correlation peak is well-localized.
    """
    from scipy.ndimage import gaussian_filter

    a = ref.astype(np.float64)
    b = src.astype(np.float64)
    # Resize to a common shape via centre-crop.
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    a = a[:h, :w]
    b = b[:h, :w]
    if pre_blur_sigma > 0:
        a = gaussian_filter(a, sigma=pre_blur_sigma)
        b = gaussian_filter(b, sigma=pre_blur_sigma)
    a = a - a.mean()
    b = b - b.mean()
    fa = np.fft.rfft2(a)
    fb = np.fft.rfft2(b)
    cc = np.fft.irfft2(fa * np.conj(fb), s=a.shape)
    peak = np.unravel_index(int(np.argmax(cc)), a.shape)
    dy = peak[0] if peak[0] < a.shape[0] // 2 else peak[0] - a.shape[0]
    dx = peak[1] if peak[1] < a.shape[1] // 2 else peak[1] - a.shape[1]
    return np.array([float(dy), float(dx)], dtype=np.float64)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — matching + RANSAC
# ─────────────────────────────────────────────────────────────────────────────


def match_beads(
    src_pts: np.ndarray,
    ref_pts: np.ndarray,
    max_distance: float = DEFAULT_MATCH_MAX_DIST,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nearest-neighbour bead matching src → ref.

    Each src point is matched to its closest ref point; pairs whose
    Euclidean distance exceeds *max_distance* are rejected.

    Returns ``(src_matched, ref_matched, distances)`` — all of length M.
    """
    from scipy.spatial import cKDTree

    empty = np.empty((0, 2), dtype=np.float64)
    if len(src_pts) == 0 or len(ref_pts) == 0:
        return empty, empty, np.empty(0, dtype=np.float64)

    tree = cKDTree(ref_pts.astype(np.float64))
    dists, idx = tree.query(src_pts.astype(np.float64), k=1)
    mask = dists < float(max_distance)
    return (
        src_pts[mask].astype(np.float64),
        ref_pts[idx[mask]].astype(np.float64),
        dists[mask],
    )


def fit_affine_2d_lstsq(
    src_pts: np.ndarray, ref_pts: np.ndarray
) -> np.ndarray | None:
    """Fit a (2, 3) affine A such that ``ref ≈ A @ [src | 1]^T``.

    Returns None if fewer than 3 pairs (under-determined).
    """
    if len(src_pts) < 3:
        return None
    n = len(src_pts)
    hom = np.column_stack([src_pts.astype(np.float64), np.ones(n)])  # (N, 3)
    rows = [
        np.linalg.lstsq(hom, ref_pts[:, k].astype(np.float64), rcond=None)[0]
        for k in range(2)
    ]
    return np.asarray(rows, dtype=np.float64)  # (2, 3)


def fit_affine_2d_ransac(
    src_pts: np.ndarray,
    ref_pts: np.ndarray,
    n_iter: int = DEFAULT_RANSAC_N_ITER,
    inlier_thr: float = DEFAULT_RANSAC_INLIER_PX,
    rng_seed: int = 42,
) -> tuple[np.ndarray | None, np.ndarray]:
    """RANSAC 6-DOF 2-D affine estimation.

    Each iteration samples 3 putative pairs and counts inliers
    (reprojection residual ≤ ``inlier_thr`` px).  Final affine is
    re-fit by least squares on the best consensus set.

    Returns ``(A, inliers)`` — ``A`` is None if input is under-determined.
    """
    n = len(src_pts)
    if n < 3:
        return None, np.zeros(n, dtype=bool)

    rng = np.random.default_rng(rng_seed)
    best_inliers = np.zeros(n, dtype=bool)
    hom = np.column_stack([src_pts.astype(np.float64), np.ones(n)])

    for _ in range(int(n_iter)):
        idx = rng.choice(n, 3, replace=False)
        A_cand = fit_affine_2d_lstsq(src_pts[idx], ref_pts[idx])
        if A_cand is None:
            continue
        pred = (A_cand @ hom.T).T
        resid = np.linalg.norm(pred - ref_pts.astype(np.float64), axis=1)
        inliers = resid < float(inlier_thr)
        if inliers.sum() > best_inliers.sum():
            best_inliers = inliers

    if best_inliers.sum() >= 3:
        A_final = fit_affine_2d_lstsq(src_pts[best_inliers], ref_pts[best_inliers])
    else:
        A_final = fit_affine_2d_lstsq(src_pts, ref_pts)
    return A_final, best_inliers


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — full pipeline
# ─────────────────────────────────────────────────────────────────────────────


def auto_calibrate(
    lo_image: np.ndarray,
    hi_image: np.ndarray,
    lo_pixel_size_um: float,
    hi_pixel_size_um: float,
    lo_size_um: float,
    hi_size_um: float,
    *,
    bead_sigma: float       = DEFAULT_BEAD_SIGMA,
    bead_min_dist: int      = DEFAULT_BEAD_MIN_DIST,
    bead_thr_rel: float     = DEFAULT_BEAD_THR_REL,
    match_max_dist: float   = DEFAULT_MATCH_MAX_DIST,
    ransac_n_iter: int      = DEFAULT_RANSAC_N_ITER,
    ransac_inlier_px: float = DEFAULT_RANSAC_INLIER_PX,
    transform_service: EtSTEDTransformService | None = None,
) -> AutoCalibrationResult:
    """Run the full bead-detect → match → RANSAC → polynomial pipeline.

    The image-coordinate convention follows the rest of the et helpers:
    *lo* (widefield) coords are reported as raw (y, x) pixel positions,
    *hi* (scan) coords are mapped into physical sample-space (µm relative
    to the scan FOV centre) before being fed to the polynomial fit, exactly
    as the manual calibration helper does.

    Parameters mirror :class:`EventTriggeredCoordTransformHelper`:

    * ``lo_size_um`` — the height of the lo-res ROI in lo-res *pixels*
      (yes, "size" is a misnomer kept from the legacy helper — it is the
      array dimension used to flip y, not a real µm size).  Pass
      ``np.shape(lo_image)[1]``.
    * ``hi_pixel_size_um`` / ``hi_size_um`` — µm per pixel and FOV
      extent of the hi-res image (read from the HDF5 attrs as in
      :meth:`loadCalibImage`).
    """
    warnings: list[str] = []
    transform_service = transform_service or EtSTEDTransformService()

    # Stage 1 — detect.
    lo_pts_yx, _ = detect_beads_2d(
        lo_image, sigma=bead_sigma, min_distance=bead_min_dist,
        threshold_rel=bead_thr_rel,
    )
    hi_pts_yx, _ = detect_beads_2d(
        hi_image, sigma=bead_sigma, min_distance=bead_min_dist,
        threshold_rel=bead_thr_rel,
    )
    if len(lo_pts_yx) < 3 or len(hi_pts_yx) < 3:
        raise RuntimeError(
            f'Auto-calibration: insufficient beads detected '
            f'(lo={len(lo_pts_yx)}, hi={len(hi_pts_yx)}). '
            f'Lower bead_thr_rel or use a denser bead sample.'
        )

    # Stage 2 — coarse shift via phase correlation of the raw images.
    coarse = coarse_shift_2d(lo_image, hi_image)

    # Stage 3 — apply coarse shift, match, RANSAC.
    lo_shifted = lo_pts_yx.astype(np.float64) + coarse
    src_m_sh, ref_m, _dists = match_beads(
        lo_shifted, hi_pts_yx.astype(np.float64), max_distance=match_max_dist
    )
    n_matched = len(src_m_sh)
    if n_matched < 3:
        raise RuntimeError(
            f'Auto-calibration: only {n_matched} bead pair(s) matched after '
            f'coarse shift (need ≥ 3 for an affine). Try increasing '
            f'match_max_dist (currently {match_max_dist:g}) or lowering '
            f'bead_thr_rel (currently {bead_thr_rel:g}).'
        )

    _A_affine, inliers = fit_affine_2d_ransac(
        src_m_sh, ref_m, n_iter=ransac_n_iter, inlier_thr=ransac_inlier_px,
    )
    n_inliers = int(inliers.sum())
    if n_inliers < HARD_MIN_INLIERS:
        raise RuntimeError(
            f'Auto-calibration: only {n_inliers} RANSAC inliers — the third-order '
            f'polynomial fit needs at least {HARD_MIN_INLIERS} (20 parameters, 2 '
            f'residuals per pair).  Try a denser bead sample or a larger '
            f'overlapping FOV.'
        )
    if n_inliers < MIN_INLIERS_FOR_POLY + 4:
        warnings.append(
            f'Only {n_inliers} RANSAC inliers — polynomial fit may be unstable; '
            f'≥ {MIN_INLIERS_FOR_POLY + 4} recommended.'
        )

    # Unshift matched lo coords back to original (raw lo pixel) space.
    lo_matched_px = src_m_sh - coarse
    hi_matched_px = ref_m
    lo_inlier_px = lo_matched_px[inliers]
    hi_inlier_px = hi_matched_px[inliers]

    # Stage 4 — convert to physical coords (matching the legacy helper's
    # axis convention) and fit the third-order polynomial.
    lo_phys = lo_inlier_px.astype(np.float32)  # raw pixels, as the helper does
    hi_phys = np.asarray([
        ((lo_size_um - float(p[1])) * hi_pixel_size_um - hi_size_um / 2,
         (lo_size_um - float(p[0])) * hi_pixel_size_um - hi_size_um / 2)
        for p in hi_inlier_px
    ], dtype=np.float32)

    coefficients = transform_service.calibrate(lo_phys, hi_phys)

    # RMS residual of the polynomial fit on the inlier set (in hi-res µm).
    if len(lo_phys) > 0:
        predicted = np.asarray(
            [transform_service.poly_thirdorder_transform(coefficients, x)
             for x in lo_phys],
            dtype=np.float64,
        )
        rms = float(np.sqrt(np.mean((predicted - hi_phys) ** 2)))
    else:
        rms = float('inf')

    return AutoCalibrationResult(
        coefficients=np.asarray(coefficients, dtype=np.float64),
        lo_coords_px=np.asarray(lo_inlier_px, dtype=np.float64),
        hi_coords_px=np.asarray(hi_inlier_px, dtype=np.float64),
        coarse_shift_px=coarse,
        n_lo_detected=int(len(lo_pts_yx)),
        n_hi_detected=int(len(hi_pts_yx)),
        n_matched=n_matched,
        n_inliers=n_inliers,
        rms_residual_px=rms,
        warnings=warnings,
    )
