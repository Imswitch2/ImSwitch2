"""Tests for the placement of MoNaLISA samples on the output raster.

The cases follow experiment E4 of
``docs/monalisa_optimal_reconstruction.md``.
"""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import (
    bandlimited_field,
    lattice_points,
)
from imswitch.improcess.reconstructors.monalisa.placement import (
    OutputRaster,
    commensurability,
    coverage,
    grid_bspline,
    lock_step_to_lattice,
    place,
    place_nearest,
    sample_positions,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import get_1d_indices

A = 11.0
HEX = np.array([[A, A / 2], [0.0, A * np.sqrt(3) / 2]])
S = 10.41
DIAMOND = np.array([[S / np.sqrt(2), -S / np.sqrt(2)], [S / np.sqrt(2), S / np.sqrt(2)]])
THETA = np.deg2rad(17.0)
HEX_ROTATED = np.array(
    [
        [A * np.cos(THETA), A * np.cos(THETA + np.pi / 3)],
        [A * np.sin(THETA), A * np.sin(THETA + np.pi / 3)],
    ]
)


def _raster_scan(nx, ny):
    return np.array([(i, j) for j in range(ny) for i in range(nx)])


CASES = {
    "rectangular": (
        np.diag([11.05, 11.05]), np.diag([11.05 / 22, 11.05 / 22]), (22, 22)
    ),
    "hexagonal": (HEX, np.diag([A / 22, A * np.sqrt(3) / 2 / 19]), (22, 19)),
    "diamond-brick": (
        DIAMOND, np.diag([S * np.sqrt(2) / 32, S / np.sqrt(2) / 16]), (32, 16)
    ),
    "hexagonal-17deg-along-lattice": (
        HEX_ROTATED, HEX_ROTATED / np.array([22, 19]), (22, 19)
    ),
}


def _samples(lattice, step, steps, shape=(110, 110)):
    """Samples of a scan on the raster of its own step, and the truth there."""
    offset = np.array([2.2, 3.7])
    reach = float(np.abs(step @ np.array(steps)).max())
    foci = lattice_points(lattice[:, 0], lattice[:, 1], offset, shape, margin=reach)
    offsets = _raster_scan(*steps) @ step.T
    qx, qy = sample_positions(foci[:, 0], foci[:, 1], offsets[:, 0], offsets[:, 1])
    raster = OutputRaster.covering(shape, step, offset)
    gx, gy = raster.coordinates(qx, qy)
    field = bandlimited_field(kmax=0.35, seed=3, mean=0.0)
    rows, cols = raster.shape
    ry, rx = np.mgrid[0:rows, 0:cols].astype(float)
    return gx, gy, field(gx, gy), field(rx, ry), raster


def _relative_error(image, truth, border=12):
    inner = (slice(border, -border), slice(border, -border))
    difference = (image - truth)[inner]
    finite = np.isfinite(difference)
    return (
        float(np.sqrt(np.mean(difference[finite] ** 2)) / np.sqrt(np.mean(truth**2))),
        float(1.0 - finite.mean()),
    )


class TestCommensurability:
    @pytest.mark.parametrize("name", CASES)
    def test_designed_scans_are_commensurate_and_cover_once(self, name):
        lattice, step, steps = CASES[name]
        index_matrix, residual = commensurability(lattice, step)
        assert residual < 1e-9
        result = coverage(index_matrix, _raster_scan(*steps))
        assert result.cell_pixels == steps[0] * steps[1]
        assert result.exactly_once

    def test_odd_step_count_is_not_commensurate_with_a_hexagonal_lattice(self):
        step = np.diag([A / 21, A * np.sqrt(3) / 2 / 19])
        _, residual = commensurability(HEX, step)
        assert residual == pytest.approx(0.5)

    def test_a_small_step_error_shows_as_a_residual(self):
        lattice, step, _ = CASES["rectangular"]
        _, residual = commensurability(lattice, step * 1.003)
        assert residual == pytest.approx(22 * 0.003, rel=0.05)

    def test_a_rectangle_of_the_right_area_can_still_miss(self):
        """The diamond cell has the area of 32 x 16 steps, and 16 x 32 steps
        tile it as well, but 64 x 8 steps of the same area do not: the area
        ratio says 1, the coverage says holes."""
        lattice, step, _ = CASES["diamond-brick"]
        index_matrix, _ = commensurability(lattice, step)
        assert coverage(index_matrix, _raster_scan(16, 32)).exactly_once
        wrong = coverage(index_matrix, _raster_scan(64, 8))
        assert wrong.cell_pixels == 64 * 8
        assert wrong.holes > 0
        assert wrong.overlaps > 0

    def test_overscan_shows_as_overlaps_without_holes(self):
        lattice, step, steps = CASES["rectangular"]
        index_matrix, _ = commensurability(lattice, step)
        result = coverage(index_matrix, _raster_scan(steps[0] + 3, steps[1]))
        assert result.holes == 0
        assert result.overlaps == 3 * steps[1]

    def test_scan_direction_does_not_change_the_coverage(self):
        lattice, step, steps = CASES["hexagonal"]
        index_matrix, _ = commensurability(lattice, step)
        assert coverage(index_matrix, -_raster_scan(*steps)).exactly_once


class TestStepLock:
    def test_a_nominal_step_within_tolerance_is_locked(self):
        lattice, step, _ = CASES["diamond-brick"]
        # The reference recording: 35 nm steps on nominally 77 nm pixels are
        # 1.2 % short of subdividing the 10.41 px diamond.
        nominal = np.diag([35.0 / 77.0, 35.0 / 77.0])
        locked, change = lock_step_to_lattice(lattice, nominal)
        assert change == pytest.approx(0.012, abs=0.001)
        np.testing.assert_allclose(locked, step, atol=1e-12)
        assert commensurability(lattice, locked)[1] < 1e-9

    def test_a_step_far_from_subdividing_is_kept(self):
        """22 / 1.024 = 21.5 steps per period: as far from 21 as from 22."""
        lattice, step, _ = CASES["rectangular"]
        nominal = step * 1.024
        kept, change = lock_step_to_lattice(lattice, nominal, tolerance=0.02)
        np.testing.assert_array_equal(kept, nominal)
        assert change > 0.02

    def test_locking_picks_the_nearest_subdivision(self):
        """A step 5 % long is 0.2 % from dividing the period by 21. Whether
        the scan then covers the cell is for the coverage to say."""
        lattice, step, steps = CASES["rectangular"]
        locked, change = lock_step_to_lattice(lattice, step * 1.05)
        index_matrix, residual = commensurability(lattice, locked)
        assert residual < 1e-9
        np.testing.assert_array_equal(index_matrix, np.diag([21, 21]))
        assert change < 0.005
        assert coverage(index_matrix, _raster_scan(*steps)).overlaps > 0


class TestExactPlacement:
    @pytest.mark.parametrize("name", CASES)
    def test_commensurate_scans_place_without_error(self, name):
        lattice, step, steps = CASES[name]
        gx, gy, values, truth, raster = _samples(lattice, step, steps)
        placed = place(gx, gy, values, raster.shape)
        assert placed.method == "exact"
        assert placed.max_residual < 1e-9
        inner = placed.count[12:-12, 12:-12]
        covered = inner > 0
        assert np.all(inner[covered] == 1)
        error = (placed.image - truth)[12:-12, 12:-12][covered]
        assert np.max(np.abs(error)) < 1e-9

    @pytest.mark.parametrize("orientation", ["+x+y", "-x-y", "+x-y", "-x+y"])
    def test_reproduces_the_legacy_integer_scatter(self, orientation):
        """Axis-aligned scan of an axis-aligned lattice: the same array the
        ``get_1d_indices`` path assembles, bit for bit."""
        rng = np.random.default_rng(0)
        nx_c, ny_c, nx_s, ny_s = 7, 5, 6, 4
        values = rng.random((nx_s * ny_s, nx_c * ny_c))
        legacy = np.zeros(nx_c * nx_s * ny_c * ny_s)
        legacy[get_1d_indices(nx_c, ny_c, nx_s, ny_s, orientation).ravel()] = (
            values.ravel()
        )
        legacy = legacy.reshape(ny_c * ny_s, nx_c * nx_s)

        step = np.diag([12.0 / nx_s, 10.0 / ny_s])
        focus_x, focus_y = np.meshgrid(12.0 * np.arange(nx_c), 10.0 * np.arange(ny_c))
        sign_x = -1 if orientation[0] == "+" else 1
        sign_y = -1 if orientation[2] == "+" else 1
        scan = _raster_scan(nx_s, ny_s) * np.array([sign_x, sign_y])
        offsets = scan @ step.T
        qx, qy = sample_positions(
            focus_x.ravel(), focus_y.ravel(), offsets[:, 0], offsets[:, 1]
        )
        origin = (
            0.0 if sign_x < 0 else -(nx_s - 1) * step[0, 0],
            0.0 if sign_y < 0 else -(ny_s - 1) * step[1, 1],
        )
        raster = OutputRaster(origin=origin, step=step, shape=legacy.shape)
        gx, gy = raster.coordinates(qx, qy)
        placed = place_nearest(gx, gy, values, raster.shape)
        np.testing.assert_array_equal(placed.image, legacy)

    def test_overlapping_samples_are_averaged_by_inverse_variance(self):
        gx = np.array([1.0, 1.0, 2.0])
        gy = np.array([1.0, 1.0, 0.0])
        placed = place_nearest(
            gx, gy, np.array([10.0, 20.0, 5.0]), (3, 4),
            variance=np.array([1.0, 3.0, 2.0]),
        )
        assert placed.image[1, 1] == pytest.approx(12.5)
        assert placed.variance[1, 1] == pytest.approx(0.75)
        assert placed.count[1, 1] == 2
        assert placed.image[0, 2] == pytest.approx(5.0)
        assert np.isnan(placed.image[2, 3])

    def test_samples_without_a_value_are_left_out(self):
        placed = place_nearest(
            np.array([0.0, 1.0]), np.array([0.0, 0.0]), np.array([np.nan, 2.0]), (1, 2)
        )
        assert np.isnan(placed.image[0, 0])
        assert placed.image[0, 1] == 2.0


class TestGridding:
    def test_a_step_error_is_interpolated_not_misplaced(self):
        """E4: a scan step 0.3 % off costs nearest placement a quarter of the
        image; the spline recovers it to 2 %."""
        lattice, step, steps = CASES["hexagonal"]
        assert step[1, 1] == pytest.approx(0.5014, abs=1e-4)
        off_step = np.diag([step[0, 0], 0.5])
        gx, gy, values, truth, raster = _samples(lattice, off_step, steps)

        assert np.abs(gx - np.rint(gx)).max() < 1e-9
        assert np.abs(gy - np.rint(gy)).max() > 0.05
        nearest_error, _ = _relative_error(
            place_nearest(gx, gy, values, raster.shape).image, truth
        )
        gridded = place(gx, gy, values, raster.shape)
        gridded_error, holes = _relative_error(gridded.image, truth)
        assert gridded.method == "gridded"
        assert gridded.converged
        assert nearest_error > 0.2
        assert gridded_error < 0.03
        assert holes == 0.0

    def test_samples_on_the_raster_are_reproduced(self):
        lattice, step, steps = CASES["rectangular"]
        gx, gy, values, truth, raster = _samples(lattice, step, steps)
        gridded = grid_bspline(gx, gy, values, raster.shape)
        error, holes = _relative_error(gridded.image, truth)
        assert gridded.converged
        assert error < 0.003
        assert holes == 0.0

    def test_an_axis_scan_of_a_rotated_lattice_leaves_holes(self):
        """E4's rotated case: an isotropic axis-aligned scan of the right
        area is not a fundamental domain of a lattice rotated by 17 degrees.
        The holes are reported, not filled."""
        steps = (22, 19)
        pitch = np.sqrt(abs(np.linalg.det(HEX_ROTATED)) / (steps[0] * steps[1]))
        step = np.diag([pitch, pitch])
        index_matrix, residual = commensurability(HEX_ROTATED, step)
        assert residual > 0.3
        gx, gy, values, truth, raster = _samples(HEX_ROTATED, step, steps)
        gridded = place(gx, gy, values, raster.shape)
        error, holes = _relative_error(gridded.image, truth)
        assert gridded.method == "gridded"
        assert 0.02 < holes < 0.10
        assert error < 0.08

    def test_noise_is_passed_through_not_smoothed(self):
        lattice, step, steps = CASES["rectangular"]
        gx, gy, values, truth, raster = _samples(lattice, step, steps)
        rng = np.random.default_rng(1)
        rms = float(np.sqrt(np.mean(truth**2)))
        noisy = values + rng.normal(0.0, 0.1 * rms, values.shape)
        error, _ = _relative_error(
            grid_bspline(gx, gy, noisy, raster.shape).image, truth
        )
        assert error == pytest.approx(0.1, abs=0.01)

    def test_weights_favour_the_precise_samples(self):
        """Two samples per pixel, one of them ten times noisier."""
        rng = np.random.default_rng(2)
        rows = cols = 40
        ry, rx = np.mgrid[0:rows, 0:cols].astype(float)
        truth = 5.0 + np.sin(rx / 6.0) * np.cos(ry / 7.0)
        gx = np.concatenate([rx.ravel() + 0.25, rx.ravel() - 0.25])
        gy = np.concatenate([ry.ravel() + 0.25, ry.ravel() - 0.25])
        exact = 5.0 + np.sin(gx / 6.0) * np.cos(gy / 7.0)
        sd = np.concatenate([np.full(rx.size, 0.02), np.full(rx.size, 0.2)])
        values = exact + rng.normal(0.0, 1.0, exact.shape) * sd
        inner = (slice(4, -4), slice(4, -4))

        def error(variance):
            image = grid_bspline(gx, gy, values, (rows, cols), variance=variance).image
            return float(np.sqrt(np.nanmean((image - truth)[inner] ** 2)))

        assert error(sd**2) < 0.5 * error(None)

    def test_no_samples(self):
        empty = grid_bspline(np.array([]), np.array([]), np.array([]), (4, 5))
        assert empty.image.shape == (4, 5)
        assert np.all(np.isnan(empty.image))
        assert not empty.converged


class TestOutputRaster:
    def test_covering_contains_the_anchor_on_a_pixel(self):
        step = np.diag([0.5, 0.55])
        raster = OutputRaster.covering((100, 120), step, (4.2, 5.7))
        gx, gy = raster.coordinates(4.2, 5.7)
        assert gx == pytest.approx(np.rint(gx), abs=1e-9)
        assert gy == pytest.approx(np.rint(gy), abs=1e-9)
        corner_x, corner_y = raster.coordinates(
            np.array([0.0, 119.0]), np.array([0.0, 99.0])
        )
        assert corner_x[0] >= -1e-9 and corner_y[0] >= -1e-9
        assert corner_x[1] <= raster.shape[1] - 1 + 1e-9
        assert corner_y[1] <= raster.shape[0] - 1 + 1e-9

    def test_a_sheared_raster_maps_back_to_the_camera(self):
        step = HEX_ROTATED / np.array([22, 19])
        raster = OutputRaster.covering((64, 64), step, (3.0, 4.0))
        gx, gy = raster.coordinates(np.array([10.0, 50.0]), np.array([20.0, 5.0]))
        back = np.asarray(raster.origin)[:, None] + step @ np.vstack([gx, gy])
        np.testing.assert_allclose(back, [[10.0, 50.0], [20.0, 5.0]], atol=1e-9)


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
