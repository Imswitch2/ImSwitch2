"""Unit tests for LiveDisplayThrottle (point-scan live-preview rate limiter).

These lock in the deterministic, wall-clock behaviour that replaced the old
shape-dependent random redraw gate in APDManager/PMTManager.updateImage.
"""

from imswitch.imcontrol.model.managers.detectors._live_display import (
    LiveDisplayThrottle,
)


class _FakeClock:
    """Manually advanced monotonic clock for deterministic tests."""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


def test_first_call_always_fires():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.05, clock=clock)
    assert throttle.due() is True


def test_second_call_within_interval_is_suppressed():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.05, clock=clock)
    assert throttle.due() is True
    clock.advance(0.01)
    assert throttle.due() is False
    clock.advance(0.03)  # total 0.04 < 0.05
    assert throttle.due() is False


def test_fires_again_once_interval_elapsed():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.05, clock=clock)
    assert throttle.due() is True
    clock.advance(0.05)  # exactly the interval -> due
    assert throttle.due() is True
    clock.advance(0.05)
    assert throttle.due() is True


def test_interval_measured_from_last_fire_not_last_call():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.10, clock=clock)
    assert throttle.due() is True         # t=0 fires
    clock.advance(0.05)
    assert throttle.due() is False        # t=0.05 suppressed, does NOT re-arm
    clock.advance(0.06)                   # t=0.11, 0.11s since the fire at t=0
    assert throttle.due() is True


def test_reset_makes_next_call_fire_immediately():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.05, clock=clock)
    assert throttle.due() is True
    clock.advance(0.01)
    assert throttle.due() is False
    throttle.reset()
    assert throttle.due() is True         # reset forgets the last fire time


def test_rate_is_bounded_over_many_fast_calls():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.05, clock=clock)
    fires = 0
    # Simulate 1000 line writes at 1 kHz line rate (1 ms apart) = 1.0 s total.
    for _ in range(1000):
        if throttle.due():
            fires += 1
        clock.advance(0.001)
    # At 20 Hz over ~1 s we expect ~21 redraws, never the full 1000.
    assert 18 <= fires <= 22


def test_zero_interval_always_fires():
    clock = _FakeClock()
    throttle = LiveDisplayThrottle(min_interval_s=0.0, clock=clock)
    assert throttle.due() is True
    assert throttle.due() is True  # no suppression when interval is 0
