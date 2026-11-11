"""Spot model of the MoNaLISA camera foci, calibrated from the data.

Every camera frame of a MoNaLISA scan is a lattice of spots. The extraction
(:mod:`.extraction`) needs to know where each spot is and how wide it is; this
module measures both from the mean frame of the recording instead of taking
them from a typed-in PSF width and an ideal lattice.

The spot is not the detection PSF: it is the detection PSF convolved with the
RESOLFT effective PSF and the pixel, so its width has to be measured. And the
foci are not exactly on the ideal lattice: distortion and field-dependent
aberrations move and widen them by amounts that tile the reconstruction with
cell-sized errors (``docs/monalisa_optimal_reconstruction.md``, sections 2.3
and 2.7).

Coordinates are ``(x, y)`` in camera pixels, x along columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.spatial import cKDTree

FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


@dataclass(frozen=True)
class FocusFit:
    """Per-focus Gaussian fit of one image (see :func:`fit_foci`)."""

    x: np.ndarray
    y: np.ndarray
    sigma: np.ndarray
    amplitude: np.ndarray
    background: np.ndarray
    ok: np.ndarray


@dataclass(frozen=True)
class SpotModel:
    """Centre and width of every focus, plus the calibration's diagnostics.

    ``x``, ``y`` and ``sigma`` are what the extraction uses. ``shared_sigma``
    is the single width that fits all foci best, ``centre_residual`` and
    ``sigma_residual`` are what each focus' own measurement differs by from
    the smooth field over the frame (NaN where a focus could not be measured).
    """

    x: np.ndarray
    y: np.ndarray
    sigma: np.ndarray
    shared_sigma: float
    haze_sigma: float | None = None
    measured: np.ndarray | None = None
    centre_residual: np.ndarray | None = None
    sigma_residual: np.ndarray | None = None
    diagnostics: dict = field(default_factory=dict)

    @classmethod
    def uniform(
        cls,
        x: np.ndarray,
        y: np.ndarray,
        sigma: float,
        haze_sigma: float | None = None,
    ) -> "SpotModel":
        """The same width for every focus, centres as given."""
        x = np.asarray(x, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        if x.shape != y.shape:
            raise ValueError("x and y must have the same length")
        if sigma <= 0:
            raise ValueError("sigma must be > 0")
        return cls(
            x=x,
            y=y,
            sigma=np.full(x.shape, float(sigma)),
            shared_sigma=float(sigma),
            haze_sigma=haze_sigma,
        )

    @property
    def num_foci(self) -> int:
        return int(self.x.size)

    def fwhm_nm(self, pixel_size_nm: float) -> float:
        """Spot FWHM in nanometres, the number shown as a diagnostic."""
        return float(FWHM_PER_SIGMA * self.shared_sigma * pixel_size_nm)


def focus_spacing(x: np.ndarray, y: np.ndarray) -> float:
    """Median distance of a focus to its nearest neighbour."""
    points = np.column_stack([np.ravel(x), np.ravel(y)])
    if points.shape[0] < 2:
        return float("inf")
    distance, _ = cKDTree(points).query(points, k=2)
    return float(np.median(distance[:, 1]))


def _default_radius(x: np.ndarray, y: np.ndarray, sigma: float) -> float:
    """Three sigma, but never into the neighbouring focus' half of the gap."""
    return float(max(2.0, min(3.0 * sigma, focus_spacing(x, y) / 2.0)))


