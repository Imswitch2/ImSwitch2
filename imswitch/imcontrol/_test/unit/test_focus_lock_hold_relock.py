"""Hold instead of unlock, lost, pause/resume and bounded relock, driven
through the real controller update loop with simulated frames (no hardware).
"""
import threading

import pytest

from imswitch.imcontrol.controller.controllers.FocusLockController import FocusLockController
from imswitch.imcontrol.model import focus_lock_quality as flq

from .focus_lock_sim import Rig


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    path = tmp_path / 'focus_lock_state.json'
    monkeypatch.setattr(flq, 'lock_state_path', lambda: path)
    return path


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def test_clean_frames_stay_locked_and_follow_drift(rig):
    engine = rig.lock()
    assert engine.reference is not None
    assert engine.thresholds.learned, 'thresholds are learned in the first seconds'
    states = []
    for _ in range(60):
        rig.zFocus += 0.005                       # slow drift
        states.append(rig.frame())
    assert set(states) == {'locked'}, 'no false hold on clean frames'
    assert abs(rig.spotRow() - rig.row0) < 0.5, 'the loop followed the drift'


def test_bad_frames_hold_then_good_frames_resume(rig):
    engine = rig.lock()
    zBefore = rig.z
    states = rig.frames(6, occlude=(-4, 0.2))     # dirt over part of the spot
    assert states[0] == 'holding'
    assert set(states) == {'holding'}, 'persistent occlusion keeps holding'
    assert rig.z == zBefore, 'Z frozen while holding'
    assert 'agreement' in engine.lastVerdict.reasons or 'jump' in engine.lastVerdict.reasons
    states = rig.frames(engine.supervisorSettings.goodFramesToResume + 1)
    assert states[-1] == 'locked'
    assert 'holding' in rig.ctrl._widget.states
    assert 'holding' in rig.ctrl._logger.text().lower()


def test_hold_does_not_wind_up_the_integrator(rig):
    rig.ctrl._widget.kiEdit.value = '40'
    rig.lock()
    rig.frames(20, amp=0)                         # spot gone: holding
    rig.frames(5)
    moves = list(rig.moves)
    rig.frames(3)
    assert all(abs(m) < 0.5 for m in rig.moves[len(moves):]), 'no windup kick on resume'


def test_bad_frames_past_timeout_go_lost_and_keep_the_setpoint(rig):
    engine = rig.lock()
    engine.setHoldSettings(5, 1.0)
    setPoint = rig.ctrl.pi.setPoint
    zBefore = rig.z
    states = rig.frames(15, amp=0)
    assert states[0] == 'holding' and states[-1] == 'lost'
    assert rig.z == zBefore
    assert engine.setPoint == pytest.approx(setPoint)
    assert engine.reference is not None
    assert rig.ctrl.locked is False
    assert FocusLockController.waitForFocusReacquired(rig.ctrl, 0.1) is False
    # good frames do not re-engage a lost lock by themselves
    assert set(rig.frames(10)) == {'lost'}
    assert rig.z == zBefore


def test_oversized_correction_holds_and_is_never_applied(rig):
    rig.lock()
    rig.ctrl._widget.kpEdit.value = '50'
    zBefore = rig.z
    # a genuine 12 µm jump: the PI step would be 6 µm > the 3 µm limit
    rig.zFocus -= 12.0
    state = rig.frame()
    assert state == 'holding'
    assert rig.z == zBefore


def test_user_unlock_from_lost_is_unlocked(rig):
    engine = rig.lock()
    engine.setHoldSettings(5, 0.5)
    rig.frames(10, amp=0)
    assert rig.state() == 'lost'
    FocusLockController.unlockFocus(rig.ctrl)
    assert rig.state() == 'unlocked'


# ---------------------------------------------------------------- pause/resume

def _in_thread(fn, *args):
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('value', fn(*args)), daemon=True)
    thread.start()
    return thread, box


def _drive(rig, thread, n=200, **frameKw):
    for _ in range(n):
        rig.pump()
        if not thread.is_alive():
            break
        rig.frame(**frameKw)
        thread.join(0.005)
    rig.pump()
    thread.join(5)
    assert not thread.is_alive()


def test_pause_resume_across_a_simulated_xy_move(rig):
    rig.lock()
    thread, box = _in_thread(FocusLockController.pauseFocusLock, rig.ctrl)
    _drive(rig, thread, n=5)
    assert box['value'] == 'paused'
    zPaused = rig.z
    # XY move: junk frames, and the new tile sits 0.8 µm off focus (tilt)
    rig.frames(5, amp=0)
    rig.zFocus += 0.8
    rig.frames(3)
    assert rig.state() == 'paused' and rig.z == zPaused, 'nothing moves while paused'
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 10.0)
    _drive(rig, thread)
    assert box['value'] == 'locked'
    assert rig.state() == 'locked'
    relockMoves = [m for m in rig.moves]
    assert all(abs(m) <= rig.ctrl._lockEngine().limits.maxStepUm + 1e-9 for m in relockMoves)
    assert abs(rig.spotRow() - rig.row0) <= rig.ctrl._lockEngine().limits.tolerancePx + 0.3


