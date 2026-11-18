"""``bestLevels`` is ImageJ's "Auto" brightness/contrast (first press)."""

import numpy as np

from imswitch.imcommon.view.guitools.imagetools import bestLevels


def test_a_few_hot_pixels_saturate_instead_of_setting_the_range():
    rng = np.random.default_rng(0)
    image = rng.normal(100.0, 10.0, (200, 200))
    image[:2, :2] = 250.0  # 4 of 40 000 pixels, under 1/5000 of them
    low, high = bestLevels(image)
    assert high < 150.0
    assert 50.0 < low < 80.0


def test_the_first_bin_counts_like_any_other():
    # The earlier port began its search at the second bin.
    image = np.concatenate([np.zeros(500), np.linspace(1.0, 255.0, 9500)])
    low, _ = bestLevels(image)
    assert low == 0.0


def test_a_background_bin_holding_over_a_tenth_is_ignored():
    image = np.concatenate([np.zeros(5000), np.linspace(100.0, 200.0, 5000)])
    low, high = bestLevels(image)
    assert low >= 99.0
    assert high <= 200.0


def test_a_uniform_image_gets_its_own_range_not_inverted_levels():
    low, high = bestLevels(np.full((64, 64), 7.0))
    assert (low, high) == (7.0, 7.0)


def test_no_qualifying_bin_falls_back_to_minimum_and_maximum():
    # Two values, half the pixels each: both bins are "background".
    image = np.repeat([2.0, 9.0], 500)
    assert bestLevels(image) == (2.0, 9.0)


def test_the_top_level_is_the_last_bins_lower_edge_as_in_imagej():
    low, high = bestLevels(np.linspace(0.0, 256.0, 10_000))
    assert (low, high) == (0.0, 255.0)
