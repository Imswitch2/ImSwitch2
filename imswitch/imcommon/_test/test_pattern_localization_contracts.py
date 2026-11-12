"""Phase-1 shared contract invariants (no algorithm dispatch)."""

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import (
    PatternGeometry, PatternLocalizationResult, PatternPrior,
)


def test_empty_prior_is_optional_and_does_not_claim_honored_hints():
    prior = PatternPrior()
    assert prior.provided_fields == ()
    geometry = PatternGeometry(origin_xy=(3, 4), basis_xy=np.diag([8., 10.]))
    result = PatternLocalizationResult(geometry=geometry, strategy="periodic", prior=prior)
    assert result.prior_usage == {}
    assert result.predicted_positions_xy is None
    assert geometry.site_indices_ij is None


def test_prior_validates_camera_xy_periods_fraction_and_finite_grid():
    for data in (
        dict(expected_period_x_px=0), dict(expected_period_y_px=-1),
        dict(expected_period_x_px=float("nan")), dict(period_tolerance_fraction=-0.2),
        dict(expected_rotation_deg=float("inf")), dict(stagger_axis="rows"),
        dict(stagger_fraction=0.5), dict(finite_shape_xy=(0, 4)),
        dict(finite_shape_xy=(2.2, 4)), dict(finite_site_indices=[[0.4, 1]]),
        dict(finite_site_indices=[[0, 1], [0, 1]]),
        dict(finite_shape_xy=(2, 2), finite_site_indices=[[2, 0]]),
    ):
        with pytest.raises((ValueError, TypeError)):
            PatternPrior(**data)

    prior = PatternPrior(
        expected_period_x_px=9.0, expected_period_y_px=13.0,
        period_tolerance_fraction=0.2,
        stagger_axis="y", stagger_fraction=-0.35,
        finite_shape_xy=(3, 2), finite_site_indices=[[0, 0], [2, 1]],
    )
    assert prior.stagger_axis == "y"
    assert prior.stagger_fraction == -0.35
    np.testing.assert_array_equal(prior.finite_site_indices, [[0, 0], [2, 1]])
    assert not prior.finite_site_indices.flags.writeable
    with pytest.raises(FrozenInstanceError):
        prior.stagger_axis = "x"


def test_prior_and_geometry_defensively_copy_source_arrays_and_metadata():
    indices = np.array([[0, 0], [1, 0]])
    params = {"target": {"period": [3, 4]}}
    prior = PatternPrior(finite_site_indices=indices, target_params=params)
    indices[0, 0] = 99
    params["target"]["period"][0] = 100
    np.testing.assert_array_equal(prior.finite_site_indices, [[0, 0], [1, 0]])
    assert prior.target_params["target"]["period"] == [3, 4]

    basis = np.array([[9., 2.], [1., 11.]])
    geom = PatternGeometry(origin_xy=(32, 18), basis_xy=basis, extent="finite",
                           site_indices_ij=[[0, 0], [1, 0]], finite_shape_xy=(2, 1))
    basis[0, 0] = 400
    assert geom.basis_xy[0, 0] == 9
    assert not geom.basis_xy.flags.writeable
    assert geom.origin_xy == (32., 18.)


def test_result_missing_measurements_keep_missing_site_and_logical_index():
    indices = [[0, 0], [1, 0], [0, 1], [1, 1]]
    geom = PatternGeometry(origin_xy=(10, 20), basis_xy=np.diag([6, 7]),
                           extent="finite", site_indices_ij=indices,
                           finite_shape_xy=(2, 2))
    expected = np.array([[10., 20.], [16., 20.], [10., 27.], [16., 27.]])
    measured = expected.copy()
    measured[2] = np.nan
    result = PatternLocalizationResult(
        geometry=geom, strategy="finite", predicted_positions_xy=expected,
        measured_positions_xy=measured, matched_mask=[True, True, False, True],
        prior=PatternPrior(finite_shape_xy=(2, 2)),
        prior_usage={"finite_shape_xy": "ignored"},
    )
    assert result.prior_usage["finite_shape_xy"] == "ignored"
    assert np.isnan(result.measured_positions_xy[2]).all()
    np.testing.assert_array_equal(result.geometry.site_indices_ij[2], [0, 1])
    assert not result.matched_mask[2]
    assert not result.predicted_positions_xy.flags.writeable

    with pytest.raises(ValueError, match="matched measured positions"):
        PatternLocalizationResult(
            geometry=geom, strategy="finite", predicted_positions_xy=expected,
            measured_positions_xy=measured, matched_mask=[True] * 4,
        )
    with pytest.raises(ValueError, match="provided hints"):
        PatternLocalizationResult(
            geometry=geom, strategy="finite", predicted_positions_xy=expected,
            prior=PatternPrior(), prior_usage={"stagger_axis": "applied"},
        )
    with pytest.raises(ValueError, match="finite predicted"):
        PatternLocalizationResult(geometry=geom, strategy="finite")
    with pytest.raises(ValueError, match="periodic geometry"):
        PatternGeometry(origin_xy=(0, 0), basis_xy=np.eye(2),
                        site_indices_ij=[[0, 0]])
