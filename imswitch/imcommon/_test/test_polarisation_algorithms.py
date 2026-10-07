"""Polarisation maths: conversions, aggregation, matching counterexamples."""
import math

import numpy as np
import pytest

from imswitch.imcommon.algorithms.polarisation import (
    FAILED,
    PASS,
    UNQUALIFIED,
    Target,
    TwoPlateModel,
    aggregate_polarisation,
    angles_from_direction,
    angular_distance_deg,
    default_targets,
    direction_from_angles,
    match_targets,
)


def test_direction_round_trip():
    psi = np.radians([-80, -10, 0, 30, 89])
    chi = np.radians([-40, 5, 0, 20, -1])
    back_psi, back_chi = angles_from_direction(direction_from_angles(psi, chi))
    np.testing.assert_allclose(back_psi, psi, atol=1e-12)
    np.testing.assert_allclose(back_chi, chi, atol=1e-12)


def test_azimuth_wraparound_is_not_averaged_arithmetically():
    """−89° and +89° are nearly the same orientation, not 0°."""
    psi = np.radians([-89.0, 89.0])
    agg = aggregate_polarisation(psi, np.zeros(2), dop=np.ones(2), power=np.ones(2))
    assert agg.defined
    assert abs(abs(math.degrees(agg.azimuth)) - 90.0) < 1e-6
    # arithmetic mean would have said 0° — a direction 180° away on the sphere
    zero = direction_from_angles(0.0, 0.0)
    assert angular_distance_deg(agg.direction, zero) > 179.0
    assert agg.dispersion_deg == pytest.approx(2.0, abs=1e-6)


def test_dop_definitions_differ_when_the_state_wanders():
    """Fully polarised samples in varying directions: instrument DOP 1, aggregate < 1."""
    psi = np.radians([0.0, 45.0])
    agg = aggregate_polarisation(psi, np.zeros(2), dop=np.ones(2), power=np.ones(2))
    assert agg.dop_instrument == pytest.approx(1.0)
    assert agg.dop_aggregate == pytest.approx(math.sqrt(2) / 2)


def test_undefined_direction_for_zero_power_and_opposite_states():
    zero_power = aggregate_polarisation([0.1], [0.0], dop=[1.0], power=[0.0])
    assert not zero_power.defined
    opposite = aggregate_polarisation(
        np.radians([0.0, 90.0]), np.zeros(2), dop=np.ones(2), power=np.ones(2))
    assert not opposite.defined  # H and V cancel: mean polarised vector is 0
    empty = aggregate_polarisation([], [], dop=[], power=[])
    assert not empty.defined and empty.n == 0


def test_power_statistics():
    agg = aggregate_polarisation([0, 0, 0], [0, 0, 0], dop=[1, 1, 1], power=[1.0, 2.0, 3.0])
    assert agg.power_mean == pytest.approx(2.0)
    assert agg.power_std == pytest.approx(1.0)


def test_two_plate_model_quarter_wave_at_45_gives_circular():
    model = TwoPlateModel(retardance2=0.0)  # plate 2 inactive
    out = model.output(45.0, 0.0)
    assert abs(abs(out[3]) - 1.0) < 1e-12
    out0 = model.output(0.0, 0.0)  # fast axis along H input: unchanged
    np.testing.assert_allclose(out0, [1, 1, 0, 0], atol=1e-12)


def test_default_targets():
    targets = default_targets(10.0)
    assert [t.name for t in targets[:3]] == ['RCP', 'LCP', 'linear 0°']
    assert len(targets) == 20
    for t in targets:
        assert np.linalg.norm(t.direction) == pytest.approx(1.0)


def test_nearest_ineligible_point_does_not_hide_an_eligible_one():
    """Review counterexample: 1° away with DOP 0.5 vs 2° away with DOP 0.99."""
    target = Target('H', np.array([1.0, 0.0, 0.0]))
    near = direction_from_angles(math.radians(0.5), 0.0)     # 1° on the sphere
    far = direction_from_angles(math.radians(1.0), 0.0)      # 2° on the sphere
    directions = np.stack([near, far])
    dop = np.array([0.5, 0.99])
    eligible = dop >= 0.95
    (match,) = match_targets([target], directions, eligible, np.ones(2, bool), 5.0)
    assert match.index == 1
    assert match.status == PASS
    assert match.distance_deg == pytest.approx(2.0, abs=1e-6)


def test_unverified_points_are_unqualified_never_pass():
    target = Target('H', np.array([1.0, 0.0, 0.0]))
    directions = np.array([[1.0, 0.0, 0.0]])
    (match,) = match_targets([target], directions, [True], [False], 5.0)
    assert match.status == UNQUALIFIED


def test_no_eligible_point_fails():
    target = Target('H', np.array([1.0, 0.0, 0.0]))
    (match,) = match_targets([target], np.array([[1.0, 0, 0]]), [False], [True], 5.0)
    assert match.status == FAILED and match.index is None


def test_beyond_threshold_fails():
    target = Target('V', np.array([-1.0, 0.0, 0.0]))
    (match,) = match_targets([target], np.array([[1.0, 0, 0]]), [True], [True], 5.0)
    assert match.status == FAILED
    assert match.distance_deg == pytest.approx(180.0)


def test_unverified_point_never_hides_a_qualifying_verified_one():
    """Review: an unverified exact match beat a verified point 2° away."""
    target = Target('H', np.array([1.0, 0.0, 0.0]))
    exact = direction_from_angles(0.0, 0.0)
    two_deg = direction_from_angles(math.radians(1.0), 0.0)
    (match,) = match_targets([target], np.stack([exact, two_deg]),
                             [True, True], [False, True], 5.0)
    assert match.status == PASS and match.index == 1


def test_unverified_match_is_reported_when_no_verified_point_qualifies():
    target = Target('H', np.array([1.0, 0.0, 0.0]))
    near = direction_from_angles(0.0, 0.0)
    far = direction_from_angles(math.radians(20.0), 0.0)
    (match,) = match_targets([target], np.stack([near, far]),
                             [True, True], [False, True], 5.0)
    assert match.status == UNQUALIFIED and match.index == 0
