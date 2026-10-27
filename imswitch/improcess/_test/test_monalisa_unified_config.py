"""Unified Gaussian MoNaLISA configuration and calibration seams."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.localization_settings import (
    LocalizationSettings,
    PeriodPrior,
)
from imswitch.improcess.reconstructors.monalisa.params import (
    LOCALIZATION_REFINEMENT_FAST,
    LOCALIZATION_REFINEMENT_FULL,
    PRESET_FAST,
    PRESET_FULL,
    apply_preset,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry
from imswitch.improcess.reconstructors.monalisa.spot_settings import (
    fwhm_to_sigma_px,
    sigma_px_to_fwhm,
)


def test_fast_and_full_presets_only_change_processing_options():
    base = default_params()
    fast = apply_preset(base, PRESET_FAST)
    full = apply_preset(base, PRESET_FULL)

    for key in ("localization_mode", "orientation_mode", "spot_width_mode", "spot_fwhm"):
        assert fast[key] == full[key] == base[key]
    assert fast["intensity_correction"] is False
    assert fast["cell_offsets"] is False
    assert fast["reassignment"] == "off"
    assert fast["localization_refinement"] == LOCALIZATION_REFINEMENT_FAST
    assert full["intensity_correction"] is True
    assert full["cell_offsets"] is True
    assert full["reassignment"] == "auto"
    assert full["localization_refinement"] == LOCALIZATION_REFINEMENT_FULL
    assert fast["sharpen"] is full["sharpen"] is False
    assert fast["relocalize_each_data_item"] is False


def test_period_prior_constrains_search_without_becoming_a_fixed_period():
    settings = LocalizationSettings(
        period_prior=PeriodPrior(
            enabled=True,
            expected_x_px=10.0,
            expected_y_px=12.0,
            tolerance_fraction=0.20,
        )
    )
    assert settings.period_band_px() == pytest.approx((8.0, 14.4))


def test_spot_fwhm_nm_and_px_are_the_same_internal_sigma():
    pixel_nm = 77.0
    fwhm_nm = 220.0
    fwhm_px = fwhm_nm / pixel_nm
    sigma_from_nm = fwhm_to_sigma_px(fwhm_nm, "nm", pixel_nm)
    sigma_from_px = fwhm_to_sigma_px(fwhm_px, "px", pixel_nm)
    assert sigma_from_nm == pytest.approx(sigma_from_px)
    assert sigma_px_to_fwhm(sigma_from_nm, "nm", pixel_nm) == pytest.approx(fwhm_nm)


def test_manual_localization_and_fwhm_bypass_automatic_spot_calibration(monkeypatch):
    # If an explicit localization + FWHM is supplied, the Fast calibration path
    # must not invoke the automatic calibration-image/spot-width machinery.
    import imswitch.improcess.reconstructors.monalisa.pipeline as pipeline

    monkeypatch.setattr(
        pipeline,
        "choose_calibration_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("automatic calibration should not run")
        ),
    )
    localization = PatternLocalization.manual_grid(8.0, 8.0, 2.0, 3.0)
    params = PipelineParams(
        spot_sigma_px=1.5,
        background="constant",
        frame_gain="off",
        cell_offsets="off",
        reassignment="off",
        variance_weighting=False,
    )
    index = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=int)
    geometry = prepare_geometry(
        np.zeros((32, 32), dtype=float),
        np.diag([4.0, 4.0]),
        index,
        params,
        localization=localization,
    )
    assert geometry.spot_model.shared_sigma == pytest.approx(1.5)
    assert geometry.diagnostics["calibration_image"] == "configured"


def test_manual_half_stagger_uses_an_exact_bravais_lattice():
    localization = PatternLocalization.manual_grid(10.0, 12.0, 1.0, 2.0, stagger_axis="x", stagger=0.5)
    assert localization.motif == ((0.0, 0.0),)
    np.testing.assert_allclose(localization.lattice.matrix, [[10.0, 5.0], [0.0, 12.0]])
    x, y = localization.points_in_frame(40, 40)
    # Consecutive physical rows are 12 px apart and alternate by 5 px in X.
    row0 = np.sort(x[np.isclose(y, 2.0)])
    row1 = np.sort(x[np.isclose(y, 14.0)])
    assert row0.size and row1.size
    assert ((row1[0] - row0[0]) % 10.0) == pytest.approx(5.0)


def test_manual_general_stagger_keeps_alternating_motif():
    localization = PatternLocalization.manual_grid(10.0, 12.0, 1.0, 2.0, stagger_axis="x", stagger=0.25)
    assert len(localization.motif) == 2
    x, y = localization.points_in_frame(40, 40)
    row0 = np.sort(x[np.isclose(y, 2.0)])
    row1 = np.sort(x[np.isclose(y, 14.0)])
    assert row0.size and row1.size
    assert ((row1[0] - row0[0]) % 10.0) == pytest.approx(2.5)


@pytest.mark.parametrize("axis, stagger", [(None, 0.0), ("x", 0.5), ("y", -0.5), ("x", 0.25)])
def test_pattern_localization_round_trip_and_manual_controls(axis, stagger):
    localization = PatternLocalization.manual_grid(
        10.0, 12.0, 1.5, 2.5, stagger_axis=axis, stagger=stagger
    )
    restored = PatternLocalization.from_dict(localization.to_dict())
    assert restored is not None
    np.testing.assert_allclose(restored.lattice.matrix, localization.lattice.matrix)
    np.testing.assert_allclose(restored.lattice.offset, localization.lattice.offset)
    assert restored.motif == localization.motif
    controls = restored.manual_grid_parameters()
    assert controls is not None
    expected_axis = "None" if axis is None else axis.upper()
    assert controls["stagger_axis"] == expected_axis
    assert controls["stagger"] == pytest.approx(stagger)


def test_auto_rectangular_basis_swap_maps_to_manual_controls():
    # General lattice detection is free to store the equivalent Y basis first.
    # Manual controls must still receive X/Y periods and folded offsets.
    from imswitch.improcess.reconstructors.monalisa.lattice import Lattice

    localization = PatternLocalization.from_lattice(
        Lattice(a1=(0.01, 11.0), a2=(10.0, 0.01), offset=(-7.5, 3.0))
    )
    controls = localization.manual_grid_parameters()
    assert controls is not None
    assert controls["period_x"] == pytest.approx(10.0, rel=2e-3)
    assert controls["period_y"] == pytest.approx(11.0, rel=2e-3)
    assert controls["stagger_axis"] == "None"


def test_fast_auto_localization_skips_spot_refinement(monkeypatch):
    import imswitch.improcess.reconstructors.monalisa.pipeline as pipeline

    rows = cols = 64
    yy, xx = np.mgrid[:rows, :cols]
    image = np.zeros((rows, cols), dtype=float)
    for y in range(4, rows, 10):
        for x in range(3, cols, 10):
            image += np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.3**2))

    monkeypatch.setattr(
        pipeline,
        "calibrate_spot_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Fast Auto localization must not refine individual foci")
        ),
    )
    params = PipelineParams(
        spot_sigma_px=1.3,
        localization_refinement="fast",
        localization_settings=LocalizationSettings(
            period_prior=PeriodPrior(
                enabled=True,
                expected_x_px=10.0,
                expected_y_px=10.0,
                tolerance_fraction=0.2,
            )
        ),
    )
    finding = pipeline.find_localization(image, None, params)
    assert finding.calibration.name == "mean-fast"
    assert finding.spot.shared_sigma == pytest.approx(1.3)
