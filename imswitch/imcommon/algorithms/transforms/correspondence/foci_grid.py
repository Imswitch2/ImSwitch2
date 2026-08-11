"""Correspondence from a regular grid of bright foci.

The first of several correspondence strategies. It suits a multifoci / SLM
target where the same known grid is visible in both images: detect the spots,
order both grids the same way, and the pairing falls out of the ordering.

Ported from ImSwitch1's ``imcontrol/model/foci_affine.py`` (branch
``testalab_scanDev``), converted to this module's ``(row, col)`` convention and
with the silent-failure paths turned into explicit errors.

Known limits, kept deliberately visible:

* It needs **exactly** ``n_rows * n_cols`` detected spots in each image. Fewer
  or more raises rather than guessing, because a mis-ordered grid produces a
  plausible-looking but wrong transform.
* Both images must show the grid at *similar* orientation. Ordering resolves
  the grid axes only up to the lattice's own 90-degree symmetry, so two views
  rotated by roughly a quarter turn from each other would be ordered
  inconsistently and every pair would be wrong.
  :func:`find_grid_correspondence` measures both angles and refuses rather than
  returning a confident, wrong pairing.

The ImSwitch1 original recovered the grid axes from the SVD of the whole point
cloud. That is unreliable for the most common case: a *square* grid has an
isotropic covariance (both singular values exactly equal), so its singular
vectors are arbitrary and two images of the same grid can be ordered along
different axes. The widget there defaults to a 10x10 grid, so the degenerate
case was the default one. Axes are recovered here from nearest-neighbour
displacement vectors instead -- local lattice geometry, which stays
well-conditioned however square the grid is.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = [
    "detect_spot_centers",
    "estimate_grid_angle",
    "order_grid_points",
    "find_grid_correspondence",
]


def detect_spot_centers(
    image: Any,
    *,
    expected_n: int | None = None,
    min_distance: int = 10,
    threshold_rel: float = 0.2,
    refine_radius: int = 5,
    gaussian_sigma: float = 1.0,
) -> np.ndarray:
    """Detect bright foci and return ``(N, 2)`` sub-pixel ``(row, col)`` centres.

    Peaks are found on a percentile-normalized, Gaussian-smoothed copy, then
    refined to the local background-subtracted centre of mass -- the refinement
    is what makes the result sub-pixel, and therefore what makes a sub-pixel
    residual meaningful.
    """
    from scipy import ndimage as ndi
    from skimage import feature, filters

    picture = _as_2d_float(image)

    low, high = np.percentile(picture, [1, 99.9])
    normalized = np.clip((picture - low) / (high - low + 1e-12), 0.0, 1.0)
    smoothed = filters.gaussian(normalized, sigma=float(gaussian_sigma))

    peak_kwargs: dict[str, Any] = {
        "min_distance": int(min_distance),
        "threshold_rel": float(threshold_rel),
    }
    if expected_n is not None:
        peak_kwargs["num_peaks"] = int(expected_n)

    # peak_local_max already returns (row, col), which is this module's
    # convention -- no swap here, deliberately.
    peaks = feature.peak_local_max(smoothed, **peak_kwargs)

    radius = int(refine_radius)
    centres = []
    for row, col in peaks:
        row_start = max(0, int(row) - radius)
        row_stop = min(picture.shape[0], int(row) + radius + 1)
        col_start = max(0, int(col) - radius)
        col_stop = min(picture.shape[1], int(col) + radius + 1)

        patch = normalized[row_start:row_stop, col_start:col_stop]
        patch = np.clip(patch - np.percentile(patch, 10), 0.0, None)
        if patch.sum() <= 0:
            centres.append([float(row), float(col)])
            continue

        offset_row, offset_col = ndi.center_of_mass(patch)
        centres.append([row_start + offset_row, col_start + offset_col])

    if not centres:
        return np.empty((0, 2), dtype=np.float64)
    return np.asarray(centres, dtype=np.float64)


def order_grid_points(points: Any, n_rows: int, n_cols: int) -> np.ndarray:
    """Sort grid points row-major along the grid's own axes.

    Raises when the point count does not match ``n_rows * n_cols`` exactly:
    ordering a partial grid would pair the wrong spots between images and yield
    a confident, wrong transform.
    """
    array = np.asarray(points, dtype=np.float64)
    n_rows = int(n_rows)
    n_cols = int(n_cols)
    expected = n_rows * n_cols

    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"expected points of shape (N, 2), got {tuple(array.shape)}")
    if n_rows < 1 or n_cols < 1:
        raise ValueError(f"grid must be at least 1x1, got {n_rows}x{n_cols}")
    if len(array) != expected:
        raise ValueError(
            f"expected exactly {expected} points for a {n_rows}x{n_cols} grid, "
            f"got {len(array)}. Adjust the detection parameters until the spot "
            f"count matches; ordering a partial grid pairs the wrong spots."
        )
    if expected == 1:
        return array.copy()

    row_axis, col_axis = _estimate_grid_axes(array, n_rows, n_cols)
    centred = array - array.mean(axis=0)
    along_rows = centred @ row_axis
    along_cols = centred @ col_axis

    ordered = []
    for row_indices in np.array_split(np.argsort(along_rows), n_rows):
        ordered.append(array[row_indices[np.argsort(along_cols[row_indices])]])
    return np.vstack(ordered)


def find_grid_correspondence(
    source_image: Any,
    target_image: Any,
    *,
    n_rows: int,
    n_cols: int,
    min_distance: int = 10,
    threshold_rel: float = 0.2,
    refine_radius: int = 5,
    gaussian_sigma: float = 1.0,
    max_relative_tilt_deg: float = 30.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return paired ``(source_points, target_points)`` in ``(row, col)``.

    Both images must show the same grid. The pairing comes from ordering each
    grid identically, so the two images may differ by any transform that keeps
    the grid a recognizable grid *and* does not rotate it far enough to swap
    which lattice direction reads as "columns".

    ``max_relative_tilt_deg`` guards that last part. A lattice is symmetric
    under a quarter turn, so once the two views disagree by something
    approaching 45 degrees it is no longer decidable which axis is which, and
    the pairing would be silently transposed. Refusing beats returning a
    confident wrong answer.
    """
    detect_kwargs = {
        "expected_n": int(n_rows) * int(n_cols),
        "min_distance": min_distance,
        "threshold_rel": threshold_rel,
        "refine_radius": refine_radius,
        "gaussian_sigma": gaussian_sigma,
    }

    source_points = detect_spot_centers(source_image, **detect_kwargs)
    target_points = detect_spot_centers(target_image, **detect_kwargs)

    try:
        source_points = order_grid_points(source_points, n_rows, n_cols)
    except ValueError as exc:
        raise ValueError(f"source image: {exc}") from None
    try:
        target_points = order_grid_points(target_points, n_rows, n_cols)
    except ValueError as exc:
        raise ValueError(f"target image: {exc}") from None

    if n_rows > 1 and n_cols > 1:
        difference = np.degrees(
            estimate_grid_angle(target_points) - estimate_grid_angle(source_points)
        )
        # Both angles live in [-45, 45], so their difference wraps at 90.
        difference = (difference + 45.0) % 90.0 - 45.0
        if abs(difference) > float(max_relative_tilt_deg):
            raise ValueError(
                f"the two grids are tilted {abs(difference):.1f} degrees apart, "
                f"past the {max_relative_tilt_deg:.0f} degree limit where grid "
                "ordering can still tell rows from columns. Beyond it the "
                "pairing may be transposed, so no transform is returned; use a "
                "correspondence strategy that does not rely on grid ordering."
            )

    return source_points, target_points


