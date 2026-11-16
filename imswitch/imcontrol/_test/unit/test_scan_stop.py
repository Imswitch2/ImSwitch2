"""A stopped scan's scanners return no faster than they may move (plan
scan-stop-now, decision 1): the ramp's own arithmetic."""
import numpy as np
import pytest

from imswitch.imcontrol.model.scan_stop import axis_limits_volts, rest_to_rest_ramp

RATE = 100_000           # the one-shot clock the stop writes on


def _speeds_and_accelerations(start, ramp):
    path = np.concatenate(([start, start], ramp, [ramp[-1]]))   # at rest before and after
    speed = np.diff(path) * RATE
    accel = np.diff(speed) * RATE
    return np.abs(speed), np.abs(accel)


@pytest.mark.parametrize('start, end, v_max, a_max', [
    (0.58, 0.0, 5_700.0, 5.7e6),       # a galvo: 0.1 µm/µs, 1e-4 µm/µs², 17.44 µm/V
    (-0.35, 0.0, 5_700.0, 5.7e6),
    (0.0, 3.0, 50.0, 2_000.0),         # long enough to cruise
    (2.0, 1.999, 50.0, 2_000.0),       # a few samples: a triangle
])
def test_the_ramp_starts_and_ends_at_rest_within_the_limits(start, end, v_max, a_max):
    ramp = rest_to_rest_ramp(start, end, v_max, a_max, RATE)
    assert ramp[-1] == end
    assert abs(ramp[0] - start) <= v_max / RATE        # one period after the start
    speed, accel = _speeds_and_accelerations(start, ramp)
    assert speed.max() <= v_max * (1 + 1e-9)
    assert accel.max() <= a_max * (1 + 1e-6)
    # monotonic: it never overshoots
    assert np.all(np.diff(np.concatenate(([start], ramp))) * np.sign(end - start) >= 0)


def test_a_move_that_cannot_reach_full_speed_is_a_triangle_and_one_that_can_cruises():
    short = rest_to_rest_ramp(0.0, 0.1, 50.0, 2_000.0, RATE)
    long = rest_to_rest_ramp(0.0, 3.0, 50.0, 2_000.0, RATE)
    short_speed, _ = _speeds_and_accelerations(0.0, short)
    long_speed, _ = _speeds_and_accelerations(0.0, long)
    assert short_speed.max() < 50.0 * 0.99                      # never at v_max
    cruising = np.isclose(long_speed, 50.0, rtol=1e-6)
    assert cruising.sum() > 10                                  # a plateau at v_max
    # the minimum time: accelerate, cruise, decelerate
    expected_s = 2 * (50.0 / 2_000.0) + (3.0 - 50.0 ** 2 / 2_000.0) / 50.0
    assert len(long) == int(np.ceil(expected_s * RATE))


def test_no_distance_is_no_ramp_and_no_limits_is_refused():
    assert rest_to_rest_ramp(1.5, 1.5, 50.0, 2_000.0, RATE).size == 0
    with pytest.raises(ValueError, match='positive speed'):
        rest_to_rest_ramp(0.0, 1.0, 0.0, 2_000.0, RATE)


def test_the_declared_limits_in_volts():
    """vel_max µm/µs and acc_max µm/µs² over conversionFactor µm/V."""
    assert axis_limits_volts({'vel_max': 0.1, 'acc_max': 1e-4, 'conversionFactor': 17.44}) == (
        pytest.approx(0.1e6 / 17.44), pytest.approx(1e-4 * 1e12 / 17.44))
    assert axis_limits_volts({'vel_max': 0.1, 'conversionFactor': 17.44}) is None
    assert axis_limits_volts({'vel_max': 0.1, 'acc_max': 1e-4}) is None
    assert axis_limits_volts({'vel_max': 0.0, 'acc_max': 1e-4, 'conversionFactor': 1.0}) is None
