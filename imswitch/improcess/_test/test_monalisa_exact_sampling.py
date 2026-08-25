"""Tests for the exact-pixel sampling mode of the fast-Gauss processor."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.base import StreamInit
from imswitch.improcess.reconstructors.monalisa.gauss_processor import (
    SAMPLING_MODE_BILINEAR,
    SAMPLING_MODE_EXACT,
    build_exact_sampling,
    make_gauss_processor,
    normalize_sampling_mode,
)
from imswitch.improcess.reconstructors.monalisa.live_session import (
    MonalisaLiveSession,
)
from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
    get_center_coords,
    get_pinhole_footprint,
)


def _render_frame(num_rows, num_cols, centers_x, centers_y, amplitudes,
                  sigma, background):
    ys, xs = np.mgrid[0:num_rows, 0:num_cols].astype(float)
    frame = np.full((num_rows, num_cols), float(background))
    for amp, cx, cy in zip(amplitudes, centers_x, centers_y):
        frame += amp * np.exp(
            -(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * sigma**2)
        )
    return frame


class TestNormalizeSamplingMode:
    def test_mapping(self):
        assert normalize_sampling_mode(None) == SAMPLING_MODE_BILINEAR
        assert normalize_sampling_mode("") == SAMPLING_MODE_BILINEAR
        assert normalize_sampling_mode("Bilinear (legacy)") == SAMPLING_MODE_BILINEAR
        assert normalize_sampling_mode("Exact pixel") == SAMPLING_MODE_EXACT
        assert normalize_sampling_mode("exact") == SAMPLING_MODE_EXACT

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="sampling mode"):
            normalize_sampling_mode("cubic")


class TestExactWeights:
    def test_amplitude_recovery_on_exact_model(self):
        """On noiseless Gaussian + constant background data with an isolated
        focus, the exact-sampling weights must return the amplitude to
        numerical precision — that is the estimator's defining property."""
        sigma = 2.0
        centers_x = np.array([30.37])
        centers_y = np.array([25.81])
        footprint = get_pinhole_footprint(1.5 * sigma)
        rows, cols, weights = build_exact_sampling(
            centers_x, centers_y, footprint, sigma, True, 60, 60
        )
        frame = _render_frame(60, 60, centers_x, centers_y, [200.0], sigma, 50.0)
        amplitude = (frame[rows, cols] * weights).sum(axis=-1)
        assert amplitude[0] == pytest.approx(200.0, abs=1e-9)

    def test_matched_filter_without_background(self):
        sigma = 2.0
        centers_x = np.array([30.0])
        centers_y = np.array([25.0])
        footprint = get_pinhole_footprint(1.5 * sigma)
        rows, cols, weights = build_exact_sampling(
            centers_x, centers_y, footprint, sigma, False, 60, 60
        )
        frame = _render_frame(60, 60, centers_x, centers_y, [200.0], sigma, 0.0)
        amplitude = (frame[rows, cols] * weights).sum(axis=-1)
        assert amplitude[0] == pytest.approx(200.0, abs=1e-9)

    def test_edge_focus_masks_out_of_frame_pixels(self):
        """A focus at the frame border must fit only the pixels that exist
        (the bilinear path clamps them onto border pixels instead, which
        double-counts) and still return a finite, sane amplitude."""
        sigma = 2.0
        centers_x = np.array([1.2])
        centers_y = np.array([0.8])
        footprint = get_pinhole_footprint(2.0 * sigma)
        rows, cols, weights = build_exact_sampling(
            centers_x, centers_y, footprint, sigma, True, 60, 60
        )
        frame = _render_frame(60, 60, centers_x, centers_y, [200.0], sigma, 50.0)
        amplitude = (frame[rows, cols] * weights).sum(axis=-1)
        assert np.isfinite(amplitude[0])
        assert amplitude[0] == pytest.approx(200.0, rel=0.05)

    def test_focus_fully_outside_frame_gets_zero_weights(self):
        sigma = 2.0
        rows, cols, weights = build_exact_sampling(
            np.array([-50.0]), np.array([-50.0]),
            get_pinhole_footprint(1.5 * sigma), sigma, True, 60, 60
        )
        np.testing.assert_array_equal(weights, 0.0)


