"""Tests for the ground-truth-free quality measures of a reconstruction."""

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from imswitch.improcess._test._monalisa_synthetic import bandlimited_field
from imswitch.improcess.reconstructors.monalisa import quality


@pytest.fixture(scope="module")
def specimen():
    gy, gx = np.mgrid[0:256, 0:256].astype(float)
    return bandlimited_field(kmax=0.15, seed=2, mean=0.0)(gx, gy)


@pytest.fixture(scope="module")
def texture():
    """A random image with a continuous spectrum, smooth over a few pixels."""
    rng = np.random.default_rng(5)
    return gaussian_filter(rng.normal(size=(384, 384)), 2.5, mode="wrap") * 10.0 + 5.0


class TestSplit:
    def test_masks_share_no_pixel_and_cover_the_frame(self):
        first, second = quality.split_masks((6, 7))
        assert not np.any(first & second)
        assert np.all(first | second)
        assert abs(int(first.sum()) - int(second.sum())) <= 1

    def test_noise_of_the_mean_of_two_images(self):
        rng = np.random.default_rng(0)
        first = rng.normal(0.0, 3.0, (200, 200))
        second = rng.normal(0.0, 3.0, (200, 200))
        assert quality.split_noise(first, second) == pytest.approx(
            3.0 / np.sqrt(2.0), rel=0.03
        )


class TestRingCorrelation:
    def test_same_image(self, specimen):
        _, correlation = quality.ring_correlation(specimen, specimen)
        np.testing.assert_allclose(correlation, 1.0, atol=1e-9)

    def test_independent_noise(self):
        rng = np.random.default_rng(1)
        _, correlation = quality.ring_correlation(
            rng.normal(size=(256, 256)), rng.normal(size=(256, 256))
        )
        assert np.max(np.abs(correlation[3:])) < 0.1

    def test_signal_where_the_specimen_is_and_noise_beyond(self, specimen):
        rng = np.random.default_rng(2)
        noise = 0.2 * np.std(specimen)
        first = specimen + rng.normal(0.0, noise, specimen.shape)
        second = specimen + rng.normal(0.0, noise, specimen.shape)
        frequency, correlation = quality.ring_correlation(first, second)
        assert np.all(correlation[frequency < 0.12] > 0.8)
        assert np.all(np.abs(correlation[frequency > 0.2]) < 0.15)
        period = quality.resolution(frequency, correlation)
        assert 1 / 0.2 < period < 1 / 0.13

    def test_shapes_must_agree(self, specimen):
        with pytest.raises(ValueError, match="shape"):
            quality.ring_correlation(specimen, specimen[:100])

    def test_resolution_of_an_image_without_signal(self):
        frequency = np.arange(1, 21) / 40.0
        assert quality.resolution(frequency, np.zeros(20)) == float("inf")
        assert quality.resolution(frequency, np.ones(20)) == pytest.approx(2.0)


def _cells(shape, index_matrix):
    """The cell (as one integer) every raster pixel lies in."""
    gy, gx = np.mgrid[0:shape[0], 0:shape[1]].astype(float)
    inverse = np.linalg.inv(np.asarray(index_matrix, dtype=float))
    m = np.floor(inverse[0, 0] * gx + inverse[0, 1] * gy).astype(int)
    n = np.floor(inverse[1, 0] * gx + inverse[1, 1] * gy).astype(int)
    return (m - m.min()) * 1000 + (n - n.min())


LATTICES = {
    "square": np.array([[16, 0], [0, 16]]),
    "diamond": np.array([[16, -16], [16, 16]]),
}


@pytest.mark.parametrize("name", LATTICES)
class TestTilingContrast:
    def test_an_image_without_the_lattice(self, texture, name):
        assert quality.tiling_contrast(texture, LATTICES[name]) == pytest.approx(
            1.0, abs=0.35
        )

    def test_a_pattern_inside_the_cell(self, texture, name):
        """The same error at the same scan position in every cell: a frame
        that is 5 % brighter than the others."""
        index_matrix = LATTICES[name]
        gy, gx = np.mgrid[0:texture.shape[0], 0:texture.shape[1]].astype(float)
        inverse = np.linalg.inv(index_matrix.astype(float))
        u = (inverse[0, 0] * gx + inverse[0, 1] * gy) % 1.0
        v = (inverse[1, 0] * gx + inverse[1, 1] * gy) % 1.0
        rng = np.random.default_rng(6)
        by_position = 1.0 + 0.05 * rng.normal(size=(16, 16))
        gain = by_position[(16 * u).astype(int) % 16, (16 * v).astype(int) % 16]
        assert quality.tiling_contrast(texture * gain, index_matrix) > 3.0

    def test_holes_are_tolerated(self, texture, name):
        holed = texture.copy()
        holed[100:110, 100:120] = np.nan
        assert np.isfinite(quality.tiling_contrast(holed, LATTICES[name]))


@pytest.mark.parametrize("name", LATTICES)
class TestSeamContrast:
    def test_an_image_without_seams(self, texture, name):
        owner = _cells(texture.shape, LATTICES[name])
        assert quality.seam_contrast(texture, owner) == pytest.approx(1.0, abs=0.05)

    def test_a_gain_that_differs_from_focus_to_focus(self, texture, name):
        rng = np.random.default_rng(3)
        owner = _cells(texture.shape, LATTICES[name])
        gain = 1.0 + 0.1 * rng.normal(size=owner.max() + 1)
        tiled = texture * gain[owner]
        assert quality.seam_contrast(tiled, owner) > 1.5
        # The spectrum at the lattice frequencies does not see it.
        assert quality.tiling_contrast(tiled, LATTICES[name]) < 2.0

    def test_pixels_without_a_focus_are_left_out(self, texture, name):
        owner = _cells(texture.shape, LATTICES[name])
        holed = texture.copy()
        holed[50:60, 50:90] = np.nan
        owner = np.where(np.isfinite(holed), owner, -1)
        assert quality.seam_contrast(holed, owner) == pytest.approx(1.0, abs=0.05)


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
