"""Tests for the sharpening filter and the filament width it is judged by."""

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from imswitch.improcess.reconstructors.monalisa import quality
from imswitch.improcess.reconstructors.monalisa.sharpen import (
    wiener_gain,
    wiener_sharpen,
)

SIZE = 400


def _filaments(width_sigma, seed=0, count=14):
    """Straight lines of a Gaussian profile, far enough apart to be alone."""
    rng = np.random.default_rng(seed)
    gy, gx = np.mgrid[0:SIZE, 0:SIZE].astype(float)
    image = np.zeros((SIZE, SIZE))
    for index in range(count):
        angle = rng.uniform(0.0, np.pi)
        # Lines through points on a coarse grid, so that they rarely meet.
        cx = 60.0 + 280.0 * ((index * 5) % count) / count
        cy = 60.0 + 280.0 * index / count
        distance = (gx - cx) * np.sin(angle) - (gy - cy) * np.cos(angle)
        along = (gx - cx) * np.cos(angle) + (gy - cy) * np.sin(angle)
        line = np.exp(-distance**2 / (2.0 * width_sigma**2)) * (np.abs(along) < 45)
        image = np.maximum(image, rng.uniform(60.0, 140.0) * line)
    return image + 20.0


class TestFilamentWidth:
    @pytest.mark.parametrize("sigma", [1.5, 2.0, 3.0])
    def test_measures_the_width_of_lines(self, sigma):
        image = _filaments(sigma)
        found = quality.find_filaments(image)
        assert len(found) >= 8
        assert quality.filament_width(image, found) == pytest.approx(
            2.355 * sigma, rel=0.05
        )

    def test_a_blur_shows_as_width(self):
        image = _filaments(2.0)
        found = quality.find_filaments(image)
        blurred = gaussian_filter(image, 1.5)
        expected = 2.355 * np.hypot(2.0, 1.5)
        assert quality.filament_width(blurred, found) == pytest.approx(expected, rel=0.05)

    def test_holds_under_noise(self):
        rng = np.random.default_rng(1)
        image = _filaments(2.0)
        noisy = image + rng.normal(0.0, 15.0, image.shape)
        found = quality.find_filaments(image)
        assert quality.filament_width(noisy, found) == pytest.approx(2.355 * 2.0, rel=0.08)

    def test_the_thinnest(self):
        image = np.maximum(_filaments(1.5, seed=2, count=8), _filaments(3.0, seed=3, count=8))
        found = quality.find_filaments(image)
        thin = found.thinnest(0.4)
        assert 0 < len(thin) < len(found)
        assert quality.filament_width(image, thin) < quality.filament_width(image, found)

    def test_an_image_without_filaments(self):
        found = quality.find_filaments(np.ones((SIZE, SIZE)))
        assert len(found) == 0
        assert np.isnan(quality.filament_width(np.ones((SIZE, SIZE)), found))


class TestWienerSharpen:
    def test_undoes_a_blur(self):
        """Lines of 2 px, blurred by 1.5 px, sharpened by 1.5 px: nearly the
        lines again."""
        sharp = _filaments(2.0)
        found = quality.find_filaments(sharp)
        blurred = gaussian_filter(sharp, 1.5)
        restored = wiener_sharpen(blurred, 1.5, regularization=0.01)
        assert quality.filament_width(blurred, found) > 1.2 * 2.355 * 2.0
        assert quality.filament_width(restored, found) == pytest.approx(
            2.355 * 2.0, rel=0.06
        )

    def test_keeps_the_level_of_the_image(self):
        image = _filaments(2.0)
        sharpened = wiener_sharpen(image, 1.5)
        assert sharpened.mean() == pytest.approx(image.mean(), rel=1e-3)
        flat = wiener_sharpen(np.full((64, 64), 7.0), 1.5)
        np.testing.assert_allclose(flat, 7.0, atol=1e-9)

    def test_noise_is_amplified_by_no_more_than_the_gain_says(self):
        rng = np.random.default_rng(2)
        noise = rng.normal(0.0, 1.0, (256, 256))
        for regularization, limit in ((0.1, 1.74), (0.03, 2.98)):
            amplified = wiener_sharpen(noise, 1.5, regularization).std()
            assert amplified < limit
            peak = wiener_gain(np.linspace(0.0, 0.7, 2000), 1.5, regularization).max()
            assert peak == pytest.approx(
                (1.0 + regularization) / (2.0 * np.sqrt(regularization)), rel=1e-3
            )

    def test_the_finest_frequencies_are_not_amplified(self):
        """Where the blur has left nothing, there is only noise to amplify."""
        assert wiener_gain(0.0, 1.5, 0.1) == pytest.approx(1.0)
        assert wiener_gain(0.5, 1.5, 0.1) < 0.01

    def test_holes_stay_holes(self):
        image = _filaments(2.0)
        image[100:120, 200:260] = np.nan
        sharpened = wiener_sharpen(image, 1.5)
        assert np.array_equal(np.isnan(sharpened), np.isnan(image))
        assert np.all(np.isfinite(sharpened[~np.isnan(image)]))

    def test_the_edges_are_not_joined(self):
        """A bright left edge must not show at the right edge."""
        image = np.full((128, 128), 10.0)
        image[:, :6] = 200.0
        sharpened = wiener_sharpen(image, 2.0)
        assert np.abs(sharpened[:, -20:] - 10.0).max() < 0.5

    def test_arguments(self):
        with pytest.raises(ValueError, match="sigma"):
            wiener_sharpen(np.ones((8, 8)), 0.0)
        with pytest.raises(ValueError, match="regularization"):
            wiener_sharpen(np.ones((8, 8)), 1.0, regularization=0.0)


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