class TestProcessorModes:
    def _grid_setup(self, fractional: bool):
        num_rows = num_cols = 120
        xp = yp = 11.05 if fractional else 10.0
        xo = 5.37 if fractional else 5.0
        yo = 4.81 if fractional else 5.0
        sigma = 2.0
        nx_c = int(np.ceil((num_cols - xo) / xp))
        ny_c = int(np.ceil((num_rows - yo) / yp))
        centers_x, centers_y = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
        rng = np.random.default_rng(11)
        amplitudes = rng.uniform(100, 300, centers_x.size)
        frame = _render_frame(
            num_rows, num_cols, centers_x, centers_y, amplitudes, sigma, 50.0
        )
        return (
            dict(
                xp=xp, xo=xo, yp=yp, yo=yo, nx_c=nx_c, ny_c=ny_c,
                nx_s=2, ny_s=2, num_rows=num_rows, num_cols=num_cols,
                gaussian_sigma_px=sigma, pinhole_radius_px=1.5 * sigma,
            ),
            frame,
            amplitudes,
            (centers_x, centers_y),
        )

    def test_integer_centers_agree_across_modes(self):
        """With integer focus centers, bilinear interpolation is exact, so
        both modes must agree wherever the footprint stays inside the frame
        (edge handling legitimately differs: clamped vs masked pixels)."""
        kwargs, frame, _, (centers_x, centers_y) = self._grid_setup(
            fractional=False
        )
        bilinear = make_gauss_processor(sampling_mode="bilinear", **kwargs)
        exact = make_gauss_processor(sampling_mode="exact", **kwargs)
        radius = kwargs["pinhole_radius_px"]
        interior = (
            (centers_x > radius) & (centers_x < kwargs["num_cols"] - 1 - radius)
            & (centers_y > radius) & (centers_y < kwargs["num_rows"] - 1 - radius)
        )
        amp_b = bilinear.process_frame(frame)
        amp_e = exact.process_frame(frame)
        np.testing.assert_allclose(amp_e[interior], amp_b[interior], rtol=1e-9)

    def test_fractional_centers_exact_beats_bilinear(self):
        """The audit's headline number: on fractional centers the bilinear
        path underestimates amplitudes by several percent (varying with each
        focus' subpixel phase), while exact sampling is unbiased."""
        kwargs, frame, amplitudes, (centers_x, centers_y) = self._grid_setup(
            fractional=True
        )
        bilinear = make_gauss_processor(sampling_mode="bilinear", **kwargs)
        exact = make_gauss_processor(sampling_mode="exact", **kwargs)

        radius = kwargs["pinhole_radius_px"]
        interior = (
            (centers_x > radius + 2)
            & (centers_x < kwargs["num_cols"] - radius - 3)
            & (centers_y > radius + 2)
            & (centers_y < kwargs["num_rows"] - radius - 3)
        )
        err_bilinear = bilinear.process_frame(frame)[interior] - amplitudes[interior]
        err_exact = exact.process_frame(frame)[interior] - amplitudes[interior]

        rmse_bilinear = float(np.sqrt((err_bilinear**2).mean()))
        rmse_exact = float(np.sqrt((err_exact**2).mean()))
        # Neighbor crosstalk affects both equally; the difference isolates
        # the interpolation bias.
        assert rmse_exact < rmse_bilinear / 3
        assert abs(err_exact.mean()) < 1.0
        assert err_bilinear.mean() < -5.0  # systematic underestimate

    def test_process_chunk_matches_process_frame(self):
        kwargs, frame, _, _ = self._grid_setup(fractional=True)
        exact = make_gauss_processor(sampling_mode="exact", **kwargs)
        chunk = np.stack([frame, frame * 2.0])
        per_chunk = exact.process_chunk(chunk)
        per_frame = exact.process_frame(frame)
        np.testing.assert_allclose(per_chunk[0], per_frame)
        np.testing.assert_allclose(per_chunk[1], per_frame * 2.0)


class TestLiveSessionPlumbing:
    def test_session_honors_sampling_mode_param(self):
        nx_s = ny_s = 4
        num_rows = num_cols = 60
        rng = np.random.default_rng(5)
        stack = rng.integers(
            40, 90, size=(nx_s * ny_s, num_rows, num_cols)
        ).astype(np.uint16)
        ys, xs = np.mgrid[0:num_rows, 0:num_cols].astype(float)
        for index in range(stack.shape[0]):
            for cy in np.arange(5.0, num_rows, 10.0):
                for cx in np.arange(5.0, num_cols, 10.0):
                    stack[index] += (
                        120 * np.exp(
                            -(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * 2.0**2)
                        )
                    ).astype(np.uint16)
        attrs = {
            "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
            "ScanStage:axis_length": [(nx_s - 1) * 50.0, (ny_s - 1) * 50.0, 1.0],
            "ScanStage:axis_step_size": [50.0, 50.0, 1.0],
            "ScanStage:axis_step_size_unit": "nm",
            "recording:frames_per_stack": nx_s * ny_s,
            "recording:num_timepoints": 1,
        }
        params = {
            "device": "CPU",
            "fast_gauss_sampling_mode": "Exact pixel",
            "row_period": 10.0,
            "col_period": 10.0,
        }
        session = MonalisaLiveSession()
        session.begin(
            StreamInit(name="s", dataset_name="d", data=stack, attrs=attrs),
            params,
        )
        assert session.processor.sampling_mode == SAMPLING_MODE_EXACT
        result = session.result()
        assert np.all(np.isfinite(result.data))
        session.close()
