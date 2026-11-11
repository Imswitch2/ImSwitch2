"""Tests for the pixel reassignment of weakly confined foci.

The scans are rendered from the specimen itself
(``_monalisa_synthetic.make_physical_scan``), so the spots move with the
emitters inside the focus, as they do on a microscope.
"""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import make_physical_scan
from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
from imswitch.improcess.reconstructors.monalisa.placement import (
    OutputRaster,
    place_nearest,
    sample_positions,
)
from imswitch.improcess.reconstructors.monalisa.reassignment import (
    _shift_between,
    measure_shift_factor,
    pinhole_offsets,
    pinhole_stack,
    reassign,
)


class Scene:
    """A physical scan with everything a reconstruction of it needs."""

    def __init__(self, **arguments):
        self.scan = make_physical_scan(**arguments)
        scan = self.scan
        self.size = scan.frames.shape[1]
        # The frame is periodic: the foci beyond its edge are the images of
        # those inside.
        xs, ys = [], []
        for shift_x in (-self.size, 0, self.size):
            for shift_y in (-self.size, 0, self.size):
                xs.append(scan.focus_x + shift_x)
                ys.append(scan.focus_y + shift_y)
        x, y = np.concatenate(xs), np.concatenate(ys)
        keep = (x > -7) & (x < self.size + 6) & (y > -7) & (y < self.size + 6)
        self.x, self.y = x[keep], y[keep]
        self.offsets = scan.scan_index @ scan.step.T
        self.raster = OutputRaster.covering(
            (self.size, self.size), scan.step, (scan.focus_x[0], scan.focus_y[0])
        )

    def operator(self, reach):
        return ExtractionOperator.build(
            self.x, self.y, self.scan.spot_sigma, (self.size, self.size),
            reach_sigma=reach,
        )

    def amplitude_image(self, reach):
        operator = self.operator(reach)
        qx, qy = sample_positions(self.x, self.y, self.offsets[:, 0], self.offsets[:, 1])
        gx, gy = self.raster.coordinates(qx, qy)
        valid = operator.valid
        amplitude = operator.apply(self.scan.frames).amplitude
        return place_nearest(
            gx[:, valid], gy[:, valid], amplitude[:, valid], self.raster.shape
        ).image

    def reassigned_image(self, reach, alpha):
        return reassign(
            self.scan.frames, self.operator(reach), self.x, self.y,
            self.offsets, self.raster, alpha,
        ).image

    def shift_factor(self):
        return measure_shift_factor(
            self.scan.frames, self.x, self.y, self.offsets, self.raster
        )

    def error(self, image):
        """Smallest relative error against the specimen under any Gaussian
        blur, scale and offset: what the image is off by, apart from its
        resolution."""
        finite = np.isfinite(image)
        best = np.inf
        for blur in np.arange(0.3, 2.0, 0.05):
            truth = self.scan.truth(self.raster, blur_sigma=blur)[finite]
            design = np.column_stack([truth, np.ones(truth.size)])
            coef = np.linalg.lstsq(design, image[finite], rcond=None)[0]
            misfit = np.sqrt(np.mean((design @ coef - image[finite]) ** 2))
            best = min(best, misfit / abs(coef[0] * np.std(truth)))
        return float(best)


def _point_width(image, truth):
    """Mean full width at half maximum of the imaged points, raster px."""
    widths = []
    reach = 9
    for cy, cx in np.argwhere(truth > 0):
        if min(cy, cx) < reach or cy + reach >= image.shape[0] or cx + reach >= image.shape[1]:
            continue
        patch = image[cy - reach:cy + reach + 1, cx - reach:cx + reach + 1]
        if not np.all(np.isfinite(patch)):
            continue
        for profile in (patch[reach], patch[:, reach]):
            profile = profile - profile.min()
            half = profile.max() / 2.0
            above = np.flatnonzero(profile >= half)
            low, high = above[0], above[-1]
            left = low - (profile[low] - half) / (profile[low] - profile[low - 1])
            right = high + (profile[high] - half) / (profile[high] - profile[high + 1])
            widths.append(right - left)
    assert len(widths) > 20
    return float(np.mean(widths))


@pytest.fixture(scope="module")
def wide_points():
    return Scene(sigma_e=1.2, sigma_d=1.5, specimen="points")


@pytest.fixture(scope="module")
def confined_points():
    return Scene(sigma_e=0.35, sigma_d=1.5, specimen="points")


