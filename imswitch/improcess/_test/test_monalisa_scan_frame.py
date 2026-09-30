"""Tests for the scan of a MoNaLISA recording and its orientation."""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import bandlimited_field
from imswitch.improcess.reconstructors.monalisa.scan_frame import (
    ORIENTATIONS,
    choose_orientation,
    raster_scan_index,
    roughness,
)


class TestRasterScanIndex:
    def test_fast_axis_runs_once_per_line(self):
        index = raster_scan_index(3, 2, "+x+y")
        np.testing.assert_array_equal(
            index, [[0, 0], [1, 0], [2, 0], [0, 1], [1, 1], [2, 1]]
        )

    def test_directions(self):
        index = raster_scan_index(3, 2, "-x+y")
        np.testing.assert_array_equal(
            index, [[0, 0], [-1, 0], [-2, 0], [0, 1], [-1, 1], [-2, 1]]
        )

    def test_fast_axis_along_y(self):
        index = raster_scan_index(3, 2, "+y-x")
        np.testing.assert_array_equal(
            index, [[0, 0], [0, 1], [0, 2], [-1, 0], [-1, 1], [-1, 2]]
        )

    @pytest.mark.parametrize("orientation", ORIENTATIONS)
    def test_every_orientation_visits_every_position_once(self, orientation):
        index = raster_scan_index(4, 6, orientation)
        assert index.shape == (24, 2)
        assert np.unique(index, axis=0).shape[0] == 24

    def test_unknown_orientation(self):
        with pytest.raises(ValueError, match="orientation"):
            raster_scan_index(3, 3, "xy")


class TestRoughness:
    def test_smooth_image_and_noise(self):
        rng = np.random.default_rng(0)
        gy, gx = np.mgrid[0:80, 0:80].astype(float)
        smooth = bandlimited_field(kmax=0.05, seed=1)(gx, gy)
        assert roughness(smooth) < 0.05
        assert roughness(rng.normal(size=(200, 200))) == pytest.approx(1.0, abs=0.03)

    def test_holes_are_ignored(self):
        gy, gx = np.mgrid[0:80, 0:80].astype(float)
        smooth = bandlimited_field(kmax=0.05, seed=1)(gx, gy)
        holed = smooth.copy()
        holed[10:20, 30:50] = np.nan
        assert roughness(holed) == pytest.approx(roughness(smooth), rel=0.1)

    def test_constant_image_has_none(self):
        assert np.isnan(roughness(np.ones((10, 10))))


class TestChooseOrientation:
    """Samples of a smooth specimen, cell by cell, recorded in one order and
    reassembled in all eight."""

    @staticmethod
    def _recording(orientation, steps=8, cells=9, noise=0.0, seed=0):
        rng = np.random.default_rng(seed)
        field = bandlimited_field(kmax=0.12, seed=4)
        index = raster_scan_index(steps, steps, orientation)
        cell_x, cell_y = np.meshgrid(np.arange(cells), np.arange(cells))
        focus_x = (steps * cell_x).ravel().astype(float)
        focus_y = (steps * cell_y).ravel().astype(float)
        # Focus f probes r_f - d_k, on a raster of one pixel per step.
        qx = focus_x[None, :] - index[:, :1]
        qy = focus_y[None, :] - index[:, 1:]
        values = field(qx, qy) + noise * rng.normal(size=qx.shape)
        size = steps * (cells + 1)

        def assemble(candidate):
            gx = focus_x[None, :] - candidate[:, :1] + steps
            gy = focus_y[None, :] - candidate[:, 1:] + steps
            image = np.full((size, size), np.nan)
            image[gy.astype(int).ravel(), gx.astype(int).ravel()] = values.ravel()
            return image

        return assemble, steps

    @pytest.mark.parametrize("orientation", ORIENTATIONS)
    def test_finds_the_orientation_recorded(self, orientation):
        assemble, steps = self._recording(orientation)
        found, scores = choose_orientation(assemble, steps, steps)
        assert found == orientation
        assert set(scores) == set(ORIENTATIONS)

    def test_holds_under_noise_as_large_as_the_specimen(self):
        """Where a total variation would not: the noise adds the same to the
        score of every orientation."""
        field_rms = 1.0
        assemble, steps = self._recording("-x+y", noise=1.0 * field_rms, seed=3)
        found, scores = choose_orientation(assemble, steps, steps)
        assert found == "-x+y"
        ranked = sorted(scores.values())
        assert (ranked[1] - ranked[0]) > 0.2 * (ranked[-1] - ranked[0])


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
