"""Unit tests for BSC203StageManager — the safety-critical bits.

These lock down two failure modes that have repeatedly bitten the BSC203:

1. **Unsigned-underflow runaway.** The controller firmware reads the absolute-
   position field as an unsigned 32-bit integer, so a negative target wraps to a
   huge positive value and drives the motor continuously toward an unreachable
   point. Every absolute move must be clamped to [0, travelRange].

2. **Home-then-recentre.** APT homing parks each axis at a physical end-stop (0),
   an awkward corner for an XY sample stage. After homing, X/Y must be recentred
   to mid-travel; Z must be left at its homed edge. Re-homing an already-homed
   stage must wait for the *new* homing to finish before recentring.

The tests run with a fake device (no hardware, no serial port).
"""

import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo
import imswitch.imcontrol.model.managers.positioners.BSC203StageManager as bsc_mod
from imswitch.imcontrol.model.managers.positioners.BSC203StageManager import (
    BSC203StageManager,
    STEPS_PER_REV,
    REV_PER_MM,
)


def _steps(um):
    """µm -> encoder steps, matching the manager's conversion."""
    return int((um / 1000) * REV_PER_MM * STEPS_PER_REV)


class FakeBSC:
    """Minimal stand-in for thorlabs_apt_device's BSC, recording commands."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        # 3 bays, 1 channel each. msg='ok' so _syncPositionFromController returns
        # immediately; homed=True so a fresh manager doesn't block on construction.
        self.status_ = [[{'homed': True, 'position': 0, 'msg': 'ok'}]
                        for _ in range(3)]
        self.velparams_ = [[{'max_velocity': 1000, 'acceleration': 100}]
                           for _ in range(3)]
        self.move_absolute_calls = []   # (position_steps, bay) — must stay EMPTY
        self.home_calls = []            # bay
        self.jog_calls = []             # (size, direction_bool, bay)
        self.jog_param_calls = []       # (bay, acceleration, max_velocity)
        self.stop_calls = []            # (bay, immediate)
        self.velparam_calls = []        # (bay, acceleration, max_velocity)
        self._last_jog_size = {}        # bay -> size

    def move_absolute(self, position, now=True, bay=0, channel=0):
        # move_absolute must NEVER be used (it runs away on downward moves).
        self.move_absolute_calls.append((position, bay))

    def home(self, bay=0):
        self.home_calls.append(bay)
        # Simulate the device completing the new homing.
        self.status_[bay][0]['homed'] = True

    def set_velocity_params(self, acceleration=0, max_velocity=0, bay=0, channel=0):
        self.velparam_calls.append((bay, acceleration, max_velocity))
        # The real device echoes the new parameters back into velparams_ (it
        # re-requests them); mirroring that is what lets the cache heal.
        self.velparams_[bay][0]['acceleration'] = acceleration
        self.velparams_[bay][0]['max_velocity'] = max_velocity

    def set_jog_params(self, size, acceleration, max_velocity,
                       continuous=False, immediate_stop=False, bay=0, channel=0):
        self._last_jog_size[bay] = size
        self.jog_param_calls.append((bay, acceleration, max_velocity))

    def move_jog(self, direction=True, bay=0, channel=0):
        size = self._last_jog_size.get(bay, 0)
        # Simulate the stage actually moving (forward increases the encoder).
        self.status_[bay][0]['position'] += size if direction else -size
        self.jog_calls.append((size, bool(direction), bay))

    def move_velocity(self, **kwargs):
        pass

    def stop(self, immediate=False, bay=0, channel=0):
        self.stop_calls.append((bay, immediate))


def _make_manager(monkeypatch, *, home=False, properties=None):
    monkeypatch.setattr(bsc_mod, 'BSC', FakeBSC, raising=False)
    # The manager also does `from thorlabs_apt_device.devices.bsc import BSC`
    # inside __init__; patch the source module so the import picks up the fake.
    import thorlabs_apt_device.devices.bsc as real_bsc
    monkeypatch.setattr(real_bsc, 'BSC', FakeBSC, raising=False)

    props = {'home': home}
    if properties:
        props.update(properties)
    info = PositionerInfo(
        managerName='BSC203StageManager',
        managerProperties=props,
        axes=['X', 'Y', 'Z'],
        forPositioning=True,
    )
    mgr = BSC203StageManager(info, 'BSC203')
    return mgr


@pytest.mark.nohardware
def test_move_never_uses_move_absolute(monkeypatch):
    """move_absolute must NEVER be called — it runs away on downward moves."""
    mgr = _make_manager(monkeypatch)
    mgr.setPosition(2000, 'X')
    mgr.move(500, 'X')
    mgr.setPosition(1000, 'X')   # downward
    mgr.move(-300, 'X')          # downward
    assert mgr.dev.move_absolute_calls == []


@pytest.mark.nohardware
def test_setposition_up_jogs_forward(monkeypatch):
    """An upward move jogs FORWARD by the exact displacement."""
    mgr = _make_manager(monkeypatch)        # starts at encoder 0
    mgr.dev.jog_calls.clear()

    mgr.setPosition(2000, 'X')

    # one forward jog of 2000 µm worth of steps on bay 0
    assert mgr.dev.jog_calls == [(_steps(2000), True, 0)]
    assert mgr.position['X'] == pytest.approx(2000, abs=1)


@pytest.mark.nohardware
def test_downward_move_jogs_reverse_positive_size(monkeypatch):
    """The bug reproducer: a downward move must become a REVERSE jog with a
    POSITIVE step size — never a move_absolute to a lower target (which runs away)."""
    mgr = _make_manager(monkeypatch)
    mgr.setPosition(1148.8, 'X')   # go up first (forward jog), encoder ~ that
    mgr.dev.jog_calls.clear()

    mgr.setPosition(1048.8, 'X')   # 100 µm DOWNWARD — the move that ran away

    assert len(mgr.dev.jog_calls) == 1
    size, direction, bay = mgr.dev.jog_calls[0]
    assert bay == 0
    assert direction is False          # reverse
    assert size > 0                    # positive step size (no unsigned underflow)
    assert size == pytest.approx(_steps(100), abs=2)
    assert mgr.dev.move_absolute_calls == []


@pytest.mark.nohardware
def test_setposition_negative_clamps_to_zero(monkeypatch):
    """A negative target clamps to 0 and jogs there (still positive jog size)."""
    mgr = _make_manager(monkeypatch)
    mgr.setPosition(300, 'X')       # go up
    mgr.dev.jog_calls.clear()

    mgr.setPosition(-500, 'X')      # target clamps to 0

    assert mgr.position['X'] == 0
    assert len(mgr.dev.jog_calls) == 1
    size, direction, _ = mgr.dev.jog_calls[0]
    assert direction is False and size == pytest.approx(_steps(300), abs=2)
    assert mgr.dev.move_absolute_calls == []


@pytest.mark.nohardware
def test_setposition_over_travel_clamps_to_max(monkeypatch):
    mgr = _make_manager(monkeypatch, properties={'travelRangeUm': 8000})
    mgr.dev.jog_calls.clear()

    mgr.setPosition(9000, 'X')      # past the 8000 µm far end

    assert mgr.position['X'] == pytest.approx(8000, abs=1)
    size, direction, _ = mgr.dev.jog_calls[0]
    assert direction is True and size == pytest.approx(_steps(8000), abs=2)
    assert mgr.dev.move_absolute_calls == []


@pytest.mark.nohardware
def test_invert_jog_axis_flips_direction(monkeypatch):
    """An axis listed in invertJogAxes jogs the opposite way for the same target."""
    mgr = _make_manager(monkeypatch, properties={'invertJogAxes': ['X']})
    mgr.dev.jog_calls.clear()

    mgr.setPosition(2000, 'X')      # upward target, but inverted -> reverse jog

    size, direction, _ = mgr.dev.jog_calls[0]
    assert direction is False
    assert size == pytest.approx(_steps(2000), abs=2)


# --- The zero-velocity wedge -------------------------------------------------
#
# Observed on the rig: one axis would stop dead or crawl in one direction, then
# ignore every later command, and the stop button did nothing. thorlabs_apt_device
# pre-fills velparams_ with max_velocity=0/acceleration=0 and replaces them only
# when the controller's async reply lands, so a move commanded in that window
# jogged with zero dynamics -- and set_jog_params wrote those zeros into the bay,
# wedging it for good.


@pytest.mark.nohardware
def test_jog_never_commands_zero_velocity(monkeypatch):
    """The reproducer. A bay that has not reported its velocity parameters must
    still jog with usable dynamics, never the zeros the driver pre-fills.

    dict.get(key, default) cannot save this: the keys are always present, just
    zero. Commanding zero writes it into the controller and wedges the bay.
    """
    mgr = _make_manager(monkeypatch)
    mgr.dev.velparams_[0][0] = {'max_velocity': 0, 'acceleration': 0}

    mgr.setPosition(2000, 'X')

    assert len(mgr.dev.jog_param_calls) == 1
    bay, acceleration, max_velocity = mgr.dev.jog_param_calls[0]
    assert bay == 0
    assert max_velocity == BSC203StageManager._FALLBACK_MAX_VELOCITY
    assert acceleration == BSC203StageManager._FALLBACK_ACCELERATION


@pytest.mark.nohardware
def test_reported_velocity_params_are_still_used(monkeypatch):
    """The fallback must not override real values the controller did report."""
    mgr = _make_manager(monkeypatch)
    mgr.dev.velparams_[0][0] = {'max_velocity': 12345, 'acceleration': 678}

    mgr.setPosition(2000, 'X')

    assert mgr.dev.jog_param_calls[0] == (0, 678, 12345)


@pytest.mark.nohardware
def test_stop_is_immediate_not_profiled(monkeypatch):
    """A profiled stop decelerates along the bay's velocity curve, so it cannot
    stop a bay whose acceleration is zero — the one state stop is needed in."""
    mgr = _make_manager(monkeypatch)

    mgr.stopAll()

    assert sorted(mgr.dev.stop_calls) == [(0, True), (1, True), (2, True)]


@pytest.mark.nohardware
def test_stop_restores_motion_params_on_a_wedged_bay(monkeypatch):
    """Stop has to be a rescue, not just a halt: it rewrites the zeroed motion
    parameters so the axis accepts commands again."""
    mgr = _make_manager(monkeypatch)
    mgr.dev.velparams_[1][0] = {'max_velocity': 0, 'acceleration': 0}

    mgr.stopAll()

    assert (1, BSC203StageManager._FALLBACK_ACCELERATION,
            BSC203StageManager._FALLBACK_MAX_VELOCITY) in mgr.dev.velparam_calls
    # And the bay now moves: the wedge is gone, not just paused.
    mgr.dev.jog_param_calls.clear()
    mgr.setPosition(1500, 'Y')
    _, acceleration, max_velocity = mgr.dev.jog_param_calls[0]
    assert max_velocity > 0 and acceleration > 0


@pytest.mark.nohardware
def test_stop_resyncs_tracked_position_to_the_encoder(monkeypatch):
    """_jogToSteps records the target optimistically, so an interrupted move
    leaves the tracked position at a place the stage never reached — and the
    next relative move would then be computed from that phantom."""
    mgr = _make_manager(monkeypatch)
    mgr.setPosition(4000, 'X')
    # Stage actually stalled a quarter of the way there.
    mgr.dev.status_[0][0]['position'] = _steps(1000)

    mgr.stopAll()

    assert mgr.position['X'] == pytest.approx(1000, abs=1)


@pytest.mark.nohardware
def test_stop_continues_after_one_axis_fails(monkeypatch):
    """The GUI stop button calls this; one bad bay must not strand the others."""
    mgr = _make_manager(monkeypatch)
    realStop = mgr.dev.stop

    def failingStop(immediate=False, bay=0, channel=0):
        if bay == 0:
            raise RuntimeError('bay 0 is not responding')
        realStop(immediate=immediate, bay=bay, channel=channel)

    monkeypatch.setattr(mgr.dev, 'stop', failingStop)

    mgr.stopAll()

    assert sorted(mgr.dev.stop_calls) == [(1, True), (2, True)]


@pytest.mark.nohardware
def test_homeall_homes_all_bays_no_recentre(monkeypatch):
    """Re-homing homes all three bays and leaves them at the end-stop (0); it must
    NOT issue any recentring move."""
    mgr = _make_manager(monkeypatch)
    # Fake device reports 'homed' synchronously and never signals in-progress.
    mgr._HOMING_START_TIMEOUT_S = 0.05
    mgr.dev.jog_calls.clear()

    mgr.homeAll()

    assert sorted(mgr.dev.home_calls) == [0, 1, 2]
    assert mgr.dev.jog_calls == []          # no recentring
    assert mgr.dev.move_absolute_calls == []


@pytest.mark.nohardware
def test_waithomed_waits_for_completion_not_stale_homed(monkeypatch):
    """_waitHomed must observe homing-in-progress and then wait for completion,
    rather than returning on the stale 'idle, homed' status."""
    mgr = _make_manager(monkeypatch)
    mgr._HOMING_START_TIMEOUT_S = 2.0

    # Simulate the controller actively homing: homing bit set, homed cleared.
    for bay in range(3):
        mgr.dev.status_[bay][0]['homed'] = False
        mgr.dev.status_[bay][0]['homing'] = True

    polls = {'n': 0}
    real_homing = mgr.homing

    def fake_homing():
        polls['n'] += 1
        if polls['n'] >= 3:  # complete homing after a couple of phase-2 checks
            for bay in range(3):
                mgr.dev.status_[bay][0]['homed'] = True
                mgr.dev.status_[bay][0]['homing'] = False
        return real_homing()

    monkeypatch.setattr(mgr, 'homing', fake_homing)
    mgr._waitHomed(timeout=5.0)

    # Phase 2 actually looped (did not fall through on stale homed).
    assert polls['n'] >= 3
    assert not mgr.homing()


@pytest.mark.nohardware
def test_waithomed_handles_already_finished_home(monkeypatch):
    """If homing completes before the in-progress state is ever observed, the
    wait must still return cleanly (via the 'homed' confirmation), not hang or
    centre prematurely."""
    mgr = _make_manager(monkeypatch)
    mgr._HOMING_START_TIMEOUT_S = 0.05  # never observe in-progress

    # Device already reports fully homed (FakeBSC default).
    mgr._waitHomed(timeout=5.0)

    assert not mgr.homing()