def _as_2d_float(image: Any) -> np.ndarray:
    array = np.squeeze(np.asarray(image))
    if array.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {array.shape}")
    return array.astype(np.float64, copy=False)


def estimate_grid_angle(points: Any) -> float:
    """Return the grid's tilt in radians, in ``[-pi/4, pi/4]``.

    The sign follows the usual rotation matrix ``[[cos, -sin], [sin, cos]]``
    acting on ``(row, col)``: a grid built by rotating an axis-aligned one
    through ``+theta`` measures ``+theta`` here.

    Measured from nearest-neighbour displacement vectors rather than from the
    spread of the whole cloud, because the spread carries no orientation
    information at all for a square grid -- see the module docstring.

    A lattice is symmetric under 90-degree rotation, so its tilt is only
    defined modulo 90 degrees; the angles are quadrupled before averaging,
    which folds both that symmetry and the sign of each vector away, and the
    result is divided back down.
    """
    array = np.asarray(points, dtype=np.float64)
    if len(array) < 2:
        raise ValueError("need at least two points to estimate a grid angle")

    upper = np.triu_indices(len(array), k=1)
    vectors = (array[:, None, :] - array[None, :, :])[upper]
    lengths = np.linalg.norm(vectors, axis=1)

    positive = lengths[lengths > 0]
    if positive.size == 0:
        raise ValueError("all grid points are coincident")

    # Nearest-neighbour steps only: those lie along the lattice axes. Anything
    # longer may be a diagonal, which sits at 45 degrees to them.
    neighbours = vectors[lengths <= 1.4 * float(positive.min())]
    if len(neighbours) == 0:
        raise ValueError("could not find any nearest-neighbour grid steps")

    # Negated so the result matches the rotation that *produced* the tilt: a
    # column-direction step (0, 1) rotated by +theta becomes (-sin, cos), whose
    # arctan2(row, col) is -theta.
    angles = np.arctan2(neighbours[:, 0], neighbours[:, 1])
    mean_angle = np.arctan2(
        float(np.mean(np.sin(4.0 * angles))), float(np.mean(np.cos(4.0 * angles)))
    )
    return float(-mean_angle / 4.0)


def _axes_from_angle(angle: float) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(row_axis, col_axis)`` for a lattice tilted by ``angle``.

    These are the image row and column directions carried through the same
    rotation, so with ``angle`` confined to ``[-45, 45]`` degrees the column
    axis always keeps a positive column component and the row axis a positive
    row component. That is what makes two images of one grid order the same
    way without needing a separate sign convention.
    """
    sin_angle = float(np.sin(angle))
    cos_angle = float(np.cos(angle))
    row_axis = np.array([cos_angle, sin_angle], dtype=np.float64)
    col_axis = np.array([-sin_angle, cos_angle], dtype=np.float64)
    return row_axis, col_axis


def _estimate_grid_axes(
    points: np.ndarray, n_rows: int, n_cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """Recover the grid's own row and column directions in ``(row, col)`` space."""
    if n_rows == 1 or n_cols == 1:
        # A single line of points has no second lattice axis to find, but it
        # also has a well-conditioned principal direction -- unlike a square
        # grid, a line is maximally anisotropic.
        centred = points - points.mean(axis=0)
        _, _, right_vectors = np.linalg.svd(centred, full_matrices=False)
        primary = right_vectors[0]
        if n_rows == 1:
            col_axis = primary if primary[1] >= 0 else -primary
            return np.array([-col_axis[1], col_axis[0]]), col_axis
        row_axis = primary if primary[0] >= 0 else -primary
        return row_axis, np.array([-row_axis[1], row_axis[0]])

    return _axes_from_angle(estimate_grid_angle(points))
