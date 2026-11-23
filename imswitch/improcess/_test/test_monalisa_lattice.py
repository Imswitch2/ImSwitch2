"""Tests for the generalized MoNaLISA illumination lattice."""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import render_spots
from imswitch.improcess.reconstructors.monalisa.lattice import (
    Lattice,
    detect_lattice,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    get_center_coords,
)


def _render_foci(shape, lattice: Lattice, sigma: float = 1.8, amp: float = 100.0):
    """Gaussian foci at every lattice point in (and just outside) the frame."""
    x, y = lattice.points_in_frame(*shape, margin=3 * sigma)
    return render_spots(shape, x, y, sigma, np.full(x.shape, amp))


def _rotated_square(period: float, degrees: float, offset=(0.0, 0.0)) -> Lattice:
    theta = np.deg2rad(degrees)
    return Lattice(
        a1=(period * np.cos(theta), period * np.sin(theta)),
        a2=(-period * np.sin(theta), period * np.cos(theta)),
        offset=offset,
    )


class TestLatticeGeometry:
    def test_rectangular_points_match_legacy_center_coords(self):
        xp, yp = 11.05, 10.3
        xo, yo = 5.37, 4.81
        num_rows = num_cols = 200
        nx_c = int(np.ceil((num_cols - xo) / xp))
        ny_c = int(np.ceil((num_rows - yo) / yp))

        legacy_x, legacy_y = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
        lattice_x, lattice_y = Lattice.rectangular(xp, yp, xo, yo).points_in_frame(
            num_rows, num_cols
        )

        np.testing.assert_allclose(lattice_x, legacy_x, atol=1e-9)
        np.testing.assert_allclose(lattice_y, legacy_y, atol=1e-9)

    def test_hexagonal_constructor_geometry(self):
        spacing = 12.0
        lattice = Lattice.hexagonal(spacing)
        assert lattice.nearest_spacing() == pytest.approx(spacing)
        assert lattice.basis_angle_deg() == pytest.approx(60.0, abs=1e-6)
        assert lattice.cell_area == pytest.approx(spacing**2 * np.sqrt(3) / 2)
        assert not lattice.is_axis_aligned_rectangular()

    def test_reduced_recovers_shortest_basis_from_skewed_input(self):
        """A unimodular transform describes the same lattice. The reduced
        pair is not unique for hexagonal symmetry, so compare invariants."""
        base = Lattice.hexagonal(12.0, offset=(3.0, 4.0))
        a1 = np.asarray(base.a1)
        a2 = np.asarray(base.a2)
        skewed = Lattice(a1=tuple(a1 + 3 * a2), a2=tuple(a2), offset=base.offset)
        reduced = skewed.reduced()
        r1, r2 = reduced._vectors()
        assert np.linalg.norm(r1) == pytest.approx(12.0)
        assert np.linalg.norm(r2) == pytest.approx(12.0)
        assert reduced.cell_area == pytest.approx(base.cell_area)
        assert reduced.basis_angle_deg() == pytest.approx(60.0)
        assert reduced.contains_point(3.0, 4.0, tol=1e-6)

    def test_axis_tilt(self):
        assert Lattice.rectangular(11.0, 11.0).axis_tilt_deg() == pytest.approx(0.0)
        assert _rotated_square(11.0, 5.0).axis_tilt_deg() == pytest.approx(
            5.0, abs=0.01
        )

    def test_contains_point(self):
        lattice = Lattice.rectangular(11.0, 9.0, 2.5, 3.5)
        assert lattice.contains_point(2.5 + 3 * 11.0, 3.5 + 2 * 9.0)
        assert not lattice.contains_point(2.5 + 3 * 11.0 + 4.0, 3.5)

    def test_to_grid_params_rejects_a_hexagonal_lattice(self):
        with pytest.raises(ValueError, match="axis-aligned"):
            Lattice.hexagonal(12.0).to_grid_params()


