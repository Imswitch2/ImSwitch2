"""Shared metric semantics for all CGH computation algorithms."""

from __future__ import annotations



import numpy as np

from ..intensity_metrics import evaluate_relative_intensity_statistics
from .array_backend import to_float
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

def evaluate_intensity_metrics_backend(
    iteration: int,
    measured_intensity,
    desired_intensity,
    efficiency: float | None,
    *,
    xp,
    using_gpu: bool,
) -> CGHIterationMetrics:
    """Evaluate metrics without transferring backend arrays to the CPU.

    This is primarily used by raster GPU GS, where copying the complete target
    support to NumPy every iteration would erase much of the FFT speedup. Only
    scalar reductions cross the device boundary.
    """
    if measured_intensity.shape != desired_intensity.shape:
        raise ValueError(
            "Measured and desired intensity shapes must match, got "
            f"{measured_intensity.shape} and {desired_intensity.shape}"
        )

    response = measured_intensity / desired_intensity
    mean_response = to_float(xp.mean(response),using_gpu)
    if mean_response <= 0.0:
        raise ValueError("Measured target response must contain positive power")

    response = response / mean_response
    maximum = to_float(xp.max(response),using_gpu)
    minimum = to_float(xp.min(response),using_gpu)
    denominator = maximum + minimum
    uniformity = (
        0.0 if denominator <= 0.0
        else 1.0 - (maximum - minimum) / denominator
    )
    normalized_std = to_float(
        xp.std(response) / xp.mean(response),using_gpu
    )
    return CGHIterationMetrics(
        iteration=iteration,
        efficiency=efficiency,
        uniformity=uniformity,
        normalized_std=normalized_std,
    )
