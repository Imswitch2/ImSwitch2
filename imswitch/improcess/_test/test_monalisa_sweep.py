"""Tests for the fast-Gauss MoNaLISA parameter-sweep mode."""

import numpy as np
import pytest
import tifffile

from imswitch.improcess.live import InMemoryStackWrapper
from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.result import (
    MonalisaProcessingResult,
    MonalisaSweepResult,
)
from imswitch.improcess.reconstructors.monalisa.sweep import (
    parse_sweep_values,
    resolve_sweep_parameter,
)


@pytest.fixture
def foci_stack():
    """Synthetic MoNaLISA scan: Gaussian foci walking with the scan step."""
    nx_s = ny_s = 5
    num_rows = num_cols = 60
    xp = yp = 10.0
    xo, yo = 5.0, 5.0
    sigma = 1.8

    ys, xs = np.mgrid[0:num_rows, 0:num_cols].astype(float)
    frames = []
    for step in range(nx_s * ny_s):
        x_shift = (step % nx_s) * (xp / nx_s)
        y_shift = (step // nx_s) * (yp / ny_s)
        frame = np.full((num_rows, num_cols), 20.0, dtype=np.float32)
        cy = yo + y_shift
        while cy < num_rows + 3 * sigma:
            cx = xo + x_shift
            while cx < num_cols + 3 * sigma:
                frame += 150.0 * np.exp(
                    -(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * sigma**2)
                ).astype(np.float32)
                cx += xp
            cy += yp
        frames.append(frame)
    return np.stack(frames), nx_s, ny_s, (xo, yo, xp, yp)


def _sweep_params(nx_s, ny_s, pattern, **overrides):
    xo, yo, xp, yp = pattern
    params = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
        "device": "CPU",
        "row_offset": yo,
        "col_offset": xo,
        "row_period": yp,
        "col_period": xp,
        "fast_gauss_gaussian_sigma_px": 1.8,
        "bleaching_correction": False,
        "sweep_enabled": True,
        "sweep_parameter": "Pinhole radius (×σ)",
        "sweep_values_text": "0.75, 1.5, 2.5",
        "scan_params": {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": [str(nx_s), str(ny_s), "1", "1"],
            "step_sizes": ["50", "50", "1", "1"],
            "unidirectional": False,
        },
    }
    params.update(overrides)
    return params


class TestParseSweepValues:
    def test_comma_list(self):
        assert parse_sweep_values("0.5, 1, 1.5") == [0.5, 1.0, 1.5]

    def test_whitespace_and_semicolons(self):
        assert parse_sweep_values(" 1;2  3 ") == [1.0, 2.0, 3.0]

    def test_inclusive_range(self):
        assert parse_sweep_values("0.5:0.25:1.0") == pytest.approx([0.5, 0.75, 1.0])

    def test_range_endpoint_with_float_step(self):
        values = parse_sweep_values("0.5:0.1:0.8")
        assert values == pytest.approx([0.5, 0.6, 0.7, 0.8])

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "a, b", "1:0:5", "5:1:2", "1:2", "-1, 2", "0", "1, nan"],
    )
    def test_rejects_bad_input(self, bad):
        with pytest.raises(ValueError):
            parse_sweep_values(bad)

    def test_rejects_runaway_range(self):
        with pytest.raises(ValueError, match="maximum"):
            parse_sweep_values("0.001:0.001:10")

    def test_resolve_parameter_labels(self):
        assert resolve_sweep_parameter("Pinhole radius (×σ)") == "pinhole_radius_sigma"
        assert resolve_sweep_parameter("Gaussian sigma (px)") == "gaussian_sigma_px"
        assert resolve_sweep_parameter("pinhole_radius_sigma") == "pinhole_radius_sigma"
        with pytest.raises(ValueError, match="choose one of"):
            resolve_sweep_parameter("magnification")


class TestSweepProcessing:
    def test_pinhole_sweep_produces_stacked_result(self, foci_stack):
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="sweep-run", dataset_name="det", data=stack, attrs={}
        )
        params = _sweep_params(nx_s, ny_s, pattern)

        result = MonalisaReconstructor().process(data_obj, params)

        assert isinstance(result, MonalisaSweepResult)
        assert result.axis_labels == ["Sweep", "Dataset", "Base", "T", "Z", "Y", "X"]
        assert result.data.shape[0] == 3
        assert result.sweep_values == [0.75, 1.5, 2.5]
        assert result.sweep_parameter_label == "Pinhole radius (×σ)"
        assert np.all(np.isfinite(result.data))
        # The swept parameter must actually change the reconstruction.
        assert not np.allclose(result.data[0], result.data[-1])

    def test_gaussian_sigma_sweep(self, foci_stack):
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="sigma-sweep", dataset_name="det", data=stack, attrs={}
        )
        params = _sweep_params(
            nx_s,
            ny_s,
            pattern,
            sweep_parameter="Gaussian sigma (px)",
            sweep_values_text="1.2:0.6:2.4",
        )
        result = MonalisaReconstructor().process(data_obj, params)
        assert isinstance(result, MonalisaSweepResult)
        assert result.sweep_values == pytest.approx([1.2, 1.8, 2.4])
        assert not np.allclose(result.data[0], result.data[-1])

    def test_sweep_matches_individual_runs(self, foci_stack):
        """Each sweep slice must equal a standalone run at that value."""
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="sweep-vs-single", dataset_name="det", data=stack, attrs={}
        )
        sweep = MonalisaReconstructor().process(
            data_obj, _sweep_params(nx_s, ny_s, pattern)
        )

        single_params = _sweep_params(
            nx_s,
            ny_s,
            pattern,
            sweep_enabled=False,
            fast_gauss_footprint_mode="Circular pinhole",
            fast_gauss_pinhole_radius_sigma=1.5,
        )
        single = MonalisaReconstructor().process(data_obj, single_params)
        np.testing.assert_allclose(sweep.data[1], single.data)

    def test_sweep_with_classic_method_raises(self, foci_stack):
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="classic-sweep", dataset_name="det", data=stack, attrs={}
        )
        params = _sweep_params(
            nx_s, ny_s, pattern, reconstruction_method="MoNaLISA"
        )
        with pytest.raises(ValueError, match="only supported for the Fast Gauss"):
            MonalisaReconstructor().process(data_obj, params)

    def test_sweep_with_bad_values_raises(self, foci_stack):
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="bad-values", dataset_name="det", data=stack, attrs={}
        )
        params = _sweep_params(nx_s, ny_s, pattern, sweep_values_text="")
        with pytest.raises(ValueError, match="Sweep values"):
            MonalisaReconstructor().process(data_obj, params)

    def test_consolidating_sweep_results_raises(self, foci_stack):
        stack, nx_s, ny_s, pattern = foci_stack
        data_obj = InMemoryStackWrapper(
            name="sweep-consolidate", dataset_name="det", data=stack, attrs={}
        )
        reconstructor = MonalisaReconstructor()
        result = reconstructor.process(
            data_obj, _sweep_params(nx_s, ny_s, pattern)
        )
        with pytest.raises(ValueError, match="consolidat"):
            reconstructor.consolidate([result, result])


