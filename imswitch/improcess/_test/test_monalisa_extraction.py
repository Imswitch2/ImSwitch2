"""Tests for the joint least-squares extraction of MoNaLISA focus amplitudes.

The numbers follow experiment E1 of
``docs/monalisa_optimal_reconstruction.md``: period 11.05 px, spot sigma
2 px, amplitudes 100-300, constant background 50, noise sd 5.
"""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import render_spots
from imswitch.improcess.reconstructors.monalisa.extraction import (
    HAZE_SUPPORT_SIGMA,
    ExtractionOperator,
    _haze_shape,
    calibrate_haze_sigma,
)
from imswitch.improcess.reconstructors.monalisa.gauss_processor import (
    calculate_gaussian_lsq_weights,
)
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice

SHAPE = (100, 100)
PERIOD = 11.05
SIGMA = 2.0
NOISE = 5.0


def _interior(x, y, shape=SHAPE, border=PERIOD + 2):
    return (
        (x > border) & (x < shape[1] - border)
        & (y > border) & (y < shape[0] - border)
    )


def _render_haze(shape, x, y, sigma, amplitude):
    rows, cols = shape
    yy, xx = np.mgrid[0:rows, 0:cols].astype(float)
    frame = np.zeros(shape)
    for cx, cy, amp in zip(x, y, amplitude):
        r2 = (xx - cx) ** 2 + (yy - cy) ** 2
        inside = r2 <= (HAZE_SUPPORT_SIGMA * sigma) ** 2
        frame += amp * np.where(inside, _haze_shape(r2, sigma), 0.0)
    return frame


@pytest.fixture(scope="module")
def scene():
    rng = np.random.default_rng(20260929)
    lattice = Lattice.rectangular(PERIOD, PERIOD, 3.3, 5.1)
    x, y = lattice.points_in_frame(*SHAPE, margin=3 * SIGMA)
    amplitude = 200.0 * (0.5 + rng.random(x.size))
    haze = 60.0 * (0.5 + rng.random(x.size))
    frame = render_spots(SHAPE, x, y, SIGMA, amplitude) + 50.0
    return {
        "x": x,
        "y": y,
        "amplitude": amplitude,
        "haze": haze,
        "frame": frame,
        "interior": _interior(x, y),
        "rng": rng,
    }


def _build(scene, **kwargs):
    return ExtractionOperator.build(scene["x"], scene["y"], SIGMA, SHAPE, **kwargs)


def _bias(scene, operator, frame=None):
    frame = scene["frame"] if frame is None else frame
    result = operator.apply(frame)
    return (result.amplitude[0] - scene["amplitude"])[scene["interior"]]


class TestCrosstalk:
    @pytest.mark.parametrize("reach", [1.5, 2.0, 2.5, 3.0, 4.0, 5.5])
    def test_the_joint_fit_has_none_at_any_reach(self, scene, reach):
        bias = _bias(scene, _build(scene, reach_sigma=reach))
        assert np.max(np.abs(bias)) < 1e-3

    def test_the_isolated_fit_grows_with_the_reach(self, scene):
        """The E1 curve: 0.06 counts at 1.5 sigma, 0.9 at 2.5, 2.8 at 3, 17 at 4."""
        worst = [
            np.max(np.abs(_bias(scene, _build(scene, reach_sigma=reach, joint=False))))
            for reach in (1.5, 2.5, 3.0, 4.0)
        ]
        assert worst[0] < 0.1
        assert 0.5 < worst[1] < 1.5
        assert 2.0 < worst[2] < 4.0
        assert 12.0 < worst[3] < 25.0

    def test_holds_for_a_rotated_hexagonal_lattice(self):
        rng = np.random.default_rng(5)
        lattice = Lattice.hexagonal(11.0, offset=(4.0, 3.0), angle_rad=np.deg2rad(17))
        x, y = lattice.points_in_frame(*SHAPE, margin=3 * SIGMA)
        amplitude = 200.0 * (0.5 + rng.random(x.size))
        frame = render_spots(SHAPE, x, y, SIGMA, amplitude) + 50.0
        inner = _interior(x, y)

        def worst(joint):
            operator = ExtractionOperator.build(
                x, y, SIGMA, SHAPE, reach_sigma=3.0, joint=joint
            )
            return np.max(np.abs((operator.apply(frame).amplitude[0] - amplitude)[inner]))

        assert worst(joint=True) < 1e-3
        assert worst(joint=False) > 1.0

    def test_holds_for_a_width_that_varies_over_the_frame(self):
        rng = np.random.default_rng(6)
        lattice = Lattice.rectangular(PERIOD, PERIOD, 3.3, 5.1)
        x, y = lattice.points_in_frame(*SHAPE, margin=3 * SIGMA)
        sigma = 1.8 + 0.4 * x / SHAPE[1]
        amplitude = 200.0 * (0.5 + rng.random(x.size))
        frame = render_spots(SHAPE, x, y, sigma, amplitude) + 50.0
        operator = ExtractionOperator.build(x, y, sigma, SHAPE, reach_sigma=3.0)
        bias = (operator.apply(frame).amplitude[0] - amplitude)[_interior(x, y)]
        assert np.max(np.abs(bias)) < 1e-3


