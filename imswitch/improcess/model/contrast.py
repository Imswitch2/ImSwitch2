"""Contrast and histogram helpers for ImProcess image display."""

from __future__ import annotations

from typing import Any

import numpy as np

# Above this many elements, a plain in-memory ndarray is downsampled before
# computing percentiles/histograms so display-only operations stay cheap.
_SAMPLE_ELEMENT_THRESHOLD = 64 * 1024 * 1024
_MAX_SAMPLE_VALUES = 2_000_000


def _flatten_finite(arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return np.asarray([], dtype=np.float64)
    flat = arr.astype(np.float64, copy=False).ravel()
    return flat[np.isfinite(flat)]


def _should_sample(data: Any) -> bool:
    """True when reading the whole array is undesirable for a display-only op.

    Any object that isn't a plain in-memory ``np.ndarray`` is treated as a
    potentially lazy/virtual source (HDF5, Zarr, dask, or ImProcess's own
    lazy wrappers) and is always sampled through ``__getitem__`` rather than
    materialized in full via ``np.asarray``/``__array__``.
    """
    if isinstance(data, np.ndarray):
        return data.size > _SAMPLE_ELEMENT_THRESHOLD
    return True


def _group_stride(axis_sizes: tuple[int, ...], remaining_factor: float) -> int:
    """Pick one stride, applied uniformly across ``axis_sizes``, that reduces
    the group's element count by roughly ``remaining_factor``."""
    if not axis_sizes or remaining_factor <= 1:
        return 1
    stride = int(np.ceil(remaining_factor ** (1.0 / len(axis_sizes))))
    return max(1, min(stride, max(axis_sizes)))


def sample_values(data: Any, *, max_samples: int = _MAX_SAMPLE_VALUES) -> np.ndarray:
    """Return a bounded flat sample of finite values without full materialization.

    Reads a strided subset via a single ``__getitem__`` slice call. Leading
    (non-spatial) axes are downsampled first so full image planes are
    preferred over degrading in-plane resolution; the last two axes are only
    strided if downsampling the leading axes alone isn't enough.
    """
    shape = tuple(int(size) for size in (getattr(data, "shape", None) or ()))
    if not shape:
        return _flatten_finite(np.asarray(data))

    total = int(np.prod(shape))
    if total == 0:
        return np.asarray([], dtype=np.float64)

    if total <= max_samples:
        key = tuple(slice(None) for _ in shape)
    else:
        leading_shape = shape[:-2] if len(shape) > 2 else ()
        spatial_shape = shape[-2:] if len(shape) >= 1 else ()

        remaining = total / max_samples
        leading_stride = _group_stride(leading_shape, remaining)
        if leading_shape:
            remaining = remaining / (leading_stride ** len(leading_shape))

        spatial_stride = _group_stride(spatial_shape, remaining)

        key = tuple(slice(0, size, leading_stride) for size in leading_shape)
        key += tuple(slice(0, size, spatial_stride) for size in spatial_shape)

    sampled = data[key] if hasattr(data, "__getitem__") else data
    return _flatten_finite(np.asarray(sampled))


def finite_values(data: Any) -> np.ndarray:
    """Return a flat float array containing only finite values."""
    if _should_sample(data):
        return sample_values(data)
    return _flatten_finite(np.asarray(data))


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


def safe_display_levels(
    minimum: float,
    maximum: float,
    *,
    min_span: float = 1.0,
) -> tuple[float, float]:
    """Return strictly increasing display levels safe for napari.

    Napari rejects degenerate (non-increasing) contrast_limits and
    contrast_limits_range. This helper expands degenerate ranges to a
    minimum span.

    Parameters
    ----------
    minimum:
        Lower display level.
    maximum:
        Upper display level.
    min_span:
        Minimum span (maximum - minimum) to enforce when the input is
        degenerate. Defaults to 1.0.

    Returns
    -------
    tuple[float, float]
        (min, max) unchanged when max > min; otherwise expanded to
        (min, min + min_span). NaN inputs are coerced to (0.0, min_span).
    """
    minimum = float(minimum)
    maximum = float(maximum)
    if not np.isfinite(minimum) or not np.isfinite(maximum):
        return 0.0, float(min_span)
    if maximum > minimum:
        return minimum, maximum
    return minimum, minimum + float(min_span)


__all__ = [
    "auto_levels",
    "finite_range",
    "finite_values",
    "histogram",
    "normalize_levels",
    "safe_display_levels",
    "sample_values",
]
