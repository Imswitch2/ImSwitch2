"""Baseline/parity checks for opt-in host adapters; no caller is rewired."""

import numpy as np
import pytest

from imswitch.imcontrol.model.grid_localization_common_adapter import (
    from_common as ulenses_from_common,
    prior_from_grid_controls,
    to_common as ulenses_to_common,
)
from imswitch.imcontrol.model.ulenses_localizer import localize_grid
from imswitch.improcess.reconstructors.monalisa.localization_common_adapter import (
    from_common as monalisa_from_common,
    prior_from_settings,
    to_common as monalisa_to_common,
)
from imswitch.improcess.reconstructors.monalisa.localization_settings import (
    LocalizationSettings, PeriodPrior,
)
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice, detect_lattice
from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization


def test_shared_period_prior_translates_monalisa_enable_state_without_changing_settings():
    disabled = LocalizationSettings(period_prior=PeriodPrior(
        enabled=False, expected_x_px=11, expected_y_px=9))
    assert prior_from_settings(disabled).provided_fields == ()
    assert disabled.period_band_px() == (3., None)

    enabled = LocalizationSettings(period_prior=PeriodPrior(
        enabled=True, expected_x_px=11, expected_y_px=9, tolerance_fraction=0.15))
    prior = prior_from_settings(enabled)
    assert (prior.expected_period_x_px, prior.expected_period_y_px) == (11., 9.)
    assert prior.period_tolerance_fraction == 0.15
    assert enabled.period_band_px() == pytest.approx((7.65, 12.65))
    assert LocalizationSettings.from_dict(enabled.to_dict()).to_dict() == enabled.to_dict()


def test_ulenses_prior_respects_legacy_default_and_auto_stagger_mode():
    default = prior_from_grid_controls()
    assert default.expected_period_x_px == 10
    assert default.expected_period_y_px == 10
    assert default.period_tolerance_fraction == 0.25
    assert default.finite_shape_xy is None
    assert default.stagger_fraction is None  # auto is not a known half stagger

    no_period = prior_from_grid_controls(
        use_period_prior=False, xp_guess=99, yp_guess=111,
        n_cols=8, n_rows=7,
        stagger_mode="manual", stagger_axis="columns", stagger_fraction=0.4,
    )
    assert no_period.expected_period_x_px is None
    assert no_period.finite_shape_xy == (8, 7)
    assert no_period.stagger_axis == "y"
    assert no_period.stagger_fraction == 0.4


def test_periodic_detection_passes_unchanged_through_host_adapter():
    # Fast synthetic MoNaLISA periodic field: the proven FFT detector executes
    # directly; this patch does not change its settings, phase, or peak ranking.
    yy, xx = np.mgrid[:192, :192]
    image = np.cos(np.pi * (xx - 3.4) / 11.0) ** 6 * np.cos(np.pi * (yy - 6.1) / 13.0) ** 6
    detected = detect_lattice(image)
    assert detected.nearest_spacing() == pytest.approx(11., abs=0.1)
    original = PatternLocalization.from_lattice(detected, calibration="auto")
    restored = monalisa_from_common(monalisa_to_common(original))
    assert restored.to_dict() == original.to_dict()
    p_original = original.points_in_frame(192, 192)
    p_restored = restored.points_in_frame(192, 192)
    np.testing.assert_array_equal(p_original[0], p_restored[0])
    np.testing.assert_array_equal(p_original[1], p_restored[1])


def test_finite_detection_passes_unchanged_through_host_adapter():
    rows, cols = np.mgrid[:160, :200]
    image = np.zeros((160, 200), dtype=float)
    for j in range(4):
        for i in range(5):
            x = 35 + i * 26 + j * 2
            y = 25 + i * 1 + j * 26
            image += np.exp(-((rows-y)**2+(cols-x)**2) / (2 * 1.2**2))
    original = localize_grid(image, xp_guess=26.0, yp_guess=26.0, n_cols=5, n_rows=4)
    assert original.shape == (4, 5)
    assert original.matched_count == 20
    restored = ulenses_from_common(ulenses_to_common(original))
    np.testing.assert_array_equal(restored.logical_indices, original.logical_indices)
    np.testing.assert_allclose(restored.grid_points_rc, original.grid_points_rc, atol=0, rtol=0)
    np.testing.assert_array_equal(restored.matched_mask, original.matched_mask)
    np.testing.assert_allclose(restored.measured_points_rc, original.measured_points_rc,
                               atol=0, rtol=0, equal_nan=True)
    assert restored.diagnostics == original.diagnostics
