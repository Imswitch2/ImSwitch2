"""Patch-3 consumer migration: shared facade, old geometry/errors and controls."""

import inspect

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import localize_pattern
from imswitch.imcommon.algorithms.grid_localization.finite.ulenses import (
    GridLocalizationError,
    localize_grid as numeric_finite_grid,
)
from imswitch.imcommon.algorithms.grid_localization.periodic.lattice import (
    Lattice,
    detect_lattice,
)
from imswitch.imcontrol.model import ulenses_localizer as ulenses
from imswitch.improcess.reconstructors.monalisa import (
    localization_common_adapter as monalisa_adapter,
    pipeline,
)
from imswitch.improcess.reconstructors.monalisa.localization_settings import (
    LocalizationSettings,
    PeriodPrior,
)


def _periodic(shape=(128, 160), xp=11., yp=13., xo=3.4, yo=6.1):
    yy, xx = np.indices(shape)
    return np.cos(np.pi*(xx-xo)/xp)**6 * np.cos(np.pi*(yy-yo)/yp)**6


def _finite(shape=(135, 180), n_cols=5, n_rows=4,
            stagger_axis=None, stagger_fraction=0.0, missing=()):
    yy, xx = np.indices(shape)
    image = np.zeros(shape)
    for j in range(n_rows):
        for i in range(n_cols):
            if (i, j) in missing:
                continue
            x = 23 + 24*i + 1.5*j
            y = 20 + 0.5*i + 25*j
            if stagger_axis == "rows":
                x += (j % 2)*24*stagger_fraction
            if stagger_axis == "columns":
                y += (i % 2)*25*stagger_fraction
            image += np.exp(-((xx-x)**2+(yy-y)**2)/(2*1.2**2))
    return image


def test_periodic_pipeline_fast_calls_shared_and_matches_old_numeric(monkeypatch):
    image = _periodic()
    expected = detect_lattice(image)
    calls = []
    actual_api = monalisa_adapter.localize_pattern

    def track(image, **kwargs):
        calls.append(kwargs)
        return actual_api(image, **kwargs)

    monkeypatch.setattr(monalisa_adapter, "localize_pattern", track)
    params = pipeline.PipelineParams(
        localization_refinement="fast", spot_sigma_px=1.4,
    )
    result = pipeline.find_localization(image, params=params)
    assert len(calls) == 1
    assert calls[0]["strategy"] == "periodic"
    np.testing.assert_array_equal(result.lattice.matrix, expected.matrix)
    np.testing.assert_array_equal(result.lattice.offset, expected.offset)
    np.testing.assert_array_equal(result.points(*image.shape),
                                  expected.points_in_frame(*image.shape))
    assert result.calibration.name == "mean-fast"
    assert result.spot.diagnostics["mode"] == "configured"


def test_periodic_locked_lattice_bypasses_shared_detection(monkeypatch):
    image = _periodic()
    lattice = Lattice.rectangular(11., 13., 3.4, 6.1)

    def forbidden(*args, **kwargs):
        raise AssertionError("locked/manual localization must not detect")

    monkeypatch.setattr(monalisa_adapter, "localize_pattern", forbidden)
    result = pipeline.find_localization(
        image, params=pipeline.PipelineParams(
            localization_refinement="fast", spot_sigma_px=1.4),
        lattice=lattice,
    )
    assert result.lattice is lattice


def test_periodic_old_prior_band_and_explicit_override_preserved(monkeypatch):
    image = _periodic()
    settings = LocalizationSettings(period_prior=PeriodPrior(
        enabled=True, expected_x_px=11., expected_y_px=13., tolerance_fraction=0.2))
    seen = []
    original = monalisa_adapter.localize_pattern

    def inspect_dispatch(image, **kwargs):
        seen.append(kwargs)
        return original(image, **kwargs)

    monkeypatch.setattr(monalisa_adapter, "localize_pattern", inspect_dispatch)
    band = settings.period_band_px()
    found = monalisa_adapter.detect_lattice_shared(image, settings=settings, band=band)
    numeric = detect_lattice(image, min_period=band[0], max_period=band[1],
                             num_candidate_peaks=settings.num_candidate_peaks,
                             min_peak_significance=settings.min_peak_significance)
    np.testing.assert_array_equal(found.matrix, numeric.matrix)
    np.testing.assert_array_equal(found.offset, numeric.offset)
    assert seen[-1]["prior"].expected_period_x_px == 11.
    assert "min_period" not in seen[-1]["options"]

    override = (9., 15.)
    overridden = monalisa_adapter.detect_lattice_shared(
        image, settings=settings, band=override)
    numeric_override = detect_lattice(
        image, min_period=override[0], max_period=override[1],
        num_candidate_peaks=settings.num_candidate_peaks,
        min_peak_significance=settings.min_peak_significance)
    np.testing.assert_array_equal(overridden.matrix, numeric_override.matrix)
    assert seen[-1].get("prior") is None
    assert seen[-1]["options"]["min_period"] == 9.