class TestShiftFactor:
    @pytest.mark.parametrize("sigma_e", [1.2, 0.8])
    def test_measured_on_the_frames(self, sigma_e):
        scene = Scene(sigma_e=sigma_e, sigma_d=1.5, specimen="points")
        factor = scene.shift_factor()
        assert factor.alpha == pytest.approx(scene.scan.alpha, abs=0.05)
        assert factor.alpha_x == pytest.approx(factor.alpha_y, abs=0.02)
        assert factor.width_ratio == pytest.approx(sigma_e / 1.5, abs=0.1)

    def test_a_confined_focus_measures_as_confined(self, confined_points):
        factor = confined_points.shift_factor()
        assert confined_points.scan.alpha == pytest.approx(0.05, abs=0.005)
        assert 0.0 <= factor.alpha < 0.08

    def test_measured_under_photon_noise(self):
        scene = Scene(
            sigma_e=1.2, sigma_d=1.5, specimen="filaments", noise="poisson",
            peak=60.0, background=100.0, seed=3,
        )
        factor = scene.shift_factor()
        assert factor.alpha == pytest.approx(scene.scan.alpha, abs=0.08)
        assert factor.spread < 0.1

    def test_an_astigmatic_focus_has_two_factors(self):
        """Measured per axis, because a real focus is not round: the
        recordings measure 0.25 along x and 0.35 along y."""
        scene = Scene(sigma_e=1.2, sigma_d=1.5, specimen="points")
        factor = scene.shift_factor()
        assert factor.per_offset.shape[1] == 4
        assert np.all(factor.per_offset[:, 3] > 0.3)


class TestReassignment:
    def test_without_a_shift_it_is_the_amplitude_image(self, wide_points):
        amplitude = wide_points.amplitude_image(2.5)
        reassigned = wide_points.reassigned_image(2.5, 0.0)
        finite = np.isfinite(amplitude) & np.isfinite(reassigned)
        assert finite.mean() > 0.7
        difference = (amplitude - reassigned)[finite]
        assert np.sqrt(np.mean(difference**2)) < 1e-3 * np.sqrt(
            np.mean(amplitude[finite] ** 2)
        )

    @pytest.mark.parametrize("reach", [2.5, 3.0])
    def test_sharper_and_brighter_than_the_amplitude_image(self, wide_points, reach):
        """The image of a point: as narrow as the theory of image scanning
        says, and more of its light in the peak."""
        scan = wide_points.scan
        truth = scan.truth(wide_points.raster)
        amplitude = wide_points.amplitude_image(reach)
        reassigned = wide_points.reassigned_image(reach, scan.alpha)
        theory = 2.355 * scan.sigma_e * scan.sigma_d / scan.spot_sigma * scan.fine_per_pixel
        assert _point_width(reassigned, truth) == pytest.approx(theory, rel=0.06)
        assert _point_width(reassigned, truth) < _point_width(amplitude, truth)
        assert np.nanmax(reassigned) > 1.08 * np.nanmax(amplitude)

    def test_the_footprint_no_longer_costs_resolution(self, wide_points):
        """Amplitudes blur as the footprint grows; reassigned pixels do not."""
        scan = wide_points.scan
        truth = scan.truth(wide_points.raster)
        amplitude = [
            _point_width(wide_points.amplitude_image(reach), truth)
            for reach in (1.5, 3.0)
        ]
        reassigned = [
            _point_width(wide_points.reassigned_image(reach, scan.alpha), truth)
            for reach in (1.5, 3.0)
        ]
        assert amplitude[1] > 1.12 * amplitude[0]
        assert reassigned[1] < 1.06 * reassigned[0]

    def test_halves_the_error_under_photon_noise(self):
        scene = Scene(
            sigma_e=1.2, sigma_d=1.5, specimen="filaments", noise="poisson",
            peak=60.0, background=100.0, seed=3,
        )
        factor = scene.shift_factor()
        small = scene.error(scene.amplitude_image(1.5))
        wide = scene.error(scene.amplitude_image(2.5))
        reassigned = scene.error(scene.reassigned_image(2.5, factor.alpha))
        assert wide < 0.6 * small
        assert reassigned < 0.6 * wide

    def test_harmless_for_confined_foci(self, confined_points):
        scan = confined_points.scan
        truth = scan.truth(confined_points.raster)
        amplitude = confined_points.amplitude_image(2.5)
        reassigned = confined_points.reassigned_image(2.5, scan.alpha)
        assert _point_width(reassigned, truth) == pytest.approx(
            _point_width(amplitude, truth), rel=0.08
        )

    def test_one_factor_per_axis(self, wide_points):
        alpha = wide_points.scan.alpha
        both = wide_points.reassigned_image(2.5, (alpha, alpha))
        one = wide_points.reassigned_image(2.5, alpha)
        np.testing.assert_allclose(both, one, equal_nan=True)

    def test_frames_and_scan_positions_must_match(self, wide_points):
        with pytest.raises(ValueError, match="scan positions"):
            reassign(
                wide_points.scan.frames[:5], wide_points.operator(2.5),
                wide_points.x, wide_points.y, wide_points.offsets,
                wide_points.raster, 0.4,
            )


