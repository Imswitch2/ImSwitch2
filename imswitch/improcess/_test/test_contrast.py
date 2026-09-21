"""Tests for ImProcess contrast helpers."""

import numpy as np
import pytest

from imswitch.improcess.model.contrast import (
    auto_levels,
    finite_range,
    finite_values,
    histogram,
    safe_display_levels,
    sample_values,
)


class _LazyArray:
    """Array-like test double matching the contract used elsewhere in
    ImProcess (e.g. processors/stack_subset's LazySubsetArray tests):
    __getitem__ is allowed, __array__ must never be called."""

    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.ndim = self._data.ndim
        self.dtype = self._data.dtype
        self.keys = []

    def __getitem__(self, key):
        self.keys.append(key)
        return self._data[key]

    def __array__(self, dtype=None):
        raise AssertionError("contrast helpers should not materialize lazy data")


def test_finite_range_ignores_nan_and_inf():
    data = np.array([np.nan, -1.0, 2.0, np.inf], dtype=np.float32)

    assert finite_range(data) == (-1.0, 2.0)


def test_finite_range_pads_constant_data():
    minimum, maximum = finite_range(np.full((4, 4), 100.0, dtype=np.float32))

    assert minimum < 100.0 < maximum
    assert maximum - minimum == pytest.approx(2.0)


def test_auto_levels_uses_saturated_percent_across_both_tails():
    data = np.arange(101, dtype=np.float32)

    minimum, maximum = auto_levels(data, saturated_percent=10.0)

    assert minimum == pytest.approx(5.0)
    assert maximum == pytest.approx(95.0)


def test_histogram_uses_only_finite_values():
    counts, edges = histogram([0.0, 1.0, np.nan, np.inf], bins=2, value_range=(0.0, 1.0))

    assert counts.tolist() == [1, 1]
    assert edges.tolist() == [0.0, 0.5, 1.0]


def test_finite_values_reads_lazy_array_via_getitem_without_materializing():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    lazy = _LazyArray(data)

    values = finite_values(lazy)

    assert lazy.keys, "expected __getitem__ to be used instead of __array__"
    np.testing.assert_array_equal(np.sort(values), np.sort(data.ravel()))


def test_auto_levels_and_histogram_accept_lazy_arrays_without_materializing():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    lazy = _LazyArray(data)

    levels = auto_levels(lazy, saturated_percent=0.0)
    counts, _edges = histogram(lazy, bins=4)

    assert levels == pytest.approx((float(data.min()), float(data.max())))
    assert counts.sum() == data.size


def test_sample_values_bounds_output_size_for_oversized_ndarray(monkeypatch):
    import imswitch.improcess.model.contrast as contrast_module

    monkeypatch.setattr(contrast_module, "_SAMPLE_WORKING_SET_BYTES", 500 * contrast_module._WORKING_SET_BYTES_PER_ELEMENT)

    data = np.arange(10_000, dtype=np.float32)

    sampled = sample_values(data, max_samples=500)

    assert 0 < sampled.size <= 500 * 2  # bounded, allowing for stride rounding
    assert sampled.min() >= data.min()
    assert sampled.max() <= data.max()


def test_auto_levels_samples_oversized_ndarray_within_tolerance(monkeypatch):
    import imswitch.improcess.model.contrast as contrast_module

    monkeypatch.setattr(contrast_module, "_SAMPLE_WORKING_SET_BYTES", 500 * contrast_module._WORKING_SET_BYTES_PER_ELEMENT)

    data = np.arange(10_000, dtype=np.float32)

    minimum, maximum = auto_levels(data, saturated_percent=10.0)

    assert minimum == pytest.approx(500.0, rel=0.2)
    assert maximum == pytest.approx(9500.0, rel=0.02)


def test_sample_values_prefers_downsampling_leading_axes_over_spatial(monkeypatch):
    import imswitch.improcess.model.contrast as contrast_module

    monkeypatch.setattr(contrast_module, "_SAMPLE_WORKING_SET_BYTES", 100 * contrast_module._WORKING_SET_BYTES_PER_ELEMENT)

    # 200 planes of 5x5 -- comfortably reducible via the leading (frame) axis
    # alone, so the 5x5 in-plane resolution should be preserved.
    data = np.arange(200 * 5 * 5, dtype=np.float32).reshape(200, 5, 5)
    lazy = _LazyArray(data)

    sample_values(lazy, max_samples=100)

    key = lazy.keys[-1]
    assert key[-2] == slice(0, 5, 1)
    assert key[-1] == slice(0, 5, 1)


def test_safe_display_levels_returns_unchanged_when_strictly_increasing():
    minimum, maximum = safe_display_levels(10.0, 20.0)
    assert minimum == 10.0
    assert maximum == 20.0


def test_safe_display_levels_expands_flat_levels_by_min_span():
    minimum, maximum = safe_display_levels(5.0, 5.0)
    assert minimum == 5.0
    assert maximum == 6.0


def test_safe_display_levels_expands_flat_levels_with_custom_span():
    minimum, maximum = safe_display_levels(0.0, 0.0, min_span=10.0)
    assert minimum == 0.0
    assert maximum == 10.0


def test_safe_display_levels_coerces_nan_to_zero_span():
    minimum, maximum = safe_display_levels(np.nan, np.nan)
    assert minimum == 0.0
    assert maximum == 1.0


def test_safe_display_levels_coerces_mixed_nan_to_zero_span():
    minimum, maximum = safe_display_levels(5.0, np.nan, min_span=2.0)
    assert minimum == 0.0
    assert maximum == 2.0


def test_a_configured_working_set_lowers_the_sample_count():
    """memory.processingWorkingSetMB is honoured by the sampler, not only by the threshold."""
    from types import SimpleNamespace

    from imswitch.imcommon.model import memory_limits
    from imswitch.improcess.model import contrast as contrast_module

    data = np.arange(4 * 512 * 512, dtype=np.uint16).reshape(4, 512, 512)
    assert data.size > 2_000_000 // 4  # big enough that the sample size matters

    memory_limits.configure(SimpleNamespace(processingWorkingSetMB=1), logger=None)
    allowed = (1024 * 1024) // contrast_module._WORKING_SET_BYTES_PER_ELEMENT
    assert contrast_module._should_sample(data)
    values = contrast_module.finite_values(data)
    assert values.size <= allowed
    assert values.size > allowed // 8

    memory_limits.reset()
    assert contrast_module._max_samples() == contrast_module._MAX_SAMPLE_VALUES
