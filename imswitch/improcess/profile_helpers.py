"""Pure functions and data structures for profile analysis (no Qt/napari dependencies)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import curve_fit


@dataclass
class FitResult:
    name: str
    x: np.ndarray
    y: np.ndarray
    summary: str
    metrics: dict = field(default_factory=dict)


class ProfileFit:
    """Base contract for profile fit backends."""

    id = "none"
    label = "No fit"

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        return None


class GaussianFit(ProfileFit):
    """Single Gaussian plus constant offset."""

    id = "gaussian"
    label = "Gaussian"

    @staticmethod
    def _model(x, offset, amplitude, center, sigma):
        return offset + amplitude * np.exp(-((x - center) ** 2) / (2 * sigma ** 2))

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        finite = np.isfinite(x) & np.isfinite(y)
        x = np.asarray(x[finite], dtype=float)
        y = np.asarray(y[finite], dtype=float)
        if x.size < 4:
            return None

        offset0 = float(np.nanmin(y))
        amplitude0 = float(np.nanmax(y) - offset0)
        if amplitude0 <= 0:
            return None
        center0 = float(x[np.nanargmax(y)])
        sigma0 = max(float((x.max() - x.min()) / 6.0), 1.0)

        try:
            popt, _ = curve_fit(
                self._model,
                x,
                y,
                p0=(offset0, amplitude0, center0, sigma0),
                bounds=(
                    [-np.inf, 0.0, float(x.min()), 1e-6],
                    [np.inf, np.inf, float(x.max()), np.inf],
                ),
                maxfev=10000,
            )
        except Exception:
            return None

        xx = np.linspace(float(x.min()), float(x.max()), max(200, x.size))
        yy = self._model(xx, *popt)
        _, amplitude, center, sigma = popt
        fwhm = 2.354820045 * abs(float(sigma))
        summary = (
            f"A={amplitude:.4g}, center={center:.4g}, "
            f"sigma={abs(float(sigma)):.4g}, FWHM={fwhm:.4g}"
        )
        metrics = {
            "fit_amplitude": amplitude,
            "fit_center": center,
            "fit_sigma": abs(sigma),
            "fit_fwhm": fwhm
        }
        return FitResult(self.label, xx, yy, summary, metrics)


class TwoGaussianFit(ProfileFit):
    """Two independent positive Gaussians plus constant offset."""

    id = "two-gaussian"
    label = "Two Gaussians"

    @staticmethod
    def _model(x, offset, amplitude1, center1, sigma1, amplitude2, center2, sigma2):
        return (
            offset
            + amplitude1 * np.exp(-((x - center1) ** 2) / (2 * sigma1 ** 2))
            + amplitude2 * np.exp(-((x - center2) ** 2) / (2 * sigma2 ** 2))
        )

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        finite = np.isfinite(x) & np.isfinite(y)
        x = np.asarray(x[finite], dtype=float)
        y = np.asarray(y[finite], dtype=float)
        if x.size < 7:
            return None

        offset0 = float(np.nanmin(y))
        amplitude_total = float(np.nanmax(y) - offset0)
        if amplitude_total <= 0:
            return None

        center1, center2 = _initial_two_peak_centers(x, y)
        span = max(float(x.max() - x.min()), 1e-6)
        sigma0 = max(span / 10.0, 1e-6)
        amplitude1 = amplitude_total
        amplitude2 = max(amplitude_total / 2.0, 1e-6)

        try:
            popt, _ = curve_fit(
                self._model,
                x,
                y,
                p0=(
                    offset0,
                    amplitude1,
                    center1,
                    sigma0,
                    amplitude2,
                    center2,
                    sigma0,
                ),
                bounds=(
                    [-np.inf, 0.0, float(x.min()), 1e-6, 0.0, float(x.min()), 1e-6],
                    [np.inf, np.inf, float(x.max()), np.inf, np.inf, float(x.max()), np.inf],
                ),
                maxfev=30000,
            )
        except Exception:
            return None

        offset, amp1, cen1, sig1, amp2, cen2, sig2 = _sort_two_gaussian_params(popt)
        xx = np.linspace(float(x.min()), float(x.max()), max(200, x.size))
        yy = self._model(xx, offset, amp1, cen1, sig1, amp2, cen2, sig2)
        fwhm1 = 2.354820045 * abs(float(sig1))
        fwhm2 = 2.354820045 * abs(float(sig2))
        distance = abs(float(cen2) - float(cen1))
        summary = (
            f"A1={amp1:.4g}, c1={cen1:.4g}, sigma1={abs(float(sig1)):.4g}; "
            f"A2={amp2:.4g}, c2={cen2:.4g}, sigma2={abs(float(sig2)):.4g}; "
            f"distance={distance:.4g}"
        )
        metrics = {
            "fit_offset": offset,
            "fit_amplitude_1": amp1,
            "fit_center_1": cen1,
            "fit_sigma_1": abs(sig1),
            "fit_fwhm_1": fwhm1,
            "fit_amplitude_2": amp2,
            "fit_center_2": cen2,
            "fit_sigma_2": abs(sig2),
            "fit_fwhm_2": fwhm2,
            "fit_center_distance": distance,
        }
        return FitResult(self.label, xx, yy, summary, metrics)


class ExponentialFit(ProfileFit):
    """Single exponential decay/rise plus constant offset."""

    id = "exponential"
    label = "Exponential"

    @staticmethod
    def _model(x, offset, amplitude, tau):
        shifted = x - float(np.min(x))
        return offset + amplitude * np.exp(-shifted / tau)

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        finite = np.isfinite(x) & np.isfinite(y)
        x = np.asarray(x[finite], dtype=float)
        y = np.asarray(y[finite], dtype=float)
        if x.size < 4:
            return None
        span = float(x.max() - x.min())
        if span <= 0:
            return None

        offset0 = float(y[-1])
        amplitude0 = float(y[0] - offset0)
        if abs(amplitude0) <= 1e-12:
            offset0 = float(np.nanmin(y))
            amplitude0 = float(np.nanmax(y) - offset0)
        if abs(amplitude0) <= 1e-12:
            return None
        tau0 = max(span / 3.0, 1e-6)

        try:
            popt, _ = curve_fit(
                self._model,
                x,
                y,
                p0=(offset0, amplitude0, tau0),
                bounds=(
                    [-np.inf, -np.inf, 1e-9],
                    [np.inf, np.inf, np.inf],
                ),
                maxfev=20000,
            )
        except Exception:
            return None

        offset, amplitude, tau = popt
        tau = abs(float(tau))
        xx = np.linspace(float(x.min()), float(x.max()), max(200, x.size))
        yy = self._model(xx, offset, amplitude, tau)
        half_length = tau * np.log(2.0)
        summary = (
            f"A={amplitude:.4g}, offset={offset:.4g}, "
            f"tau={tau:.4g}, half-length={half_length:.4g}"
        )
        metrics = {
            "fit_offset": float(offset),
            "fit_amplitude": float(amplitude),
            "fit_tau": tau,
            "fit_half_length": float(half_length),
        }
        return FitResult(self.label, xx, yy, summary, metrics)


def _initial_two_peak_centers(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    corrected = np.asarray(y, dtype=float) - float(np.nanmin(y))
    first_index = int(np.nanargmax(corrected))
    span = max(float(x.max() - x.min()), 1e-6)
    min_separation = span / 8.0
    distances = np.abs(np.asarray(x, dtype=float) - float(x[first_index]))
    masked = np.where(distances >= min_separation, corrected, -np.inf)
    if np.isfinite(masked).any():
        second_index = int(np.nanargmax(masked))
    else:
        second_index = 0 if first_index != 0 else x.size - 1
    centers = sorted((float(x[first_index]), float(x[second_index])))
    return centers[0], centers[1]


def _sort_two_gaussian_params(params):
    offset, amp1, cen1, sig1, amp2, cen2, sig2 = [float(value) for value in params]
    if cen1 <= cen2:
        return offset, amp1, cen1, abs(sig1), amp2, cen2, abs(sig2)
    return offset, amp2, cen2, abs(sig2), amp1, cen1, abs(sig1)


def build_profile_record(kind, x, y, *, length_px, length_scaled, unit, fit_metrics=None) -> dict:
    """Build a profile result record (PURE function, no Qt/napari dependencies)."""
    if len(y) == 0:
        record = {
            "kind": kind,
            "n_samples": 0,
            "length_px": length_px,
            f"length_{unit}": length_scaled,
            "min": float("nan"),
            "max": float("nan"),
            "mean": float("nan"),
        }
    else:
        record = {
            "kind": kind,
            "n_samples": len(y),
            "length_px": length_px,
            f"length_{unit}": length_scaled,
            "min": float(np.nanmin(y)),
            "max": float(np.nanmax(y)),
            "mean": float(np.nanmean(y)),
        }
    if fit_metrics:
        for key, value in fit_metrics.items():
            record[key] = value
    return record
