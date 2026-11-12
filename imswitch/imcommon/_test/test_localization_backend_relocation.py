"""Patch-2 relocation tests: old entrypoints remain aliases to shared numerics."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import (
    PatternPrior, localize_pattern,
)
from imswitch.imcommon.algorithms.grid_localization.finite.ulenses import (
    GridLocalizationError, GridLocalizationResult, localize_grid,
    refine_grid_foci, search_missing_grid_foci,
)
from imswitch.imcommon.algorithms.grid_localization.periodic import (
    Lattice, PatternLocalization, detect_lattice,
)
from imswitch.imcommon.algorithms.grid_localization.legacy import monalisa as old_monalisa
from imswitch.imcommon.algorithms.grid_localization.legacy import ulenses as old_ulenses
from imswitch.imcontrol.model import ulenses_localizer as control_api
from imswitch.improcess.reconstructors.monalisa import (
    calibration as process_calibration,
    lattice as process_lattice,
    localizer as process_legacy,
)


def _periodic_image(shape=(192, 192), xp=11., yp=13., xo=3.4, yo=6.1):
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    return (np.cos(np.pi * (xx - xo) / xp) ** 6
            * np.cos(np.pi * (yy - yo) / yp) ** 6)


def _finite_image(shape=(160, 200), columns=5, rows=4):
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    image = np.zeros(shape)
    for j in range(rows):
        for i in range(columns):
            x, y = 35 + i * 26 + j * 2, 25 + i + j * 26
            image += np.exp(-((xx-x)**2 + (yy-y)**2) / (2 * 1.2**2))
    return image


def test_old_periodic_imports_are_exact_aliases():
    assert process_lattice.Lattice is Lattice
    assert process_lattice.detect_lattice is detect_lattice
    assert process_calibration.PatternLocalization is PatternLocalization
    assert process_legacy.LocalizationResult is old_monalisa.LocalizationResult
    assert process_legacy.localizer is old_monalisa.localizer
    assert process_legacy.localization_from_pattern is old_monalisa.localization_from_pattern


def test_old_finite_imports_are_exact_aliases():
    assert control_api.GridLocalizationResult is GridLocalizationResult
    assert control_api.GridLocalizationError is GridLocalizationError
    assert control_api.localize_grid.__wrapped__ is localize_grid
    assert control_api.refine_grid_foci is refine_grid_foci
    assert control_api.search_missing_grid_foci is search_missing_grid_foci
    assert control_api.LocalizationResult is old_ulenses.LocalizationResult
    assert control_api.localizer is old_ulenses.localizer


def test_periodic_shared_dispatch_is_numerically_identical_to_old_detector():
    image = _periodic_image()
    direct = process_lattice.detect_lattice(image)
    shared = localize_pattern(image, strategy="periodic")
    assert shared.strategy == "periodic"
    assert shared.geometry.site_indices_ij is None
    np.testing.assert_array_equal(shared.geometry.basis_xy, direct.matrix)
    np.testing.assert_array_equal(shared.geometry.origin_xy, direct.offset)
    expected = direct.points_in_frame(*image.shape)
    actual = PatternLocalization(
        lattice=Lattice(
            a1=tuple(shared.geometry.basis_xy[:, 0]),
            a2=tuple(shared.geometry.basis_xy[:, 1]),
            offset=shared.geometry.origin_xy,
        )
    ).points_in_frame(*image.shape)
    np.testing.assert_array_equal(actual, expected)


def test_finite_shared_dispatch_preserves_grid_indices_and_measurements():
    image = _finite_image()
    opts = dict(xp_guess=26., yp_guess=26., n_cols=5, n_rows=4)
    direct = control_api.localize_grid(image, **opts)
    shared = localize_pattern(image, strategy="finite", options=opts)
    assert shared.strategy == "finite"
    np.testing.assert_array_equal(shared.geometry.site_indices_ij, direct.logical_indices)
    np.testing.assert_array_equal(shared.predicted_positions_xy, direct.grid_points_rc[:, ::-1])
    np.testing.assert_array_equal(shared.matched_mask, direct.matched_mask)
    np.testing.assert_array_equal(shared.measured_positions_xy,
                                  direct.measured_points_rc[:, ::-1])
    assert shared.diagnostics == direct.diagnostics


def test_periodic_prior_guides_search_without_setting_finite_extent():
    image = _periodic_image()
    prior = PatternPrior(expected_period_x_px=11, expected_period_y_px=13,
                         period_tolerance_fraction=0.30,
                         modality_id="488-multifoci",
                         finite_shape_xy=(9, 7))
    result = localize_pattern(image, strategy="periodic", prior=prior)
    assert result.geometry.extent == "periodic"
    assert result.geometry.site_indices_ij is None
    assert result.prior_usage["expected_period_x_px"] == "applied"
    assert result.prior_usage["period_tolerance_fraction"] == "applied"
    assert result.prior_usage["finite_shape_xy"] == "ignored"
    assert result.prior_usage["modality_id"] == "ignored"


def test_finite_prior_supplies_grid_shape_without_changing_direct_default():
    image = _finite_image()
    prior = PatternPrior(expected_period_x_px=26., expected_period_y_px=26.,
                         finite_shape_xy=(5, 4))
    result = localize_pattern(image, strategy="finite", prior=prior)
    assert result.geometry.finite_shape_xy == (5, 4)
    assert result.prior_usage["finite_shape_xy"] == "applied"
    assert result.prior_usage["expected_period_y_px"] == "applied"
    assert result.matched_mask.sum() == 20


def test_finite_sparse_indices_not_silently_inferred_as_full_grid():
    image = _finite_image()
    prior = PatternPrior(finite_site_indices=np.array([[0, 0], [3, 2]]))
    with pytest.raises(NotImplementedError, match="Exact finite_site_indices"):
        localize_pattern(image, strategy="finite", prior=prior)


def test_conflicting_prior_and_explicit_backend_setting_fails_loudly():
    image = _finite_image()
    with pytest.raises(ValueError, match="Conflicting 'n_cols'"):
        localize_pattern(image, strategy="finite", prior=PatternPrior(finite_shape_xy=(5, 4)),
                         options={"n_cols": 7})


def test_both_unmodified_legacy_projection_fits_are_available():
    image = _periodic_image(xp=8.0, yp=11.0, xo=2.3, yo=4.1)
    for old, new in ((process_legacy.localizer, old_monalisa.localizer),
                     (control_api.localizer, old_ulenses.localizer)):
        a, b = old(image.copy(), xp_guess=8.0, yp_guess=11.0), new(image.copy(), xp_guess=8.0, yp_guess=11.0)
        assert a == b
        assert a.xp == pytest.approx(8.0, abs=0.2)
        assert a.yp == pytest.approx(11.0, abs=0.2)
