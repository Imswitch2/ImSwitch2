"""Contrast and histogram helpers for ImProcess image display."""

from __future__ import annotations

from typing import Any

import numpy as np


def finite_values(data: Any) -> np.ndarray:
    """Return a flat float array containing only finite values."""
    arr = np.asarray(data)
    if arr.size == 0:
        return np.asarray([], dtype=np.float64)
    flat = arr.astype(np.float64, copy=False).ravel()
    return flat[np.isfinite(flat)]


def finite_range(data: Any, *, pad_fraction: float = 0.005) -> tuple[float, float]:
    """Return a strictly increasing finite display range for ``data``."""
    values = finite_values(data)
    if values.size == 0:
        return 0.0, 1.0

    minimum = float(np.min(values))
    maximum = float(np.max(values))
    return normalize_levels(minimum, maximum, pad_fraction=pad_fraction)


def auto_levels(
    data: Any,
    *,
    saturated_percent: float = 0.35,
    pad_fraction: float = 0.005,
) -> tuple[float, float]:
    """Return percentile-based display levels.

    ``saturated_percent`` follows ImageJ's convention: it is the total
    percentage allowed to saturate across both tails.
    """
    values = finite_values(data)
    if values.size == 0:
        return 0.0, 1.0

    saturated_percent = max(0.0, min(float(saturated_percent), 100.0))
    lower = saturated_percent / 2.0
    upper = 100.0 - lower
    minimum, maximum = np.percentile(values, [lower, upper])
    return normalize_levels(float(minimum), float(maximum), pad_fraction=pad_fraction)


def histogram(
    data: Any,
    *,
    bins: int = 256,
    value_range: tuple[float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return histogram counts and bin edges for finite values."""
    values = finite_values(data)
    if values.size == 0:
        return np.zeros(int(bins), dtype=np.int64), np.linspace(0.0, 1.0, int(bins) + 1)

    if value_range is None:
        value_range = finite_range(values)
    counts, edges = np.histogram(values, bins=int(bins), range=value_range)
    return counts, edges


def normalize_levels(
    minimum: float,
    maximum: float,
    *,
    pad_fraction: float = 0.005,
) -> tuple[float, float]:
    """Return finite, strictly increasing levels."""
    minimum = float(minimum)
    maximum = float(maximum)
    if not np.isfinite(minimum) or not np.isfinite(maximum):
        return 0.0, 1.0
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    if minimum == maximum:
        pad = max(abs(minimum) * float(pad_fraction), 1.0)
        return minimum - pad, maximum + pad
    return minimum, maximum


__all__ = [
    "auto_levels",
    "finite_range",
    "finite_values",
    "histogram",
    "normalize_levels",
]
