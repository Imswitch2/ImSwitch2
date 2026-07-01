"""Tests for ImProcess contrast helpers."""

import numpy as np
import pytest

from imswitch.improcess.model.contrast import auto_levels, finite_range, histogram


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