class TestPinholeStack:
    """The raw frames, one image per pixel of the footprint."""

    @pytest.fixture(scope="class")
    @classmethod
    def scene(cls):
        return Scene(sigma_e=1.2, sigma_d=1.5, specimen="filaments", seed=3)

    @staticmethod
    def _stack(scene, radius=2.0, alpha=None):
        return pinhole_stack(
            scene.scan.frames, scene.x, scene.y, scene.offsets, scene.raster,
            radius=radius, alpha=alpha,
        )

    @pytest.mark.parametrize("radius, count", [(1.0, 5), (1.5, 9), (3.0, 29), (5.0, 81)])
    def test_one_image_per_pixel_of_the_footprint(self, radius, count):
        dx, dy = pinhole_offsets(radius)
        assert dx.size == count
        assert (dx[0], dy[0]) == (0, 0)
        distance = np.hypot(dx, dy)
        assert np.all(np.diff(distance) >= 0)
        assert distance.max() <= radius
        assert len(set(zip(dx.tolist(), dy.tolist()))) == count

    def test_every_image_is_the_raw_pixels_where_the_focus_was(self, scene):
        stack = self._stack(scene)
        assert stack.images.shape == (13, *scene.raster.shape)
        assert not stack.shifted
        frames = scene.scan.frames
        inside = (
            (scene.x > 4) & (scene.x < scene.size - 5)
            & (scene.y > 4) & (scene.y < scene.size - 5)
        )
        focus = int(np.flatnonzero(inside)[7])
        cx, cy = int(round(scene.x[focus])), int(round(scene.y[focus]))
        for pinhole in (0, 3, 9):
            dx, dy = int(stack.dx[pinhole]), int(stack.dy[pinhole])
            for frame in (0, 57, 300):
                qx = scene.x[focus] - scene.offsets[frame, 0]
                qy = scene.y[focus] - scene.offsets[frame, 1]
                gx, gy = scene.raster.coordinates(qx, qy)
                placed = stack.images[pinhole, int(round(gy)), int(round(gx))]
                assert placed == pytest.approx(frames[frame, cy + dy, cx + dx], rel=1e-6)

    def test_nothing_is_subtracted(self, scene):
        """Raw: the background of the frames is in every image."""
        stack = self._stack(scene)
        assert np.nanmin(stack.images) >= 20.0 - 1e-6

    def test_an_image_is_displaced_by_the_shift_factor_times_its_offset(self, scene):
        stack = self._stack(scene)
        step = np.asarray(scene.raster.step)
        central = np.nan_to_num(stack.images[0])[20:-20, 20:-20]
        for pinhole in range(1, 9):
            image = np.nan_to_num(stack.images[pinhole])[20:-20, 20:-20]
            sx, sy, _ = _shift_between(central, image)
            d = np.array([stack.dx[pinhole], stack.dy[pinhole]], dtype=float)
            measured = -(step @ np.array([sx, sy])) @ d / (d @ d)
            assert measured == pytest.approx(scene.scan.alpha, abs=0.06)

    def test_shifted_images_coincide(self, scene):
        stack = self._stack(scene, alpha=scene.scan.alpha)
        assert stack.shifted
        central = np.nan_to_num(stack.images[0])[20:-20, 20:-20]
        for pinhole in range(1, 9):
            image = np.nan_to_num(stack.images[pinhole])[20:-20, 20:-20]
            sx, sy, correlation = _shift_between(central, image)
            assert np.hypot(sx, sy) < 0.15
            assert correlation > 0.9

    def test_off_centre_pinholes_are_dimmer(self, scene):
        stack = self._stack(scene, radius=3.0)
        level = np.array([np.nanmean(image) - 20.0 for image in stack.images])
        distance = np.hypot(stack.dx, stack.dy)
        assert level[0] == level.max()
        assert level[distance > 2.5].max() < 0.6 * level[0]

    def test_pinholes_beyond_the_frame_have_no_samples_there(self, scene):
        stack = pinhole_stack(
            scene.scan.frames, np.array([1.2]), np.array([30.0]), scene.offsets,
            scene.raster, radius=3.0,
        )
        left = int(np.flatnonzero((stack.dx == -3) & (stack.dy == 0))[0])
        assert np.isnan(stack.images[left]).all()
        assert np.isfinite(stack.images[0]).any()


class TestFootprintValues:
    def test_weighted_sum_over_a_footprint_is_the_amplitude(self, wide_points):
        operator = wide_points.operator(2.5)
        frames = wide_points.scan.frames[100:103]
        values = operator.footprint_values(frames)
        entries = operator.footprints
        assert values.shape == (3, entries.focus.size)
        amplitude = operator.apply(frames).amplitude
        focus = int(np.flatnonzero(operator.valid)[operator.valid.sum() // 2])
        own = entries.focus == focus
        weight = entries.weight[own]
        estimate = values[:, own] @ weight / (weight @ weight)
        np.testing.assert_allclose(estimate, amplitude[:, focus], rtol=5e-3, atol=0.05)

    def test_entries_lie_within_the_reach(self, wide_points):
        operator = wide_points.operator(2.0)
        entries = operator.footprints
        distance = np.hypot(entries.dx, entries.dy)
        assert distance.max() <= 2.0 * wide_points.scan.spot_sigma + 1e-9
        assert np.all(operator.valid[entries.focus])
        np.testing.assert_allclose(
            entries.weight,
            np.exp(-distance**2 / (2 * wide_points.scan.spot_sigma**2)),
        )


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