class TestNoise:
    @pytest.mark.parametrize(
        "reach, expected", [(1.5, 4.77), (2.5, 2.18), (3.0, 1.91), (4.0, 1.82)]
    )
    def test_predicted_noise_matches_the_design_document(self, scene, reach, expected):
        operator = _build(scene, reach_sigma=reach)
        predicted = NOISE * np.sqrt(operator.variance_gain[scene["interior"]]).mean()
        assert predicted == pytest.approx(expected, rel=0.01)

    def test_predicted_noise_is_the_measured_noise(self, scene):
        operator = _build(scene, reach_sigma=3.0)
        rng = np.random.default_rng(1)
        frames = scene["frame"][None] + rng.normal(0.0, NOISE, (600, *SHAPE))
        result = operator.apply(frames)
        inner = scene["interior"]
        measured = result.amplitude[:, inner].std(axis=0, ddof=1)
        predicted = NOISE * np.sqrt(operator.variance_gain[inner])
        np.testing.assert_allclose(measured, predicted, rtol=0.15)
        assert measured.mean() == pytest.approx(predicted.mean(), rel=0.02)
        # The residual of the fit is the pixel noise.
        assert result.noise_sigma.mean() == pytest.approx(NOISE, rel=0.02)

    def test_the_isolated_residual_shows_the_neighbours(self, scene):
        """A noiseless frame leaves no residual in the joint fit; the isolated
        fit cannot explain the neighbouring spots."""
        joint = _build(scene, reach_sigma=3.0).apply(scene["frame"])
        isolated = _build(scene, reach_sigma=3.0, joint=False).apply(scene["frame"])
        assert joint.noise_sigma[0] < 0.01
        assert isolated.noise_sigma[0] > 1.0

    def test_weighting_by_the_variance_lowers_the_noise(self):
        """E2: peak 200 over a background of 2 gains 15-25 % in variance."""
        rng = np.random.default_rng(2)
        lattice = Lattice.rectangular(16.0, 16.0, 8.0, 8.0)
        x, y = lattice.points_in_frame(64, 64)
        expected = render_spots((64, 64), x, y, SIGMA, np.full(x.size, 200.0)) + 2.0
        variance = expected + 1.6**2
        frames = rng.poisson(expected, (1500, 64, 64)) + rng.normal(
            0, 1.6, (1500, 64, 64)
        )
        plain = ExtractionOperator.build(x, y, SIGMA, (64, 64), reach_sigma=2.0)
        weighted = ExtractionOperator.build(
            x, y, SIGMA, (64, 64), reach_sigma=2.0, pixel_variance=variance
        )
        var_plain = plain.apply(frames).amplitude.var(axis=0, ddof=1).mean()
        result = weighted.apply(frames)
        var_weighted = result.amplitude.var(axis=0, ddof=1).mean()
        assert result.amplitude.mean() == pytest.approx(200.0, rel=0.01)
        assert 1.08 < var_plain / var_weighted < 1.35
        # With the variance given, the gain is the amplitude variance itself.
        assert weighted.variance_gain.mean() == pytest.approx(var_weighted, rel=0.1)


