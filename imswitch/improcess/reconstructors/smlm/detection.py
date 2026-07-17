"""Net-gradient spot detection — pure-function core.

Independent implementation of the Picasso (Jungmann Lab) net-gradient
detector, written from the published method (Schnitzbauer et al., Nat Protoc
12, 1198-1228, 2017); no Picasso code is copied. See ``ACKNOWLEDGMENTS.md``.

For every pixel,
the local image gradient is projected onto the *inward* radial direction and
summed over a small box. A real emitter's PSF slopes all point toward its
centre, so the projections add up to a large positive value; uncorrelated
noise gradients cancel. Because the score integrates the slopes *around* the
peak rather than the peak pixel itself, it stays sharply localised even when
the peak is broad or saturated (a flat top has no single-pixel gradient but
its flanks still slope inward).

.. note::

   Earlier revisions of this code scored each pixel
   by ``up + down + left + right`` — a single-pixel discrete Laplacian, not a
   net gradient. That measure is noise-dominated and, worse, vanishes on broad
   or saturated peaks, so bright beads were missed while noise spikes were
   detected. This is the real Picasso net gradient.

No Qt/registry coupling: numpy/scipy in, ``(N, 2)`` integer ``(row, col)``
coordinates out, so it can be unit-tested against synthetic frames and swapped
for a vectorized/GPU implementation later behind the same signature.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import correlate, gaussian_filter, maximum_filter


def _radial_gradient_kernels(box: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(wy, wx)`` correlation kernels for the inward gradient sum.

    ``wy[dy, dx]`` / ``wx[dy, dx]`` are the y/x components of the unit vector
    pointing from box offset ``(dy, dx)`` back to the box centre. Correlating
    the gradient fields with these kernels evaluates, at every pixel, the sum
    over the box of ``grad · inward_direction`` — the Picasso net gradient.
    """
    half = box // 2
    yy, xx = np.mgrid[-half:half + 1, -half:half + 1].astype(np.float32)
    radius = np.hypot(yy, xx)
    radius[half, half] = 1.0  # avoid div-by-zero at the centre
    wy = (-yy / radius).astype(np.float32)
    wx = (-xx / radius).astype(np.float32)
    wy[half, half] = 0.0  # the centre pixel carries no radial direction
    wx[half, half] = 0.0
    return wy, wx


def net_gradient_map(frame: np.ndarray, roi: int = 7, sigma: float = 1.0) -> np.ndarray:
    """Compute the Picasso net-gradient score at every pixel.

    Parameters
    ----------
    frame:
        2D image.
    roi:
        Box size (px) over which slopes are integrated; also the fitting ROI.
    sigma:
        Gaussian pre-smoothing width (px); ``0`` disables smoothing.

    Returns
    -------
    float32 array, same shape as ``frame``.
    """
    frame = np.asarray(frame, dtype=np.float32)
    smoothed = gaussian_filter(frame, sigma=sigma) if sigma > 0 else frame
    gy, gx = np.gradient(smoothed)
    box = max(3, int(roi) | 1)  # odd, >= 3
    wy, wx = _radial_gradient_kernels(box)
    return (
        correlate(gy, wy, mode="constant")
        + correlate(gx, wx, mode="constant")
    ).astype(np.float32)


def detect_spots(
    frame: np.ndarray,
    threshold: float,
    roi: int = 7,
    sigma: float = 1.0,
) -> np.ndarray:
    """Detect candidate emitter centres in a single frame.

    Parameters
    ----------
    frame:
        2D image.
    threshold:
        Net-gradient threshold; only net-gradient local maxima above this are
        kept. Higher rejects more noise. Interpretable via the live preview's
        candidate-count feedback.
    roi:
        Box/ROI size (px). Sets both the net-gradient integration window and
        the fitting window; candidates closer than ``roi // 2`` to any edge are
        dropped so a full ROI can be extracted for fitting.
    sigma:
        Gaussian pre-smoothing width (px).

    Returns
    -------
    ndarray of shape ``(N, 2)``, dtype intp, rows of ``(row, col)`` = ``(y, x)``.
    Empty ``(0, 2)`` when nothing passes.
    """
    frame = np.asarray(frame)
    if frame.ndim != 2:
        raise ValueError(f"detect_spots expects a 2D frame, got ndim={frame.ndim}")

    box = max(3, int(roi) | 1)
    net_grad = net_gradient_map(frame, roi=roi, sigma=sigma)

    # Candidates = net-gradient local maxima above threshold. Non-maximum
    # suppression over the box keeps one detection per PSF; because the score
    # peaks at the emitter centre even for saturated beads, this finds broad
    # peaks the old strict-intensity-maximum test missed.
    local_max = maximum_filter(net_grad, size=box) == net_grad
    candidate_mask = local_max & (net_grad > threshold)

    coords = np.argwhere(candidate_mask)
    if coords.size == 0:
        return np.empty((0, 2), dtype=np.intp)

    # Edge exclusion: keep only candidates with a full ROI inside the frame.
    half = roi // 2
    height, width = frame.shape
    rows, cols = coords[:, 0], coords[:, 1]
    inside = (
        (rows - half >= 0)
        & (rows + half < height)
        & (cols - half >= 0)
        & (cols + half < width)
    )
    return coords[inside].astype(np.intp)


__all__ = ["detect_spots", "net_gradient_map"]
