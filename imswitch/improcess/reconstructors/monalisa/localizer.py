"""Localization of MoNaLISA scan grid from raw frames."""

from dataclasses import dataclass, replace

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks

from .lattice import Lattice, detect_lattice


@dataclass(frozen=True)
class LocalizationResult:
    """Result of scan grid localization.

    ``xp/xo/yp/yo`` are the axis-aligned grid parameters the reassignment
    pipeline consumes. ``lattice`` is the best-known general description of
    the illumination pattern — normally the equivalent rectangular
    :class:`~.lattice.Lattice`, but a detection-backed result may carry a
    slightly tilted basis so callers can inspect how rectangular the pattern
    really was.
    """

    xp: float
    xo: float
    yp: float
    yo: float
    nx_c: int
    ny_c: int
    num_cols: int
    num_rows: int
    lattice: Lattice | None = None


def localization_from_pattern(
    row_offset: float,
    col_offset: float,
    row_period: float,
    col_period: float,
    num_rows: int,
    num_cols: int,
) -> LocalizationResult:
    """Build a localization result from widget pattern parameters.

    The legacy MoNaLISA widget stores pattern parameters as
    ``(row_offset, col_offset, row_period, col_period)``.  The fast-Gauss
    processor needs the same information as ``(xo, yo, xp, yp)`` plus the
    derived number of foci. Keeping this conversion here avoids a second
    hand-rolled mapping in offline code.
    """
    yp = float(row_period)
    xp = float(col_period)
    if xp <= 0 or yp <= 0:
        raise ValueError("Pattern periods must be positive")
    yo = float(np.mod(float(row_offset), yp))
    xo = float(np.mod(float(col_offset), xp))

    nx_c = int(np.ceil((int(num_cols) - xo) / xp))
    ny_c = int(np.ceil((int(num_rows) - yo) / yp))
    return LocalizationResult(
        xp=xp,
        xo=xo,
        yp=yp,
        yo=yo,
        nx_c=nx_c,
        ny_c=ny_c,
        num_cols=int(num_cols),
        num_rows=int(num_rows),
        lattice=Lattice.rectangular(xp, yp, xo, yo),
    )


def localization_result_centers(
    loc: LocalizationResult,
) -> tuple[np.ndarray, np.ndarray]:
    """Focus centers for a localization, in legacy row-major order.

    Uses the general lattice enumeration, which for the axis-aligned grid
    reproduces ``scan_geometry.get_center_coords`` exactly. This is the seam
    a non-rectangular pattern will flow through once reassignment learns to
    handle one.
    """
    lattice = loc.lattice or Lattice.rectangular(loc.xp, loc.yp, loc.xo, loc.yo)
    return lattice.points_in_frame(loc.num_rows, loc.num_cols)


def _find_best_peak_index(peaks: tuple) -> int:
    """
    Find the index of the peak with the greatest prominence.

    Args:
        peaks: Peak data from scipy.signal.find_peaks.

    Returns:
        Index of the peak with the greatest prominence.
    """
    prominences = peaks[1]["prominences"]
    heights = peaks[1]["peak_heights"]

    if len(prominences) < 2:
        return 0

    sorted_prom_indices = np.argsort(prominences)
    p1_index = sorted_prom_indices[-1]
    p2_index = sorted_prom_indices[-2]

    p1_prom = prominences[p1_index]
    p2_prom = prominences[p2_index]

    p1_height = heights[p1_index]
    p2_height = heights[p2_index]

    diff_prom = np.abs((p1_prom - p2_prom) / (p1_prom + p2_prom))
    diff_height = np.abs((p1_height - p2_height) / (p1_height + p2_height))

    tol = 0.2
    if (diff_prom < tol) and (diff_height < tol) and (p1_height < p2_height):
        return p2_index

    return p1_index