class TestIsolatedParity:
    @pytest.mark.parametrize("reach", [1.5, 2.5])
    @pytest.mark.parametrize("background", ["constant", "none"])
    def test_weights_equal_the_fast_gauss_weights(self, scene, reach, background):
        """Same footprint, same model: the isolated mode is the fast-Gauss
        estimator on exact pixels."""
        operator = _build(scene, reach_sigma=reach, joint=False, background=background)
        for focus in np.flatnonzero(scene["interior"])[::9]:
            pixel, weight = operator.weights(focus)
            dx = pixel % SHAPE[1] - scene["x"][focus]
            dy = pixel // SHAPE[1] - scene["y"][focus]
            assert np.all(np.hypot(dx, dy) <= reach * SIGMA + 1e-9)
            reference = calculate_gaussian_lsq_weights(
                dx, dy, SIGMA, fit_background=background == "constant"
            )
            np.testing.assert_allclose(weight, reference, atol=1e-8)

    def test_weights_reproduce_apply(self, scene):
        operator = _build(scene, reach_sigma=3.0)
        focus = int(np.flatnonzero(scene["interior"])[5])
        pixel, weight = operator.weights(focus)
        direct = float(weight @ scene["frame"].ravel()[pixel])
        assert direct == pytest.approx(
            operator.apply(scene["frame"]).amplitude[0, focus], rel=1e-9
        )

    def test_joint_weights_reach_under_the_neighbours(self, scene):
        """Why the joint fit is applied through the normal equations: its
        weights cover several times the focus' own footprint."""
        operator = _build(scene, reach_sigma=3.0)
        focus = int(np.flatnonzero(scene["interior"])[5])
        pixel, weight = operator.weights(focus)
        distance = np.hypot(
            pixel % SHAPE[1] - scene["x"][focus], pixel // SHAPE[1] - scene["y"][focus]
        )
        outside = distance > 3.0 * SIGMA
        assert outside.sum() > 3 * (~outside).sum()
        truncated = float(weight[~outside] @ scene["frame"].ravel()[pixel[~outside]])
        assert abs(truncated - scene["amplitude"][focus]) > 0.1


class TestHaze:
    def test_a_haze_of_the_modelled_shape_is_removed(self, scene):
        frame = scene["frame"] + _render_haze(
            SHAPE, scene["x"], scene["y"], 6.0, scene["haze"]
        )
        operator = _build(
            scene, reach_sigma=3.0, background="constant+haze", haze_sigma=6.0
        )
        result = operator.apply(frame)
        inner = scene["interior"]
        assert np.max(np.abs((result.amplitude[0] - scene["amplitude"])[inner])) < 0.01
        assert np.max(np.abs((result.haze[0] - scene["haze"])[inner])) < 0.05

    def test_without_the_term_the_haze_biases_the_amplitudes(self, scene):
        frame = scene["frame"] + _render_haze(
            SHAPE, scene["x"], scene["y"], 6.0, scene["haze"]
        )
        bias = _bias(scene, _build(scene, reach_sigma=3.0), frame)
        assert np.sqrt(np.mean(bias**2)) > 3.0

    def test_a_haze_of_the_wrong_width_is_worse_than_a_small_footprint(self, scene):
        """The reason the haze width is fitted and the term is off by default
        (section 8 of the design document)."""
        frame = scene["frame"] + _render_haze(
            SHAPE, scene["x"], scene["y"], 6.0, scene["haze"]
        )
        wrong = _build(
            scene, reach_sigma=3.0, background="constant+haze", haze_sigma=4.5
        )
        small = _build(scene, reach_sigma=1.5)
        rms = lambda bias: float(np.sqrt(np.mean(bias**2)))  # noqa: E731
        assert rms(_bias(scene, wrong, frame)) > rms(_bias(scene, small, frame))

    def test_the_width_is_recovered_from_the_frame(self, scene):
        frame = scene["frame"] + _render_haze(
            SHAPE, scene["x"], scene["y"], 6.0, scene["haze"]
        )
        rng = np.random.default_rng(4)
        frame = frame + rng.normal(0, 0.5, SHAPE)
        width, ratio = calibrate_haze_sigma(
            frame, scene["x"], scene["y"], SIGMA, reach_sigma=3.0
        )
        assert width == pytest.approx(6.0, abs=0.3)
        assert ratio < 0.5

    def test_no_haze_in_the_frame_means_no_gain(self, scene):
        rng = np.random.default_rng(4)
        frame = scene["frame"] + rng.normal(0, 0.5, SHAPE)
        _, ratio = calibrate_haze_sigma(
            frame, scene["x"], scene["y"], SIGMA, reach_sigma=3.0
        )
        assert ratio > 0.9


