"""Tests for the MoNaLISA spot model calibration.

Experiments E6 and E7 of ``docs/monalisa_optimal_reconstruction.md``.
"""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import render_spots
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.spot_model import (
    SpotModel,
    calibrate_spot_model,
    fit_foci,
    fit_shared_sigma,
    smooth_field,
)

SHAPE = (160, 160)
CENTRE = 80.0


def _diamond(period=10.41, offset=(4.0, 3.0)):
    half = period / np.sqrt(2)
    return Lattice(a1=(half, half), a2=(-half, half), offset=offset)


def _mean_frame(lattice, sigma=2.0, photons=2000.0, background=200.0, seed=0,
                shift=None, width=None):
    rng = np.random.default_rng(seed)
    x, y = lattice.points_in_frame(*SHAPE, margin=8.0)
    true_x, true_y = x.copy(), y.copy()
    if shift is not None:
        dx, dy = shift(x, y)
        true_x, true_y = x + dx, y + dy
    widths = np.full(x.shape, sigma) if width is None else width(x, y)
    amplitude = photons * (0.7 + 0.6 * rng.random(x.size))
    frame = render_spots(SHAPE, true_x, true_y, widths, amplitude) + background
    return rng.poisson(frame).astype(float), x, y, true_x, true_y, widths


def _inner(x, y, border=14.0):
    return (
        (x > border) & (x < SHAPE[1] - border)
        & (y > border) & (y < SHAPE[0] - border)
    )


class TestSharedSigma:
    def test_recovered_within_one_percent(self):
        frame, x, y, *_ = _mean_frame(Lattice.rectangular(16.0, 16.0, 5.0, 6.0))
        assert fit_shared_sigma(frame, x, y, radius=6.0) == pytest.approx(
            2.0, rel=0.01
        )

    @pytest.mark.parametrize(
        "lattice",
        [
            Lattice.rectangular(11.05, 11.05, 5.0, 6.0),
            Lattice.hexagonal(11.0, offset=(4.0, 3.0), angle_rad=np.deg2rad(17)),
            _diamond(),
        ],
        ids=["rectangular", "hexagonal-17deg", "diamond"],
    )
    def test_calibration_is_not_narrowed_by_the_neighbours(self, lattice):
        """Fitted one focus at a time the spots come out 1-3 % narrow: the
        neighbours' tails pass for background. Taking the neighbours out
        first removes that."""
        frame, x, y, *_ = _mean_frame(lattice)
        model = calibrate_spot_model(frame, x, y)
        assert model.shared_sigma == pytest.approx(2.0, rel=0.01)
        assert np.median(model.sigma) == pytest.approx(2.0, rel=0.01)

    def test_on_the_raw_frame_the_neighbours_narrow_the_fit(self):
        """What the calibration guards against."""
        frame, x, y, *_ = _mean_frame(_diamond())
        assert fit_shared_sigma(frame, x, y, radius=6.0) < 1.96


class TestFociFit:
    def test_centres_to_a_few_hundredths_of_a_pixel(self):
        lattice = Lattice.rectangular(16.0, 16.0, 5.0, 6.0)
        rng = np.random.default_rng(1)
        frame, x, y, *_ = _mean_frame(lattice)
        start_x = x + rng.uniform(-0.4, 0.4, x.shape)
        start_y = y + rng.uniform(-0.4, 0.4, y.shape)
        fit = fit_foci(frame, start_x, start_y, 2.0, radius=6.0)
        inner = _inner(x, y) & fit.ok
        assert inner.sum() > 40
        error = np.hypot(fit.x - x, fit.y - y)[inner]
        assert np.sqrt(np.mean(error**2)) < 0.03
        assert np.median(fit.sigma[inner]) == pytest.approx(2.0, rel=0.01)

    def test_foci_at_the_frame_edge_are_not_fitted(self):
        frame, x, y, *_ = _mean_frame(Lattice.rectangular(16.0, 16.0, 5.0, 6.0))
        fit = fit_foci(frame, x, y, 2.0, radius=6.0)
        outside = (x < 6) | (x > SHAPE[1] - 7) | (y < 6) | (y > SHAPE[0] - 7)
        assert outside.any()
        assert not fit.ok[outside].any()
        np.testing.assert_array_equal(fit.x[outside], x[outside])

    def test_an_empty_position_is_not_a_focus(self):
        frame = np.random.default_rng(2).poisson(200.0, SHAPE).astype(float)
        frame += render_spots(SHAPE, [40.0], [40.0], 2.0, [2000.0])
        fit = fit_foci(frame, np.array([40.3, 100.0]), np.array([39.8, 100.0]), 2.0,
                       radius=6.0)
        assert fit.ok[0]
        assert fit.x[0] == pytest.approx(40.0, abs=0.05)
        # Whatever the fit of pure noise returns, its amplitude is negligible.
        assert not fit.ok[1] or fit.amplitude[1] < 0.05 * fit.amplitude[0]


