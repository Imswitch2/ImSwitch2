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

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.optimize import minimize_scalar
from scipy.sparse.linalg import splu
from scipy.spatial import cKDTree

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
    """

    amplitude: np.ndarray
    background: np.ndarray | None
    haze: np.ndarray | None
    noise_sigma: np.ndarray


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


def _joint_terms(x, y, sigma, frame_shape, fit_pixel, background, haze_sigma):
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
        _, owner = cKDTree(np.column_stack([x, y])).query(
            np.column_stack([row_pixel % cols, row_pixel // cols])
        )
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
            reach_sigma: Footprint radius in units of each focus' sigma. The
                fit uses the pixels within this radius of any focus.
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
        rows, cols = (int(n) for n in frame_shape)
        usable, variance = _usable_pixels(rows * cols, pixel_mask, pixel_variance)

        focus, pixel, dist2 = _disc_entries(x, y, reach_sigma * sigma, (rows, cols))
        keep = usable[pixel]
        focus, pixel, dist2 = focus[keep], pixel[keep], dist2[keep]
        pixels_per_focus = np.bincount(focus, minlength=x.size)
        centre_inside = (x >= -0.5) & (x < cols - 0.5) & (y >= -0.5) & (y < rows - 0.5)
        valid = centre_inside & (pixels_per_focus >= int(min_pixels))
        if not valid.any():
            raise ValueError("No focus has enough footprint pixels inside the frame")

        if joint:
            row_pixel, terms = _joint_terms(
                x, y, sigma, (rows, cols), pixel, background, haze_sigma
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

    def _solve(self, frames: np.ndarray, chunk: int) -> tuple[np.ndarray, np.ndarray]:
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
        noise = np.empty(frames.shape[0])
        dof = max(self._row_pixel.size - self.num_coefficients, 1)
        for start in range(0, frames.shape[0], chunk):
            data = flat[start:start + chunk][:, self._row_pixel].astype(float).T
            projected = self._projector @ data
            solution = self._solver.solve(projected)
            coefficients[start:start + chunk] = solution.T
            weighted = data if self._row_weight is None else data * self._row_weight[:, None]
            rss = np.sum(data * weighted, axis=0) - np.sum(projected * solution, axis=0)
            noise[start:start + chunk] = np.sqrt(np.maximum(rss, 0.0) / dof)
        return coefficients, noise

    def _per_focus(self, coefficients: np.ndarray, column: np.ndarray) -> np.ndarray:
        out = np.full((coefficients.shape[0], self.num_foci), np.nan)
        have = self.valid & (column >= 0)
        out[:, have] = coefficients[:, column[have]]
        return out

    def apply(self, frames: np.ndarray, chunk: int = 64) -> ExtractionResult:
        """Extract the coefficients of ``frames``, shape ``(K, rows, cols)``.

        A single 2D frame is accepted and treated as ``K = 1``.
        """
        coefficients, noise = self._solve(frames, int(chunk))
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
        )

    def footprint_values(self, frames: np.ndarray) -> np.ndarray:
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
        coefficients, _ = self._solve(frames, max(1, frames.shape[0]))
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
