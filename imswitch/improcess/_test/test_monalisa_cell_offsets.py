"""Tests for the offsets of the cells, read from their borders."""

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from imswitch.improcess.reconstructors.monalisa import quality
from imswitch.improcess.reconstructors.monalisa.cell_offsets import fit_cell_offsets

LATTICES = {
    "square": np.array([[16, 0], [0, 16]]),
    "diamond-brick": np.array([[16, -16], [16, 16]]),
}


def _cells(shape, index_matrix):
    """The cell of every raster pixel, numbered from 0.

    For the diamond the cells are the bricks of 16 x 32 pixels a scan of
    16 x 32 steps fills.
    """
    gy, gx = np.mgrid[0:shape[0], 0:shape[1]]
    if index_matrix[0, 1] == 0:
        column, row = gx // 16, gy // 16
    else:
        column = gx // 16
        row = (gy - 16 * (column % 2)) // 32 + 1
    _, cell = np.unique(column * 1000 + row, return_inverse=True)
    return cell.reshape(shape)


@pytest.fixture(scope="module")
def specimen():
    rng = np.random.default_rng(5)
    image = gaussian_filter(rng.normal(size=(320, 320)), 3.0, mode="wrap")
    return 100.0 * image / image.std() + 300.0


@pytest.mark.parametrize("name", LATTICES)
class TestFitCellOffsets:
    def test_recovers_the_offsets_of_the_cells(self, specimen, name):
        rng = np.random.default_rng(1)
        owner = _cells(specimen.shape, LATTICES[name])
        num = owner.max() + 1
        truth = rng.normal(0.0, 30.0, num)
        fitted = fit_cell_offsets(specimen + truth[owner], owner, num)
        # What all cells share is not measured; compare what differs.
        error = (fitted - fitted.mean()) - (truth - truth.mean())
        assert np.std(error) < 0.2 * np.std(truth)
        corrected = specimen + truth[owner] - fitted[owner]
        assert quality.seam_contrast(specimen + truth[owner], owner) > 1.5
        assert quality.seam_contrast(corrected, owner) == pytest.approx(1.0, abs=0.1)

    def test_an_image_without_offsets_keeps_its_cells(self, specimen, name):
        owner = _cells(specimen.shape, LATTICES[name])
        fitted = fit_cell_offsets(specimen, owner, owner.max() + 1)
        assert np.std(fitted) < 0.06 * np.std(specimen)

    def test_under_noise(self, specimen, name):
        rng = np.random.default_rng(2)
        owner = _cells(specimen.shape, LATTICES[name])
        num = owner.max() + 1
        truth = rng.normal(0.0, 30.0, num)
        noisy = specimen + truth[owner] + rng.normal(0.0, 30.0, specimen.shape)
        fitted = fit_cell_offsets(noisy, owner, num)
        error = (fitted - fitted.mean()) - (truth - truth.mean())
        assert np.std(error) < 0.5 * np.std(truth)

    def test_noise_is_not_taken_for_offsets(self, specimen, name):
        """A recording of sparse filaments: the borders show steps, and they
        are the noise of their pixels. Moving whole cells by them would tile
        the image."""
        rng = np.random.default_rng(3)
        owner = _cells(specimen.shape, LATTICES[name])
        noisy = 0.1 * specimen + rng.normal(0.0, 30.0, specimen.shape)
        raw = fit_cell_offsets(noisy, owner, owner.max() + 1, shrink=False)
        fitted, share = fit_cell_offsets(
            noisy, owner, owner.max() + 1, return_share=True
        )
        assert np.std(raw) > 3.0
        assert share < 0.2
        assert np.std(fitted) < 0.2 * np.std(raw)

    def test_offsets_that_are_there_are_kept_whole(self, specimen, name):
        rng = np.random.default_rng(1)
        owner = _cells(specimen.shape, LATTICES[name])
        truth = rng.normal(0.0, 30.0, owner.max() + 1)
        _, share = fit_cell_offsets(
            specimen + truth[owner], owner, owner.max() + 1, return_share=True
        )
        assert share > 0.9

    def test_a_slope_across_the_image_is_not_an_offset(self, specimen, name):
        """The step from pixel to pixel that the slope of the image makes is
        expected, and not counted."""
        owner = _cells(specimen.shape, LATTICES[name])
        gy, gx = np.mgrid[0:320, 0:320].astype(float)
        ramp = 2.0 * gx + 1.0 * gy
        fitted = fit_cell_offsets(ramp, owner, owner.max() + 1)
        np.testing.assert_allclose(fitted, 0.0, atol=1e-6)


class TestEdgeCases:
    def test_pixels_without_a_focus(self, specimen):
        owner = _cells(specimen.shape, LATTICES["square"])
        holed = specimen.copy()
        holed[40:80, 100:200] = np.nan
        owner = np.where(np.isfinite(holed), owner, -1)
        fitted = fit_cell_offsets(holed, owner, int(owner.max()) + 1)
        assert np.all(np.isfinite(fitted))

    def test_one_cell_has_no_border(self):
        fitted = fit_cell_offsets(np.ones((8, 8)), np.zeros((8, 8), dtype=int), 3)
        np.testing.assert_array_equal(fitted, np.zeros(3))

    def test_foci_without_a_cell_get_no_offset(self, specimen):
        owner = _cells(specimen.shape, LATTICES["square"])
        num = int(owner.max()) + 5
        fitted = fit_cell_offsets(specimen, owner, num)
        assert fitted.shape == (num,)
        np.testing.assert_array_equal(fitted[-4:], 0.0)


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
