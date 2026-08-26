"""Tests for the general-lattice (scatter + gridding) fast-Gauss path."""

import numpy as np
import pytest

from imswitch.improcess.live import InMemoryStackWrapper
from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.lattice_recon import (
    assemble_image,
    choose_orientation,
    sample_positions,
    scan_offsets_px,
)
from imswitch.improcess.reconstructors.monalisa.result import (
    MonalisaSpotCloud,
    MonalisaSweepResult,
)

PIXEL_SIZE_NM = 100.0
FOCUS_SIGMA = 1.8


def _sample_field(x, y):
    """Smooth, positive synthetic specimen."""
    return (
        100.0
        + 50.0 * np.sin(2 * np.pi * np.asarray(x) / 37.0)
        + 35.0 * np.cos(2 * np.pi * np.asarray(y) / 29.0)
    )


def _simulate_acquisition(
    lattice: Lattice,
    nx_s: int,
    ny_s: int,
    step_px: tuple[float, float],
    shape: tuple[int, int],
    orientation=("x", 1, 1),
    background: float = 20.0,
):
    """Render a stack: static foci whose brightness follows the sample.

    The sample-moves model the pipeline assumes: focus f in frame k reports
    the specimen at ``focus_position + scan_offset(k)`` while the focus
    itself stays put in camera space.
    """
    rows, cols = shape
    margin = 4 * FOCUS_SIGMA
    foci_x, foci_y = lattice.points_in_frame(rows, cols, margin=margin)

    patches = []
    for cx, cy in zip(foci_x, foci_y):
        x1 = max(0, int(np.floor(cx - margin)))
        x2 = min(cols, int(np.ceil(cx + margin)) + 1)
        y1 = max(0, int(np.floor(cy - margin)))
        y2 = min(rows, int(np.ceil(cy + margin)) + 1)
        if x1 >= x2 or y1 >= y2:
            patches.append(None)
            continue
        ys, xs = np.mgrid[y1:y2, x1:x2].astype(float)
        gauss = np.exp(
            -(((xs - cx) ** 2) + ((ys - cy) ** 2)) / (2 * FOCUS_SIGMA**2)
        )
        patches.append(((slice(y1, y2), slice(x1, x2)), gauss))

    offsets = scan_offsets_px(nx_s, ny_s, step_px[0], step_px[1], orientation)
    frames = np.full((nx_s * ny_s, rows, cols), background, dtype=np.float64)
    for index, offset in enumerate(offsets):
        amplitudes = _sample_field(foci_x + offset[0], foci_y + offset[1])
        for amplitude, patch in zip(amplitudes, patches):
            if patch is not None:
                window, gauss = patch
                frames[index][window] += amplitude * gauss
    return frames


def _params(nx_s, ny_s, step_px, period_scale, **overrides):
    params = {
        "reconstruction_method": "Fast Gauss MoNaLISA",
        "device": "CPU",
        "pixel_size_nm": PIXEL_SIZE_NM,
        "row_offset": 0.0,
        "col_offset": 0.0,
        "row_period": period_scale,
        "col_period": period_scale,
        "fast_gauss_gaussian_sigma_px": FOCUS_SIGMA,
        "fast_gauss_footprint_mode": "Circular pinhole",
        "fast_gauss_pinhole_radius_sigma": 1.5,
        "bleaching_correction": False,
        "scan_params": {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["pos", "pos", "pos"],
            "steps": [str(nx_s), str(ny_s), "1", "1"],
            "step_sizes": [
                str(step_px[0] * PIXEL_SIZE_NM),
                str(step_px[1] * PIXEL_SIZE_NM),
                "1",
                "1",
            ],
            "unidirectional": False,
        },
    }
    params.update(overrides)
    return params


def _diamond_setup():
    """45-degree rotated square (spacing 12 px); scan = one fundamental domain."""
    half = 12.0 / np.sqrt(2)
    lattice = Lattice(
        a1=(half, half), a2=(-half, half), offset=(3.3, 5.1)
    )
    conventional = 12.0 * np.sqrt(2)  # axis-aligned repeat
    nx_s, ny_s = 12, 6
    step_px = (conventional / nx_s, conventional / 2 / ny_s)
    return lattice, nx_s, ny_s, step_px