def test_relock_hits_the_clamps_fails_and_returns_to_last_good_z(rig):
    engine = rig.lock()
    FocusLockController.pauseFocusLock(rig.ctrl)        # GUI thread: inline
    zGood = rig.z
    rig.zFocus += 5.0                                    # beyond the 3 µm budget
    rig.moves.clear()
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 30.0)
    _drive(rig, thread, n=400, dt=0.1)
    assert box['value'] == 'failed'
    assert rig.state() == 'lost'
    limits = engine.limits
    assert all(abs(m) <= limits.maxStepUm + 1e-9 for m in rig.moves[:-1])
    excursion, peak = 0.0, 0.0
    for m in rig.moves:
        excursion += m
        peak = max(peak, abs(excursion))
    assert peak <= limits.maxExcursionUm + 1e-9, 'total excursion clamped'
    assert rig.z == pytest.approx(zGood), 'back at the last good Z'
    assert 'excursion' in FocusLockController.getFocusLockResumeReason(rig.ctrl)


def test_no_relock_move_without_calibration(monkeypatch):
    rig = Rig(monkeypatch, calibrated=False)
    rig.lock()
    FocusLockController.pauseFocusLock(rig.ctrl)
    rig.zFocus += 0.5
    rig.moves.clear()
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 5.0)
    _drive(rig, thread)
    assert box['value'] == 'failed'
    assert rig.moves == []
    assert 'calibration' in FocusLockController.getFocusLockResumeReason(rig.ctrl)


def test_stop_during_a_resume_wait_returns_promptly(rig):
    rig.lock()
    FocusLockController.pauseFocusLock(rig.ctrl)
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 60.0)
    for _ in range(3):
        rig.pump()
        rig.frame(amp=0)                                 # spot gone: relock waits
    zBefore = rig.z
    FocusLockController.cancelFocusLockResume(rig.ctrl)
    thread.join(2)
    assert not thread.is_alive(), 'Stop does not hang'
    assert box['value'] == 'cancelled'
    rig.pump()
    assert rig.state() == 'lost'
    rig.frames(3)
    assert rig.z == zBefore


def test_exception_during_relock_leaves_z_and_reports(rig):
    rig.lock()
    FocusLockController.pauseFocusLock(rig.ctrl)
    rig.zFocus += 0.8
    rig.moveError = RuntimeError('serial timeout')
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 10.0)
    zBefore = rig.z
    _drive(rig, thread)
    assert box['value'] == 'failed'
    assert rig.z == zBefore
    assert 'serial timeout' in FocusLockController.getFocusLockResumeReason(rig.ctrl)
    assert rig.state() == 'lost'


def test_resume_from_the_gui_thread_is_refused(rig):
    rig.lock()
    with pytest.raises(RuntimeError):
        FocusLockController.resumeFocusLock(rig.ctrl)


def test_resume_without_a_lock_is_a_no_op(rig):
    rig.frame()
    thread, box = _in_thread(FocusLockController.resumeFocusLock, rig.ctrl, 0.0, 1.0)
    _drive(rig, thread, n=5)
    assert box['value'] == 'unlocked'
    assert rig.moves == []


def test_relock_from_lost_by_the_button(rig):
    engine = rig.lock()
    engine.setHoldSettings(5, 0.5)
    rig.frames(10, amp=0)
    assert rig.state() == 'lost'
    rig.zFocus += 0.6
    FocusLockController.startRelock(rig.ctrl)
    states = rig.frames(60)
    assert states[-1] == 'locked'


def test_scan_during_hold_hands_over_cleanly(rig):
    rig.lock()
    rig.frames(2, amp=0)
    assert rig.state() == 'holding'
    rig.ctrl._widget.ScanBlock = None
    FocusLockController.scanUnlockFocus(rig.ctrl)
    assert rig.state() == 'suspended'
    assert rig.ctrl._lockEngine().mode == flq.LOCKED


# ---------------------------------------------------------------- persistence

def test_saved_state_restores_limits_and_reengages_only_on_request(rig, monkeypatch, _state_file):
    engine = rig.lock()
    FocusLockController.setFocusLockRelockLimits(rig.ctrl, 0.25, 2.0, 0.8, 8.0, False, 1.0)
    setPoint = engine.setPoint
    assert _state_file.exists()
    # "restart": a fresh controller on the same setup/actuator
    fresh = Rig(monkeypatch, calibrated=False)
    fresh.z = rig.z + 0.4                       # the sample moved meanwhile
    FocusLockController._loadStoredLockSettings(fresh.ctrl)
    assert fresh.ctrl._lockEngine().limits.maxStepUm == 0.25
    assert fresh.ctrl._storedLockAvailable
    fresh.frames(5)
    assert fresh.state() == 'unlocked' and fresh.moves == [], 'never engages by itself'
    FocusLockController.reengagePreviousFocusLock(fresh.ctrl)
    states = fresh.frames(80)
    assert states[-1] == 'locked'
    assert fresh.ctrl.pi.setPoint == pytest.approx(setPoint)
    assert all(abs(m) <= 0.25 + 1e-9 for m in fresh.moves[:3])


def test_reengage_without_stored_calibration_is_refused(rig, monkeypatch):
    rig.ctrl._lockEngine().pxPerUm = None
    rig.lock()
    FocusLockController.unlockFocus(rig.ctrl)
    with pytest.raises(RuntimeError, match='calibration'):
        FocusLockController.reengagePreviousFocusLock(rig.ctrl)
