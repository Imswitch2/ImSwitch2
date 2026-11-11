"""Placement of extracted focus amplitudes on the sample-space raster.

Focus ``f`` at camera position ``r_f`` probes, in the frame recorded with the
sample displaced by ``d_k``, the sample point ``q = r_f - d_k``. This module
turns the amplitudes of all foci and frames into an image:

* :func:`commensurability` and :func:`coverage` say whether the scan and the
  illumination lattice fit together: whether every sample lands exactly on a
  pixel of the output raster, and whether every pixel receives exactly one.
* :func:`place_nearest` is the reconstruction when they do: a placement, with
  no interpolation and no loss.
* :func:`grid_bspline` is the reconstruction when they do not: the cubic
  B-spline surface that fits the scattered samples in the least-squares
  sense, evaluated on the raster.

The criteria hold for any lattice and any pair of scan step vectors, not only
for axis-aligned steps (``docs/monalisa_optimal_reconstruction.md``, sections
1.1, 2.4 and 8).

Coordinates are ``(x, y)`` in camera pixels, x along columns. Raster
coordinates are ``(gx, gy)`` in raster pixels, gx along columns.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import LinearOperator, cg

COMMENSURATE_TOLERANCE = 0.05
HOLE_DENSITY = 0.1
DEFAULT_SMOOTHING = 1e-4


@dataclass(frozen=True)
class OutputRaster:
    """The sample-space pixel grid of a reconstruction.

    Attributes:
        origin: Camera position ``(x, y)`` of raster pixel ``(0, 0)``.
        step: 2x2 matrix whose columns are the camera-space vectors of one
            raster pixel along the raster's x and y. For a scan along the
            camera axes this is ``diag(step_x, step_y)``; for a scan along
            the lattice vectors the raster is sheared.
        shape: ``(rows, cols)``.
    """

    origin: tuple[float, float]
    step: np.ndarray
    shape: tuple[int, int]

    @classmethod
    def axis_aligned(
        cls,
        origin: tuple[float, float],
        step_x: float,
        step_y: float,
        shape: tuple[int, int],
    ) -> "OutputRaster":
        return cls(
            origin=(float(origin[0]), float(origin[1])),
            step=np.array([[float(step_x), 0.0], [0.0, float(step_y)]]),
            shape=(int(shape[0]), int(shape[1])),
        )

    @classmethod
    def covering(
        cls,
        frame_shape: tuple[int, int],
        step: np.ndarray,
        anchor: tuple[float, float],
    ) -> "OutputRaster":
        """The raster over a camera frame whose pixel grid contains ``anchor``.

        ``anchor`` is a lattice point: with the raster locked to it every
        sample of a commensurate scan falls on a raster pixel.
        """
        step = np.asarray(step, dtype=float).reshape(2, 2)
        rows, cols = frame_shape
        corners = np.array(
            [[0.0, 0.0], [cols - 1.0, 0.0], [0.0, rows - 1.0], [cols - 1.0, rows - 1.0]]
        )
        coords = np.linalg.solve(step, (corners - np.asarray(anchor, float)).T)
        low = np.floor(coords.min(axis=1) + 1e-9).astype(int)
        high = np.ceil(coords.max(axis=1) - 1e-9).astype(int)
        origin = np.asarray(anchor, float) + step @ low
        return cls(
            origin=(float(origin[0]), float(origin[1])),
            step=step,
            shape=(int(high[1] - low[1] + 1), int(high[0] - low[0] + 1)),
        )

    def coordinates(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Raster coordinates ``(gx, gy)`` of camera positions ``(x, y)``."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        inverse = np.linalg.inv(np.asarray(self.step, dtype=float))
        dx, dy = x - self.origin[0], y - self.origin[1]
        return (
            inverse[0, 0] * dx + inverse[0, 1] * dy,
            inverse[1, 0] * dx + inverse[1, 1] * dy,
        )


def sample_positions(
    focus_x: np.ndarray,
    focus_y: np.ndarray,
    offset_x: np.ndarray,
    offset_y: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample-space positions ``q = r_f - d_k``, shape ``(frames, foci)``.

    ``offset_x``/``offset_y`` are the displacements ``d_k`` of the sample in
    camera pixels, one per frame.
    """
    focus_x = np.asarray(focus_x, dtype=float).ravel()
    focus_y = np.asarray(focus_y, dtype=float).ravel()
    offset_x = np.asarray(offset_x, dtype=float).ravel()
    offset_y = np.asarray(offset_y, dtype=float).ravel()
    return (
        focus_x[None, :] - offset_x[:, None],
        focus_y[None, :] - offset_y[:, None],
    )


