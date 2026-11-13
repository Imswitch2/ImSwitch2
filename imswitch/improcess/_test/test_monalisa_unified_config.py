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
    PRESET_LEGACY,
    apply_preset,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry
from imswitch.improcess.reconstructors.monalisa.spot_settings import (
    fwhm_to_sigma_px,
    sigma_px_to_fwhm,
)


def test_fast_and_full_presets_only_change_optional_processing():
    base = default_params()
    fast = apply_preset(base, PRESET_FAST)
    full = apply_preset(base, PRESET_FULL)
    for key in (
        "localization_mode", "localization_refinement", "orientation_mode",
        "spot_fwhm", "spot_width_mode", "estimator", "footprint_shape",
        "intensity_correction", "cell_offsets", "variance_weighting",
        "device", "placement_mode",
    ):
        assert fast[key] == full[key] == base[key], key
    assert fast["reassignment"] == "off"
    assert full["reassignment"] == "auto"
    assert fast["sharpen"] is full["sharpen"] is False
    assert "relocalize_each_data_item" not in fast


def test_legacy_preset_keeps_general_gaussian_stages_inert():
    base = default_params()
    legacy = apply_preset(base, PRESET_LEGACY)
    assert legacy["preset"] == PRESET_LEGACY
    assert legacy["device"] == "CPU"
    assert legacy["spot_width_mode"] == "Manual/default"
    assert legacy["intensity_correction"] is False
    assert legacy["cell_offsets"] is False
    assert legacy["reassignment"] == "off"
    assert legacy["variance_weighting"] is False


def test_legacy_pattern_conversion_accepts_rectangular_and_rejects_staggered():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor

    rectangular = PatternLocalization.manual_grid(10.0, 12.0, 1.5, 2.5)
    assert MonalisaReconstructor._legacy_pattern_from_localization(rectangular) == pytest.approx(
        (2.5, 1.5, 12.0, 10.0)
    )

    staggered = PatternLocalization.manual_grid(10.0, 12.0, 1.5, 2.5, stagger_axis="x", stagger=0.5)
    with pytest.raises(ValueError, match="axis-aligned rectangular"):
        MonalisaReconstructor._legacy_pattern_from_localization(staggered)


def test_legacy_mode_delegates_to_classic_adapter_without_loading_dll():
    from types import SimpleNamespace

    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    from imswitch.improcess.reconstructors.monalisa.scan_params import default_scan_params

    class FakeLegacyAdapter:
        def __init__(self):
            self.params = None

        def process(self, data_obj, params, context=None):
            self.params = dict(params)
            return SimpleNamespace(name=data_obj.name, metadata={})

    reconstructor = MonalisaReconstructor()
    adapter = FakeLegacyAdapter()
    reconstructor._legacy_adapter = adapter
    params = apply_preset(default_params(), PRESET_LEGACY)
    params.update(
        localization_mode="Manual",
        manual_period_x_px=10.0,
        manual_period_y_px=12.0,
        manual_offset_x_px=1.5,
        manual_offset_y_px=2.5,
        spot_fwhm=250.0,
        spot_fwhm_unit="nm",
        legacy_bleaching_correction=True,
        legacy_bg_modelling="Gaussian",
        legacy_bg_gaussian_size_nm=900.0,
        scan_params=default_scan_params(),
    )
    source = SimpleNamespace(name="raw", attrs={}, dataPath="")

    result = reconstructor.process(source, params)

    assert adapter.params["pattern"] == pytest.approx((2.5, 1.5, 12.0, 10.0))
    assert adapter.params["psf_fwhm_nm"] == pytest.approx([250.0])
    assert adapter.params["bg_modelling"] == "Gaussian"
    assert adapter.params["bg_gaussian_size_nm"] == pytest.approx(900.0)
    assert adapter.params["bleaching_correction"] is True
    assert result.params["preset"] == PRESET_LEGACY
    assert result.metadata["monalisa_calibration"]["preset"] == PRESET_LEGACY


def test_legacy_mode_is_rejected_by_current_live_session():
    from imswitch.improcess.reconstructors.monalisa.live_session import MonalisaLiveSession

    params = apply_preset(default_params(), PRESET_LEGACY)
    with pytest.raises(ValueError, match="offline-only"):
        MonalisaLiveSession._validate_unified_live_params(params)

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


def test_presets_preserve_full_asymmetric_reassignment():
    full = apply_preset(default_params(), PRESET_FULL)
    full.update(reassignment="fixed", reassignment_asymmetric=True,
                shift_factor_x=.35, shift_factor_y=.42)
    fast = apply_preset(full, PRESET_FAST)
    assert fast["full_options"]["reassignment_asymmetric"] is True
    back = apply_preset(fast, PRESET_FULL)
    assert back["reassignment_asymmetric"] is True
    assert (back["shift_factor_x"], back["shift_factor_y"]) == (.35, .42)


def test_reassignment_is_masked_to_the_scanned_camera_axis():
    from types import SimpleNamespace

    from imswitch.improcess.reconstructors.monalisa.pipeline import _shift_factor_to_use

    scan_index = np.column_stack([np.zeros(5), np.arange(5)])
    geometry = SimpleNamespace(
        params=PipelineParams(reassignment=0.4),
        shift_factor=None,
        step=np.eye(2),
    )
    assert _shift_factor_to_use(geometry, scan_index) == pytest.approx((0.0, 0.4))


def test_asymmetric_reassignment_is_masked_to_the_scanned_camera_axis():
    from types import SimpleNamespace

    from imswitch.improcess.reconstructors.monalisa.pipeline import _shift_factor_to_use

    scan_index = np.column_stack([np.arange(5), np.zeros(5)])
    geometry = SimpleNamespace(
        params=PipelineParams(reassignment=(0.2, 0.7)),
        shift_factor=None,
        step=np.eye(2),
    )
    assert _shift_factor_to_use(geometry, scan_index) == pytest.approx((0.2, 0.0))


def test_final_modes_preserve_streaming_admission_boundary():
    """The unified Session engine follows Fast, never Full or DLL-equivalent."""
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    from imswitch.improcess.reconstructors.monalisa.params import PRESET_DLL

    base = default_params()
    for mode in (PRESET_FULL, PRESET_DLL):
        supported, reason = MonalisaReconstructor.streaming_capability(
            {**base, "preset": mode}
        )
        assert supported is False
        assert reason
    supported, _ = MonalisaReconstructor.streaming_capability(base)
    assert supported is True  # Raw layout readiness remains a separate gate.


def test_unified_lattice_params_keep_scientific_fit_selection_for_streaming():
    """Session's geometry adapter must not revert Fast to the old Joint-only fit."""
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    reconstruction = MonalisaReconstructor()
    base = default_params()
    base.update(estimator="local", footprint_shape="circle",
                footprint_mode="fixed", footprint_size_px=7.0,
                spot_width_mode="Manual/default")
    converted = reconstruction.unified_lattice_params(base)
    assert converted["estimator"] == "local"
    assert converted["footprint_shape"] == "circle"
    assert converted["footprint_mode"] == "fixed"
    assert converted["footprint_size_px"] == pytest.approx(7.0)
    assert converted["frame_gain"] is True
    assert converted["cell_offsets"] is True
