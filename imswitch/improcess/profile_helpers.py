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
