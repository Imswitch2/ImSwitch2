"""Contrast and histogram helpers for ImProcess image display."""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.imcommon.model import memory_limits

# A plain in-memory ndarray is downsampled before computing percentiles or
# histograms once the working set the exact path allocates -- a float64 copy,
# a finite mask and the compacted result, _WORKING_SET_BYTES_PER_ELEMENT per
# element -- exceeds this budget. The threshold used to count ELEMENTS
# (64 Mi), which is dtype-blind and measures the one quantity that does not
# determine the cost: a 16-frame 2048x2048 uint16 stack, exactly at it, paid
# a ~1 GiB transient for two numbers on the contrast slider.
#
# The literal is the default -- 1 GiB, the same as ``MemoryOptions`` -- and
# ``memory.processingWorkingSetMB`` in ``imcontrol_options.json`` overrides it
# per machine; the sample size follows it (see ``_sample_working_set_bytes`` /
# ``_max_samples``): lowering the allowance must lower what gets read, or the
# setting means nothing here.
_SAMPLE_WORKING_SET_BYTES = 1024 * 1024 * 1024
_WORKING_SET_BYTES_PER_ELEMENT = 8 + 1 + 8
_MAX_SAMPLE_VALUES = 2_000_000


def _sample_working_set_bytes() -> int:
    """The working set in force: the configured setting, else the literal."""
    return memory_limits.effectiveBytes('processingWorkingSetBytes', _SAMPLE_WORKING_SET_BYTES)


def _max_samples() -> int:
    """How many values a sample may hold within the working set in force."""
    return max(1, min(_MAX_SAMPLE_VALUES,
                      _sample_working_set_bytes() // _WORKING_SET_BYTES_PER_ELEMENT))


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
        return data.size * _WORKING_SET_BYTES_PER_ELEMENT > _sample_working_set_bytes()
    return True


def _kept(size: int, stride: int) -> int:
    """How many elements ``slice(0, size, stride)`` keeps."""
    return len(range(0, int(size), max(1, int(stride))))


def _group_stride(axis_sizes: tuple[int, ...], remaining_factor: float) -> int:
    """Pick one stride, applied uniformly across ``axis_sizes``, that reduces
    the group's element count by roughly ``remaining_factor``.

    Only axes longer than one count towards the exponent: a singleton axis
    keeps its one element whatever the stride, and charging it a share of
    the reduction left the other axes under-strided (a ``(1, 100, 100, 100)``
    stack came back with three times its allowance)."""
    reducible = [size for size in axis_sizes if size > 1]
    if not reducible or remaining_factor <= 1:
        return 1
    stride = int(np.ceil(remaining_factor ** (1.0 / len(reducible))))
    return max(1, min(stride, max(reducible)))


def _strided_key(shape: tuple[int, ...], max_samples: int) -> tuple:
    """A slice key over ``shape`` keeping at most ``max_samples`` elements.

    Leading (non-spatial) axes are strided first so full image planes are
    preferred over degrading in-plane resolution; the last two axes only if
    that is not enough. The count is taken from what each slice actually
    keeps, not from the stride arithmetic, and the spatial stride is raised
    until the total fits -- the arithmetic is a first guess, the count is
    the contract."""
    max_samples = max(1, int(max_samples))
    leading_shape = shape[:-2] if len(shape) > 2 else ()
    spatial_shape = shape[-2:]

    def kept(sizes, stride):
        return int(np.prod([_kept(size, stride) for size in sizes])) if sizes else 1

    def smallest_stride(sizes, fits):
        """The smallest uniform stride over ``sizes`` for which ``fits`` holds.

        What a stride keeps never grows as the stride grows, so a binary
        search finds it; a stride equal to the longest axis keeps one element
        per axis, which is the floor. A stride guessed from the reduction
        factor alone missed whenever the axes could not all shrink by it --
        ``(2, 1000000, 1, 1)`` kept 166 667 values against 61 680 allowed,
        because the size-2 axis cannot give up a factor of six."""
        if not sizes:
            return 1
        low, high = 1, max(1, max(sizes))
        if fits(low):
            return low
        if not fits(high):
            return high
        while high - low > 1:
            middle = (low + high) // 2
            if fits(middle):
                high = middle
            else:
                low = middle
        return high

    spatial_total = int(np.prod(spatial_shape))
    # Leading axes first, as far as they alone can go: whole planes are kept
    # for as long as that meets the allowance.
    leading_stride = smallest_stride(
        leading_shape, lambda s: kept(leading_shape, s) * spatial_total <= max_samples)
    leading_kept = kept(leading_shape, leading_stride)
    spatial_stride = smallest_stride(
        spatial_shape, lambda s: leading_kept * kept(spatial_shape, s) <= max_samples)
    key = tuple(slice(0, size, leading_stride) for size in leading_shape)
    key += tuple(slice(0, size, spatial_stride) for size in spatial_shape)
    return key


def sample_values(data: Any, *, max_samples: int | None = None) -> np.ndarray:
    """Return a bounded flat sample of finite values without full materialization.

    ``max_samples`` defaults to what the working set in force allows, so a
    smaller ``processingWorkingSetMB`` reads fewer values rather than the same
    two million under a smaller nominal budget.

    Reads a strided subset via a single ``__getitem__`` slice call. Leading
    (non-spatial) axes are downsampled first so full image planes are
    preferred over degrading in-plane resolution; the last two axes are only
    strided if downsampling the leading axes alone isn't enough.
    """
    if max_samples is None:
        max_samples = _max_samples()
    max_samples = max(1, int(max_samples))
    shape = tuple(int(size) for size in (getattr(data, "shape", None) or ()))
    if not shape:
        return _flatten_finite(np.asarray(data))

    total = int(np.prod(shape))
    if total == 0:
        return np.asarray([], dtype=np.float64)

    if total <= max_samples:
        key = tuple(slice(None) for _ in shape)
    else:
        key = _strided_key(shape, max_samples)

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