class TestEdgesAndMasks:
    def test_foci_outside_the_frame_are_fitted_but_not_returned(self, scene):
        operator = _build(scene, reach_sigma=3.0)
        x, y = scene["x"], scene["y"]
        outside = (x < 0) | (x > SHAPE[1] - 1) | (y < 0) | (y > SHAPE[0] - 1)
        assert outside.any()
        assert not operator.valid[outside].any()
        result = operator.apply(scene["frame"])
        assert np.all(np.isnan(result.amplitude[0, ~operator.valid]))
        # The foci at the edge are still right, because their outside
        # neighbours are part of the model.
        edge = operator.valid & ~scene["interior"]
        bias = (result.amplitude[0] - scene["amplitude"])[edge]
        assert np.max(np.abs(bias)) < 0.05

    def test_masked_pixels_do_not_enter(self, scene):
        focus = int(np.flatnonzero(scene["interior"])[3])
        hot = (int(round(scene["y"][focus])), int(round(scene["x"][focus])) + 1)
        frame = scene["frame"].copy()
        frame[hot] += 5000.0
        mask = np.ones(SHAPE, dtype=bool)
        mask[hot] = False
        spoiled = _build(scene, reach_sigma=3.0).apply(frame).amplitude[0, focus]
        masked = _build(scene, reach_sigma=3.0, pixel_mask=mask)
        assert abs(spoiled - scene["amplitude"][focus]) > 100.0
        assert masked.apply(frame).amplitude[0, focus] == pytest.approx(
            scene["amplitude"][focus], abs=1e-3
        )

    def test_residual_is_nan_outside_the_fit(self, scene):
        operator = _build(scene, reach_sigma=1.5)
        residual = operator.residual(scene["frame"])
        assert residual.shape == SHAPE
        assert np.isfinite(residual).sum() == operator.num_fit_pixels
        assert np.nanmax(np.abs(residual)) < 0.05

    def test_a_stack_and_its_frames_give_the_same_result(self, scene):
        operator = _build(scene, reach_sigma=2.5)
        rng = np.random.default_rng(8)
        frames = scene["frame"][None] + rng.normal(0, NOISE, (5, *SHAPE))
        together = operator.apply(frames, chunk=2).amplitude
        for index in range(frames.shape[0]):
            single = operator.apply(frames[index]).amplitude[0]
            np.testing.assert_allclose(together[index], single, equal_nan=True)


class TestArguments:
    def test_unknown_background(self, scene):
        with pytest.raises(ValueError, match="background"):
            _build(scene, background="gradient")

    def test_haze_needs_a_width(self, scene):
        with pytest.raises(ValueError, match="haze_sigma"):
            _build(scene, background="constant+haze")

    def test_frame_shape_is_checked(self, scene):
        with pytest.raises(ValueError, match="shape"):
            _build(scene).apply(np.zeros((3, 50, 50)))

    def test_no_focus_in_the_frame(self):
        with pytest.raises(ValueError, match="No focus"):
            ExtractionOperator.build([500.0], [500.0], SIGMA, SHAPE)


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
