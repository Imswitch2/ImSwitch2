"""Tests for the generalized MoNaLISA illumination-lattice groundwork."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.lattice import (
    Lattice,
    detect_lattice,
)
from imswitch.improcess.reconstructors.monalisa.localizer import (
    localizer,
    localization_result_centers,
    robust_localize,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    get_center_coords,
    get_interp_coords,
    get_interp_coords_for_centers,
    get_rectangles_coords,
)


def _render_foci(shape, lattice: Lattice, sigma: float = 1.8, amp: float = 100.0):
    """Render Gaussian foci at every lattice point in (and just outside) the frame."""
    rows, cols = shape
    ys, xs = np.mgrid[0:rows, 0:cols].astype(float)
    img = np.zeros(shape)
    margin = 3 * sigma
    Xc, Yc = lattice.points_in_frame(rows, cols, margin=margin)
    for cx, cy in zip(Xc, Yc):
        img += amp * np.exp(-(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * sigma**2))
    return img


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

    def test_interp_coords_for_centers_match_legacy_path(self):
        xp = yp = 10.0
        xo, yo = 5.0, 5.0
        nx_c = ny_c = 10
        footprint = get_rectangles_coords(3)

        legacy = get_interp_coords(xp, xo, yp, yo, nx_c, ny_c, 100, 100, 3)
        Xc, Yc = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
        seam = get_interp_coords_for_centers(Xc, Yc, footprint, 100, 100)

        np.testing.assert_allclose(seam[0], legacy[0])
        np.testing.assert_allclose(seam[1], legacy[1])

    def test_hexagonal_constructor_geometry(self):
        spacing = 12.0
        lattice = Lattice.hexagonal(spacing)
        assert lattice.nearest_spacing() == pytest.approx(spacing)
        assert lattice.basis_angle_deg() == pytest.approx(60.0, abs=1e-6)
        assert lattice.cell_area == pytest.approx(spacing**2 * np.sqrt(3) / 2)
        assert not lattice.is_axis_aligned_rectangular()

    def test_reduced_recovers_shortest_basis_from_skewed_input(self):
        """A unimodular transform describes the same lattice; reduction must
        recover an equally short basis with identical invariants. The reduced
        *pair* is deliberately not unique for hexagonal symmetry (three
        equally short directions), so compare invariants, not matrices."""
        base = Lattice.hexagonal(12.0, offset=(3.0, 4.0))
        a1 = np.asarray(base.a1)
        a2 = np.asarray(base.a2)
        skewed = Lattice(
            a1=tuple(a1 + 3 * a2),  # unimodular transform: same lattice
            a2=tuple(a2),
            offset=base.offset,
        )
        reduced = skewed.reduced()
        r1, r2 = reduced._vectors()
        assert np.linalg.norm(r1) == pytest.approx(12.0)
        assert np.linalg.norm(r2) == pytest.approx(12.0)
        assert reduced.cell_area == pytest.approx(base.cell_area)
        assert reduced.basis_angle_deg() == pytest.approx(60.0)
        # Same point set: the base offset (a lattice point) lies on it.
        assert reduced.contains_point(3.0, 4.0, tol=1e-6)

    def test_axis_tilt(self):
        assert Lattice.rectangular(11.0, 11.0).axis_tilt_deg() == pytest.approx(0.0)
        theta = np.deg2rad(5.0)
        rotated = Lattice(
            a1=(11 * np.cos(theta), 11 * np.sin(theta)),
            a2=(-11 * np.sin(theta), 11 * np.cos(theta)),
        )
        assert rotated.axis_tilt_deg() == pytest.approx(5.0, abs=0.01)

    def test_contains_point(self):
        lattice = Lattice.rectangular(11.0, 9.0, 2.5, 3.5)
        assert lattice.contains_point(2.5 + 3 * 11.0, 3.5 + 2 * 9.0)
        assert not lattice.contains_point(2.5 + 3 * 11.0 + 4.0, 3.5)


class TestDetectLattice:
    def test_axis_aligned_grid(self):
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        img = _render_foci((256, 256), truth)
        detected = detect_lattice(img)

        xp, xo, yp, yo = detected.to_grid_params()
        assert xp == pytest.approx(11.05, abs=0.05)
        assert yp == pytest.approx(11.05, abs=0.05)
        # Offsets agree modulo the lattice.
        assert detected.contains_point(5.37, 4.81, tol=0.03)

    def test_anisotropic_grid(self):
        truth = Lattice.rectangular(9.0, 14.0, 3.3, 6.1)
        img = _render_foci((256, 256), truth)
        detected = detect_lattice(img)
        xp, xo, yp, yo = detected.to_grid_params()
        assert xp == pytest.approx(9.0, abs=0.05)
        assert yp == pytest.approx(14.0, abs=0.08)
        assert detected.contains_point(3.3, 6.1, tol=0.03)

    def test_rotated_square(self):
        theta = np.deg2rad(5.0)
        truth = Lattice(
            a1=(11.0 * np.cos(theta), 11.0 * np.sin(theta)),
            a2=(-11.0 * np.sin(theta), 11.0 * np.cos(theta)),
            offset=(3.3, 6.1),
        )
        img = _render_foci((256, 256), truth)
        detected = detect_lattice(img)

        a1, a2 = detected._vectors()
        assert np.linalg.norm(a1) == pytest.approx(11.0, abs=0.1)
        assert np.linalg.norm(a2) == pytest.approx(11.0, abs=0.1)
        assert detected.axis_tilt_deg() == pytest.approx(5.0, abs=0.5)
        assert not detected.is_axis_aligned_rectangular()
        assert detected.contains_point(3.3, 6.1, tol=0.05)

    def test_hexagonal(self):
        truth = Lattice.hexagonal(12.0, offset=(5.0, 2.0))
        img = _render_foci((256, 256), truth)
        detected = detect_lattice(img)

        a1, a2 = detected._vectors()
        assert np.linalg.norm(a1) == pytest.approx(12.0, abs=0.15)
        assert np.linalg.norm(a2) == pytest.approx(12.0, abs=0.15)
        assert detected.basis_angle_deg() == pytest.approx(60.0, abs=1.0)
        assert detected.contains_point(5.0, 2.0, tol=0.05)

    def test_noise_tolerance(self):
        rng = np.random.default_rng(7)
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        img = _render_foci((256, 256), truth) + rng.normal(50, 10, (256, 256))
        detected = detect_lattice(img)
        xp, _, yp, _ = detected.to_grid_params()
        assert xp == pytest.approx(11.05, abs=0.1)
        assert yp == pytest.approx(11.05, abs=0.1)

    def test_blank_image_raises(self):
        with pytest.raises(ValueError, match="periodic"):
            detect_lattice(np.zeros((128, 128)))


class TestRobustLocalize:
    def test_coarse_period_without_any_guess(self):
        """The headline robustness win: a 14 px pattern localizes correctly
        with no guess at all (the 1D-only path silently returned ~12.4)."""
        truth = Lattice.rectangular(14.0, 14.0, 4.2, 3.7)
        img = _render_foci((256, 256), truth)
        loc = robust_localize(img)
        assert loc.xp == pytest.approx(14.0, abs=0.1)
        assert loc.yp == pytest.approx(14.0, abs=0.1)
        # Offset accuracy is bounded by the 1D refinement's edge effects
        # (partial periods at the frame border bias the template correlation
        # by a fraction of a pixel).
        assert loc.xo == pytest.approx(4.2, abs=0.5)
        assert loc.yo == pytest.approx(3.7, abs=0.5)

    def test_matches_plain_localizer_on_standard_grid(self):
        truth = Lattice.rectangular(10.0, 10.0, 5.0, 5.0)
        img = _render_foci((200, 200), truth)
        robust = robust_localize(img)
        plain = localizer(img, xp_guess=10.0, yp_guess=10.0)
        assert robust.xp == pytest.approx(plain.xp, abs=0.02)
        assert robust.yp == pytest.approx(plain.yp, abs=0.02)
        assert robust.xo == pytest.approx(plain.xo, abs=0.05)
        assert robust.yo == pytest.approx(plain.yo, abs=0.05)

    def test_rejects_clearly_rotated_pattern(self):
        theta = np.deg2rad(8.0)
        truth = Lattice(
            a1=(11.0 * np.cos(theta), 11.0 * np.sin(theta)),
            a2=(-11.0 * np.sin(theta), 11.0 * np.cos(theta)),
            offset=(3.3, 6.1),
        )
        img = _render_foci((256, 256), truth)
        with pytest.raises(ValueError, match="axis-aligned"):
            robust_localize(img)

    def test_rejects_hexagonal_pattern(self):
        truth = Lattice.hexagonal(12.0, offset=(5.0, 2.0))
        img = _render_foci((256, 256), truth)
        with pytest.raises(ValueError, match="axis-aligned"):
            robust_localize(img)

    def test_result_carries_detected_lattice(self):
        truth = Lattice.rectangular(11.05, 11.05, 5.37, 4.81)
        img = _render_foci((256, 256), truth)
        loc = robust_localize(img)
        assert loc.lattice is not None
        assert loc.lattice.contains_point(5.37, 4.81, tol=0.05)

    def test_centers_accessor_matches_scan_geometry(self):
        truth = Lattice.rectangular(10.0, 10.0, 5.0, 5.0)
        img = _render_foci((200, 200), truth)
        loc = localizer(img, xp_guess=10.0, yp_guess=10.0)

        legacy_x, legacy_y = get_center_coords(
            loc.xp, loc.xo, loc.yp, loc.yo, loc.nx_c, loc.ny_c
        )
        seam_x, seam_y = localization_result_centers(loc)
        np.testing.assert_allclose(seam_x, legacy_x, atol=1e-9)
        np.testing.assert_allclose(seam_y, legacy_y, atol=1e-9)
