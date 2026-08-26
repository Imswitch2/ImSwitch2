"""Low-level relative-intensity metric formulas shared across CGH domains."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class IntensityStatistics:
    """Uniformity and normalized spread of a relative target response."""

    uniformity: float
    normalized_std: float


def evaluate_relative_intensity_statistics(
    measured_intensity: np.ndarray,
    desired_intensity: np.ndarray,
) -> IntensityStatistics:
    """Evaluate common metrics on ``measured / desired`` target response."""
    measured = np.asarray(measured_intensity,dtype=np.float64)
    desired = np.asarray(desired_intensity,dtype=np.float64)

    if measured.shape != desired.shape:
        raise ValueError(
            "Measured and desired intensity shapes must match, got "
            f"{measured.shape} and {desired.shape}"
        )
    if not np.all(np.isfinite(measured)):
        raise ValueError("Measured intensity contains non-finite values")
    if not np.all(np.isfinite(desired)):
        raise ValueError("Desired intensity contains non-finite values")
    if np.any(measured < 0):
        raise ValueError("Measured intensity cannot contain negative values")
    if np.any(desired < 0):
        raise ValueError("Desired intensity cannot contain negative values")

    support = desired > 0
    if not np.any(support):
        raise ValueError("Desired intensity has no positive target support")

    response = measured[support] / desired[support]
    mean_response = float(np.mean(response))
    if mean_response <= 0.0:
        raise ValueError("Measured target response must contain positive power")

    response = response / mean_response
    maximum = float(np.max(response))
    minimum = float(np.min(response))
    denominator = maximum + minimum
    uniformity = 0.0 if denominator <= 0.0 else 1.0 - (
        maximum - minimum
    ) / denominator
    normalized_std = float(np.std(response) / np.mean(response))
    return IntensityStatistics(
        uniformity=uniformity,
        normalized_std=normalized_std,
    )


__all__ = ["IntensityStatistics","evaluate_relative_intensity_statistics"]