class TestDetectLattice:
    def test_axis_aligned_grid(self):
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        detected = detect_lattice(_render_foci((256, 256), truth))
        xp, _, yp, _ = detected.to_grid_params()
        assert xp == pytest.approx(11.05, abs=0.05)
        assert yp == pytest.approx(11.05, abs=0.05)
        assert detected.contains_point(5.37, 4.81, tol=0.03)

    def test_anisotropic_grid(self):
        truth = Lattice.rectangular(9.0, 14.0, 3.3, 6.1)
        detected = detect_lattice(_render_foci((256, 256), truth))
        xp, _, yp, _ = detected.to_grid_params()
        assert xp == pytest.approx(9.0, abs=0.05)
        assert yp == pytest.approx(14.0, abs=0.08)
        assert detected.contains_point(3.3, 6.1, tol=0.03)

    def test_rotated_square(self):
        truth = _rotated_square(11.0, 5.0, offset=(3.3, 6.1))
        detected = detect_lattice(_render_foci((256, 256), truth))
        a1, a2 = detected._vectors()
        assert np.linalg.norm(a1) == pytest.approx(11.0, abs=0.1)
        assert np.linalg.norm(a2) == pytest.approx(11.0, abs=0.1)
        assert detected.axis_tilt_deg() == pytest.approx(5.0, abs=0.5)
        assert not detected.is_axis_aligned_rectangular()
        assert detected.contains_point(3.3, 6.1, tol=0.05)

    def test_hexagonal(self):
        truth = Lattice.hexagonal(12.0, offset=(5.0, 2.0))
        detected = detect_lattice(_render_foci((256, 256), truth))
        a1, a2 = detected._vectors()
        assert np.linalg.norm(a1) == pytest.approx(12.0, abs=0.15)
        assert np.linalg.norm(a2) == pytest.approx(12.0, abs=0.15)
        assert detected.basis_angle_deg() == pytest.approx(60.0, abs=1.0)
        assert detected.contains_point(5.0, 2.0, tol=0.05)

    def test_noise_tolerance(self):
        rng = np.random.default_rng(7)
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        img = _render_foci((256, 256), truth) + rng.normal(50, 10, (256, 256))
        xp, _, yp, _ = detect_lattice(img).to_grid_params()
        assert xp == pytest.approx(11.05, abs=0.1)
        assert yp == pytest.approx(11.05, abs=0.1)

    def test_blank_image_raises(self):
        with pytest.raises(ValueError, match="periodic"):
            detect_lattice(np.zeros((128, 128)))


class TestFitToPoints:
    """The real-space refinement on top of the spectral detection."""

    def test_indices_of_returns_the_generating_indices(self):
        lattice = Lattice.hexagonal(12.0, offset=(5.0, 2.0), angle_rad=0.3)
        m = np.array([0, 3, -2, 7])
        n = np.array([0, -1, 4, 2])
        points = (
            np.asarray(lattice.offset)[:, None]
            + lattice.matrix @ np.vstack([m, n])
        )
        indices, residual = lattice.indices_of(points[0], points[1])
        np.testing.assert_array_equal(indices[:, 0], m)
        np.testing.assert_array_equal(indices[:, 1], n)
        np.testing.assert_allclose(residual, 0.0, atol=1e-9)

    @pytest.mark.parametrize(
        "truth",
        [
            Lattice.rectangular(11.05, 10.97, 5.37, 4.81),
            Lattice.hexagonal(12.0, offset=(5.0, 2.0), angle_rad=np.deg2rad(17.0)),
            _rotated_square(10.41, 45.0, offset=(4.0, 3.0)),
        ],
        ids=["rectangular", "hexagonal-17deg", "diamond"],
    )
    def test_recovers_the_lattice_from_a_slightly_wrong_start(self, truth):
        """A 0.2 % period error, the spectral detection's accuracy, is 0.8 px
        at the edge of a 464 px frame. The regression removes it."""
        rng = np.random.default_rng(3)
        x, y = truth.points_in_frame(464, 464)
        x = x + rng.normal(0, 0.02, x.shape)
        y = y + rng.normal(0, 0.02, y.shape)
        matrix = truth.matrix * 1.002
        start = Lattice(
            a1=tuple(matrix[:, 0]),
            a2=tuple(matrix[:, 1]),
            offset=(truth.offset[0] + 0.3, truth.offset[1] - 0.2),
        )
        # Index against a start anchored inside the frame, as the detection's is.
        fitted, residual = start.fit_to_points(x, y)

        # Same point set: every true point lies on the fitted lattice.
        tx, ty = truth.points_in_frame(464, 464)
        _, distance = fitted.indices_of(tx, ty)
        assert distance.max() < 0.01
        assert fitted.cell_area == pytest.approx(truth.cell_area, rel=2e-4)
        assert np.sqrt(np.mean(residual**2)) == pytest.approx(0.02, abs=0.005)

    def test_residuals_are_the_distortion(self):
        truth = Lattice.rectangular(11.0, 11.0, 5.0, 5.0)
        x, y = truth.points_in_frame(200, 200)
        bump = 0.2 * np.sin(2 * np.pi * x / 200.0)
        _, residual = truth.fit_to_points(x, y + bump, outlier_px=None)
        expected = bump - np.polyval(np.polyfit(x, bump, 1), x)
        np.testing.assert_allclose(residual[:, 1], expected, atol=1e-3)
        np.testing.assert_allclose(residual[:, 0], 0.0, atol=1e-9)

    def test_outliers_do_not_pull_the_fit(self):
        truth = Lattice.rectangular(11.0, 11.0, 5.0, 5.0)
        x, y = truth.points_in_frame(200, 200)
        x = x.copy()
        x[::17] += 2.0
        fitted, _ = truth.fit_to_points(x, y)
        np.testing.assert_allclose(fitted.matrix, truth.matrix, atol=1e-9)
        np.testing.assert_allclose(fitted.offset, truth.offset, atol=1e-9)

    def test_needs_three_non_collinear_points(self):
        lattice = Lattice.rectangular(11.0, 11.0)
        with pytest.raises(ValueError, match="three"):
            lattice.fit_to_points([0.0, 11.0], [0.0, 0.0])
        with pytest.raises(ValueError, match="collinear"):
            lattice.fit_to_points([0.0, 11.0, 22.0], [0.0, 0.0, 0.0])


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
