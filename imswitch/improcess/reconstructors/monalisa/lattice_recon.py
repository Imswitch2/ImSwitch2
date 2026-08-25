"""General-lattice fast-Gauss reassignment (scatter + gridding).

The rectangular pipeline places one extracted amplitude per output pixel by
integer arithmetic — possible only because an axis-aligned grid scanned in
steps that subdivide its periods lands every sample exactly on a square output
raster. For any other Bravais lattice (rotated square / "diamond", hexagonal,
sheared) the sample positions

    position(frame, focus) = focus_position + scan_offset(frame)

do not form a square raster. This module reconstructs from those scattered
samples directly: it resolves the scan orientation by total variation (the
same criterion the rectangular path uses), splats every sample onto a regular
output raster with bilinear weights, and normalizes by the accumulated
weight. Output pixels the scan never covered are NaN rather than silently
zero — the coverage fraction is part of the returned diagnostics.

Everything here is pure geometry/numerics over ``(positions, values)``; the
per-focus amplitude extraction lives in :mod:`.gauss_processor` and the
pattern detection in :mod:`.lattice`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Bilinear splat weight below which an output pixel counts as uncovered.
#: A sample fully inside one pixel contributes weight 1; the nominal sampling
#: density is >= 1 sample per output pixel, so pixels far below this received
#: only a sliver of a distant sample and would extrapolate noise.
MIN_SPLAT_WEIGHT = 0.1

#: All scan orientations the reassignment considers: which frame index runs
#: fast (along x or along y) and the sign of each axis. Mirrors the eight
#: candidates of the rectangular path's ``get_orientation``.
ORIENTATIONS: tuple[tuple[str, int, int], ...] = tuple(
    (fast_axis, sign_x, sign_y)
    for fast_axis in ("x", "y")
    for sign_x in (1, -1)
    for sign_y in (1, -1)
)


@dataclass(frozen=True)
class ScatterAssembly:
    """One gridded reconstruction plus the geometry that produced it."""

    image: np.ndarray
    origin_px: tuple[float, float]  # (x, y) of output pixel (0, 0)
    pitch_px: tuple[float, float]  # (x, y) output pixel pitch
    coverage: float  # fraction of output pixels with sufficient weight
    orientation: tuple[str, int, int] | None = None


def scan_offsets_px(
    nx_s: int,
    ny_s: int,
    step_x_px: float,
    step_y_px: float,
    orientation: tuple[str, int, int],
) -> np.ndarray:
    """Per-frame ``(x, y)`` scan offsets in camera pixels, frame-ordered.

    ``orientation = (fast_axis, sign_x, sign_y)``: the fast frame index runs
    along ``fast_axis`` with that axis' step count, the slow index along the
    other; signs flip the direction of each axis. Frame count is always
    ``nx_s * ny_s`` (``nx_s`` steps belong to x, ``ny_s`` to y, regardless of
    which is fast).
    """
    nx_s = int(nx_s)
    ny_s = int(ny_s)
    if nx_s < 1 or ny_s < 1:
        raise ValueError("Scan step counts must be >= 1")
    if not (step_x_px > 0 and step_y_px > 0):
        raise ValueError("Scan steps in pixels must be positive")
    fast_axis, sign_x, sign_y = orientation

    if fast_axis == "x":
        i = np.tile(np.arange(nx_s), ny_s)
        j = np.repeat(np.arange(ny_s), nx_s)
    elif fast_axis == "y":
        j = np.tile(np.arange(ny_s), nx_s)
        i = np.repeat(np.arange(nx_s), ny_s)
    else:
        raise ValueError(f"Unknown fast axis {fast_axis!r}")

    offsets = np.empty((nx_s * ny_s, 2), dtype=np.float64)
    offsets[:, 0] = int(sign_x) * i * float(step_x_px)
    offsets[:, 1] = int(sign_y) * j * float(step_y_px)
    return offsets


def sample_positions(
    foci_xy: np.ndarray,
    offsets_xy: np.ndarray,
) -> np.ndarray:
    """All ``(frames * foci, 2)`` sample positions, frame-major.

    Row ``k * num_foci + f`` is focus ``f`` in frame ``k`` — matching
    ``amplitudes.reshape(-1)`` for an ``(frames, foci)`` amplitude matrix.
    """
    foci_xy = np.asarray(foci_xy, dtype=np.float64)
    offsets_xy = np.asarray(offsets_xy, dtype=np.float64)
    return (offsets_xy[:, None, :] + foci_xy[None, :, :]).reshape(-1, 2)


def splat_bilinear(
    positions: np.ndarray,
    values: np.ndarray,
    origin: tuple[float, float],
    pitch: tuple[float, float],
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Scatter samples onto a regular raster with bilinear weights.

    Returns ``(accumulated, weights)`` of ``shape`` (rows, cols); the caller
    divides where the weight is meaningful. Samples outside the raster are
    dropped (their out-of-range corner weights are simply not deposited).
    """
    positions = np.asarray(positions, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    if positions.shape != (values.size, 2):
        raise ValueError("positions must be (N, 2) matching values")
    rows, cols = int(shape[0]), int(shape[1])

    gx = (positions[:, 0] - float(origin[0])) / float(pitch[0])
    gy = (positions[:, 1] - float(origin[1])) / float(pitch[1])
    x0 = np.floor(gx).astype(np.int64)
    y0 = np.floor(gy).astype(np.int64)
    fx = gx - x0
    fy = gy - y0

    accumulated = np.zeros((rows, cols), dtype=np.float64)
    weights = np.zeros((rows, cols), dtype=np.float64)
    for dy in (0, 1):
        for dx in (0, 1):
            px = x0 + dx
            py = y0 + dy
            weight = (fx if dx else 1.0 - fx) * (fy if dy else 1.0 - fy)
            keep = (px >= 0) & (px < cols) & (py >= 0) & (py < rows) & (weight > 0)
            np.add.at(accumulated, (py[keep], px[keep]), weight[keep] * values[keep])
            np.add.at(weights, (py[keep], px[keep]), weight[keep])
    return accumulated, weights


def assemble_image(
    positions: np.ndarray,
    values: np.ndarray,
    pitch: tuple[float, float],
    origin: tuple[float, float] | None = None,
    shape: tuple[int, int] | None = None,
    min_weight: float = MIN_SPLAT_WEIGHT,
) -> ScatterAssembly:
    """Grid scattered samples into a weight-normalized image.

    When ``origin``/``shape`` are omitted they are derived from the sample
    extent (origin at the minimum position; one pixel per pitch). Pixels whose
    accumulated splat weight stays below ``min_weight`` become NaN — honest
    holes instead of extrapolated values.
    """
    positions = np.asarray(positions, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 2 or positions.shape[0] == 0:
        raise ValueError("positions must be a non-empty (N, 2) array")
    if not (pitch[0] > 0 and pitch[1] > 0):
        raise ValueError("Output pitch must be positive")

    if origin is None:
        origin = (float(positions[:, 0].min()), float(positions[:, 1].min()))
    if shape is None:
        extent_x = float(positions[:, 0].max()) - origin[0]
        extent_y = float(positions[:, 1].max()) - origin[1]
        shape = (
            int(np.floor(extent_y / pitch[1] + 0.5)) + 1,
            int(np.floor(extent_x / pitch[0] + 0.5)) + 1,
        )

    accumulated, weights = splat_bilinear(positions, values, origin, pitch, shape)
    covered = weights >= float(min_weight)
    image = np.full(weights.shape, np.nan, dtype=np.float64)
    image[covered] = accumulated[covered] / weights[covered]
    coverage = float(np.count_nonzero(covered)) / covered.size
    return ScatterAssembly(
        image=image,
        origin_px=(float(origin[0]), float(origin[1])),
        pitch_px=(float(pitch[0]), float(pitch[1])),
        coverage=coverage,
    )


def nan_total_variation(image: np.ndarray) -> float:
    """Total variation over finite-neighbor pairs only (NaN-hole aware)."""
    image = np.asarray(image, dtype=np.float64)
    total = 0.0
    for axis in (0, 1):
        diffs = np.diff(image, axis=axis)
        finite = np.isfinite(diffs)
        if np.any(finite):
            total += float(np.abs(diffs[finite]).sum())
    return total


def choose_orientation(
    foci_xy: np.ndarray,
    amplitudes: np.ndarray,
    nx_s: int,
    ny_s: int,
    step_px: tuple[float, float],
) -> tuple[str, int, int]:
    """Pick the scan orientation whose reconstruction has the lowest TV.

    Same principle as the rectangular path's ``get_orientation``: only the
    correct fast/slow assignment and axis signs place neighboring sample
    values next to each other, so the true orientation minimizes total
    variation. ``amplitudes`` is one complete stack, shape
    ``(nx_s * ny_s, num_foci)``.
    """
    amplitudes = np.asarray(amplitudes, dtype=np.float64)
    expected_frames = int(nx_s) * int(ny_s)
    if amplitudes.shape != (expected_frames, np.asarray(foci_xy).shape[0]):
        raise ValueError(
            f"amplitudes must be (frames, foci) = ({expected_frames}, "
            f"{np.asarray(foci_xy).shape[0]}), got {amplitudes.shape}"
        )

    best_orientation = ORIENTATIONS[0]
    best_score = np.inf
    values = amplitudes.reshape(-1)
    for orientation in ORIENTATIONS:
        offsets = scan_offsets_px(nx_s, ny_s, step_px[0], step_px[1], orientation)
        positions = sample_positions(foci_xy, offsets)
        assembly = assemble_image(positions, values, pitch=step_px)
        score = nan_total_variation(assembly.image)
        if score < best_score:
            best_score = score
            best_orientation = orientation
    return best_orientation


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