def test_finite_compatibility_uses_shared_dispatch_and_preserves_results(monkeypatch):
    image = _finite(missing=((2, 1),))
    opts = dict(xp_guess=24.2, yp_guess=25., n_cols=5, n_rows=4,
                stagger_mode="none", use_period_prior=True)
    original = numeric_finite_grid(image, **opts)
    seen = []
    original_api = ulenses.localize_pattern

    def track(image, **kwargs):
        seen.append(kwargs)
        return original_api(image, **kwargs)

    monkeypatch.setattr(ulenses, "localize_pattern", track)
    got = ulenses.localize_grid(image, **opts)
    assert len(seen) == 1
    assert seen[0]["strategy"] == "finite"
    assert seen[0]["prior"].finite_shape_xy == (5, 4)
    assert seen[0]["options"] == opts
    assert type(got) is type(original)
    np.testing.assert_array_equal(got.logical_indices, original.logical_indices)
    np.testing.assert_array_equal(got.grid_points_rc, original.grid_points_rc)
    np.testing.assert_array_equal(got.measured_points_rc, original.measured_points_rc)
    np.testing.assert_array_equal(got.matched_mask, original.matched_mask)
    np.testing.assert_array_equal(got.origin_rc, original.origin_rc)
    np.testing.assert_array_equal(got.basis_x_rc, original.basis_x_rc)
    np.testing.assert_array_equal(got.basis_y_rc, original.basis_y_rc)
    assert got.diagnostics == original.diagnostics


@pytest.mark.parametrize("axis", ["rows", "columns"])
def test_finite_manual_stagger_retains_original_algorithm(axis):
    image = _finite(stagger_axis=axis, stagger_fraction=0.5)
    opts = dict(xp_guess=24., yp_guess=25., n_cols=5, n_rows=4,
                stagger_mode="manual", stagger_axis=axis, stagger_fraction=0.5)
    expected = numeric_finite_grid(image, **opts)
    actual = ulenses.localize_grid(image, **opts)
    assert actual.stagger_axis == expected.stagger_axis
    np.testing.assert_array_equal(actual.logical_indices, expected.logical_indices)
    np.testing.assert_array_equal(actual.grid_points_rc, expected.grid_points_rc)
    np.testing.assert_array_equal(actual.matched_mask, expected.matched_mask)
    np.testing.assert_array_equal(actual.measured_points_rc, expected.measured_points_rc)


def test_finite_compatibility_signature_and_error_type():
    assert inspect.signature(ulenses.localize_grid) == inspect.signature(numeric_finite_grid)
    image = np.zeros((64, 64))
    kwargs = dict(n_cols=3, n_rows=3, xp_guess=13., yp_guess=13.)
    with pytest.raises(Exception) as original_error:
        numeric_finite_grid(image, **kwargs)
    with pytest.raises(type(original_error.value)) as wrapped_error:
        ulenses.localize_grid(image, **kwargs)
    assert str(wrapped_error.value) == str(original_error.value)


def test_existing_legacy_finite_entrypoints_are_still_exports():
    assert ulenses.localizer.__module__.endswith("legacy.ulenses")
    assert ulenses.refine_grid_foci.__module__.endswith("finite.ulenses")
    assert ulenses.search_missing_grid_foci.__module__.endswith("finite.ulenses")


def test_transform_strategy_consumes_same_finite_wrapper(monkeypatch):
    """Transform's registration and correspondence schema remain host-owned."""
    from imswitch.imcontrol.model import transform_correspondence
    image = _finite(missing=((2, 1),))
    calls = []
    real = transform_correspondence.localize_grid

    def trace(image, **kwargs):
        calls.append(dict(kwargs))
        return real(image, **kwargs)

    monkeypatch.setattr(transform_correspondence, "localize_grid", trace)
    spec = transform_correspondence.ulenses_grid_strategy_spec()
    params = dict(spec.default_parameters)
    params.update({
        "xp_guess_px": 24.2, "yp_guess_px": 25.,
        "grid_size_mode": "manual", "n_cols": 5, "n_rows": 4,
        "stagger_mode": "none",
    })
    localization = spec.localize_handler(image, params)
    assert len(calls) == 1
    assert calls[0]["n_cols"] == 5 and calls[0]["n_rows"] == 4
    assert type(localization.payload) is ulenses.GridLocalizationResult
    np.testing.assert_array_equal(
        localization.points,
        localization.payload.measured_points_rc[localization.payload.matched_mask],
    )
    assert localization.metadata["matched_count"] == localization.payload.matched_count