def _estimate_period(period_guess: float, input_data: np.ndarray) -> float:
    """
    Estimate the dominant period in the 1D input data using FFT and Gaussian fitting.

    Args:
        period_guess: Initial period guess. Only spectral peaks within
            ``1/period_guess +- 0.025`` cycles/px are considered, so the guess
            must be within roughly +-20% of the true period (tighter for short
            periods, looser for long ones).
        input_data: 1D data array (e.g., row or column averages).

    Returns:
        Refined estimate of the period.

    Raises:
        ValueError: If no spectral peak exists near the guessed frequency.
    """
    data_size = input_data.size

    fft_max_index = data_size // 2 + 1
    abs_fft = np.abs(np.fft.fft(input_data)[0:fft_max_index])
    # Frequency labels for the positive-frequency bins of the length-N
    # transform. An earlier version passed fft_max_index to fftfreq, which
    # compressed the axis by ~2x (compensated by targeting 2/period) and
    # labeled the upper half of the bins with negative frequencies — silently
    # making every period below ~4 px unfindable.
    fft_freqs = np.fft.rfftfreq(data_size)

    target_freq = 1 / period_guess
    tol = 0.025
    abs_fft[fft_freqs < target_freq - tol] = 0
    abs_fft[fft_freqs > target_freq + tol] = 0

    peaks = find_peaks(abs_fft, prominence=[0, np.inf], width=0, height=0)
    if len(peaks[0]) == 0:
        raise ValueError(
            f'No periodic component found near the period guess '
            f'{period_guess:.4g} px. Check the pattern period parameters '
            f'(the search window only covers roughly +-20% around the guess).'
        )
    best_peak_index = _find_best_peak_index(peaks)
    peak_index = peaks[0][best_peak_index]
    peak_width = peaks[1]["widths"][best_peak_index]

    peak_spread = 6 * peak_width
    win_min = int(max(0, peak_index - peak_spread))
    win_max = int(min(peak_index + peak_spread, fft_max_index))
    win_range = np.arange(start=win_min, step=1, stop=win_max, dtype=int)

    def _gauss_fun(x0, x, y):
        a, b, mu, sigma = x0
        return (a + b * np.exp(-((x - mu) ** 2) / (2 * sigma**2))) - y

    x0_guesses = np.array(
        [np.min(abs_fft), np.max(abs_fft) - np.min(abs_fft), peak_index, peak_width]
    )
    res_gauss_fit = least_squares(
        _gauss_fun, x0_guesses, args=(win_range, abs_fft[win_range]), ftol=1e-12, xtol=1e-12
    )
    est_period = np.abs(data_size / res_gauss_fit.x[2])

    return est_period


def _estimate_offset(est_period: float, input_data: np.ndarray) -> float:
    """
    Estimate the phase offset in the 1D input data using a cosine fitting approach.

    Args:
        est_period: Estimated period in the input data.
        input_data: 1D data array.

    Returns:
        Estimated offset in pixels.
    """

    def _cos_fun(x0, x, y):
        offset = x0
        return np.cos(np.pi * (x - offset) / est_period) ** 6 - y

    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(_cos_fun, x0=0.0, args=(x_range, input_data), ftol=1e-12, xtol=1e-12)
    est_offset = np.mod(res_cos_fit.x[0], est_period)

    return est_offset


def _optimize_parameters(
    est_period: float,
    est_offset: float,
    input_data: np.ndarray,
) -> tuple[float, float]:
    """
    Simultaneously optimize both period and offset using least squares.

    Args:
        est_period: Initial period estimate.
        est_offset: Initial offset estimate.
        input_data: 1D data array.

    Returns:
        Optimized (period, offset).
    """

    def _cos_fun(x0, x, y):
        period, offset = x0
        return np.cos(np.pi * (x - offset) / period) ** 6 - y

    x0_guesses = np.array([est_period, est_offset])
    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(
        fun=_cos_fun, x0=x0_guesses, args=(x_range, input_data), ftol=1e-12, xtol=1e-12
    )

    opt_period = np.abs(res_cos_fit.x[0])
    opt_offset = np.mod(res_cos_fit.x[1], opt_period)

    return opt_period, opt_offset


def localizer(
    img_data: np.ndarray,
    xp_guess: float = 10.0,
    yp_guess: float = 10.0,
) -> LocalizationResult:
    """
    Find grid periods and offsets in 2D image data.

    Handles both single frames and summed image stacks.

    Args:
        img_data: 2D frame or 3D stack of frames.
        xp_guess: Initial guess for x-period.
        yp_guess: Initial guess for y-period.

    Returns:
        Localization result containing periods, offsets, and grid dimensions.
    """
    img_stack_sum = _summed_image(img_data)
    num_rows, num_cols = img_stack_sum.shape

    # Band-pass around the expected pattern frequency: the low-pass sigma is
    # a fifth of the mean period (== the historical fixed 2.0 px at the
    # default 10 px guess), so localization of coarser or finer patterns is
    # smoothed proportionally instead of with a constant kernel.
    sigma_low = (xp_guess + yp_guess) / 10
    sigma_high = (xp_guess + yp_guess) / 2
    img_stack_sum -= gaussian_filter(img_stack_sum, sigma_high)
    img_stack_sum = gaussian_filter(img_stack_sum, sigma_low)
    img_stack_sum -= img_stack_sum.mean()

    x_img_avg = img_stack_sum.mean(axis=0)
    y_img_avg = img_stack_sum.mean(axis=1)

    xp_est = _estimate_period(xp_guess, x_img_avg)
    xo_est = _estimate_offset(xp_est, x_img_avg)

    yp_est = _estimate_period(yp_guess, y_img_avg)
    yo_est = _estimate_offset(yp_est, y_img_avg)

    xp, xo = _optimize_parameters(xp_est, xo_est, x_img_avg)
    yp, yo = _optimize_parameters(yp_est, yo_est, y_img_avg)

    nx_c = int(np.ceil((num_cols - xo) / xp))
    ny_c = int(np.ceil((num_rows - yo) / yp))

    return LocalizationResult(
        xp,
        xo,
        yp,
        yo,
        nx_c,
        ny_c,
        num_cols,
        num_rows,
        lattice=Lattice.rectangular(xp, yp, xo, yo),
    )