# ------------------------------------------------------ scan against lattice
def commensurability(
    lattice_matrix: np.ndarray, step: np.ndarray
) -> tuple[np.ndarray, float]:
    """How well the lattice fits on the raster spanned by the scan steps.

    The scan is commensurate with the lattice when both lattice vectors are
    integer combinations of the two step vectors, i.e. when ``N = step^-1 A``
    is an integer matrix. Then every sample of every focus lands on a raster
    pixel.

    Args:
        lattice_matrix: 2x2, lattice basis vectors as columns (``Lattice.matrix``).
        step: 2x2, scan step vectors as columns.

    Returns:
        ``(N, residual)``: ``N`` rounded to integers, and the largest distance
        of an entry of ``step^-1 A`` to an integer, in raster pixels. The
        mismatch a focus ``m`` periods from the raster origin accumulates is
        ``m * residual``.
    """
    lattice_matrix = np.asarray(lattice_matrix, dtype=float).reshape(2, 2)
    step = np.asarray(step, dtype=float).reshape(2, 2)
    ratio = np.linalg.solve(step, lattice_matrix)
    integer = np.rint(ratio)
    return integer.astype(int), float(np.max(np.abs(ratio - integer)))


def _relative_step_change(step: np.ndarray, locked: np.ndarray) -> float:
    """Largest relative change of either scan-axis vector."""
    step_norm = np.linalg.norm(step, axis=0)
    delta_norm = np.linalg.norm(locked - step, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        axis_change = np.where(
            step_norm > 1e-12,
            delta_norm / step_norm,
            np.where(delta_norm <= 1e-12, 0.0, np.inf),
        )
    return float(np.max(axis_change))


def _cell_count_integer(
    lattice_matrix: np.ndarray, step: np.ndarray, scan_index: np.ndarray
) -> np.ndarray | None:
    """Nearest commensurate index matrix with one class per acquired position.

    A MoNaLISA in-plane scan enumerates one illumination cell.  If it contains
    ``P`` distinct positions, the quotient of the scan raster by the lattice
    therefore has exactly ``P`` phase classes, i.e. ``|det N| == P`` for
    ``N = step^-1 lattice``.  The recorded metric step is only an approximate
    scale and must not silently change that topology (21 acquired phases into
    22 expected phases, for example).
    """
    scan_index = np.rint(np.asarray(scan_index)).astype(np.int64).reshape(-1, 2)
    positions = int(np.unique(scan_index, axis=0).shape[0])
    if positions < 1:
        return None

    ratio = np.linalg.solve(step, lattice_matrix)
    centre = np.rint(ratio).astype(int)
    if int(round(abs(np.linalg.det(centre)))) == positions:
        return centre

    # Nominal stage/pixel calibration is normally only a few percent away, so
    # the correct integer topology is close to the independently rounded one.
    # Searching a small neighbourhood also handles rotated/staggered cells
    # whose index matrix has off-diagonal entries.
    best = None
    best_score = None
    ranges = [range(int(value) - 3, int(value) + 4) for value in centre.ravel()]
    for a in ranges[0]:
        for b in ranges[1]:
            for c in ranges[2]:
                for d in ranges[3]:
                    candidate = np.array([[a, b], [c, d]], dtype=int)
                    det = int(round(np.linalg.det(candidate)))
                    if abs(det) != positions:
                        continue
                    try:
                        locked = lattice_matrix @ np.linalg.inv(candidate)
                    except np.linalg.LinAlgError:
                        continue
                    change = _relative_step_change(step, locked)
                    distance = float(np.linalg.norm(ratio - candidate))
                    score = (change, distance)
                    if best_score is None or score < best_score:
                        best_score = score
                        best = candidate
    return best


def lock_step_to_lattice(
    lattice_matrix: np.ndarray,
    step: np.ndarray,
    tolerance: float = 0.02,
    scan_index: np.ndarray | None = None,
) -> tuple[np.ndarray, float]:
    """Lock the scan step to the measured illumination lattice.

    With ``scan_index`` the acquisition itself says how many periodic phases
    were sampled: a MoNaLISA scan is one illumination cell, so the locked
    raster is constrained to have exactly one phase class per distinct scan
    position.  This count/topology outranks the approximate metric step stored
    in metadata.  The returned ``change`` still reports how far the effective
    lattice-derived step is from that nominal value.

    Without ``scan_index`` this keeps the historical behaviour: choose the
    nearest integer subdivision and accept it only within ``tolerance``.
    """
    lattice_matrix = np.asarray(lattice_matrix, dtype=float).reshape(2, 2)
    step = np.asarray(step, dtype=float).reshape(2, 2)

    if scan_index is not None:
        integer = _cell_count_integer(lattice_matrix, step, scan_index)
        if integer is None or int(round(abs(np.linalg.det(integer)))) == 0:
            return step, float("inf")
        locked = lattice_matrix @ np.linalg.inv(integer)
        return locked, _relative_step_change(step, locked)

    integer, _ = commensurability(lattice_matrix, step)
    if int(round(abs(np.linalg.det(integer)))) == 0:
        return step, float("inf")
    locked = lattice_matrix @ np.linalg.inv(integer)
    change = _relative_step_change(step, locked)
    if change > tolerance:
        return step, change
    return locked, change


@dataclass(frozen=True)
class Coverage:
    """How often a scan visits each raster pixel of one lattice cell.

    ``counts`` has one entry per raster pixel of the cell (``cell_pixels`` of
    them): 0 is a hole, 1 is exactly once, more is an overlap.
    """

    cell_pixels: int
    counts: np.ndarray

    @property
    def holes(self) -> int:
        return int(np.sum(self.counts == 0))

    @property
    def overlaps(self) -> int:
        return int(np.sum(self.counts > 1))

    @property
    def exactly_once(self) -> bool:
        return bool(np.all(self.counts == 1))


def coverage(index_matrix: np.ndarray, scan_index: np.ndarray) -> Coverage:
    """Count the scan positions per raster pixel of the lattice cell.

    With ``N`` from :func:`commensurability` the raster pixels fall into
    ``|det N|`` classes that the lattice maps onto each other; a scan covers
    the sample exactly once when its positions hit every class once. This is
    the test the ratio of scan area to cell area only approximates: it also
    holds for cells no scan rectangle can tile without a brick offset.

    Args:
        index_matrix: Integer 2x2 matrix ``N``.
        scan_index: ``(frames, 2)`` integer scan positions in units of the
            step vectors.
    """
    index_matrix = np.asarray(index_matrix).reshape(2, 2)
    n = np.rint(index_matrix).astype(np.int64)
    det = int(n[0, 0] * n[1, 1] - n[0, 1] * n[1, 0])
    if det == 0:
        raise ValueError("The lattice is degenerate on this raster")
    cell_pixels = abs(det)
    adjugate = np.array([[n[1, 1], -n[0, 1]], [-n[1, 0], n[0, 0]]], dtype=np.int64)
    scan_index = np.rint(np.asarray(scan_index)).astype(np.int64).reshape(-1, 2)
    # Two positions are in the same class iff adj(N) (t - t') = 0 mod det.
    key = (scan_index @ adjugate.T) % cell_pixels
    _, hits = np.unique(key, axis=0, return_counts=True)
    counts = np.zeros(cell_pixels, dtype=int)
    counts[: hits.size] = np.sort(hits)[::-1]
    return Coverage(cell_pixels=cell_pixels, counts=counts)


# ---------------------------------------------------------------- placement
@dataclass(frozen=True)
class PlacedImage:
    """An image on the output raster and what is known about each pixel.

    Attributes:
        image: ``(rows, cols)``, NaN where no sample landed.
        count: Samples per pixel (:func:`place_nearest`) or the local sample
            density (:func:`grid_bspline`).
        variance: Variance of each pixel when sample variances were given.
        max_residual: Largest distance of a sample to the pixel it was placed
            on, in raster pixels; 0 for a commensurate scan.
        converged: Whether the gridding iteration reached its tolerance.
        method: ``"exact"`` (placed) or ``"gridded"`` (interpolated).
    """

    image: np.ndarray
    count: np.ndarray
    variance: np.ndarray | None = None
    max_residual: float = 0.0
    converged: bool = True
    method: str = "exact"


@dataclass(frozen=True)
class PlacementPlan:
    """Prepared placement geometry, reusable for any values on the samples.

    ``method`` is decided from coordinates once.  For exact/direct placement
    ``flat_pixel`` contains the precomputed output pixel of every sample
    (``-1`` outside the raster), so amplitude/background/haze do not repeat
    coordinate rounding and bounds work.
    """

    gx: np.ndarray
    gy: np.ndarray
    shape: tuple[int, int]
    method: str
    max_residual: float
    smoothing: float = DEFAULT_SMOOTHING
    flat_pixel: np.ndarray | None = None

    def place(
        self, values: np.ndarray, variance: np.ndarray | None = None
    ) -> PlacedImage:
        if self.method == "exact":
            return _place_prepared_exact(self, values, variance)
        return grid_bspline(
            self.gx, self.gy, values, self.shape, variance, smoothing=self.smoothing
        )

    def place_device(self, values, variance=None):
        """Direct placement on CuPy, preserving arrays on the device.

        Interpolation deliberately remains the CPU/SciPy exceptional path.
        """
        if self.method != "exact":
            raise RuntimeError("Device placement is only available for direct placement")
        from .extraction import CUPY_AVAILABLE, cp

        if not CUPY_AVAILABLE:
            raise RuntimeError("CuPy is not available")
        rows, cols = self.shape
        values_array = cp.asarray(values)
        if values_array.size != self.gx.size:
            raise ValueError("values do not match the prepared placement samples")
        variance_array = None
        if variance is not None:
            variance_array = cp.broadcast_to(cp.asarray(variance), values_array.shape)
        values = values_array.reshape(-1)
        variance = None if variance_array is None else variance_array.reshape(-1)
        flat_pixel = cp.asarray(self.flat_pixel)
        keep = (flat_pixel >= 0) & cp.isfinite(values)
        weight = cp.ones(values.size, dtype=cp.float64)
        if variance is not None:
            keep = keep & cp.isfinite(variance) & (variance > 0)
            weight = cp.where(keep, 1.0 / cp.where(keep, variance, 1.0), 0.0)
        index = flat_pixel[keep]
        size = rows * cols
        total_weight = cp.bincount(index, weights=weight[keep], minlength=size)
        total = cp.bincount(index, weights=(weight * values)[keep], minlength=size)
        count = cp.bincount(index, minlength=size)
        image = cp.where(count > 0, total / total_weight, cp.nan).reshape(rows, cols)
        pixel_variance = None
        if variance is not None:
            pixel_variance = cp.where(
                count > 0, 1.0 / total_weight, cp.nan
            ).reshape(rows, cols)
        return PlacedImage(
            image=image, count=count.reshape(rows, cols), variance=pixel_variance,
            max_residual=self.max_residual, method="exact",
        )


def _flatten_samples(gx, gy, values, variance):
    values = np.asarray(values, dtype=float)
    if variance is not None:
        variance = np.broadcast_to(
            np.asarray(variance, dtype=float), values.shape
        ).ravel()
    gx = np.asarray(gx, dtype=float).ravel()
    gy = np.asarray(gy, dtype=float).ravel()
    values = values.ravel()
    if not (gx.size == gy.size == values.size):
        raise ValueError("gx, gy and values must have the same number of samples")
    keep = np.isfinite(gx) & np.isfinite(gy) & np.isfinite(values)
    weight = np.ones(values.size)
    if variance is not None:
        keep &= np.isfinite(variance) & (variance > 0)
        weight = np.where(keep, 1.0 / np.where(keep, variance, 1.0), 0.0)
    return gx[keep], gy[keep], values[keep], weight[keep]


def prepare_placement(
    gx: np.ndarray,
    gy: np.ndarray,
    shape: tuple[int, int],
    tolerance: float = COMMENSURATE_TOLERANCE,
    smoothing: float = DEFAULT_SMOOTHING,
    mode: str = "auto",
) -> PlacementPlan:
    """Prepare placement once.

    ``mode`` is ``"auto"`` (direct when within ``tolerance``), ``"direct"``
    (require direct placement) or ``"interpolate"`` (always grid).
    """
    rows, cols = int(shape[0]), int(shape[1])
    gx = np.asarray(gx, dtype=float)
    gy = np.asarray(gy, dtype=float)
    if gx.shape != gy.shape:
        raise ValueError("gx and gy must have the same shape")
    fx, fy = gx.ravel(), gy.ravel()
    finite = np.isfinite(fx) & np.isfinite(fy)
    component_residual = np.maximum(
        np.abs(fx[finite] - np.rint(fx[finite])),
        np.abs(fy[finite] - np.rint(fy[finite])),
    )
    compatible = component_residual.size == 0 or float(component_residual.max()) <= tolerance
    mode = str(mode).strip().lower()
    if mode not in ("auto", "direct", "interpolate"):
        raise ValueError(f"Unknown placement mode {mode!r}")
    if mode == "direct" and not compatible:
        largest = float(component_residual.max()) if component_residual.size else 0.0
        raise ValueError(
            f"Direct placement requested, but samples are {largest:.4g} raster pixels "
            f"off-grid (tolerance {float(tolerance):.4g})"
        )
    direct = compatible if mode == "auto" else mode == "direct"
    euclidean = np.hypot(
        fx[finite] - np.rint(fx[finite]), fy[finite] - np.rint(fy[finite])
    )
    max_residual = float(euclidean.max()) if euclidean.size else 0.0
    flat_pixel = None
    if direct:
        ix = np.rint(fx).astype(np.int64)
        iy = np.rint(fy).astype(np.int64)
        inside = finite & (ix >= 0) & (ix < cols) & (iy >= 0) & (iy < rows)
        flat_pixel = np.full(fx.size, -1, dtype=np.int64)
        flat_pixel[inside] = iy[inside] * cols + ix[inside]
    return PlacementPlan(
        gx=gx, gy=gy, shape=(rows, cols),
        method="exact" if direct else "gridded",
        max_residual=max_residual, smoothing=float(smoothing), flat_pixel=flat_pixel,
    )


def _place_prepared_exact(
    plan: PlacementPlan, values: np.ndarray, variance: np.ndarray | None = None
) -> PlacedImage:
    rows, cols = plan.shape
    values_array = np.asarray(values, dtype=float)
    if values_array.size != plan.gx.size:
        raise ValueError("values do not match the prepared placement samples")
    variance_array = None
    if variance is not None:
        variance_array = np.broadcast_to(
            np.asarray(variance, dtype=float), values_array.shape
        )
    values = values_array.ravel()
    variance = None if variance_array is None else variance_array.ravel()
    flat_pixel = np.asarray(plan.flat_pixel, dtype=np.int64)
    keep = (flat_pixel >= 0) & np.isfinite(values)
    weight = np.ones(values.size)
    if variance is not None:
        keep &= np.isfinite(variance) & (variance > 0)
        weight = np.where(keep, 1.0 / np.where(keep, variance, 1.0), 0.0)
    flat = flat_pixel[keep]
    size = rows * cols
    total_weight = np.bincount(flat, weights=weight[keep], minlength=size)
    total = np.bincount(flat, weights=(weight * values)[keep], minlength=size)
    count = np.bincount(flat, minlength=size)
    with np.errstate(invalid="ignore", divide="ignore"):
        image = np.where(count > 0, total / total_weight, np.nan)
        pixel_variance = (
            np.where(count > 0, 1.0 / total_weight, np.nan)
            if variance is not None else None
        )
    return PlacedImage(
        image=image.reshape(rows, cols), count=count.reshape(rows, cols),
        variance=None if pixel_variance is None else pixel_variance.reshape(rows, cols),
        max_residual=plan.max_residual, method="exact",
    )


def place_nearest(
    gx: np.ndarray,
    gy: np.ndarray,
    values: np.ndarray,
    shape: tuple[int, int],
    variance: np.ndarray | None = None,
) -> PlacedImage:
    """Put every sample on its nearest raster pixel.

    Samples sharing a pixel are averaged, weighted by their inverse variance
    when ``variance`` is given. For a commensurate scan that covers the cell
    once this is exact: one sample per pixel, at the pixel.
    """
    rows, cols = int(shape[0]), int(shape[1])
    gx, gy, values, weight = _flatten_samples(gx, gy, values, variance)
    ix = np.rint(gx).astype(np.int64)
    iy = np.rint(gy).astype(np.int64)
    inside = (ix >= 0) & (ix < cols) & (iy >= 0) & (iy < rows)
    ix, iy = ix[inside], iy[inside]
    residual = np.hypot(gx[inside] - ix, gy[inside] - iy)
    flat = iy * cols + ix
    size = rows * cols
    total_weight = np.bincount(flat, weights=weight[inside], minlength=size)
    total = np.bincount(flat, weights=(weight * values)[inside], minlength=size)
    count = np.bincount(flat, minlength=size)
    with np.errstate(invalid="ignore", divide="ignore"):
        image = np.where(count > 0, total / total_weight, np.nan)
        pixel_variance = (
            np.where(count > 0, 1.0 / total_weight, np.nan)
            if variance is not None
            else None
        )
    return PlacedImage(
        image=image.reshape(rows, cols),
        count=count.reshape(rows, cols),
        variance=None if pixel_variance is None else pixel_variance.reshape(rows, cols),
        max_residual=float(residual.max()) if residual.size else 0.0,
    )


# ----------------------------------------------------------------- gridding
_PAD = 2


def _bspline3(t: np.ndarray) -> np.ndarray:
    """Cubic B-spline, support (-2, 2)."""
    t = np.abs(t)
    return np.where(
        t < 1.0,
        2.0 / 3.0 - t * t + 0.5 * t * t * t,
        np.where(t < 2.0, (2.0 - t) ** 3 / 6.0, 0.0),
    )


def _interpolation_matrix(
    gx: np.ndarray, gy: np.ndarray, rows: int, cols: int, kernel: str
) -> sparse.csr_matrix:
    """Sparse matrix that evaluates raster coefficients at the samples."""
    x0 = np.floor(gx).astype(np.int64)
    y0 = np.floor(gy).astype(np.int64)
    taps = (-1, 0, 1, 2) if kernel == "cubic" else (0, 1)
    sample = np.arange(gx.size)
    row_parts, col_parts, value_parts = [], [], []
    for dy in taps:
        for dx in taps:
            px, py = x0 + dx, y0 + dy
            if kernel == "cubic":
                value = _bspline3(gx - px) * _bspline3(gy - py)
            else:
                value = (1.0 - np.abs(gx - px)) * (1.0 - np.abs(gy - py))
            inside = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
            row_parts.append(sample[inside])
            col_parts.append((py * cols + px)[inside])
            value_parts.append(value[inside])
    return sparse.csr_matrix(
        (
            np.concatenate(value_parts),
            (np.concatenate(row_parts), np.concatenate(col_parts)),
        ),
        shape=(gx.size, rows * cols),
    )


def _second_difference(n: int) -> sparse.csr_matrix:
    """Second difference along one axis, zero at the two ends."""
    main = np.full(n, -2.0)
    main[[0, -1]] = 0.0
    upper = np.ones(n - 1)
    upper[0] = 0.0
    lower = np.ones(n - 1)
    lower[-1] = 0.0
    return sparse.diags([lower, main, upper], [-1, 0, 1], format="csr")


def _roughness(rows: int, cols: int) -> sparse.csr_matrix:
    """``L^T L`` with ``L`` the Laplacian of the coefficient grid."""
    laplacian = sparse.kron(sparse.identity(rows), _second_difference(cols)) + sparse.kron(
        _second_difference(rows), sparse.identity(cols)
    )
    return (laplacian.T @ laplacian).tocsr()


def grid_bspline(
    gx: np.ndarray,
    gy: np.ndarray,
    values: np.ndarray,
    shape: tuple[int, int],
    variance: np.ndarray | None = None,
    smoothing: float = DEFAULT_SMOOTHING,
    rtol: float = 1e-8,
    max_iterations: int = 500,
) -> PlacedImage:
    """Least-squares cubic B-spline surface through scattered samples.

    Finds the spline coefficients ``c`` that minimize
    ``sum_i w_i (spline(c)(g_i) - v_i)^2 + smoothing * |Laplacian c|^2`` by
    conjugate gradients and returns the spline at the raster pixels. The
    roughness penalty only decides what the data leave open: it continues the
    surface smoothly through thinly sampled regions instead of pulling it to
    zero there.

    The tight CG tolerance makes gridding reproducible when Gaussian
    coefficients are extracted in streaming chunks rather than one batch.
    A looser solve can amplify round-off differences after cell-offset
    correction and cause finite/streaming disagreement.

    At the default ``smoothing`` noise is passed through, not smoothed: with
    one sample per pixel on the raster the result has the samples' noise.
    Off the raster an interpolation amplifies noise (by 10-25 % in the cases
    of the design document); a larger ``smoothing`` trades that against
    resolution, 1e-3 attenuating the finest structure by about a tenth.
    Pixels whose neighbourhood holds no samples are NaN; the surface there
    would be an extrapolation.

    Args:
        gx, gy: Sample positions in raster coordinates.
        values: Sample values.
        shape: ``(rows, cols)`` of the raster.
        variance: Optional sample variances; the fit weights each sample by
            its inverse.
        smoothing: Weight of the roughness penalty relative to the mean
            sample weight per pixel.
    """
    rows, cols = int(shape[0]), int(shape[1])
    gx, gy, values, weight = _flatten_samples(gx, gy, values, variance)
    near = (gx > -1.0) & (gx < cols) & (gy > -1.0) & (gy < rows)
    gx, gy, values, weight = gx[near], gy[near], values[near], weight[near]
    if values.size == 0:
        empty = np.full((rows, cols), np.nan)
        return PlacedImage(
            image=empty, count=np.zeros((rows, cols)), converged=False,
            method="gridded",
        )

    # The coefficient grid extends two pixels beyond the raster so that every
    # sample has its full 4x4 support.
    grid_rows, grid_cols = rows + 2 * _PAD, cols + 2 * _PAD
    evaluate = _interpolation_matrix(gx + _PAD, gy + _PAD, grid_rows, grid_cols, "cubic")
    splat = evaluate.T.tocsr()
    size = grid_rows * grid_cols
    mean_weight = float(weight.sum()) / (rows * cols)
    penalty = (smoothing * mean_weight) * _roughness(grid_rows, grid_cols)
    floor = 1e-9 * mean_weight

    def normal(c: np.ndarray) -> np.ndarray:
        return splat @ (weight * (evaluate @ c)) + penalty @ c + floor * c

    diagonal = (
        splat.multiply(splat) @ weight + penalty.diagonal() + floor
    )
    operator = LinearOperator((size, size), matvec=normal, dtype=float)
    preconditioner = LinearOperator(
        (size, size), matvec=lambda c: c / diagonal, dtype=float
    )
    right_hand_side = splat @ (weight * values)

    density = (
        _interpolation_matrix(gx, gy, rows, cols, "linear").T @ np.ones(gx.size)
    ).reshape(rows, cols)
    start = np.where(diagonal > 0, right_hand_side / diagonal, 0.0)
    coefficients, info = cg(
        operator,
        right_hand_side,
        x0=start,
        rtol=rtol,
        maxiter=max_iterations,
        M=preconditioner,
    )
    coefficients = coefficients.reshape(grid_rows, grid_cols)
    smooth = (
        coefficients[:, :-2] + 4.0 * coefficients[:, 1:-1] + coefficients[:, 2:]
    ) / 6.0
    smooth = (smooth[:-2] + 4.0 * smooth[1:-1] + smooth[2:]) / 6.0
    image = smooth[_PAD - 1:_PAD - 1 + rows, _PAD - 1:_PAD - 1 + cols]
    image = np.where(density >= HOLE_DENSITY, image, np.nan)
    residual = np.hypot(gx - np.rint(gx), gy - np.rint(gy))
    return PlacedImage(
        image=image,
        count=density,
        max_residual=float(residual.max()),
        converged=info == 0,
        method="gridded",
    )


def place(
    gx: np.ndarray,
    gy: np.ndarray,
    values: np.ndarray,
    shape: tuple[int, int],
    variance: np.ndarray | None = None,
    tolerance: float = COMMENSURATE_TOLERANCE,
    smoothing: float = DEFAULT_SMOOTHING,
) -> PlacedImage:
    """Place exactly when the samples sit on the raster, grid otherwise.

    The decision is made on the samples themselves, not on the nominal
    geometry: measured focus centres and a scan step that is a fraction of a
    percent off both move samples off the raster.
    """
    return prepare_placement(
        gx, gy, shape, tolerance=tolerance, smoothing=smoothing
    ).place(values, variance)


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
