"""Sampling an image along a line.

One implementation, used by both the Profile panel and the ROI manager's line
measurements. They were two: the profile plotted a Gaussian-weighted average
across the line width while the measurements had no sampler at all, so a line
ROI could be drawn and never measured. Two samplers would have meant a profile
curve and a "mean along the line" that disagree about what the line contains.

Pure numpy/scipy — no Qt, no napari — so it can be used from a widget, from a
measurement, and from a worker thread.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import map_coordinates


def sample_count(r0: float, c0: float, r1: float, c1: float) -> int:
    """How many samples a line of this length gets: one per pixel step."""
    return max(int(np.ceil(np.hypot(r1 - r0, c1 - c0))) + 1, 2)


def line_samples(
    image,
    r0: float,
    c0: float,
    r1: float,
    c1: float,
    *,
    width: int = 1,
    weighting: str = "uniform",
) -> np.ndarray | None:
    """Interpolated values along the segment, or None if it is degenerate.

    ``width`` > 1 averages perpendicular to the line over that many parallel
    samples. ``weighting`` picks how they are combined:

    * ``"uniform"`` — a plain mean, which is what ImageJ's line width does and
      therefore what a measurement reported as ImageJ-comparable must use.
    * ``"gaussian"`` — the Profile panel's long-standing behaviour, kept so
      switching it to the shared sampler does not silently change the curves
      people have been reading.

    Off-image samples read as 0 rather than raising, matching the profile
    panel: a line dragged past the edge should still plot.
    """
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        return None
    drow, dcol = float(r1) - float(r0), float(c1) - float(c0)
    length = float(np.hypot(drow, dcol))
    if length <= 0:
        return None

    count = sample_count(r0, c0, r1, c1)
    rows = np.linspace(float(r0), float(r1), count)
    cols = np.linspace(float(c0), float(c1), count)

    def read(row_samples, col_samples):
        return map_coordinates(
            arr, [row_samples, col_samples], order=1, mode="constant", cval=0
        )

    width = max(1, int(width))
    if width == 1:
        return read(rows, cols)

    perp_r, perp_c = -dcol / length, drow / length
    offsets = np.linspace(-(width - 1) / 2, (width - 1) / 2, width)
    if str(weighting).lower() == "gaussian":
        sigma = max(width / 4.0, 1e-6)
        weights = np.exp(-(offsets**2) / (2 * sigma**2))
        weights /= weights.sum()
    else:
        weights = np.full(width, 1.0 / width)

    profile = np.zeros(count, dtype=float)
    for weight, offset in zip(weights, offsets):
        profile += weight * read(rows + offset * perp_r, cols + offset * perp_c)
    return profile


def polyline_samples(
    image, vertices, *, width: int = 1, weighting: str = "uniform"
) -> np.ndarray | None:
    """Values along a multi-segment path, joined end to end.

    Shared vertices are counted once, so a two-segment path does not report the
    corner pixel twice and pull the mean towards it.
    """
    points = np.asarray(vertices, dtype=np.float64)
    if points.ndim != 2 or points.shape[0] < 2:
        return None
    pieces = []
    for start, end in zip(points[:-1], points[1:]):
        piece = line_samples(
            image, start[0], start[1], end[0], end[1],
            width=width, weighting=weighting,
        )
        if piece is None:
            continue
        pieces.append(piece if not pieces else piece[1:])
    if not pieces:
        return None
    return np.concatenate(pieces)


__all__ = ["line_samples", "polyline_samples", "sample_count"]