class TestSweepResult:
    def _make_result(self, num_t=1, sweep_values=(0.75, 1.5)):
        rng = np.random.default_rng(3)
        data = rng.random((len(sweep_values), 1, 1, num_t, 1, 8, 9)).astype(
            np.float32
        )
        scan_params = {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": ["3", "3", "1", str(num_t)],
            "step_sizes": ["50", "50", "1", "1"],
            "unidirectional": False,
        }
        return MonalisaSweepResult(
            name="sweep",
            data=data,
            scan_params=scan_params,
            sweep_parameter_label="Pinhole radius (×σ)",
            sweep_values=list(sweep_values),
            output_pixel_size_nm=(50.0, 50.0),
        )

    def test_display_layers_expose_sweep_axis(self):
        result = self._make_result()
        layers = result.display_layers()
        assert len(layers) == 1  # single base
        layer = layers[0]
        assert layer.axis_labels == ["Sweep", "Dataset", "T", "Z", "Y", "X"]
        assert layer.data.shape[0] == 2
        assert layer.metadata["sweep_parameter"] == "Pinhole radius (×σ)"
        assert layer.metadata["sweep_values"] == [0.75, 1.5]

    def test_mismatched_value_count_rejected(self):
        with pytest.raises(ValueError, match="Sweep axis"):
            MonalisaSweepResult(
                name="bad",
                data=np.zeros((3, 1, 1, 1, 1, 4, 4), dtype=np.float32),
                scan_params={
                    "dimensions": [
                        "Right-Left", "Up-Down", "Back-Front", "Timepoints"
                    ],
                    "directions": ["pos", "pos", "pos"],
                    "steps": ["2", "2", "1", "1"],
                    "step_sizes": ["50", "50", "1", "1"],
                    "unidirectional": False,
                },
                sweep_parameter_label="Pinhole radius (×σ)",
                sweep_values=[1.0, 2.0],
            )

    def test_save_single_timepoint_folds_sweep_into_imagej_t(self, tmp_path):
        result = self._make_result(num_t=1, sweep_values=(0.75, 1.5, 2.5))
        out = tmp_path / "sweep.tiff"
        result.save(out)

        with tifffile.TiffFile(str(out)) as handle:
            assert handle.is_imagej
            series = handle.series[0]
            data = series.asarray()
            labels = handle.imagej_metadata.get("Labels")

        # ImageJ T axis == sweep axis
        assert data.shape[0] == 3
        assert labels is not None and len(labels) == 3
        assert "Pinhole radius" in labels[0] and "0.75" in labels[0]
        assert "2.5" in labels[-1]
        np.testing.assert_allclose(data.reshape(3, 8, 9), result.data[:, 0, 0, 0, 0])

    def test_save_multi_timepoint_writes_one_file_per_value(self, tmp_path):
        result = self._make_result(num_t=2, sweep_values=(0.75, 1.5))
        out = tmp_path / "sweep.tiff"
        result.save(out)

        written = sorted(tmp_path.glob("*.tiff"))
        assert len(written) == 2
        assert not out.exists()  # only per-value files
        for path in written:
            with tifffile.TiffFile(str(path)) as handle:
                assert handle.series[0].asarray().shape[0] == 2  # T axis

    def test_plain_result_save_unchanged(self, tmp_path):
        """The refactored writer must keep the non-sweep 6D save identical."""
        data = np.random.default_rng(5).random((1, 2, 1, 1, 8, 9)).astype(np.float32)
        result = MonalisaProcessingResult(
            name="plain",
            data=data,
            scan_params={
                "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
                "directions": ["pos", "pos", "pos"],
                "steps": ["3", "3", "1", "1"],
                "step_sizes": ["50", "50", "1", "1"],
                "unidirectional": False,
            },
        )
        out = tmp_path / "plain.tiff"
        result.save(out)
        with tifffile.TiffFile(str(out)) as handle:
            assert handle.is_imagej
            saved = handle.series[0].asarray()
        np.testing.assert_allclose(saved.reshape(2, 8, 9), data[0, :, 0, 0])
