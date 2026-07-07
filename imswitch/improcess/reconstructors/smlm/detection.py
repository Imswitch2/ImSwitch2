"""Net-gradient spot detection — pure-function core.

Ported from napari-storm (``super-resolution/napari-storm``,
``picasso_localization/picasso_localiztion.py``), itself a reimplementation of
the Picasso (jungmannlab) net-gradient detector. No Qt/registry coupling: this
is numpy/scipy in, ``(N, 2)`` integer ``(row, col)`` coordinates out, so it can
be unit-tested against synthetic frames and swapped for a vectorized/GPU
implementation later behind the same signature.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter


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
        Net-gradient threshold; only local maxima whose summed 4-direction
        gradient exceeds this are kept.
    roi:
        Fitting ROI size (px). Candidates closer than ``roi // 2`` to any edge
        are dropped so a full ROI can be extracted for fitting.
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

    smoothed = gaussian_filter(frame.astype(np.float32), sigma=sigma)

    # 4-direction forward differences (Picasso-style).
    up = np.zeros_like(smoothed)
    down = np.zeros_like(smoothed)
    left = np.zeros_like(smoothed)
    right = np.zeros_like(smoothed)
    up[1:] = smoothed[1:] - smoothed[:-1]
    down[:-1] = smoothed[:-1] - smoothed[1:]
    left[:, 1:] = smoothed[:, 1:] - smoothed[:, :-1]
    right[:, :-1] = smoothed[:, :-1] - smoothed[:, 1:]

    # Net gradient only where the point is a local peak in every direction.
    peak = (up > 0) & (down > 0) & (left > 0) & (right > 0)
    net_grad = np.where(peak, up + down + left + right, 0.0)

    above_thresh = net_grad > threshold
    local_max = maximum_filter(smoothed, size=3) == smoothed
    candidate_mask = above_thresh & local_max

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


__all__ = ["detect_spots"]
