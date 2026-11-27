"""Sparse calibration visits covering the *same* XY area as an imaging tiling grid.

The tiling pattern defines the anchor: serpentine starts in a corner, while
spiral starts at the centre. Coordinates are offsets in micrometres relative to
the current stage position; sampling density is independent of physical span.
"""

from __future__ import annotations

import math

from .spiral import SERPENTINE, SPIRAL, tile_moves


def calibration_visits(area_nx: int, area_ny: int, tile_step_um: float,
                       sample_nx: int, sample_ny: int, pattern: str):
    """Return (sample_gx, sample_gy, dx_um, dy_um) in the selected visit order.

    The first sample need not be at (0, 0) for an even-sized centred spiral;
    callers should take an independent origin reference *before* traversing.
    """
    counts = (area_nx, area_ny, sample_nx, sample_ny)
    if any(not isinstance(n, int) or isinstance(n, bool) for n in counts):
        raise ValueError('Calibration grid dimensions must be integers')
    if min(counts) < 2:
        raise ValueError('Tilt plane calibration needs at least 2 points on each axis')
    if not math.isfinite(float(tile_step_um)) or tile_step_um <= 0:
        raise ValueError('Calibration tile step must be a finite positive number')
    if pattern not in (SERPENTINE, SPIRAL):
        raise ValueError(f'Unknown tiling pattern: {pattern!r}')

    # Explicit integer expression matches spiral._bounds for both odd and even
    # sizes; parentheses matter for negative-floor division with even n.
    def bounds(n):
        if pattern == SERPENTINE:
            return 0, n - 1
        half = (n - 1) // 2
        return -half, n - 1 - half

    ax0, ax1 = bounds(area_nx)
    ay0, ay1 = bounds(area_ny)
    sx0, sx1 = bounds(sample_nx)
    sy0, sy1 = bounds(sample_ny)
    step_x = (ax1 - ax0) * float(tile_step_um) / (sx1 - sx0)
    step_y = (ay1 - ay0) * float(tile_step_um) / (sy1 - sy0)

    result = []
    x = y = 0
    for dx, dy in tile_moves(sample_nx, sample_ny, pattern):
        x += dx
        y += dy
        result.append((x, y, ax0 * tile_step_um + (x - sx0) * step_x,
                       ay0 * tile_step_um + (y - sy0) * step_y))
    return result
