"""Named adapters between this module's convention and everyone else's.

The tree currently has at least three live conventions -- ``(row, col)`` in
``detector_transform``, ``(z, y, x)`` in the multicolor aligner, and
pystackreg's transposed ``(x, y)``. That is exactly why the conversions live
here, named, tested and in one place, instead of being open-coded wherever two
libraries meet.

This module's internal convention is row-major ``(..., y, x)`` with integer
coordinates naming pixel centres. Everything below converts *to or from* that.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

__all__ = [
    "axis_swap_matrix",
    "xy_matrix_to_yx",
    "yx_matrix_to_xy",
    "points_xy_to_yx",
    "points_yx_to_xy",
    "pixel_matrix_to_world",
    "world_matrix_to_pixel",
    "matrix_to_ndimage",
]


def _as_homogeneous(matrix: Any, ndim: int = 2) -> np.ndarray:
    """Accept ``(ndim, ndim+1)`` or ``(ndim+1, ndim+1)``, return the latter."""
    array = np.asarray(matrix, dtype=np.float64)
    if array.shape == (ndim, ndim + 1):
        homogeneous = np.eye(ndim + 1, dtype=np.float64)
        homogeneous[:ndim, :] = array
        return homogeneous
    if array.shape == (ndim + 1, ndim + 1):
        return array.copy()
    raise ValueError(
        f"expected matrix of shape ({ndim}, {ndim + 1}) or "
        f"({ndim + 1}, {ndim + 1}), got {tuple(array.shape)}"
    )


def axis_swap_matrix() -> np.ndarray:
    """The 2-D homogeneous matrix exchanging the two spatial axes."""
    return np.array(
        [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )


def xy_matrix_to_yx(matrix: Any) -> np.ndarray:
    """Convert an ``(x, y)``-ordered affine into ``(y, x)`` order.

    Conjugation by the swap: ``S @ H @ S`` (``S`` is its own inverse).
    """
    swap = axis_swap_matrix()
    return swap @ _as_homogeneous(matrix) @ swap


def yx_matrix_to_xy(matrix: Any) -> np.ndarray:
    """Convert a ``(y, x)``-ordered affine into ``(x, y)`` order."""
    # The swap is an involution, so this is the same operation; both names
    # exist so call sites read in the direction they mean.
    return xy_matrix_to_yx(matrix)


def points_xy_to_yx(points: Any) -> np.ndarray:
    """Reorder an ``(N, 2)`` point array from ``(x, y)`` to ``(y, x)``."""
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"expected points of shape (N, 2), got {tuple(array.shape)}")
    return array[:, ::-1].copy()


def points_yx_to_xy(points: Any) -> np.ndarray:
    """Reorder an ``(N, 2)`` point array from ``(y, x)`` to ``(x, y)``."""
    return points_xy_to_yx(points)


def pixel_matrix_to_world(matrix: Any, scale: Sequence[float] | None) -> np.ndarray:
    """Convert a pixel-space affine into the world-space affine napari stores.

    A pixel-space matrix and a physical-space matrix are different objects: if
    a layer carries a ``scale``, the transform napari applies lives in scaled
    world coordinates. The conversion is the conjugation ``S @ H @ S^-1``.

    This is also the seed of the physical-units decision (D3 in the plan): the
    same conjugation is what will let a calibration be stored in micrometres
    and applied at whatever pixel size the detector currently has.
    """
    homogeneous = _as_homogeneous(matrix)
    values = (
        np.ones(2, dtype=np.float64)
        if scale is None
        else np.asarray(scale, dtype=np.float64).reshape(-1)
    )
    if values.size < 2:
        values = np.ones(2, dtype=np.float64)
    else:
        values = values[-2:]
    if np.any(values == 0):
        raise ValueError(f"scale entries must be non-zero, got {values.tolist()}")

    scaling = np.diag([values[0], values[1], 1.0])
    return scaling @ homogeneous @ np.linalg.inv(scaling)


def world_matrix_to_pixel(matrix: Any, scale: Sequence[float] | None) -> np.ndarray:
    """Inverse of :func:`pixel_matrix_to_world`."""
    homogeneous = _as_homogeneous(matrix)
    values = (
        np.ones(2, dtype=np.float64)
        if scale is None
        else np.asarray(scale, dtype=np.float64).reshape(-1)
    )
    if values.size < 2:
        values = np.ones(2, dtype=np.float64)
    else:
        values = values[-2:]
    if np.any(values == 0):
        raise ValueError(f"scale entries must be non-zero, got {values.tolist()}")

    scaling = np.diag([values[0], values[1], 1.0])
    return np.linalg.inv(scaling) @ homogeneous @ scaling


def matrix_to_ndimage(matrix: Any) -> tuple[np.ndarray, np.ndarray]:
    """Split a *forward* affine into the ``(matrix, offset)`` ndimage wants.

    ``scipy.ndimage`` computes ``output[o] = input[matrix @ o + offset]``, i.e.
    it consumes the *inverse* (target -> source) mapping. Callers of this
    module store forward transforms and never see this; the conversion is here
    so that when it is needed it is named rather than open-coded.
    """
    homogeneous = _as_homogeneous(matrix)
    ndim = homogeneous.shape[0] - 1
    inverse = np.linalg.inv(homogeneous)
    return inverse[:ndim, :ndim].copy(), inverse[:ndim, ndim].copy()