class TestField:
    def test_a_smooth_distortion_is_recovered(self):
        """E7: 0.3 px of distortion misplaces a cell by 23 nm. The field
        brings the centres back to a few hundredths of a pixel, including
        the foci at the edge that could not be measured themselves."""
        def shift(x, y):
            u, v = (x - CENTRE) / CENTRE, (y - CENTRE) / CENTRE
            return 0.3 * u * u - 0.1 * v, 0.25 * u * v + 0.1 * v**3

        lattice = Lattice.rectangular(11.05, 11.05, 5.0, 6.0)
        frame, x, y, true_x, true_y, _ = _mean_frame(lattice, shift=shift)
        model = calibrate_spot_model(frame, x, y, mode="field")
        inside = (x >= 0) & (x < SHAPE[1]) & (y >= 0) & (y < SHAPE[0])
        error = np.hypot(model.x - true_x, model.y - true_y)[inside]
        ideal = np.hypot(x - true_x, y - true_y)[inside]
        assert ideal.max() > 0.3
        assert np.sqrt(np.mean(error**2)) < 0.02
        assert error.max() < 0.08
        assert model.diagnostics["centre_field_rms_px"] > 0.1
        assert model.diagnostics["centre_residual_rms_px"] < 0.05

    def test_a_width_gradient_is_recovered(self):
        def width(x, y):
            return 1.8 + 0.5 * x / SHAPE[1]

        lattice = Lattice.rectangular(11.05, 11.05, 5.0, 6.0)
        frame, x, y, _, _, widths = _mean_frame(lattice, width=width)
        model = calibrate_spot_model(frame, x, y, mode="field")
        inner = _inner(x, y)
        np.testing.assert_allclose(model.sigma[inner], widths[inner], rtol=0.02)

    def test_shared_mode_keeps_the_centres(self):
        lattice = Lattice.rectangular(11.05, 11.05, 5.0, 6.0)
        frame, x, y, *_ = _mean_frame(lattice)
        model = calibrate_spot_model(frame, x, y, mode="shared")
        np.testing.assert_array_equal(model.x, x)
        assert np.all(model.sigma == model.shared_sigma)

    def test_dim_foci_do_not_enter_the_field(self):
        """Foci over an empty part of the specimen carry no position."""
        lattice = Lattice.rectangular(11.05, 11.05, 5.0, 6.0)
        rng = np.random.default_rng(3)
        x, y = lattice.points_in_frame(*SHAPE, margin=8.0)
        amplitude = np.where(x < 60, 20.0, 2000.0)
        frame = rng.poisson(
            render_spots(SHAPE, x, y, 2.0, amplitude) + 200.0
        ).astype(float)
        model = calibrate_spot_model(frame, x, y)
        assert not model.measured[(x < 55) & _inner(x, y)].any()
        assert model.measured[(x > 65) & _inner(x, y)].all()
        assert model.shared_sigma == pytest.approx(2.0, rel=0.01)

    def test_smooth_field_ignores_outliers(self):
        rng = np.random.default_rng(4)
        x = rng.uniform(0, 100, 400)
        y = rng.uniform(0, 100, 400)
        truth = 0.01 * x - 0.002 * y + 1e-4 * x * y
        values = truth + rng.normal(0, 0.01, x.shape)
        values[::25] += 3.0
        np.testing.assert_allclose(smooth_field(x, y, values), truth, atol=0.01)

    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="mode"):
            calibrate_spot_model(np.zeros(SHAPE), [10.0], [10.0], mode="spline")


class TestModulation:
    """Calibration on the temporal variance of the frames."""

    @staticmethod
    def _frames(static=None, seed=0):
        from imswitch.improcess._test._monalisa_synthetic import make_scan

        scan = make_scan(
            shape=(128, 128), steps=(12, 12), noise="poisson", static=static,
            seed=seed,
        )
        lattice = Lattice.rectangular(11.0, 11.0, 4.2, 5.7)
        x, y = lattice.points_in_frame(128, 128, margin=6.0)
        return scan.frames, x, y

    def test_the_variance_frame_gives_the_spot_width(self):
        frames, x, y = self._frames()
        model = calibrate_spot_model(frames.var(axis=0), x, y, modulation=True)
        assert model.shared_sigma == pytest.approx(2.0, rel=0.03)
        assert model.diagnostics["image"] == "variance"

    def test_a_structured_background_spoils_the_mean_frame_only(self):
        from scipy.ndimage import gaussian_filter

        rng = np.random.default_rng(7)
        static = gaussian_filter(rng.random((128, 128)), 3.0)
        static = 1500.0 * (static - static.min()) / np.ptp(static)
        frames, x, y = self._frames(static=static)
        on_variance = calibrate_spot_model(
            frames.var(axis=0), x, y, mode="measured", modulation=True
        )
        on_mean = calibrate_spot_model(frames.mean(axis=0), x, y, mode="measured")
        inner = _inner(x, y, border=12.0) & (x < 116) & (y < 116)

        def scatter(model):
            use = inner & model.measured
            return np.sqrt(np.mean((model.x - x)[use] ** 2 + (model.y - y)[use] ** 2))

        # The photon noise of the background is in the variance as well,
        # which widens the spots a little.
        assert on_variance.shared_sigma == pytest.approx(2.0, rel=0.08)
        assert scatter(on_variance) < 0.25
        assert scatter(on_mean) > 2.0 * scatter(on_variance)

    def test_without_a_distortion_the_foci_stay_on_the_lattice(self):
        frames, x, y = self._frames()
        model = calibrate_spot_model(frames.var(axis=0), x, y, modulation=True)
        assert not model.diagnostics["centre_field_used"]
        assert not model.diagnostics["sigma_field_used"]
        np.testing.assert_array_equal(model.x, x)
        np.testing.assert_array_equal(model.sigma, model.shared_sigma)


class TestSpotModel:
    def test_uniform(self):
        model = SpotModel.uniform([1.0, 2.0], [3.0, 4.0], 1.3)
        assert model.num_foci == 2
        np.testing.assert_array_equal(model.sigma, [1.3, 1.3])
        assert model.fwhm_nm(77.0) == pytest.approx(235.7, abs=0.1)

    def test_uniform_checks_its_arguments(self):
        with pytest.raises(ValueError, match="sigma"):
            SpotModel.uniform([1.0], [1.0], 0.0)
        with pytest.raises(ValueError, match="same length"):
            SpotModel.uniform([1.0, 2.0], [1.0], 1.0)


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
