"""Shared metric semantics for all CGH computation algorithms."""

from __future__ import annotations



import numpy as np

from ..intensity_metrics import evaluate_relative_intensity_statistics
from .types import CGHIterationMetrics


def normalize_relative_intensity(values: np.ndarray) -> np.ndarray:
    """Validate nonnegative relative intensities and normalize their maximum."""
    intensities = np.asarray(values,dtype=np.float64)
    if not np.all(np.isfinite(intensities)):
        raise ValueError("Target intensity contains non-finite values")
    if np.any(intensities < 0):
        raise ValueError("Target intensity cannot contain negative values")

    maximum = float(np.max(intensities)) if intensities.size else 0.0
    if maximum <= 0.0:
        raise ValueError("Target intensity must contain a positive value")

    return intensities / maximum


def evaluate_intensity_metrics(
    iteration: int,
    measured_intensity: np.ndarray,
    desired_intensity: np.ndarray,
    efficiency: float | None,
) -> CGHIterationMetrics:
    """Evaluate common metrics from measured and desired target intensities."""
    statistics = evaluate_relative_intensity_statistics(
        measured_intensity,desired_intensity,
    )
    return CGHIterationMetrics(
        iteration=iteration,
        efficiency=efficiency,
        uniformity=statistics.uniformity,
        normalized_std=statistics.normalized_std,
    )