def _summed_image(img_data: np.ndarray) -> np.ndarray:
    """Collapse an optional frame axis into the 2D image localization uses."""
    if img_data.ndim == 3:
        return np.double(img_data.sum(axis=0))
    if img_data.ndim == 2:
        return np.double(img_data)
    raise ValueError(f"Expected 2D or 3D array, got {img_data.ndim}D")


def detection_band(
    xp_guess: float | None = None, yp_guess: float | None = None
) -> dict:
    """Spectral period band for lattice detection, from rough period guesses.

    Real summed stacks carry strong low-frequency *sample* structure whose
    spectral peaks outweigh the illumination pattern's; an unbanded detection
    happily locks onto them (measured: a 464 px frame with an 11 px grid
    "detected" 163 px basis vectors). The pattern period is always known to
    within a factor of ~2 (widget values, or the historical ~10 px scale), so
    the band is derived from that rough scale rather than searched globally.
    """
    guesses = [
        float(guess)
        for guess in (xp_guess, yp_guess)
        if guess is not None and np.isfinite(guess) and float(guess) > 0
    ]
    if not guesses:
        guesses = [10.0]
    return {
        "min_period": max(3.0, 0.35 * min(guesses)),
        "max_period": 2.5 * max(guesses),
    }


def robust_localize(
    img_data: np.ndarray,
    xp_guess: float | None = None,
    yp_guess: float | None = None,
    rectangular_tol: float = 0.05,
) -> LocalizationResult:
    """Localize the scan grid without requiring a good period guess.

    Runs the guess-free 2D lattice detection first and uses the detected
    periods to seed :func:`localizer`'s precise 1D refinement — so coarse or
    fine patterns localize correctly even when the caller's guess (or the
    10 px default) is far off. A detected pattern that is not an axis-aligned
    rectangular grid (rotated beyond ``rectangular_tol`` radians, hexagonal,
    ...) raises a ValueError describing the measured geometry, since the
    reassignment step cannot consume it — a readable failure instead of a
    silently scrambled reconstruction.

    Args:
        img_data: 2D frame or 3D stack of frames.
        xp_guess: Fallback x-period seed when detection finds nothing.
        yp_guess: Fallback y-period seed when detection finds nothing.
        rectangular_tol: Maximum relative off-axis basis component (~tilt in
            radians) still treated as an axis-aligned rectangular grid.

    Returns:
        Localization result; its ``lattice`` field carries the detected
        basis (including any small tilt) when detection succeeded.
    """
    summed = _summed_image(img_data)

    detected = None
    try:
        detected = detect_lattice(
            summed, **detection_band(xp_guess, yp_guess)
        )
    except ValueError:
        pass

    grid_params = None
    if detected is not None:
        # Raises with the measured geometry for non-rectangular lattices.
        grid_params = detected.to_grid_params(tol=rectangular_tol)

    if grid_params is not None:
        seed_xp, _, seed_yp, _ = grid_params
    else:
        seed_xp = float(xp_guess) if xp_guess and float(xp_guess) > 0 else 10.0
        seed_yp = float(yp_guess) if yp_guess and float(yp_guess) > 0 else 10.0

    try:
        result = localizer(img_data, xp_guess=seed_xp, yp_guess=seed_yp)
    except ValueError:
        if grid_params is None:
            raise
        # The 1D refinement found no usable projection peak, but the 2D
        # detection did localize the lattice — fall back to it directly.
        det_xp, det_xo, det_yp, det_yo = grid_params
        num_rows, num_cols = summed.shape
        result = localization_from_pattern(
            row_offset=det_yo,
            col_offset=det_xo,
            row_period=det_yp,
            col_period=det_xp,
            num_rows=num_rows,
            num_cols=num_cols,
        )

    if detected is not None:
        # Preserve the detection-backed basis (it may carry a small tilt the
        # axis-aligned grid parameters cannot express).
        result = replace(result, lattice=detected)
    return result


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
