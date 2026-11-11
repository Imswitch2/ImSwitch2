"""Joint least-squares extraction of the focus amplitudes of a MoNaLISA frame.

A frame is modelled as a sum of spots, one per focus, on a background::

    frame = sum_f  amplitude_f * spot_f  +  background

and the amplitudes of *all* foci are fitted together. The isolated per-focus
fit of the fast-Gauss path assumes the neighbouring spots are absent, so it
reads a share of their light as its own: a ghost of the image one lattice
period away, which grows with the footprint and is what kept the pinhole
small. The joint fit has no such crosstalk, which makes a wide, low-noise
footprint safe (``docs/monalisa_optimal_reconstruction.md``, section 2.3 and
the review in section 8).

The fit is linear and its geometry is fixed for a recording, so everything
expensive happens once in :meth:`ExtractionOperator.build`. Per frame the
work is one sparse product with the design and one pre-factorized sparse
solve of the normal equations.

Coordinates are ``(x, y)`` in camera pixels, x along columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.optimize import minimize_scalar
from scipy.sparse.linalg import splu
from scipy.spatial import cKDTree

try:
    import cupy as cp
    import cupyx.scipy.sparse as cusparse
    import cupyx.scipy.sparse.linalg as cusparse_linalg

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None
    cusparse = None
    cusparse_linalg = None

BACKGROUND_MODELS = ("none", "constant", "constant+haze")

DEFAULT_REACH_SIGMA = 2.5
# The spot columns are cut where the Gaussian has fallen to 3e-7 of its peak.
SPOT_SUPPORT_SIGMA = 5.5
HAZE_SUPPORT_SIGMA = 3.0
DEFAULT_MIN_PIXELS = 10
_RIDGE = 1e-10
_BLOCK_ENTRIES = 2_000_000


@dataclass(frozen=True)
class ExtractionResult:
    """Coefficients of a batch of frames, one row per frame, one column per focus.

    Foci the operator could not fit (see ``ExtractionOperator.valid``) are NaN.
    ``noise_sigma`` is the per-frame residual standard deviation of the fit:
    the noise level if the model is right, and larger if it is not.

    ``coefficients`` is the fitted linear-model state retained for downstream
    operations such as reassignment.  It is deliberately not part of the
    displayed/public result contract, but keeping it prevents those operations
    from solving the same normal equations a second time.
    """

    amplitude: np.ndarray
    background: np.ndarray | None
    haze: np.ndarray | None
    noise_sigma: np.ndarray
    coefficients: np.ndarray = field(repr=False, compare=False)

    def sliced(self, item) -> "ExtractionResult":
        """View a subset of frames while retaining their fitted state."""
        background = None if self.background is None else self.background[item]
        haze = None if self.haze is None else self.haze[item]
        return ExtractionResult(
            amplitude=self.amplitude[item],
            background=background,
            haze=haze,
            noise_sigma=self.noise_sigma[item],
            coefficients=self.coefficients[item],
        )


@dataclass(frozen=True)
class Footprints:
    """The pixels of every extracted focus' footprint, one entry per pair.

    Attributes:
        focus: Focus index of each entry.
        row: Row of the fit the entry's pixel is.
        dx, dy: Offset of the pixel from the focus centre, px.
        weight: Value of the focus' spot model at the pixel.
    """

    focus: np.ndarray
    row: np.ndarray
    dx: np.ndarray
    dy: np.ndarray
    weight: np.ndarray


def _disc_entries(
    x: np.ndarray,
    y: np.ndarray,
    radius: np.ndarray,
    frame_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """In-frame pixels within ``radius`` of each centre.

    Returns ``(focus, pixel, distance2)``: for every (focus, pixel) pair the
    focus index, the flat pixel index and the squared distance of the pixel
    to the true (sub-pixel) centre.
    """
    rows, cols = frame_shape
    focus_parts, pixel_parts, dist_parts = [], [], []
    window = (2 * (int(np.ceil(float(radius.max()))) + 1) + 1) ** 2
    block = max(1, _BLOCK_ENTRIES // window)
    for start in range(0, x.size, block):
        stop = min(start + block, x.size)
        bx, by, br = x[start:stop], y[start:stop], radius[start:stop]
        half = int(np.ceil(float(br.max()))) + 1
        span = np.arange(-half, half + 1)
        oy, ox = np.meshgrid(span, span, indexing="ij")
        px = np.rint(bx).astype(np.int64)[:, None] + ox.ravel()[None, :]
        py = np.rint(by).astype(np.int64)[:, None] + oy.ravel()[None, :]
        dist2 = (px - bx[:, None]) ** 2 + (py - by[:, None]) ** 2
        keep = (
            (dist2 <= (br * br)[:, None])
            & (px >= 0)
            & (px < cols)
            & (py >= 0)
            & (py < rows)
        )
        focus = np.broadcast_to(np.arange(start, stop)[:, None], px.shape)
        focus_parts.append(focus[keep])
        pixel_parts.append((py * cols + px)[keep])
        dist_parts.append(dist2[keep])
    return (
        np.concatenate(focus_parts),
        np.concatenate(pixel_parts),
        np.concatenate(dist_parts),
    )


def _square_entries(
    x: np.ndarray, y: np.ndarray, side: int, frame_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Square sensor-pixel support matching Local camera_pixels exactly.

    Centres are rounded only to select pixel coordinates; Gaussian distances
    always use the true subpixel centre.  Border clipping is identical to Local.
    """
    rows, cols = frame_shape
    half = (side - 1) // 2
    offsets = np.arange(-half, half + 1, dtype=np.int64)
    oy, ox = np.meshgrid(offsets, offsets, indexing="ij")
    dx, dy = ox.ravel(), oy.ravel()
    block = max(1, _BLOCK_ENTRIES // (side * side))
    focuses, pixels, distances = [], [], []
    for start in range(0, x.size, block):
        stop = min(start + block, x.size)
        px = np.rint(x[start:stop]).astype(np.int64)[:, None] + dx
        py = np.rint(y[start:stop]).astype(np.int64)[:, None] + dy
        keep = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
        indices = np.broadcast_to(np.arange(start, stop)[:, None], px.shape)
        focuses.append(indices[keep])
        pixels.append((py * cols + px)[keep])
        distances.append(((px - x[start:stop, None])**2
                          + (py - y[start:stop, None])**2)[keep])
    return np.concatenate(focuses), np.concatenate(pixels), np.concatenate(distances)


def _lattice_cell_entries(
    x: np.ndarray,
    y: np.ndarray,
    frame_shape: tuple[int, int],
    lattice,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Assign camera pixels to canonical DLL unit cells, as Local does.

    The partition is made from lattice period/offset, *not* the potentially
    refined fitting centres. The returned distances use the actual fitting
    centres, however. Each returned pixel has precisely one owner/focus.
    The geometry is restricted to an axis-aligned rectangular lattice.
    """
    if lattice is None:
        raise ValueError("Joint lattice unit cells require a lattice")
    try:
        per_x, off_x, per_y, off_y = lattice.to_grid_params(tol=0.02)
    except ValueError as exc:
        raise ValueError("Joint lattice unit cells require an axis-aligned rectangular lattice") from exc
    if min(per_x, per_y) <= 0:
        raise ValueError("Invalid DLL grid period")
    rows, cols = frame_shape
    nr = 1 + int((rows - 1 - off_y) / per_y + .5)
    nc = 1 + int((cols - 1 - off_x) / per_x + .5)
    if nr < 1 or nc < 1:
        raise ValueError("DLL grid does not intersect the camera")
    ir = np.rint((y - off_y) / per_y).astype(np.int64)
    ic = np.rint((x - off_x) / per_x).astype(np.int64)
    grid_x = off_x + ic * per_x
    grid_y = off_y + ir * per_y
    assigned = (
        (ir >= 0) & (ir < nr) & (ic >= 0) & (ic < nc)
        & (np.abs(x - grid_x) < .45 * per_x)
        & (np.abs(y - grid_y) < .45 * per_y)
    )
    focus_for_cell = np.full(nr * nc, -1, dtype=np.int64)
    focus = np.flatnonzero(assigned)
    cells = ir[focus] * nc + ic[focus]
    if np.unique(cells).size != cells.size:
        raise ValueError("Joint lattice support has duplicate focus cells")
    focus_for_cell[cells] = focus

    # The half-open floor(+0.5) convention is identical to Local's partition,
    # including its handling of even lattice periods and fractional offsets.
    yy = np.floor((np.arange(rows) - off_y) / per_y + .5).astype(np.int64)
    xx = np.floor((np.arange(cols) - off_x) / per_x + .5).astype(np.int64)
    inside = ((yy[:, None] >= 0) & (yy[:, None] < nr)
              & (xx[None, :] >= 0) & (xx[None, :] < nc))
    pixels = np.flatnonzero(inside)
    cell_ids = (yy[:, None] * nc + xx[None, :]).ravel()[pixels]
    owners = focus_for_cell[cell_ids]
    keep = owners >= 0
    pixel, owners = pixels[keep], owners[keep]
    dist2 = (pixel % cols - x[owners])**2 + (pixel // cols - y[owners])**2
    return owners, pixel, dist2


def _haze_shape(dist2: np.ndarray, sigma: float) -> np.ndarray:
    """A wide Gaussian, shifted to reach zero at its support radius.

    Cutting a wide Gaussian at three sigma would leave a 1 % step in the
    model; shifting it removes the step.
    """
    floor = np.exp(-0.5 * HAZE_SUPPORT_SIGMA**2)
    return (np.exp(-dist2 / (2.0 * sigma * sigma)) - floor) / (1.0 - floor)


def _check_arguments(x, y, sigma, reach_sigma, background, haze_sigma):
    if background not in BACKGROUND_MODELS:
        raise ValueError(
            f"Unknown background model {background!r}; "
            f"expected one of {BACKGROUND_MODELS}"
        )
    if background == "constant+haze" and not (haze_sigma and haze_sigma > 0):
        raise ValueError("The haze background needs a positive haze_sigma")
    if reach_sigma <= 0:
        raise ValueError("reach_sigma must be > 0")
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    if x.size == 0 or x.shape != y.shape:
        raise ValueError("x and y must be non-empty and of the same length")
    sigma = np.broadcast_to(np.asarray(sigma, dtype=float), x.shape).copy()
    if np.any(sigma <= 0) or not np.all(np.isfinite(sigma)):
        raise ValueError("sigma must be finite and > 0")
    return x, y, sigma


def _usable_pixels(num_pixels, pixel_mask, pixel_variance):
    """Which pixels may enter the fit, and their variance if one was given."""
    usable = np.ones(num_pixels, dtype=bool)
    variance = None
    if pixel_mask is not None:
        usable &= np.asarray(pixel_mask, dtype=bool).reshape(num_pixels)
    if pixel_variance is not None:
        variance = np.asarray(pixel_variance, dtype=float).reshape(num_pixels)
        usable &= np.isfinite(variance) & (variance > 0)
    return usable, variance


def _joint_terms(
    x, y, sigma, frame_shape, fit_pixel, background, haze_sigma,
    *, lattice_owners=None,
):
    """Rows and model terms of the joint fit: one row per fitted pixel.

    Every term is ``(kind, focus, row, value)``, the entries of the design
    matrix that belong to one kind of coefficient.
    """
    cols = frame_shape[1]
    row_pixel = np.unique(fit_pixel)
    row_of_pixel = np.full(frame_shape[0] * cols, -1, dtype=np.int64)
    row_of_pixel[row_pixel] = np.arange(row_pixel.size)

    def within(radius):
        focus, pixel, dist2 = _disc_entries(x, y, radius, frame_shape)
        row = row_of_pixel[pixel]
        fitted = row >= 0
        return focus[fitted], row[fitted], dist2[fitted]

    focus, row, dist2 = within(SPOT_SUPPORT_SIGMA * sigma)
    terms = [("spot", focus, row, np.exp(-dist2 / (2.0 * sigma[focus] ** 2)))]
    if background == "constant+haze":
        focus, row, dist2 = within(np.full(x.size, HAZE_SUPPORT_SIGMA * haze_sigma))
        terms.append(("haze", focus, row, _haze_shape(dist2, float(haze_sigma))))
    if background != "none":
        # The constant of a focus holds over the pixels nearest to it.
        if lattice_owners is None:
            _, owner = cKDTree(np.column_stack([x, y])).query(
                np.column_stack([row_pixel % cols, row_pixel // cols])
            )
        else:
            # DLL-style cells own their background regardless of fitted centre.
            owner = lattice_owners[row_pixel]
            if np.any(owner < 0):
                raise ValueError("Joint lattice background has unowned fit pixels")
        terms.append(
            ("constant", owner, np.arange(row_pixel.size), np.ones(row_pixel.size))
        )
    return row_pixel, terms


def _isolated_terms(focus, pixel, dist2, sigma, background, haze_sigma):
    """Rows and model terms of the isolated fits.

    Every focus gets private copies of its footprint's pixels, so overlapping
    footprints do not couple the fits.
    """
    row = np.arange(pixel.size)
    terms = [("spot", focus, row, np.exp(-dist2 / (2.0 * sigma[focus] ** 2)))]
    if background == "constant+haze":
        terms.append(("haze", focus, row, _haze_shape(dist2, float(haze_sigma))))
    if background != "none":
        terms.append(("constant", focus, row, np.ones(pixel.size)))
    return pixel, terms


def _assemble(terms, num_rows, valid):
    """The design matrix, one column per (kind, focus) that has any support.

    The foci around the frame enter the joint fit as long as their spot
    reaches into it; a focus without a spot column cannot be extracted.

    Returns ``(design, column_of, valid)``: ``column_of[kind][focus]`` is the
    column of that coefficient, or -1.
    """
    num_foci = valid.size
    column_of = {}
    rows, columns, values = [], [], []
    next_column = 0
    for kind, focus, row, value in terms:
        energy = np.bincount(focus, weights=value * value, minlength=num_foci)
        present = energy > 0
        if kind == "spot":
            present &= energy > 1e-6 * float(np.median(energy[valid]))
            valid = valid & present
        column = np.full(num_foci, -1, dtype=np.int64)
        column[present] = next_column + np.arange(int(present.sum()))
        next_column += int(present.sum())
        keep = column[focus] >= 0
        rows.append(row[keep])
        columns.append(column[focus[keep]])
        values.append(value[keep])
        column_of[kind] = column
    design = sparse.csc_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(columns))),
        shape=(num_rows, next_column),
    )
    return design, column_of, valid


class ExtractionOperator:
    """Precomputed least-squares extraction for one geometry.

    Build it with :meth:`build`, apply it with :meth:`apply`.

    Attributes:
        frame_shape: ``(rows, cols)`` of the frames it applies to.
        num_foci: Number of foci it was built for.
        valid: ``(num_foci,)`` bool, foci whose amplitude it returns. A focus
            is not valid when its centre is outside the frame or fewer than
            ``min_pixels`` pixels of its footprint are inside.
        pixels_per_focus: ``(num_foci,)`` in-frame footprint pixels.
        joint: Whether the foci are fitted together.
        background: The background model.
    """

    def __init__(
        self,
        frame_shape: tuple[int, int],
        row_pixel: np.ndarray,
        design: sparse.csc_matrix,
        row_weight: np.ndarray | None,
        amplitude_column: np.ndarray,
        background_column: np.ndarray | None,
        haze_column: np.ndarray | None,
        valid: np.ndarray,
        pixels_per_focus: np.ndarray,
        joint: bool,
        background: str,
        reach_sigma: float,
        footprints: Footprints,
    ):
        self.footprints = footprints
        self.frame_shape = tuple(int(n) for n in frame_shape)
        self.num_foci = int(valid.size)
        self.valid = valid
        self.pixels_per_focus = pixels_per_focus
        self.joint = bool(joint)
        self.background = background
        self.reach_sigma = float(reach_sigma)
        self._row_pixel = row_pixel
        self._row_weight = row_weight
        self._design = design
        self._amplitude_column = amplitude_column
        self._background_column = background_column
        self._haze_column = haze_column

        weighted = design if row_weight is None else sparse.diags(row_weight) @ design
        self._projector = weighted.T.tocsr()
        normal = (self._projector @ design).tocsc()
        diagonal = normal.diagonal()
        normal = normal + sparse.diags(_RIDGE * diagonal + 1e-300, format="csc")
        self._solver = splu(
            normal,
            permc_spec="MMD_AT_PLUS_A",
            diag_pivot_thresh=0.0,
            options={"SymmetricMode": True},
        )
        self._variance_gain: np.ndarray | None = None
        self._device_executor = None

    # ------------------------------------------------------------------ build
    @classmethod
    def build(
        cls,
        x: np.ndarray,
        y: np.ndarray,
        sigma: float | np.ndarray,
        frame_shape: tuple[int, int],
        *,
        reach_sigma: float = DEFAULT_REACH_SIGMA,
        fit_radius_px: float | None = None,
        footprint_mode: str = "auto",
        fit_shape: str = "circle",
        fit_size_px: float | None = None,
        lattice=None,
        background: str = "constant",
        haze_sigma: float | None = None,
        joint: bool = True,
        pixel_variance: np.ndarray | None = None,
        pixel_mask: np.ndarray | None = None,
        min_pixels: int = DEFAULT_MIN_PIXELS,
    ) -> "ExtractionOperator":
        """Set up the extraction for foci at ``(x, y)`` with widths ``sigma``.

        Args:
            x, y: Focus centres, px. Include the foci just outside the frame
                (``Lattice.points_in_frame(margin=...)``): their tails reach
                into it and the joint fit accounts for them.
            sigma: Spot width, one value or one per focus.
            frame_shape: ``(rows, cols)``.
            reach_sigma: Fit-pixel radius in spot sigmas for "auto" (2.5 by
                default). This is distinct from the 5.5-sigma model support.
            fit_radius_px: Fixed circular fit-pixel radius, in camera pixels,
                used when footprint_mode is "fixed".
            footprint_mode: "auto" (sigma-scaled circle), "fixed" (circle of
                given radius), or "lattice" (DLL rectangular unit-cell pixels).
            fit_shape: "square" selects an odd-square camera-pixel footprint;
                "circle" retains the original radial fit-pixel selection.
            fit_size_px: Odd side in camera pixels for square support.
            lattice: Required for "lattice". The lattice defines pixel
                ownership, while Gaussian centres remain the localized ones.
                Only axis-aligned rectangular lattices are supported.
            background: ``"none"``, ``"constant"`` (one constant per focus,
                over the pixels nearest to it) or ``"constant+haze"`` (plus
                one wide Gaussian per focus).
            haze_sigma: Width of the haze Gaussian, px; required for
                ``"constant+haze"``.
            joint: Fit all foci together. ``False`` fits every focus on its
                own footprint as if it were alone: the fast-Gauss estimator,
                kept for comparison.
            pixel_variance: Optional ``(rows, cols)`` noise variance; the fit
                is then weighted by its inverse.
            pixel_mask: Optional ``(rows, cols)`` bool, ``False`` for pixels
                to leave out (hot or dead pixels).
            min_pixels: Foci with fewer in-frame footprint pixels are not
                extracted.
        """
        x, y, sigma = _check_arguments(
            x, y, sigma, reach_sigma, background, haze_sigma
        )
        footprint_mode = str(footprint_mode).strip().lower()
        if footprint_mode not in ("auto", "fixed", "lattice"):
            raise ValueError("Joint footprint must be auto, fixed, or lattice")
        if fit_radius_px is not None:
            fit_radius_px = float(fit_radius_px)
            if not np.isfinite(fit_radius_px) or fit_radius_px <= 0:
                raise ValueError("Joint fit radius must be finite and > 0")
        if footprint_mode == "fixed" and fit_radius_px is None and fit_shape != "square":
            raise ValueError("Fixed Joint footprint requires a fit radius")
        if footprint_mode == "lattice" and not joint:
            raise ValueError("Joint lattice footprint requires joint extraction")
        if fit_shape not in ("square", "circle"):
            raise ValueError("Joint footprint shape must be square or circle")
        if fit_shape == "square":
            if fit_size_px is None or not np.isfinite(fit_size_px):
                raise ValueError("Square Joint footprint requires a finite side")
            if fit_size_px < 3 or fit_size_px > 255 or fit_size_px % 2 != 1:
                raise ValueError("Square Joint footprint side must be an odd integer 3–255 px")
        rows, cols = (int(n) for n in frame_shape)
        usable, variance = _usable_pixels(rows * cols, pixel_mask, pixel_variance)

        if footprint_mode == "lattice":
            focus, pixel, dist2 = _lattice_cell_entries(x, y, (rows, cols), lattice)
        elif fit_shape == "square":
            focus, pixel, dist2 = _square_entries(x, y, int(fit_size_px), (rows, cols))
        else:
            radius = (reach_sigma * sigma if footprint_mode == "auto" else
                      np.full(x.size, fit_radius_px, dtype=float))
            focus, pixel, dist2 = _disc_entries(x, y, radius, (rows, cols))
        keep = usable[pixel]
        focus, pixel, dist2 = focus[keep], pixel[keep], dist2[keep]
        pixels_per_focus = np.bincount(focus, minlength=x.size)
        centre_inside = (x >= -0.5) & (x < cols - 0.5) & (y >= -0.5) & (y < rows - 0.5)
        valid = centre_inside & (pixels_per_focus >= int(min_pixels))
        if not valid.any():
            raise ValueError("No focus has enough footprint pixels inside the frame")

        if joint:
            lattice_owners = None
            if footprint_mode == "lattice":
                lattice_owners = np.full(rows * cols, -1, dtype=np.int64)
                lattice_owners[pixel] = focus
            row_pixel, terms = _joint_terms(
                x, y, sigma, (rows, cols), pixel, background, haze_sigma,
                lattice_owners=lattice_owners,
            )
            row = np.searchsorted(row_pixel, pixel)
        else:
            keep = valid[focus]
            focus, pixel, dist2 = focus[keep], pixel[keep], dist2[keep]
            row_pixel, terms = _isolated_terms(
                focus, pixel, dist2, sigma, background, haze_sigma
            )
            row = np.arange(pixel.size)
        design, column_of, valid = _assemble(terms, row_pixel.size, valid)
        row_weight = None if variance is None else 1.0 / variance[row_pixel]
        keep = valid[focus]
        footprints = Footprints(
            focus=focus[keep],
            row=row[keep],
            dx=(pixel % cols - x[focus])[keep],
            dy=(pixel // cols - y[focus])[keep],
            weight=np.exp(-dist2 / (2.0 * sigma[focus] ** 2))[keep],
        )
        return cls(
            frame_shape=(rows, cols),
            row_pixel=row_pixel,
            design=design,
            row_weight=row_weight,
            amplitude_column=column_of["spot"],
            background_column=column_of.get("constant"),
            haze_column=column_of.get("haze"),
            valid=valid,
            pixels_per_focus=pixels_per_focus,
            joint=joint,
            background=background,
            reach_sigma=reach_sigma,
            footprints=footprints,
        )

    # ------------------------------------------------------------------ apply
    @property
    def num_fit_pixels(self) -> int:
        """Distinct frame pixels that enter the fit."""
        return int(np.unique(self._row_pixel).size)

    @property
    def num_coefficients(self) -> int:
        return int(self._design.shape[1])

    def _solve(
        self, frames: np.ndarray, chunk: int, *, with_noise: bool = True
    ) -> tuple[np.ndarray, np.ndarray | None]:
        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[None]
        if frames.ndim != 3 or frames.shape[1:] != self.frame_shape:
            raise ValueError(
                f"Expected frames of shape (K, {self.frame_shape[0]}, "
                f"{self.frame_shape[1]}), got {frames.shape}"
            )
        flat = frames.reshape(frames.shape[0], -1)
        coefficients = np.empty((frames.shape[0], self.num_coefficients))
        noise = np.empty(frames.shape[0]) if with_noise else None
        dof = max(self._row_pixel.size - self.num_coefficients, 1)
        for start in range(0, frames.shape[0], chunk):
            data = flat[start:start + chunk][:, self._row_pixel].astype(float).T
            projected = self._projector @ data
            solution = self._solver.solve(projected)
            coefficients[start:start + chunk] = solution.T
            if with_noise:
                weighted = (
                    data if self._row_weight is None
                    else data * self._row_weight[:, None]
                )
                rss = np.sum(data * weighted, axis=0) - np.sum(
                    projected * solution, axis=0
                )
                noise[start:start + chunk] = np.sqrt(np.maximum(rss, 0.0) / dof)
        return coefficients, noise

    def _per_focus(self, coefficients: np.ndarray, column: np.ndarray) -> np.ndarray:
        out = np.full((coefficients.shape[0], self.num_foci), np.nan)
        have = self.valid & (column >= 0)
        out[:, have] = coefficients[:, column[have]]
        return out

    def _result(self, coefficients: np.ndarray, noise: np.ndarray) -> ExtractionResult:
        """Build the public per-focus view of one already-solved fit."""
        background = haze = None
        if self._background_column is not None:
            background = self._per_focus(coefficients, self._background_column)
        if self._haze_column is not None:
            haze = self._per_focus(coefficients, self._haze_column)
        return ExtractionResult(
            amplitude=self._per_focus(coefficients, self._amplitude_column),
            background=background,
            haze=haze,
            noise_sigma=noise,
            coefficients=coefficients,
        )

    def _fast_result(self, coefficients: np.ndarray) -> ExtractionResult:
        """The fitted planes Fast needs, without residual/noise diagnostics.

        Mature Fast Gauss solved the same joint normal equations but copied
        only the per-focus coefficient planes needed for reconstruction.  The
        unified pipeline initially routed Fast through :meth:`apply`, which
        also calculates residual noise and retains the complete coefficient
        matrix.  This lightweight result restores the old execution contract
        while keeping the exact same estimator.

        ``coefficients`` is intentionally empty: Fast modes that need the raw
        fitted state (reassignment/pinhole diagnostics) must use the full
        extraction path instead.
        """
        background = haze = None
        if self._background_column is not None:
            background = self._per_focus(coefficients, self._background_column)
        if self._haze_column is not None:
            haze = self._per_focus(coefficients, self._haze_column)
        return ExtractionResult(
            amplitude=self._per_focus(coefficients, self._amplitude_column),
            background=background,
            haze=haze,
            noise_sigma=np.full(coefficients.shape[0], np.nan),
            coefficients=np.empty((coefficients.shape[0], 0), dtype=float),
        )

    def apply(self, frames: np.ndarray, chunk: int = 64) -> ExtractionResult:
        """Extract the coefficients of ``frames``, shape ``(K, rows, cols)``.

        A single 2D frame is accepted and treated as ``K = 1``.  The fitted
        coefficients are retained in the result so downstream processing can
        reuse this solve.
        """
        coefficients, noise = self._solve(frames, int(chunk))
        return self._result(coefficients, noise)

    def apply_device(self, frames, device: str = "CPU", chunk: int = 64) -> ExtractionResult:
        """Extract on the requested backend, returning the normal host result.

        The GPU factorization is cached on the operator because one calibrated
        geometry is reused for every stack/timepoint of a recording.  If CuPy
        is unavailable, ``GPU`` falls back to the CPU backend, matching the
        existing Fast-Gauss behavior.
        """
        choice = str(device or "CPU").strip().lower()
        if choice not in ("cpu", "gpu"):
            raise ValueError(f"Unknown extraction device {device!r}")
        if choice == "gpu" and CUPY_AVAILABLE:
            if self._device_executor is None:
                self._device_executor = DeviceExtractionExecutor(self)
            return self._device_executor.apply(frames)
        return self.apply(frames, chunk=chunk)

    def apply_fast_device(
        self, frames, device: str = "CPU", chunk: int = 64
    ) -> ExtractionResult:
        """Fast joint extraction without residual/noise work or retained state.

        The mathematical solve is identical to :meth:`apply_device`.  Only
        execution differs: residual/noise estimation is skipped and only the
        per-focus amplitude/background/haze planes needed by Fast are copied
        back from the GPU.  This is the execution model used by the mature
        pre-merge Fast-Gauss path.
        """
        choice = str(device or "CPU").strip().lower()
        if choice not in ("cpu", "gpu"):
            raise ValueError(f"Unknown extraction device {device!r}")
        if choice == "gpu" and CUPY_AVAILABLE:
            if self._device_executor is None:
                self._device_executor = DeviceExtractionExecutor(self)
            return self._device_executor.apply_fast(frames)
        coefficients, _ = self._solve(frames, int(chunk), with_noise=False)
        return self._fast_result(coefficients)

    def footprint_values(
        self, frames: np.ndarray, extracted: ExtractionResult | None = None
    ) -> np.ndarray:
        """What every focus alone put on the pixels of its footprint.

        The frames minus the fitted background and minus the fitted spots of
        all other foci, at the entries of :attr:`footprints`: shape
        ``(K, entries)``. These are the values an image-scanning
        reconstruction reassigns (:mod:`.reassignment`); weighted with the
        spot model and summed over a footprint they give back the amplitude.
        """
        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[None]
        if extracted is None:
            coefficients, _ = self._solve(frames, max(1, frames.shape[0]))
        else:
            coefficients = np.asarray(extracted.coefficients)
            if coefficients.ndim == 1:
                coefficients = coefficients[None]
            if coefficients.shape != (frames.shape[0], self.num_coefficients):
                raise ValueError(
                    "The retained extraction does not match the supplied frames "
                    f"({coefficients.shape} coefficients for {frames.shape[0]} frames)"
                )
        flat = frames.reshape(frames.shape[0], -1)
        data = flat[:, self._row_pixel].astype(float)
        residual = data - (self._design @ coefficients.T).T
        entries = self.footprints
        amplitude = coefficients[:, self._amplitude_column[entries.focus]]
        return residual[:, entries.row] + amplitude * entries.weight[None, :]

    def residual(self, frame: np.ndarray) -> np.ndarray:
        """``frame`` minus its fit, NaN outside the fitted pixels.

        What the model does not explain: a structured residual means a wrong
        spot width, displaced centres or a background the model cannot hold.
        In the isolated mode overlapping footprints show the last focus' fit.
        """
        frame = np.asarray(frame, dtype=float)
        coefficients, _ = self._solve(frame, 1)
        fitted = self._design @ coefficients[0]
        out = np.full(self.frame_shape[0] * self.frame_shape[1], np.nan)
        out[self._row_pixel] = frame.reshape(-1)[self._row_pixel] - fitted
        return out.reshape(self.frame_shape)

    # ------------------------------------------------------------ diagnostics
    def weights(self, focus: int) -> tuple[np.ndarray, np.ndarray]:
        """The linear weights that produce the amplitude of one focus.

        Returns ``(pixel, weight)`` with ``amplitude = sum(weight *
        frame.ravel()[pixel])``. For diagnostics and tests: the joint weights
        of a focus reach under its neighbours' spots, so applying the
        extraction through them would cost several times the normal-equation
        solve :meth:`apply` uses.
        """
        column = int(self._amplitude_column[int(focus)])
        if column < 0 or not self.valid[int(focus)]:
            raise ValueError(f"Focus {focus} is not extracted")
        unit = np.zeros(self.num_coefficients)
        unit[column] = 1.0
        weight = self._design @ self._solver.solve(unit)
        if self._row_weight is not None:
            weight = weight * self._row_weight
        keep = weight != 0
        return self._row_pixel[keep], weight[keep]

    @property
    def variance_gain(self) -> np.ndarray:
        """Amplitude variance per unit pixel variance, ``(num_foci,)``.

        For white noise of variance ``s2`` the amplitude of focus ``f`` has
        variance ``s2 * variance_gain[f]``. With ``pixel_variance`` given to
        :meth:`build` the gain is the amplitude variance itself.
        """
        if self._variance_gain is None:
            gain = np.full(self.num_foci, np.nan)
            foci = np.flatnonzero(self.valid)
            columns = self._amplitude_column[foci]
            for start in range(0, foci.size, 256):
                block = columns[start:start + 256]
                unit = np.zeros((self.num_coefficients, block.size))
                unit[block, np.arange(block.size)] = 1.0
                solved = self._solver.solve(unit)
                gain[foci[start:start + 256]] = solved[block, np.arange(block.size)]
            self._variance_gain = gain
        return self._variance_gain


class DeviceExtractionExecutor:
    """GPU executor for any :class:`ExtractionOperator`.

    The geometry lives entirely in ``ExtractionOperator``. This class only
    mirrors its sparse projection and pre-factorized normal-equation solve on
    CuPy, so rectangular Fast Gauss and arbitrary lattice geometries use the
    same device backend.
    """

    def __init__(self, operator: ExtractionOperator):
        if not CUPY_AVAILABLE:
            raise RuntimeError("CuPy is not available")
        self.operator = operator
        self.row_pixel = cp.asarray(operator._row_pixel)
        self.projector = cusparse.csr_matrix(operator._projector.astype(np.float64))
        normal = (operator._projector @ operator._design).tocsc()
        diagonal = normal.diagonal()
        normal = normal + sparse.diags(_RIDGE * diagonal + 1e-300, format="csc")
        self.solver = cusparse_linalg.splu(
            cusparse.csc_matrix(normal.astype(np.float64))
        )
        self.row_weight = (
            None if operator._row_weight is None else cp.asarray(operator._row_weight)
        )
        self.num_coefficients = operator.num_coefficients
        self.frame_shape = operator.frame_shape

    def solve(self, frames, *, with_noise: bool = True):
        """Return the fitted coefficients, and optionally noise, on the device.

        Fast Gauss only needs amplitudes, so its hot path deliberately skips
        the residual/noise calculation that the full extraction result needs.
        """
        chunk = cp.asarray(frames)
        if chunk.ndim == 2:
            chunk = chunk[None]
        if chunk.ndim != 3 or tuple(chunk.shape[1:]) != self.frame_shape:
            raise ValueError(
                f"Expected frames of shape (K, {self.frame_shape[0]}, "
                f"{self.frame_shape[1]}), got {tuple(chunk.shape)}"
            )
        flat = chunk.reshape(chunk.shape[0], -1)
        data = flat[:, self.row_pixel].astype(cp.float64).T
        projected = self.projector @ data
        solution = self.solver.solve(projected)
        coefficients = solution.T
        if not with_noise:
            return coefficients, None
        weighted = data if self.row_weight is None else data * self.row_weight[:, None]
        rss = cp.sum(data * weighted, axis=0) - cp.sum(projected * solution, axis=0)
        dof = max(self.operator._row_pixel.size - self.num_coefficients, 1)
        noise = cp.sqrt(cp.maximum(rss, 0.0) / dof)
        return coefficients, noise

    def amplitudes(self, frames):
        """Per-focus amplitudes on the device, invalid foci returned as zero."""
        coefficients, _ = self.solve(frames, with_noise=False)
        columns = self.operator._amplitude_column
        have = self.operator.valid & (columns >= 0)
        device_columns = cp.asarray(np.where(have, columns, 0))
        amplitude = coefficients[:, device_columns]
        return cp.where(cp.asarray(have)[None, :], amplitude, 0.0).astype(cp.float32)

    def _per_focus(self, coefficients, columns):
        if columns is None:
            return None
        have = self.operator.valid & (columns >= 0)
        device_columns = cp.asarray(np.where(have, columns, 0))
        values = coefficients[:, device_columns]
        return cp.where(cp.asarray(have)[None, :], values, cp.nan)

    def apply_fast(self, frames) -> ExtractionResult:
        """Fast host result: selected per-focus planes, no residual/noise.

        The solve still contains all model coefficients on the device, as the
        joint estimator requires, but the large generic result and residual
        calculation are avoided.  Only reconstruction coefficient planes are
        transferred back to the host.
        """
        coefficients, _ = self.solve(frames, with_noise=False)
        amplitude = self._per_focus(coefficients, self.operator._amplitude_column)
        background = self._per_focus(coefficients, self.operator._background_column)
        haze = self._per_focus(coefficients, self.operator._haze_column)
        count = int(coefficients.shape[0])
        return ExtractionResult(
            amplitude=cp.asnumpy(amplitude),
            background=None if background is None else cp.asnumpy(background),
            haze=None if haze is None else cp.asnumpy(haze),
            noise_sigma=np.full(count, np.nan),
            coefficients=np.empty((count, 0), dtype=float),
        )

    def apply(self, frames) -> ExtractionResult:
        """Run on the GPU and copy the solved fit to a normal host result."""
        coefficients, noise = self.solve(frames, with_noise=True)
        return self.operator._result(cp.asnumpy(coefficients), cp.asnumpy(noise))


def calibrate_haze_sigma(
    mean_frame: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    sigma: float | np.ndarray,
    *,
    reach_sigma: float = DEFAULT_REACH_SIGMA,
    bounds: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Fit the haze width to the mean frame, and say how much it explains.

    A haze term of the wrong width biases the amplitudes more than having
    none (section 8 of the design document), so its width is fitted rather
    than assumed, and the term should only be switched on when it removes a
    substantial share of the residual.

    Returns ``(haze_sigma, residual_ratio)``: the best width and the residual
    standard deviation with the haze term over the one without. A ratio near
    one means the constant background already explains the frame.
    """
    mean_frame = np.asarray(mean_frame, dtype=float)
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    median_sigma = float(np.median(np.broadcast_to(np.asarray(sigma, float), x.shape)))
    if bounds is None:
        if x.size >= 2:
            distance, _ = cKDTree(np.column_stack([x, y])).query(
                np.column_stack([x, y]), k=2
            )
            spacing = float(np.median(distance[:, 1]))
        else:
            spacing = 6.0 * median_sigma
        bounds = (1.5 * median_sigma, max(spacing, 3.0 * median_sigma))

    def noise(background: str, haze_sigma: float | None = None) -> float:
        operator = ExtractionOperator.build(
            x, y, sigma, mean_frame.shape,
            reach_sigma=reach_sigma, background=background, haze_sigma=haze_sigma,
        )
        return float(operator.apply(mean_frame).noise_sigma[0])

    result = minimize_scalar(
        lambda width: noise("constant+haze", float(width)),
        bounds=bounds, method="bounded", options={"xatol": 0.05},
    )
    baseline = noise("constant")
    ratio = float(result.fun / baseline) if baseline > 0 else 1.0
    return float(result.x), ratio


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
