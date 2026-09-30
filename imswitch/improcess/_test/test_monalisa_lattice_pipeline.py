"""End-to-end tests of the MoNaLISA lattice pipeline on synthetic scans.

Each scan has a known specimen, so the reconstruction is compared with the
truth on its own output raster.
"""

import numpy as np
import pytest

from imswitch.improcess._test._monalisa_synthetic import (
    make_physical_scan,
    make_scan,
)
from imswitch.improcess.reconstructors.monalisa import quality
from imswitch.improcess.reconstructors.monalisa.spot_model import (
    calibrate_spot_model,
    shared_sigma_fit,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import (
    PipelineParams,
    choose_calibration_image,
    find_foci,
    prepare_geometry,
    reconstruct_scan,
    reconstruct_stack,
)

A = 11.0
S = 10.41
THETA = np.deg2rad(17.0)
HEX_ROTATED = np.array(
    [
        [A * np.cos(THETA), A * np.cos(THETA + np.pi / 3)],
        [A * np.sin(THETA), A * np.sin(THETA + np.pi / 3)],
    ]
)

LATTICES = {
    "rectangular": dict(a1=(11.05, 0.0), a2=(0.0, 11.05)),
    "hexagonal": dict(
        a1=(A, 0.0), a2=(A / 2, A * np.sqrt(3) / 2), steps=(22, 19),
        step=np.diag([A / 22, A * np.sqrt(3) / 2 / 19]),
    ),
    "diamond-brick": dict(
        a1=(S / np.sqrt(2), S / np.sqrt(2)), a2=(-S / np.sqrt(2), S / np.sqrt(2)),
        steps=(32, 16), step=np.diag([S * np.sqrt(2) / 32, S / np.sqrt(2) / 16]),
    ),
    "hexagonal-17deg-along-lattice": dict(
        a1=HEX_ROTATED[:, 0], a2=HEX_ROTATED[:, 1], steps=(22, 19),
        step=HEX_ROTATED / np.array([22, 19]),
    ),
}


# The estimator by itself: no correction of the frames or of the cells. What
# the corrections do is tested where they are switched on.
PLAIN = dict(frame_gain="off", cell_offsets="off", reassignment="off")


def _reconstruct(params=None, nominal_step=None, **scan_arguments):
    params = params or PipelineParams(**PLAIN)
    scan = make_scan(**scan_arguments)
    step = scan.step if nominal_step is None else nominal_step(scan.step)
    geometry = prepare_geometry(
        scan.frames.mean(axis=0), step, scan.scan_index, params
    )
    result = reconstruct_stack(scan.frames, geometry, scan.scan_index)
    return scan, geometry, result


def _relative_error(scan, geometry, result, border=26):
    """Error over the pixels that were reconstructed, away from the frame edge.

    One period of the raster's border belongs to foci too close to the frame
    edge to be extracted.
    """
    truth = scan.truth(geometry.raster)
    inner = (slice(border, -border), slice(border, -border))
    difference = (result.amplitude.image - truth)[inner]
    finite = np.isfinite(difference)
    assert finite.mean() > 0.3
    return float(
        np.sqrt(np.mean(difference[finite] ** 2)) / np.sqrt(np.mean(truth[inner] ** 2))
    )


class TestNoiseless:
    @pytest.mark.parametrize("name", LATTICES)
    def test_every_lattice_is_reconstructed_by_placement(self, name):
        """The lattice is detected, not given; the scan is commensurate, so
        the image is placed, not interpolated."""
        scan, geometry, result = _reconstruct(**LATTICES[name])
        diagnostics = geometry.diagnostics
        assert diagnostics["spot_sigma_px"] == pytest.approx(2.0, rel=0.005)
        assert diagnostics["lattice_fit_residual_px"] < 0.01
        assert diagnostics["commensurate_residual"] < 1e-9
        assert diagnostics["coverage_holes"] == 0
        assert diagnostics["coverage_overlaps"] == 0
        assert result.diagnostics["placement"] == "exact"
        assert _relative_error(scan, geometry, result) < 2e-3

    def test_the_background_plane_is_the_background(self):
        scan, geometry, result = _reconstruct(background=35.0)
        plane = result.background.image[30:-30, 30:-30]
        assert np.nanmean(plane) == pytest.approx(35.0, abs=0.05)
        assert result.haze is None


class TestStep:
    def test_a_nominal_step_one_percent_off_is_locked_to_the_lattice(self):
        scan, geometry, result = _reconstruct(nominal_step=lambda step: 1.01 * step)
        assert geometry.diagnostics["step_locked"]
        assert geometry.diagnostics["step_lock_change"] == pytest.approx(0.01, abs=1e-3)
        np.testing.assert_allclose(geometry.step, scan.step, atol=1e-4)
        assert result.diagnostics["placement"] == "exact"
        assert _relative_error(scan, geometry, result) < 2e-3

    def test_a_real_step_error_is_gridded(self):
        """The scan really steps 1 % short: the samples are where they are,
        off the raster, and are interpolated onto it."""
        scan = make_scan(step=np.diag([0.495, 0.495]))
        params = PipelineParams(lock_tolerance=0.0, **PLAIN)
        geometry = prepare_geometry(
            scan.frames.mean(axis=0), scan.step, scan.scan_index, params
        )
        result = reconstruct_stack(scan.frames, geometry, scan.scan_index)
        assert geometry.diagnostics["commensurate_residual"] > 0.1
        assert result.diagnostics["placement"] == "gridded"
        assert _relative_error(scan, geometry, result) < 0.05

    def test_frames_and_scan_positions_must_match(self):
        scan, geometry, _ = _reconstruct()
        with pytest.raises(ValueError, match="scan positions"):
            reconstruct_stack(scan.frames[:10], geometry, scan.scan_index)


class TestDistortion:
    def test_measured_centres_remove_the_cell_tiling(self):
        """E7: with foci 0.3 px off the ideal lattice, placing by the ideal
        lattice misplaces whole cells. The measured centres remove it."""
        def shift(x, y):
            u, v = (x - 48.0) / 48.0, (y - 48.0) / 48.0
            return 0.3 * u * u, 0.2 * u * v

        ideal = _reconstruct(
            PipelineParams(spot_mode="shared", **PLAIN), centre_shift=shift
        )
        measured = _reconstruct(
            PipelineParams(spot_mode="field", **PLAIN), centre_shift=shift
        )
        assert ideal[2].diagnostics["placement"] == "exact"
        assert measured[2].diagnostics["placement"] == "gridded"
        assert measured[2].amplitude.converged
        error_ideal = _relative_error(*ideal)
        error_measured = _relative_error(*measured)
        assert error_ideal > 0.015
        assert error_measured < 0.2 * error_ideal


class TestNoise:
    @pytest.fixture(scope="class")
    @classmethod
    def errors(cls):
        out = {}
        for name, params in {
            "joint 2.5": PipelineParams(reach_sigma=2.5, **PLAIN),
            "isolated 2.5": PipelineParams(reach_sigma=2.5, joint=False, **PLAIN),
            "isolated 1.5": PipelineParams(reach_sigma=1.5, joint=False, **PLAIN),
        }.items():
            out[name] = _relative_error(*_reconstruct(params, noise="poisson"))
        return out

    def test_the_footprint_sets_the_noise(self, errors):
        """The wider footprint is what lowers the noise, by more than half."""
        assert errors["isolated 2.5"] < 0.5 * errors["isolated 1.5"]

    def test_the_joint_fit_is_not_noisier_at_the_same_footprint(self, errors):
        assert errors["joint 2.5"] <= 1.02 * errors["isolated 2.5"]

    def test_the_variance_plane_is_the_measured_variance_for_white_noise(self):
        scan, geometry, result = _reconstruct(noise="gaussian", read_noise=5.0)
        truth = scan.truth(geometry.raster)
        inner = (slice(30, -30), slice(30, -30))
        error = (result.amplitude.image - truth)[inner]
        predicted = result.amplitude.variance[inner]
        assert result.noise_sigma.mean() == pytest.approx(5.0, rel=0.03)
        assert np.nanmean(error**2) == pytest.approx(np.nanmean(predicted), rel=0.1)

    def test_the_variance_plane_is_a_lower_bound_for_photon_noise(self):
        scan, geometry, result = _reconstruct(noise="poisson")
        truth = scan.truth(geometry.raster)
        inner = (slice(30, -30), slice(30, -30))
        error = (result.amplitude.image - truth)[inner]
        ratio = np.nanmean(error**2) / np.nanmean(result.amplitude.variance[inner])
        assert 1.2 < ratio < 2.2


def static_160(rng):
    """Light that does not move with the scan, structured on the scale of
    the foci and five times as bright."""
    from scipy.ndimage import gaussian_filter

    static = gaussian_filter(rng.random((160, 160)), 4.0)
    return 1500.0 * (static - static.min()) / np.ptp(static)


class TestReconstructScan:
    """The one entry point: frames, nominal step and scan shape in, image out."""

    @pytest.mark.parametrize("orientation", ["+x+y", "-x+y", "+y-x", "-y-x"])
    def test_reads_the_orientation_from_the_frames(self, orientation):
        scan = make_scan(orientation=orientation, noise="poisson")
        result = reconstruct_scan(scan.frames, scan.step, (22, 22))
        assert result.diagnostics["orientation"] == orientation
        assert result.diagnostics["orientation_margin"] > 0.2
        assert _relative_error(scan, result.geometry, result) < 0.07

    def test_foci_without_a_shift_are_placed(self):
        scan = make_scan()
        result = reconstruct_scan(scan.frames, scan.step, (22, 22))
        assert abs(result.diagnostics["shift_factor"]) < 0.05
        assert result.diagnostics["shift_factor_used"] is None
        assert result.diagnostics["placement"] == "exact"
        # What the corrections cost where there is nothing to correct: with
        # some eighty foci the mean of a frame still holds a percent of
        # specimen, of which the smooth gain takes a part, and the borders
        # of the cells are crossed by a specimen as fine as two pixels.
        assert result.diagnostics["frame_gain_rms"] < 0.015
        level = float(np.nanmean(result.amplitude.image))
        assert result.diagnostics["cell_offset_rms"] < 0.03 * level
        assert _relative_error(scan, result.geometry, result) < 0.03

    def test_wide_foci_are_reassigned(self):
        scan = make_physical_scan(cells=10, sigma_e=1.2, sigma_d=1.5, noise="poisson", seed=3)
        result = reconstruct_scan(scan.frames, scan.step, (22, 22))
        assert result.diagnostics["orientation"] == "+x+y"
        assert result.diagnostics["shift_factor"] == pytest.approx(scan.alpha, abs=0.08)
        assert result.diagnostics["placement"] == "reassigned"
        assert result.geometry.lattice.nearest_spacing() == pytest.approx(11.0, abs=0.05)
        assert result.geometry.coverage.exactly_once
        assert result.geometry.diagnostics["spot_sigma_px"] == pytest.approx(
            scan.spot_sigma, rel=0.08
        )

    def test_the_gain_of_the_frames_is_taken_out(self):
        """A first frame of every line that is 12 % brighter, and a scan that
        bleaches by 10 %: both show as a pattern in every cell."""
        frame = np.arange(22 * 22)
        gain = (1.0 + 0.12 * (frame % 22 == 0)) * (1.0 - 0.1 * (frame // 22) / 21.0)
        gain = gain / gain.mean()
        scan = make_scan(shape=(160, 160), frame_gain=gain)
        kept = reconstruct_scan(
            scan.frames, scan.step, (22, 22), PipelineParams(**PLAIN),
            orientation="+x+y",
        )
        removed = reconstruct_scan(
            scan.frames, scan.step, (22, 22),
            PipelineParams(**{**PLAIN, "frame_gain": "smooth"}), orientation="+x+y",
        )
        assert removed.diagnostics["frame_gain_rms"] == pytest.approx(
            np.std(gain), rel=0.15
        )
        error_kept = _relative_error(scan, kept.geometry, kept)
        error_removed = _relative_error(scan, removed.geometry, removed)
        assert error_kept == pytest.approx(np.std(gain), rel=0.2)
        assert error_removed < 0.4 * error_kept

    def test_the_variance_frame_is_calibrated_on_when_it_shows_the_foci(self):
        scan = make_scan()
        mean, var = scan.frames.mean(axis=0), scan.frames.var(axis=0)
        chosen = choose_calibration_image(mean, var)
        assert chosen.name == "variance"
        assert chosen.contrast["variance"] > 0.3
        assert chosen.contrast["variance"] >= 0.5 * chosen.contrast["mean"]
        assert chosen.image is not mean or chosen.name == "mean"

    def test_a_variance_of_shot_noise_yields_to_the_mean_frame(self):
        """On a dim recording the temporal variance is the shot noise of the
        mean: flat, with no foci in it. Read from it, the foci collapsed
        onto single pixels and the footprint had no pixels; the mean frame,
        which shows them, is calibrated on instead."""
        rng = np.random.default_rng(11)
        scan = make_scan()
        mean = scan.frames.mean(axis=0)
        # Read noise with a trace of the foci: with the lattice known, the
        # foci explain next to nothing of it.
        noise = 40.0 + 0.01 * (mean - mean.min()) + rng.normal(0.0, 3.0, mean.shape)
        lattice = find_foci(mean).lattice
        chosen = choose_calibration_image(mean, noise, lattice=lattice)
        assert chosen.name == "mean"
        assert chosen.contrast["variance"] < 0.5 * chosen.contrast["mean"]
        # Noise alone: no lattice on it, and the mean frame again.
        flat = choose_calibration_image(mean, rng.normal(40.0, 4.0, mean.shape))
        assert flat.name == "mean" and np.isnan(flat.contrast["variance"])

        geometry = prepare_geometry(
            mean, scan.step, scan.scan_index, modulation_frame=noise
        )
        assert geometry.diagnostics["calibration_image"] == "mean"
        assert geometry.diagnostics["spot_sigma_px"] == pytest.approx(2.0, rel=0.1)

    def test_the_foci_fitted_on_noise_do_not_collapse(self):
        """The shared width stays inside its bounds even when every focus
        fitted on its own lands on the brightest pixel of its window."""
        rng = np.random.default_rng(12)
        scan = make_scan()
        x, y = scan.focus_x, scan.focus_y
        noise = rng.normal(100.0, 5.0, scan.frames.shape[1:])
        model = calibrate_spot_model(noise, x, y, 2.0, mode="shared", modulation=False)
        assert 0.5 <= model.shared_sigma <= 6.0
        fit = shared_sigma_fit(noise, x, y, radius=4.0)
        assert fit.explained < 0.1
        fit = shared_sigma_fit(scan.frames.mean(axis=0), x, y, radius=4.0)
        assert fit.explained > 0.5

    def test_find_foci_says_what_it_found(self):
        scan = make_scan()
        finding = find_foci(scan.frames.mean(axis=0), scan.frames.var(axis=0))
        rows, cols = scan.frames.shape[1:]
        x, y = finding.points(rows, cols)
        assert x.size == scan.focus_x[
            (scan.focus_x >= 0) & (scan.focus_x < cols)
            & (scan.focus_y >= 0) & (scan.focus_y < rows)
        ].size
        text = finding.describe()
        assert "foci" in text and "variance frame" in text and "contrast" in text

    def test_a_static_background_does_not_reach_the_image(self):
        """Out-of-focus light five times as bright as the foci, structured on
        their scale: the calibration on the variance of the frames does not
        see it, and the fit takes it for background. What it leaves in the
        image is its photon noise."""
        rng = np.random.default_rng(5)
        scan = make_scan(shape=(160, 160), static=static_160(rng))
        result = reconstruct_scan(scan.frames, scan.step, (22, 22))
        assert result.geometry.diagnostics["spot_sigma_px"] == pytest.approx(2.0, rel=0.05)
        assert result.diagnostics["orientation"] == "+x+y"
        assert _relative_error(scan, result.geometry, result) < 0.04

    def test_without_the_cell_offsets_the_background_tiles_the_image(self):
        rng = np.random.default_rng(5)
        scan = make_scan(shape=(160, 160), static=static_160(rng))
        kept = reconstruct_scan(
            scan.frames, scan.step, (22, 22), PipelineParams(**PLAIN),
            orientation="+x+y",
        )
        removed = reconstruct_scan(
            scan.frames, scan.step, (22, 22),
            PipelineParams(**{**PLAIN, "cell_offsets": "seams"}), orientation="+x+y",
        )
        assert _relative_error(scan, kept.geometry, kept) > 0.2
        assert _relative_error(scan, removed.geometry, removed) < 0.04
        assert quality.seam_contrast(kept.amplitude.image, kept.owner) > 2.5
        assert quality.seam_contrast(
            removed.amplitude.image, removed.owner
        ) == pytest.approx(1.0, abs=0.1)

    def test_two_halves_of_the_pixels_give_the_same_image(self):
        scan = make_scan(noise="poisson")
        first, second = (
            reconstruct_scan(
                scan.frames, scan.step, (22, 22), orientation="+x+y", pixel_mask=mask
            )
            for mask in quality.split_masks(scan.frames.shape[1:])
        )
        a = quality.central_part(first.amplitude.image)
        b = quality.central_part(second.amplitude.image)
        frequency, correlation = quality.ring_correlation(a, b)
        assert np.all(correlation[frequency < 0.25] > 0.8)
        assert quality.seam_contrast(first.amplitude.image, first.owner) < 1.3

    def test_frames_must_make_the_scan(self):
        scan = make_scan()
        with pytest.raises(ValueError, match="scan"):
            reconstruct_scan(scan.frames[:100], scan.step, (22, 22))

    def test_a_lattice_finer_than_its_spots_is_refused(self):
        """A harmonic taken for the pattern would reconstruct without a
        word; foci closer than three of their widths cannot be foci."""
        from imswitch.improcess.reconstructors.monalisa.lattice import Lattice

        scan = make_scan()
        with pytest.raises(ValueError, match="harmonic"):
            reconstruct_scan(
                scan.frames, scan.step, (22, 22),
                lattice=Lattice.rectangular(5.5, 5.5, 4.2, 5.7),
            )

    def test_the_sharpened_image_is_a_plane_of_its_own(self):
        scan = make_physical_scan(cells=10, sigma_e=0.8, sigma_d=1.5, specimen="filaments", seed=3)
        plain = reconstruct_scan(scan.frames, scan.step, (22, 22), orientation="+x+y")
        sharp = reconstruct_scan(
            scan.frames, scan.step, (22, 22),
            PipelineParams(sharpen_sigma_px=1.5), orientation="+x+y",
        )
        assert plain.sharpened is None
        np.testing.assert_allclose(
            sharp.amplitude.image, plain.amplitude.image, equal_nan=True
        )
        assert np.array_equal(
            np.isnan(sharp.sharpened), np.isnan(sharp.amplitude.image)
        )
        # Sharper: more of the image's variance in its fine structure.
        def fine(image):
            filled = np.nan_to_num(image)
            return np.std(np.diff(filled, axis=1)) / np.std(filled)

        assert fine(sharp.sharpened) > 1.1 * fine(sharp.amplitude.image)

    @pytest.mark.parametrize("kind", ["raw", "shifted"])
    def test_the_pinhole_stack_is_an_output_on_request(self, kind):
        scan = make_physical_scan(cells=10, sigma_e=1.2, sigma_d=1.5, seed=3)
        plain = reconstruct_scan(scan.frames, scan.step, (22, 22), orientation="+x+y")
        assert plain.pinholes is None
        result = reconstruct_scan(
            scan.frames, scan.step, (22, 22),
            PipelineParams(pinhole_stack=kind), orientation="+x+y",
        )
        stack = result.pinholes
        assert stack.shifted == (kind == "shifted")
        assert stack.images.shape[1:] == result.amplitude.image.shape
        # The footprint of the reconstruction: 2.5 sigma of a 1.9 px spot.
        radius = 2.5 * result.geometry.spot_model.shared_sigma
        assert stack.images.shape[0] == np.sum(
            np.add.outer(np.arange(-9, 10) ** 2, np.arange(-9, 10) ** 2) <= radius**2
        )
        np.testing.assert_allclose(
            result.amplitude.image, plain.amplitude.image, equal_nan=True
        )

    def test_unknown_pinhole_stack(self):
        scan = make_scan()
        with pytest.raises(ValueError, match="pinhole"):
            reconstruct_scan(
                scan.frames, scan.step, (22, 22),
                PipelineParams(pinhole_stack="all"), orientation="+x+y",
            )

    @pytest.mark.parametrize("name", ["rectangular", "diamond-brick"])
    def test_without_a_pixel_size(self, name):
        """The metadata of a recording may not hold the pixel size. A scan
        that covers the cell once has the step that follows from the cell."""
        arguments = dict(LATTICES[name])
        shape = arguments.get("steps", (22, 22))
        scan = make_scan(**arguments)
        result = reconstruct_scan(scan.frames, None, shape, PipelineParams(**PLAIN))
        assert not result.geometry.diagnostics["step_given"]
        np.testing.assert_allclose(result.geometry.step, scan.step, atol=2e-4)
        assert result.geometry.coverage.exactly_once
        assert result.diagnostics["placement"] == "exact"
        assert _relative_error(scan, result.geometry, result) < 2e-3


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