def _patches(
    image: np.ndarray, x: np.ndarray, y: np.ndarray, radius: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Integer-pixel windows around each focus, with a disc as the footprint.

    Returns ``(inside, px, py, values, weight)``: ``inside`` marks the foci
    whose window lies entirely in the frame; the other arrays hold those
    foci's windows, one row per focus. ``weight`` is 1 for the pixels within
    ``radius`` of the focus and 0 elsewhere, and 0 where the image is not
    finite. A disc, not the square: on a rotated lattice the corners of a
    square window reach into the neighbouring spots.
    """
    rows, cols = image.shape
    half_width = int(np.ceil(radius))
    cx = np.rint(x).astype(int)
    cy = np.rint(y).astype(int)
    inside = (
        (cx - half_width >= 0)
        & (cx + half_width < cols)
        & (cy - half_width >= 0)
        & (cy + half_width < rows)
    )
    span = np.arange(-half_width, half_width + 1)
    oy, ox = np.meshgrid(span, span, indexing="ij")
    px = cx[inside, None] + ox.ravel()[None, :]
    py = cy[inside, None] + oy.ravel()[None, :]
    values = image[py, px].astype(float)
    px, py = px.astype(float), py.astype(float)
    r2 = (px - x[inside, None]) ** 2 + (py - y[inside, None]) ** 2
    weight = ((r2 <= radius * radius) & np.isfinite(values)).astype(float)
    return inside, px, py, np.where(weight > 0, values, 0.0), weight


#: Widths the shared fit may take, px. A spot narrower than half a pixel
#: is not a spot the camera resolved; one wider than six is not a focus.
SHARED_SIGMA_BOUNDS = (0.5, 6.0)


@dataclass(frozen=True)
class SharedSigmaFit:
    """The one width that fits all foci, and how much of the image it is.

    ``explained`` is the fraction of the pixel variance within the fit
    windows that the spots account for beyond a constant per window: about
    0 where the image is noise around the lattice points, 0.3 to 0.8 where
    the foci are plainly there. It is what says whether an image shows the
    foci at all.
    """

    sigma: float
    explained: float


def fit_shared_sigma(
    image: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    radius: float = 4.0,
    bounds: tuple[float, float] = SHARED_SIGMA_BOUNDS,
) -> float:
    """The one Gaussian width that fits every focus of ``image`` best.

    Variable projection: for a trial sigma each focus' amplitude and constant
    background are solved in closed form over the disc of ``radius`` around
    it, and the summed squared residual is minimized over sigma alone.
    """
    return shared_sigma_fit(image, x, y, radius, bounds).sigma


def shared_sigma_fit(
    image: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    radius: float = 4.0,
    bounds: tuple[float, float] = SHARED_SIGMA_BOUNDS,
) -> SharedSigmaFit:
    """:func:`fit_shared_sigma` with the fraction of the image it explains."""
    image = np.asarray(image, dtype=float)
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    inside, px, py, values, weight = _patches(image, x, y, float(radius))
    if not inside.any():
        raise ValueError("No focus has its fit window inside the frame")
    r2 = (px - x[inside, None]) ** 2 + (py - y[inside, None]) ** 2
    count = weight.sum(axis=1)
    sum_v = values.sum(axis=1)
    sum_vv = (values * values).sum(axis=1)
    # What a constant per window leaves: the variance the spots may explain.
    flat = float(np.sum(sum_vv - sum_v * sum_v / np.maximum(count, 1.0)))

    def cost(sigma: float) -> float:
        g = weight * np.exp(-r2 / (2.0 * sigma * sigma))
        sum_g = g.sum(axis=1)
        sum_gg = (g * g).sum(axis=1)
        sum_gv = (g * values).sum(axis=1)
        det = sum_gg * count - sum_g * sum_g
        det = np.where(np.abs(det) < 1e-12, np.inf, det)
        amplitude = (count * sum_gv - sum_g * sum_v) / det
        background = (sum_gg * sum_v - sum_g * sum_gv) / det
        return float(np.sum(sum_vv - amplitude * sum_gv - background * sum_v))

    result = minimize_scalar(cost, bounds=bounds, method="bounded")
    explained = 1.0 - float(result.fun) / flat if flat > 0 else 0.0
    return SharedSigmaFit(float(result.x), float(np.clip(explained, 0.0, 1.0)))


def fit_foci(
    image: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    sigma: float | np.ndarray,
    radius: float | None = None,
    fit_sigma: bool = True,
    iterations: int = 12,
    max_shift: float = 1.5,
) -> FocusFit:
    """Fit a Gaussian plus a constant to every focus of ``image`` at once.

    A damped Gauss-Newton iteration on amplitude, background, centre and
    (optionally) width over the disc of ``radius`` around each focus, batched
    over the foci. Foci whose window leaves the frame, or whose fit wanders
    more than ``max_shift`` px from its start, keep their starting values and
    are marked not ``ok``.
    """
    image = np.asarray(image, dtype=float)
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    sigma0 = np.broadcast_to(np.asarray(sigma, dtype=float), x.shape).copy()
    if radius is None:
        radius = _default_radius(x, y, float(np.median(sigma0)))
    inside, px, py, values, weight = _patches(image, x, y, float(radius))

    out_x, out_y, out_sigma = x.copy(), y.copy(), sigma0.copy()
    out_amplitude = np.full(x.shape, np.nan)
    out_background = np.full(x.shape, np.nan)
    ok = np.zeros(x.shape, dtype=bool)
    if not inside.any():
        return FocusFit(out_x, out_y, out_sigma, out_amplitude, out_background, ok)

    cx, cy, width = x[inside].copy(), y[inside].copy(), sigma0[inside].copy()
    background = np.min(np.where(weight > 0, values, np.inf), axis=1)
    background = np.where(np.isfinite(background), background, 0.0)
    amplitude = np.maximum(np.max(values, axis=1) - background, 1e-6)
    num_params = 5 if fit_sigma else 4
    damping = np.full(cx.shape, 1e-3)

    def residual_and_jacobian(amplitude, background, cx, cy, width):
        dx = px - cx[:, None]
        dy = py - cy[:, None]
        g = np.exp(-(dx * dx + dy * dy) / (2.0 * width[:, None] ** 2))
        residual = weight * (values - (amplitude[:, None] * g + background[:, None]))
        scaled = amplitude[:, None] * g / width[:, None] ** 2
        columns = [g, np.ones_like(g), scaled * dx, scaled * dy]
        if fit_sigma:
            columns.append(scaled * (dx * dx + dy * dy) / width[:, None])
        return residual, weight[:, :, None] * np.stack(columns, axis=2)

    residual, jacobian = residual_and_jacobian(amplitude, background, cx, cy, width)
    cost = np.sum(residual * residual, axis=1)
    for _ in range(int(iterations)):
        normal = np.einsum("fpi,fpj->fij", jacobian, jacobian)
        gradient = np.einsum("fpi,fp->fi", jacobian, residual)
        diagonal = np.einsum("fii->fi", normal)
        normal = normal + np.einsum(
            "fi,ij->fij", damping[:, None] * diagonal + 1e-12, np.eye(num_params)
        )
        try:
            step = np.linalg.solve(normal, gradient[:, :, None])[:, :, 0]
        except np.linalg.LinAlgError:
            break
        step[:, 2:4] = np.clip(step[:, 2:4], -1.0, 1.0)
        trial = (
            amplitude + step[:, 0],
            background + step[:, 1],
            cx + step[:, 2],
            cy + step[:, 3],
            np.clip(width + step[:, 4], 0.3, None) if fit_sigma else width,
        )
        trial_residual, trial_jacobian = residual_and_jacobian(*trial)
        trial_cost = np.sum(trial_residual * trial_residual, axis=1)
        better = trial_cost < cost
        amplitude = np.where(better, trial[0], amplitude)
        background = np.where(better, trial[1], background)
        cx = np.where(better, trial[2], cx)
        cy = np.where(better, trial[3], cy)
        width = np.where(better, trial[4], width)
        residual = np.where(better[:, None], trial_residual, residual)
        jacobian = np.where(better[:, None, None], trial_jacobian, jacobian)
        cost = np.where(better, trial_cost, cost)
        damping = np.where(better, damping * 0.3, damping * 10.0)

    shift = np.hypot(cx - x[inside], cy - y[inside])
    good = (
        np.isfinite(cx)
        & np.isfinite(cy)
        & np.isfinite(width)
        & (amplitude > 0)
        & (shift <= max_shift)
        & (width < radius)
        & (weight.sum(axis=1) >= 2 * num_params)
    )
    index = np.flatnonzero(inside)
    out_amplitude[index] = amplitude
    out_background[index] = background
    out_x[index[good]] = cx[good]
    out_y[index[good]] = cy[good]
    out_sigma[index[good]] = width[good]
    ok[index[good]] = True
    return FocusFit(out_x, out_y, out_sigma, out_amplitude, out_background, ok)


def _polynomial_design(x: np.ndarray, y: np.ndarray, order: int) -> np.ndarray:
    return np.column_stack(
        [x**i * y**j for i in range(order + 1) for j in range(order + 1 - i)]
    )


def _num_terms(order: int) -> int:
    return (order + 1) * (order + 2) // 2


def smooth_field(
    x: np.ndarray,
    y: np.ndarray,
    values: np.ndarray,
    valid: np.ndarray | None = None,
    weights: np.ndarray | None = None,
    order: int = 3,
    clip: float = 4.0,
) -> np.ndarray:
    """Evaluate a smooth 2D polynomial fitted to ``values`` at every ``(x, y)``.

    The field pools all foci, so a single focus' measurement noise and
    outliers do not reach the extraction; what a focus differs by from the
    field is the distortion diagnostic. A polynomial of order 3 holds an
    affine map plus the lowest radial distortion term, which is what a
    pattern imaged through an objective carries. Points further than
    ``clip`` robust standard deviations from the fit are dropped and the fit
    repeated.

    Returns the field at all points, including those not ``valid``.
    """
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    values = np.asarray(values, dtype=float).ravel()
    use = np.isfinite(values)
    if valid is not None:
        use &= np.asarray(valid, dtype=bool).ravel()
    w = np.ones(x.shape) if weights is None else np.asarray(weights, float).ravel()
    use &= np.isfinite(w) & (w > 0)
    if not use.any():
        raise ValueError("No valid point to fit a field to")

    centre = np.array([x[use].mean(), y[use].mean()])
    scale = max(float(np.ptp(x[use])), float(np.ptp(y[use])), 1.0) / 2.0
    while order > 0 and use.sum() < 3 * _num_terms(order):
        order -= 1
    design = _polynomial_design((x - centre[0]) / scale, (y - centre[1]) / scale, order)

    def solve(selection: np.ndarray) -> np.ndarray:
        root = np.sqrt(w[selection])
        coef, *_ = np.linalg.lstsq(
            design[selection] * root[:, None], values[selection] * root, rcond=None
        )
        return coef

    coef = solve(use)
    for _ in range(2):
        deviation = values - design @ coef
        spread = 1.4826 * np.median(np.abs(deviation[use] - np.median(deviation[use])))
        if not np.isfinite(spread) or spread <= 0:
            break
        keep = use & (np.abs(deviation) <= clip * spread)
        if keep.sum() == use.sum() or keep.sum() < _num_terms(order):
            break
        use = keep
        coef = solve(use)
    return design @ coef


def _decrowd(
    image: np.ndarray, x: np.ndarray, y: np.ndarray, sigma: np.ndarray
) -> np.ndarray:
    """``image`` with every focus' neighbours taken out.

    All foci are fitted together; each pixel then keeps the fitted spot and
    background of the focus nearest to it plus the residual, and loses the
    fitted spots of all other foci. NaN outside the fitted pixels.
    """
    from .extraction import ExtractionOperator

    operator = ExtractionOperator.build(
        x, y, sigma, image.shape, reach_sigma=3.0, background="constant"
    )
    fit = operator.apply(image)
    rows, cols = image.shape
    yy, xx = np.mgrid[0:rows, 0:cols]
    _, owner = cKDTree(np.column_stack([x, y])).query(
        np.column_stack([xx.ravel(), yy.ravel()])
    )
    owner = owner.reshape(rows, cols)
    amplitude = np.nan_to_num(fit.amplitude[0])
    background = np.nan_to_num(fit.background[0])
    r2 = (xx - x[owner]) ** 2 + (yy - y[owner]) ** 2
    own = amplitude[owner] * np.exp(-r2 / (2.0 * sigma[owner] ** 2)) + background[owner]
    decrowded = operator.residual(image) + own
    return np.where(operator.valid[owner], decrowded, np.nan)


def _significant_field(
    field_values: np.ndarray,
    residual: np.ndarray,
    measured: np.ndarray,
    order: int,
    factor: float = 3.0,
) -> bool:
    """Whether a fitted field is more than the scatter it was fitted to.

    A polynomial fitted to scattered measurements is never exactly zero. Its
    rms is compared with the standard error the scatter of the measurements
    gives a fit of that many terms.
    """
    count = int(measured.sum())
    if count <= _num_terms(order):
        return False
    scatter = 1.4826 * float(np.median(np.abs(residual[measured])))
    standard_error = scatter * np.sqrt(_num_terms(order) / count)
    return float(np.sqrt(np.mean(field_values**2))) > factor * standard_error


def calibrate_spot_model(
    image: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    sigma_guess: float | None = None,
    mode: str = "field",
    field_order: int = 3,
    min_relative_amplitude: float = 0.2,
    passes: int = 2,
    modulation: bool = False,
) -> SpotModel:
    """Measure every focus' centre and width on an image of the foci.

    Two images of a scan show its foci. The mean frame shows them on top of
    everything that does not move with the scan: out-of-focus light, camera
    offset, fixed pattern. The temporal variance of the frames (pass it with
    ``modulation=True``) shows only what the scan modulates, which is the
    foci; its spots are the squares of the real ones, narrower by the square
    root of two, which the calibration accounts for. On recordings of cells
    the mean frame's background is structured on the scale of the foci and
    several times brighter than they are, and the variance is the image to
    calibrate on.

    Each focus is measured with its neighbours taken out (they are fitted
    jointly and subtracted), because a fit that takes the neighbours' tails
    for background comes out too narrow: by 0.5 to 3 % for foci 5 sigma
    apart, more the wider the window.

    The centres of single foci scatter by several tenths of a pixel on real
    recordings, because the spot of a focus depends on the specimen it
    happens to scan. What the foci have in common is a smooth field, and the
    field is only used where it exceeds what that scatter would produce by
    itself; otherwise the foci are on the lattice and have one width.

    Args:
        image: Mean frame or temporal variance of the frames of one scan.
        x, y: Focus centres of the detected lattice, px. They may lie
            slightly outside the frame.
        sigma_guess: Starting spot width; measured from the data when
            ``None``.
        mode: ``"field"`` uses the smooth field over the frame for centres
            and widths (the default), ``"measured"`` each focus' own fit where
            it succeeded, ``"shared"`` the input centres and one width.
        field_order: Polynomial order of the smooth field.
        min_relative_amplitude: Foci dimmer than this fraction of the bright
            ones (the 90th percentile) are not used.
        passes: Rounds of taking the neighbours out and measuring again.
        modulation: ``image`` is a temporal variance.
    """
    if mode not in ("field", "measured", "shared"):
        raise ValueError(f"Unknown spot model mode {mode!r}")
    image = np.asarray(image, dtype=float)
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    widening = np.sqrt(2.0) if modulation else 1.0

    guess = (sigma_guess if sigma_guess else 2.0) / widening
    radius = _default_radius(x, y, guess)
    shared = fit_shared_sigma(image, x, y, radius=radius)
    fit = FocusFit(
        x, y, np.full(x.shape, shared), None, None, np.zeros(x.shape, dtype=bool)
    )
    measured = np.zeros(x.shape, dtype=bool)
    for _ in range(max(1, int(passes))):
        radius = _default_radius(x, y, shared)
        clean = _decrowd(image, fit.x, fit.y, fit.sigma)
        fit = fit_foci(clean, x, y, shared, radius=radius)
        measured = fit.ok.copy()
        if not measured.any():
            break
        bright = np.percentile(fit.amplitude[measured], 90)
        measured &= fit.amplitude >= min_relative_amplitude * bright
        if not measured.any():
            break
        # The width of the bright foci: a dim focus is fitted to whatever
        # else is in its window. Outside the bounds of the shared fit the
        # foci fitted one by one are not foci (on an image of noise around
        # the lattice points every fit collapses onto its brightest pixel):
        # the bounded shared width stands.
        brightest = measured & (fit.amplitude >= np.median(fit.amplitude[measured]))
        candidate = float(np.median(fit.sigma[brightest]))
        if not (SHARED_SIGMA_BOUNDS[0] <= candidate <= SHARED_SIGMA_BOUNDS[1]):
            measured[:] = False
            break
        shared = candidate
    diagnostics = {
        "num_foci": int(x.size),
        "num_measured": int(measured.sum()),
        "fit_radius_px": float(radius),
        "mode": mode,
        "image": "variance" if modulation else "mean",
    }
    if mode == "shared" or measured.sum() < 6:
        return SpotModel(
            x=x,
            y=y,
            sigma=np.full(x.shape, widening * shared),
            shared_sigma=widening * shared,
            measured=measured,
            diagnostics=diagnostics,
        )

    weights = np.where(measured, np.nan_to_num(fit.amplitude), 0.0)
    weights = np.minimum(weights, np.percentile(weights[measured], 90))
    field_dx = smooth_field(x, y, fit.x - x, measured, weights, field_order)
    field_dy = smooth_field(x, y, fit.y - y, measured, weights, field_order)
    field_sigma = smooth_field(x, y, fit.sigma, measured, weights, field_order)
    field_sigma = np.clip(field_sigma, 0.5 * shared, 2.0 * shared)

    centres_vary = _significant_field(
        np.hypot(field_dx, field_dy),
        np.hypot(fit.x - x - field_dx, fit.y - y - field_dy),
        measured, field_order,
    )
    widths_vary = _significant_field(
        field_sigma - shared, fit.sigma - field_sigma, measured, field_order
    )
    if not centres_vary:
        field_dx = np.zeros(x.shape)
        field_dy = np.zeros(x.shape)
    if not widths_vary:
        field_sigma = np.full(x.shape, shared)

    centre_residual = np.full((x.size, 2), np.nan)
    centre_residual[measured, 0] = (fit.x - x - field_dx)[measured]
    centre_residual[measured, 1] = (fit.y - y - field_dy)[measured]
    sigma_residual = np.full(x.shape, np.nan)
    sigma_residual[measured] = widening * (fit.sigma - field_sigma)[measured]
    diagnostics.update(
        centre_field_used=bool(centres_vary),
        sigma_field_used=bool(widths_vary),
        centre_field_rms_px=float(np.sqrt(np.mean(field_dx**2 + field_dy**2))),
        centre_residual_rms_px=float(
            np.sqrt(np.nanmean(np.sum(centre_residual**2, axis=1)))
        ),
        sigma_field_min_px=float(widening * field_sigma.min()),
        sigma_field_max_px=float(widening * field_sigma.max()),
    )

    out_x, out_y, out_sigma = x + field_dx, y + field_dy, field_sigma
    if mode == "measured":
        out_x = np.where(measured, fit.x, out_x)
        out_y = np.where(measured, fit.y, out_y)
        out_sigma = np.where(measured, fit.sigma, out_sigma)
    return SpotModel(
        x=out_x,
        y=out_y,
        sigma=widening * out_sigma,
        shared_sigma=widening * shared,
        measured=measured,
        centre_residual=centre_residual,
        sigma_residual=sigma_residual,
        diagnostics=diagnostics,
    )


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