def _reconstruction_accuracy(result):
    """Compare the gridded image with the specimen at each output pixel."""
    image = result.data[0, 0, 0, 0]
    diag = result.recon_diagnostics
    origin = diag["output_origin_px"]
    step_x, step_y = diag["step_px"]
    ys, xs = np.mgrid[0 : image.shape[0], 0 : image.shape[1]]
    truth = _sample_field(origin[0] + xs * step_x, origin[1] + ys * step_y)

    crop_y = max(2, int(0.12 * image.shape[0]))
    crop_x = max(2, int(0.12 * image.shape[1]))
    inner = np.zeros(image.shape, dtype=bool)
    inner[crop_y:-crop_y, crop_x:-crop_x] = True
    valid = inner & np.isfinite(image)
    assert valid.sum() > 100
    error = image[valid] - truth[valid]
    return float(np.sqrt(np.mean(error**2)) / np.std(truth[valid]))


class TestGeneralLatticeReconstruction:
    def _run(self, lattice, nx_s, ny_s, step_px, orientation=("x", 1, 1),
             period_scale=12.0, **param_overrides):
        frames = _simulate_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160), orientation=orientation
        )
        data_obj = InMemoryStackWrapper(
            name="general", dataset_name="det", data=frames, attrs={}
        )
        params = _params(nx_s, ny_s, step_px, period_scale, **param_overrides)
        return MonalisaReconstructor().process(data_obj, params)

    def test_diamond_lattice_reconstructs_the_specimen(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        result = self._run(lattice, nx_s, ny_s, step_px)

        assert result.spots is not None
        assert result.recon_diagnostics["pattern_geometry"] == "general"
        assert result.recon_diagnostics["coverage"] > 0.9
        # The P x P/2 scan is exactly one fundamental domain of the diamond
        # (area == the primitive cell): every sample position visited once.
        assert result.recon_diagnostics["scan_cell_ratio"] == pytest.approx(
            1.0, rel=0.05
        )
        assert _reconstruction_accuracy(result) < 0.06

    def test_hexagonal_lattice_reconstructs_the_specimen(self):
        spacing = 13.0
        lattice = Lattice.hexagonal(spacing, offset=(4.0, 2.5))
        nx_s, ny_s = 10, 8
        step_px = (spacing / nx_s, spacing * np.sqrt(3) / 2 / ny_s)
        result = self._run(
            lattice, nx_s, ny_s, step_px, period_scale=13.0
        )
        assert result.spots is not None
        assert result.recon_diagnostics["coverage"] > 0.9
        assert result.recon_diagnostics["scan_cell_ratio"] == pytest.approx(
            1.0, rel=0.05
        )
        assert _reconstruction_accuracy(result) < 0.06

    def test_flipped_scan_direction_is_recovered(self):
        """The TV orientation search must find a negative slow axis (the real
        diamond dataset scans Y in the negative direction)."""
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        result = self._run(lattice, nx_s, ny_s, step_px, orientation=("x", 1, -1))
        orientation = result.recon_diagnostics["scan_orientation"]
        assert tuple(orientation) == ("x", 1, -1)
        assert _reconstruction_accuracy(result) < 0.06

    def test_rectangular_grid_through_forced_general_path(self):
        """An axis-aligned grid through the scatter path must reconstruct the
        specimen too — the general machinery contains the rectangular case."""
        lattice = Lattice.rectangular(11.0, 11.0, 5.3, 4.7)
        nx_s = ny_s = 8
        step_px = (11.0 / nx_s, 11.0 / ny_s)
        result = self._run(
            lattice, nx_s, ny_s, step_px,
            period_scale=11.0,
            fast_gauss_pattern_geometry="General lattice",
        )
        assert result.spots is not None
        assert _reconstruction_accuracy(result) < 0.06

    def test_spot_intensities_match_specimen_at_spot_positions(self):
        """The pre-gridding cloud is self-describing: each spot's intensity is
        the specimen at its position (up to extraction error)."""
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        result = self._run(lattice, nx_s, ny_s, step_px)
        spots = result.spots
        positions = spots.positions_px
        intensities = spots.intensities[0]

        interior = (
            (positions[:, 0] > 15) & (positions[:, 0] < 145)
            & (positions[:, 1] > 15) & (positions[:, 1] < 145)
        )
        assert interior.sum() > 200
        truth = _sample_field(positions[interior, 0], positions[interior, 1])
        error = intensities[interior] - truth
        assert float(np.sqrt(np.mean(error**2))) < 0.03 * float(np.mean(truth))

    def test_auto_mode_keeps_rectangular_data_on_the_legacy_path(self):
        lattice = Lattice.rectangular(11.0, 11.0, 5.3, 4.7)
        nx_s = ny_s = 8
        step_px = (11.0 / nx_s, 11.0 / ny_s)
        frames = _simulate_acquisition(lattice, nx_s, ny_s, step_px, (160, 160))
        data_obj = InMemoryStackWrapper(
            name="rect", dataset_name="det", data=frames, attrs={}
        )
        params = _params(
            nx_s, ny_s, step_px, 11.0,
            row_offset=4.7, col_offset=5.3,
        )
        auto = MonalisaReconstructor().process(data_obj, params)
        forced = MonalisaReconstructor().process(
            data_obj,
            dict(params, fast_gauss_pattern_geometry="Rectangular grid"),
        )
        assert auto.spots is None
        np.testing.assert_array_equal(auto.data, forced.data)

    def test_sweep_composes_with_the_general_path(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_acquisition(lattice, nx_s, ny_s, step_px, (160, 160))
        data_obj = InMemoryStackWrapper(
            name="sweep-general", dataset_name="det", data=frames, attrs={}
        )
        params = _params(
            nx_s, ny_s, step_px, 12.0,
            sweep_enabled=True,
            sweep_parameter="Pinhole radius (×σ)",
            sweep_values_text="1.0, 2.0",
        )
        result = MonalisaReconstructor().process(data_obj, params)
        assert isinstance(result, MonalisaSweepResult)
        assert result.data.shape[0] == 2

    def test_missing_pixel_size_raises(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        with pytest.raises(ValueError, match="pixel size"):
            self._run(lattice, nx_s, ny_s, step_px, pixel_size_nm=None)

    def test_forced_general_without_pattern_raises(self):
        rng = np.random.default_rng(4)
        frames = rng.normal(100, 3, (16, 80, 80))
        data_obj = InMemoryStackWrapper(
            name="blank", dataset_name="det", data=frames, attrs={}
        )
        params = _params(
            4, 4, (2.0, 2.0), 11.0,
            fast_gauss_pattern_geometry="General lattice",
        )
        with pytest.raises(ValueError, match="no .*lattice was detected"):
            MonalisaReconstructor().process(data_obj, params)


def _ism_sample_field(x, y):
    """Specimen with structure near the emission-spot scale, so reassignment
    quality is actually measurable (the smooth field barely blurs)."""
    return _sample_field(x, y) + 25.0 * np.sin(
        2 * np.pi * np.asarray(x) / 10.7
    ) * np.cos(2 * np.pi * np.asarray(y) / 9.3)


def _simulate_ism_acquisition(
    lattice: Lattice,
    nx_s: int,
    ny_s: int,
    step_px: tuple[float, float],
    shape: tuple[int, int],
    alpha_true: float = 0.5,
    spot_sigma: float = 2.4,
    orientation=("x", 1, 1),
    background: float = 0.0,
):
    """Render the ISM forward model: the camera pixel at offset ``d`` from a
    focus sees the specimen at ``alpha_true * d`` beside the excitation spot,
    weighted by the emission-spot envelope."""
    rows, cols = shape
    margin = 3 * spot_sigma
    foci_x, foci_y = lattice.points_in_frame(rows, cols, margin=margin)
    offsets = scan_offsets_px(nx_s, ny_s, step_px[0], step_px[1], orientation)

    frames = np.full((nx_s * ny_s, rows, cols), background, dtype=np.float64)
    half = int(np.ceil(margin))
    for cx, cy in zip(foci_x, foci_y):
        x1 = max(0, int(np.floor(cx - half)))
        x2 = min(cols, int(np.ceil(cx + half)) + 1)
        y1 = max(0, int(np.floor(cy - half)))
        y2 = min(rows, int(np.ceil(cy + half)) + 1)
        if x1 >= x2 or y1 >= y2:
            continue
        ys, xs = np.mgrid[y1:y2, x1:x2].astype(float)
        dx = xs - cx
        dy = ys - cy
        envelope = np.exp(-((dx**2) + (dy**2)) / (2 * spot_sigma**2))
        window = (slice(y1, y2), slice(x1, x2))
        for index, offset in enumerate(offsets):
            specimen = _ism_sample_field(
                cx + offset[0] + alpha_true * dx,
                cy + offset[1] + alpha_true * dy,
            )
            frames[index][window] += envelope * specimen
    return frames


def _affine_accuracy(result):
    """Relative RMS after an affine (gain + offset) fit to the specimen.

    ISM is linear in the specimen up to a collection-efficiency gain and the
    unsubtracted background offset, so the fit removes exactly those two
    nuisance parameters before comparing structure.
    """
    image = result.data[0, 0, 0, 0]
    diag = result.recon_diagnostics
    origin = diag["output_origin_px"]
    step_x, step_y = diag["step_px"]
    ys, xs = np.mgrid[0 : image.shape[0], 0 : image.shape[1]]
    truth = _ism_sample_field(origin[0] + xs * step_x, origin[1] + ys * step_y)

    crop_y = max(2, int(0.12 * image.shape[0]))
    crop_x = max(2, int(0.12 * image.shape[1]))
    inner = np.zeros(image.shape, dtype=bool)
    inner[crop_y:-crop_y, crop_x:-crop_x] = True
    valid = inner & np.isfinite(image)
    assert valid.sum() > 100
    design = np.column_stack([truth[valid], np.ones(valid.sum())])
    coefficients, _, _, _ = np.linalg.lstsq(design, image[valid], rcond=None)
    fitted = design @ coefficients
    return float(
        np.sqrt(np.mean((image[valid] - fitted) ** 2))
        / (abs(coefficients[0]) * np.std(truth[valid]))
    )


class TestEnhancedConfocalISM:
    def _run(self, frames, nx_s, ny_s, step_px, **overrides):
        data_obj = InMemoryStackWrapper(
            name="ism", dataset_name="det", data=frames, attrs={}
        )
        settings = dict(
            reconstruction_method="Enhanced confocal (ISM)",
            fast_gauss_gaussian_sigma_px=2.4,
            ism_reassignment_factor=0.5,
        )
        settings.update(overrides)
        params = _params(nx_s, ny_s, step_px, 12.0, **settings)
        return MonalisaReconstructor().process(data_obj, params)

    def test_ism_reconstructs_the_specimen_on_a_diamond(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_ism_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160), alpha_true=0.5
        )
        result = self._run(frames, nx_s, ny_s, step_px)

        assert result.recon_diagnostics["pattern_geometry"] == "ism"
        assert result.recon_diagnostics["ism_reassignment_factor"] == 0.5
        # The raster spans the full (alpha = 1) footprint reach so factor
        # sweeps align; at alpha = 0.5 its border rim is honestly NaN.
        assert result.recon_diagnostics["coverage"] > 0.9
        assert result.spots is None
        assert _affine_accuracy(result) < 0.08

    def test_matched_alpha_beats_confocal_binning(self):
        """Reassigning with the generator's true factor must be sharper than
        alpha = 0 (pure open-pinhole binning) on the same frames."""
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_ism_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160), alpha_true=0.5
        )
        matched = self._run(frames, nx_s, ny_s, step_px)
        binned = self._run(
            frames, nx_s, ny_s, step_px, ism_reassignment_factor=0.0
        )
        assert _affine_accuracy(matched) < _affine_accuracy(binned)

    def test_ism_on_rectangular_grid(self):
        lattice = Lattice.rectangular(11.0, 11.0, 5.3, 4.7)
        nx_s = ny_s = 8
        step_px = (11.0 / nx_s, 11.0 / ny_s)
        frames = _simulate_ism_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160), alpha_true=0.5
        )
        result = self._run(frames, nx_s, ny_s, step_px)
        assert _affine_accuracy(result) < 0.08

    def test_ism_factor_sweep(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_ism_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160)
        )
        result = self._run(
            frames, nx_s, ny_s, step_px,
            sweep_enabled=True,
            sweep_parameter="ISM reassignment factor",
            sweep_values_text="0.3, 0.5",
        )
        assert isinstance(result, MonalisaSweepResult)
        assert result.data.shape[0] == 2
        assert result.sweep_parameter_label == "ISM reassignment factor"

    def test_ism_sweep_parameter_rejected_for_fast_gauss(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_acquisition(lattice, nx_s, ny_s, step_px, (160, 160))
        data_obj = InMemoryStackWrapper(
            name="wrong-sweep", dataset_name="det", data=frames, attrs={}
        )
        params = _params(
            nx_s, ny_s, step_px, 12.0,
            sweep_enabled=True,
            sweep_parameter="ISM reassignment factor",
            sweep_values_text="0.3, 0.5",
        )
        with pytest.raises(ValueError, match="only applies"):
            MonalisaReconstructor().process(data_obj, params)

    def test_invalid_factor_rejected(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_ism_acquisition(
            lattice, nx_s, ny_s, step_px, (160, 160)
        )
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            self._run(frames, nx_s, ny_s, step_px, ism_reassignment_factor=1.5)


class TestSpotCloudResult:
    def test_save_writes_csv_next_to_tiff(self, tmp_path):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        frames = _simulate_acquisition(lattice, nx_s, ny_s, step_px, (160, 160))
        data_obj = InMemoryStackWrapper(
            name="csv", dataset_name="det", data=frames, attrs={}
        )
        result = MonalisaReconstructor().process(
            data_obj, _params(nx_s, ny_s, step_px, 12.0)
        )
        out = tmp_path / "diamond.tiff"
        result.save(out)

        csv_path = tmp_path / "diamond_spots.csv"
        assert out.exists() and csv_path.exists()
        lines = csv_path.read_text().splitlines()
        assert lines[0] == "timepoint,frame,focus,x_px,y_px,x_nm,y_nm,intensity"
        assert len(lines) - 1 == result.spots.num_spots

    def test_shape_validation(self):
        with pytest.raises(ValueError, match="Inconsistent"):
            MonalisaSpotCloud(
                positions_px=np.zeros((3, 2)),
                intensities=np.zeros((1, 2)),
                frame_indices=np.zeros(3, dtype=int),
                focus_indices=np.zeros(3, dtype=int),
                pixel_size_nm=100.0,
            )


class TestLatticeReconPrimitives:
    def test_splat_of_constant_field_is_constant(self):
        rng = np.random.default_rng(2)
        positions = rng.uniform(2, 18, size=(4000, 2))
        assembly = assemble_image(
            positions, np.full(4000, 7.5), pitch=(1.0, 1.0)
        )
        finite = np.isfinite(assembly.image)
        assert finite.mean() > 0.9
        np.testing.assert_allclose(assembly.image[finite], 7.5)

    def test_scan_offsets_fast_axis_and_signs(self):
        offsets = scan_offsets_px(3, 2, 1.0, 10.0, ("x", 1, -1))
        assert offsets.shape == (6, 2)
        np.testing.assert_allclose(offsets[:3, 0], [0, 1, 2])  # fast x
        np.testing.assert_allclose(offsets[:3, 1], 0)
        np.testing.assert_allclose(offsets[3:, 1], -10.0)  # slow y, negative

        swapped = scan_offsets_px(3, 2, 1.0, 10.0, ("y", 1, 1))
        np.testing.assert_allclose(swapped[:2, 1], [0, 10.0])  # fast y
        np.testing.assert_allclose(swapped[:2, 0], 0)

    def test_choose_orientation_recovers_generator(self):
        lattice, nx_s, ny_s, step_px = _diamond_setup()
        foci_x, foci_y = lattice.points_in_frame(160, 160)
        foci_xy = np.column_stack([foci_x, foci_y])
        truth = ("y", -1, 1)
        offsets = scan_offsets_px(nx_s, ny_s, step_px[0], step_px[1], truth)
        positions = sample_positions(foci_xy, offsets)
        values = _sample_field(positions[:, 0], positions[:, 1])
        amplitudes = values.reshape(offsets.shape[0], -1)
        assert choose_orientation(
            foci_xy, amplitudes, nx_s, ny_s, step_px
        ) == truth
