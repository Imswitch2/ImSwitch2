"""Grid localization used by the uLenses alignment tool.

This module intentionally lives in :mod:`imswitch.imcontrol` so the uLenses
alignment workflow remains available when ImProcess is not installed or
started.
"""

from dataclasses import dataclass
from typing import Tuple

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks


@dataclass(frozen=True)
class LocalizationResult:
    xp: float
    xo: float
    yp: float
    yo: float
    nx_c: int
    ny_c: int
    num_cols: int
    num_rows: int


def _find_best_peak_index(peaks: Tuple) -> int:
    """Return the index of the most suitable detected FFT peak."""
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
    """Estimate the dominant period in a 1D signal using FFT + Gaussian fit."""
    data_size = input_data.size

    fft_max_index = data_size // 2 + 1
    abs_fft = np.abs(np.fft.fft(input_data)[0:fft_max_index])
    fft_freqs = np.fft.fftfreq(fft_max_index)

    target_freq = 2 / period_guess
    tol = 0.05
    abs_fft[fft_freqs < target_freq - tol] = 0

    peaks = find_peaks(abs_fft, prominence=[0, np.inf], width=0, height=0)
    best_peak_index = _find_best_peak_index(peaks)
    peak_index = peaks[0][best_peak_index]
    peak_width = peaks[1]["widths"][best_peak_index]

    peak_spread = 6 * peak_width
    win_min = int(max(0, peak_index - peak_spread))
    win_max = int(min(peak_index + peak_spread, fft_max_index))
    win_range = np.arange(start=win_min, step=1, stop=win_max, dtype=int)

    def _gauss_fun(x0, x, y):
        a, b, mu, sigma = x0
        return (a + b * np.exp(-(x - mu) ** 2 / (2 * sigma ** 2))) - y

    x0_guesses = np.array([
        np.min(abs_fft),
        np.max(abs_fft) - np.min(abs_fft),
        peak_index,
        peak_width,
    ])
    res_gauss_fit = least_squares(
        _gauss_fun,
        x0_guesses,
        args=(win_range, abs_fft[win_range]),
        ftol=1e-12,
        xtol=1e-12,
    )
    return np.abs(data_size / res_gauss_fit.x[2])


def _estimate_offset(est_period: float, input_data: np.ndarray) -> float:
    """Estimate the phase offset in a 1D periodic signal."""

    def _cos_fun(x0, x, y):
        offset = x0
        return np.cos(np.pi * (x - offset) / est_period) ** 6 - y

    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(
        _cos_fun,
        x0=0.0,
        args=(x_range, input_data),
        ftol=1e-12,
        xtol=1e-12,
    )
    return np.mod(res_cos_fit.x[0], est_period)


def _optimize_parameters(
    est_period: float,
    est_offset: float,
    input_data: np.ndarray,
) -> Tuple[float, float]:
    """Jointly refine period and offset by least-squares fitting."""

    def _cos_fun(x0, x, y):
        period, offset = x0
        return np.cos(np.pi * (x - offset) / period) ** 6 - y

    x0_guesses = np.array([est_period, est_offset])
    x_range = np.arange(input_data.size)
    res_cos_fit = least_squares(
        fun=_cos_fun,
        x0=x0_guesses,
        args=(x_range, input_data),
        ftol=1e-12,
        xtol=1e-12,
    )

    opt_period = np.abs(res_cos_fit.x[0])
    opt_offset = np.mod(res_cos_fit.x[1], opt_period)
    return opt_period, opt_offset


def localizer(
    img_data: np.ndarray,
    xp_guess: float = 10.0,
    yp_guess: float = 10.0,
) -> LocalizationResult:
    """Find grid periods and offsets in a 2D frame or summed 3D stack."""
    if img_data.ndim == 3:
        _, num_rows, num_cols = img_data.shape
        img_stack_sum = np.double(img_data.sum(axis=0))
    elif img_data.ndim == 2:
        num_rows, num_cols = img_data.shape
        img_stack_sum = np.double(img_data)
    else:
        raise ValueError(f"Expected 2D or 3D array, got {img_data.ndim}D")

    sigma_low = 2.0
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
    )
