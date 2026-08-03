"""Focus-lock / scan-Z arbitration.

The focus lock and a hardware Z scan can drive the same physical piezo -- the
STED setup reaches one through the analog ``ND-PiezoZ`` and the serial
``PiezoZ`` -- so an actively correcting lock opposes the intentional Z
waveform.

The lock therefore yields the actuator for the duration of a scan. What is
under test here is the *boundary*, which used to be wrong in three ways:

- it suspended on ``sigScanStarted``, published after the NI-DAQ tasks are
  already running and after the scan has already parked its positioners;
- it resumed on ``sigScanDone``, which is never published at all when a scan
  fails or is aborted, leaving the lock silently dead while the button still
  read "Unlock";
- it resumed by flipping ``locked`` back on, retaining the pre-scan PI
  integrator and error.
"""

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.FocusLockController import (
    STATE_LOCKED,
    STATE_REACQUIRE_FAILED,
    STATE_REACQUIRING,
    STATE_SUSPENDED,
    STATE_UNLOCKED,
    FocusLockController,
    PI,
)


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):  # pragma: no cover - unused
        pass


class _Edit:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class _Button:
    def __init__(self):
        self.checked = True
        self.text = 'Unlock'

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        self.checked = value

    def setText(self, value):
        self.text = value


class _CheckBox:
    def __init__(self, checked=True):
        self.checked = checked

    def isChecked(self):
        return self.checked


class _Plot:
    def removeItem(self, item):
        self.removed = item


class _Graph:
    lineLock = None


class _Widget:
    def __init__(self, scanBlock=True):
        self.kpEdit = _Edit('1')
        self.kiEdit = _Edit('0')
        self.lockButton = _Button()
        self.ScanBlock = _CheckBox(scanBlock)
        self.focusPlot = _Plot()
        self.focusLockGraph = _Graph()
        self.states = []

    def setLockState(self, state):
        self.states.append(state)


class _PositionerInfo:
    def __init__(self, axes, physicalActuator=None):
        self.axes = axes
        if physicalActuator is not None:
            self.physicalActuator = physicalActuator


class _SetupInfo:
    """Mirrors example_sted.json: one piezo reachable under two names."""

    def __init__(self, positioners=None):
        self.positioners = positioners if positioners is not None else {
            'ND-GalvoX': _PositionerInfo(['X']),
            'ND-GalvoY': _PositionerInfo(['Y']),
            'ND-PiezoZ': _PositionerInfo(['Z']),
            'PiezoZ': _PositionerInfo(['Z']),
        }


def _makeController(*, scanBlock=True, locked=True, setPoint=10.0,
                    positioners=None, tolerancePx=0.5, timeoutS=1.0,
                    samples=3):
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._shutdownComplete = False
    ctrl._focusCalibrationActive = False
    ctrl._logger = _Logger()
    ctrl._widget = _Widget(scanBlock=scanBlock)
    ctrl._setupInfo = _SetupInfo(positioners)

    ctrl.positioner = 'PiezoZ'
    ctrl.positionerAxis = 'Z'
    ctrl.focusTime = 100.0
    ctrl.aboutToLockDiffMax = 0.4
    ctrl.reacquireTimeoutS = timeoutS
    ctrl.reacquireTolerancePx = tolerancePx
    ctrl.reacquireSampleCount = samples

    ctrl._scanSuspendDepth = 0
    ctrl._scanOwnsFocusActuator = True
    ctrl._suspendedLock = False
    ctrl._preScanSetPoint = None
    ctrl._reacquireDeadline = None
    ctrl._reacquireSamples = None
    ctrl._reacquireFailed = False

    import threading
    ctrl._reacquireDone = threading.Event()
    ctrl._reacquireDone.set()

    ctrl.setPointSignal = setPoint
    ctrl.locked = locked
    ctrl.aboutToLock = False
    ctrl._lastPIUpdate = None
    ctrl.lockPosition = 0.0

    if locked:
        ctrl.pi = PI(setPoint, 0.001, 1.0, 0.0, nominalDt=0.1)
        # Drive the loop once so it carries real integrator state.
        ctrl.pi.update(setPoint + 4.0, 0.1)
    else:
        ctrl.pi = None

    ctrl.positionerReads = 0

    def getPositionerAbs():
        ctrl.positionerReads += 1
        return 42.0

    ctrl.getPositionerAbs = getPositionerAbs
    return ctrl


def _suspend(ctrl):
    FocusLockController.scanUnlockFocus(ctrl)


def _end(ctrl):
    FocusLockController.scanLockFocus(ctrl)


def _feed(ctrl, value, n=1):
    """Advance the reacquisition barrier with ``n`` estimates of ``value``."""
    for _ in range(n):
        ctrl.setPointSignal = value
        FocusLockController.aboutToLockUpdate(ctrl)


# --------------------------------------------------------------------------
# Suspension boundary
# --------------------------------------------------------------------------

def test_scan_start_suspends_an_active_lock():
    ctrl = _makeController()

    _suspend(ctrl)

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_SUSPENDED


def test_suspension_captures_the_setpoint_being_held_not_the_live_signal():
    """The signal drifts during the scan; the setpoint to return to does not."""
    ctrl = _makeController(setPoint=10.0)
    heldSetPoint = ctrl.pi.setPoint

    ctrl.setPointSignal = 999.0  # a wildly displaced live reading
    _suspend(ctrl)

    assert ctrl._preScanSetPoint == heldSetPoint


def test_suspension_clears_a_pending_about_to_lock():
    """Finding 5: a pending re-lock used to keep running through the scan."""
    ctrl = _makeController(locked=False)
    ctrl.aboutToLock = True

    _suspend(ctrl)

    assert ctrl.aboutToLock is False


def test_unchecked_scan_block_opts_out_entirely():
    ctrl = _makeController(scanBlock=False)

    _suspend(ctrl)

    assert ctrl.locked is True
    assert ctrl._scanSuspendDepth == 0


def test_scan_block_defaults_to_enabled_when_the_widget_has_no_checkbox():
    ctrl = _makeController()
    del ctrl._widget.ScanBlock

    assert FocusLockController.scanBlockEnabled(ctrl) is True


# --------------------------------------------------------------------------
# Depth counting -- workflows can publish these signals themselves
# --------------------------------------------------------------------------

def test_nested_starts_need_matching_ends_before_resuming():
    ctrl = _makeController()

    _suspend(ctrl)
    _suspend(ctrl)
    _end(ctrl)

    assert FocusLockController.focusLockState(ctrl) == STATE_SUSPENDED

    _end(ctrl)
    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRING


def test_a_stray_end_never_resumes_actuation():
    ctrl = _makeController()

    _end(ctrl)

    assert ctrl.locked is True
    assert ctrl._scanSuspendDepth == 0


def test_duplicate_start_does_not_overwrite_the_captured_setpoint():
    ctrl = _makeController(setPoint=10.0)
    heldSetPoint = ctrl.pi.setPoint

    _suspend(ctrl)
    ctrl.setPointSignal = 999.0
    _suspend(ctrl)

    assert ctrl._preScanSetPoint == heldSetPoint


# --------------------------------------------------------------------------
# Terminal coverage -- sigScanEnded fires on failure and abort too
# --------------------------------------------------------------------------

def test_a_scan_that_never_completes_still_releases_the_lock():
    """The regression: scanFailed publishes sigScanEnded, never sigScanDone.

    Bound to sigScanDone, the lock stayed suspended for the rest of the
    session while the button still read "Unlock".
    """
    ctrl = _makeController()

    _suspend(ctrl)
    _end(ctrl)  # the only terminal a failed/aborted run publishes

    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRING


def test_a_scan_started_while_unlocked_does_not_lock_afterwards():
    ctrl = _makeController(locked=False)

    _suspend(ctrl)
    _end(ctrl)

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_UNLOCKED


# --------------------------------------------------------------------------
# Conflict predicate
# --------------------------------------------------------------------------

def test_two_names_for_one_piezo_conflict_without_any_configuration():
    """The example_sted.json case: ND-PiezoZ scanned, PiezoZ locked."""
    ctrl = _makeController()

    assert FocusLockController.scanTouchesFocusActuator(
        ctrl, ['ND-GalvoX', 'ND-GalvoY', 'ND-PiezoZ']
    ) is True


def test_an_xy_only_scan_does_not_conflict():
    ctrl = _makeController()

    assert FocusLockController.scanTouchesFocusActuator(
        ctrl, ['ND-GalvoX', 'ND-GalvoY']
    ) is False


def test_the_focus_positioner_itself_always_conflicts():
    ctrl = _makeController()

    assert FocusLockController.scanTouchesFocusActuator(ctrl, ['PiezoZ']) is True


def test_unknown_actuators_are_treated_as_conflicting():
    ctrl = _makeController()

    assert FocusLockController.scanTouchesFocusActuator(ctrl, []) is True
    assert FocusLockController.scanTouchesFocusActuator(ctrl, None) is True
    assert FocusLockController.scanTouchesFocusActuator(
        ctrl, ['SomethingNotInTheSetup']
    ) is True


def test_declaring_different_physical_actuators_resolves_the_axis_clash():
    """Two genuinely separate Z stages must not be forced to conflict."""
    ctrl = _makeController(positioners={
        'ObjectivePiezo': _PositionerInfo(['Z'], physicalActuator='objective'),
        'PiezoZ': _PositionerInfo(['Z'], physicalActuator='sample'),
    })

    assert FocusLockController.scanTouchesFocusActuator(
        ctrl, ['ObjectivePiezo']
    ) is False


def test_declaring_the_same_physical_actuator_conflicts_across_axis_names():
    ctrl = _makeController(positioners={
        'ScanZ': _PositionerInfo(['W'], physicalActuator='sample'),
        'PiezoZ': _PositionerInfo(['Z'], physicalActuator='sample'),
    })

    assert FocusLockController.scanTouchesFocusActuator(ctrl, ['ScanZ']) is True


def test_a_non_conflicting_scan_hands_the_lock_straight_back():
    ctrl = _makeController()

    _suspend(ctrl)
    FocusLockController.scanActuatorsResolved(ctrl, ['ND-GalvoX', 'ND-GalvoY'])

    assert ctrl.locked is True
    assert FocusLockController.focusLockState(ctrl) == STATE_LOCKED


def test_a_conflicting_scan_stays_suspended():
    ctrl = _makeController()

    _suspend(ctrl)
    FocusLockController.scanActuatorsResolved(ctrl, ['ND-PiezoZ'])

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_SUSPENDED


def test_early_release_skips_the_reacquisition_barrier_at_scan_end():
    ctrl = _makeController()

    _suspend(ctrl)
    FocusLockController.scanActuatorsResolved(ctrl, ['ND-GalvoX'])
    _end(ctrl)

    assert FocusLockController.focusLockState(ctrl) == STATE_LOCKED


# --------------------------------------------------------------------------
# Reacquisition barrier
# --------------------------------------------------------------------------

def test_resume_rebuilds_the_pi_instead_of_reusing_the_stale_one():
    """Finding 4: the retained integrator was re-applied as a relative move."""
    ctrl = _makeController(setPoint=10.0)
    stalePI = ctrl.pi
    assert stalePI.out != 0.0, 'precondition: the loop carries integrator state'

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 10.0, n=ctrl.reacquireSampleCount)

    assert ctrl.locked is True
    assert ctrl.pi is not stalePI
    assert ctrl.pi.started is False, 'the rebuilt loop must start from scratch'
    assert ctrl.pi.setPoint == 10.0
    assert ctrl._lastPIUpdate is None


def test_a_settled_but_displaced_signal_does_not_re_engage():
    """The old barrier was variance-only, so it locked onto the wrong plane."""
    ctrl = _makeController(setPoint=10.0, tolerancePx=0.5)

    _suspend(ctrl)
    _end(ctrl)
    # Perfectly stable, but 5 px away from where the lock was holding.
    _feed(ctrl, 15.0, n=ctrl.reacquireSampleCount)

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRING


def test_a_noisy_signal_at_the_right_place_does_not_re_engage():
    ctrl = _makeController(setPoint=10.0, samples=4)

    _suspend(ctrl)
    _end(ctrl)
    for value in (10.0, 14.0, 6.0, 10.0):
        ctrl.setPointSignal = value
        FocusLockController.aboutToLockUpdate(ctrl)

    assert ctrl.locked is False


def test_the_barrier_gives_up_rather_than_waiting_forever():
    ctrl = _makeController(setPoint=10.0, timeoutS=0.0)

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 15.0, n=ctrl.reacquireSampleCount)

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRE_FAILED
    assert ctrl._logger.warnings, 'giving up must be reported'
    assert ctrl._widget.lockButton.checked is False, (
        'the button must stop claiming a lock that is not held'
    )


def test_giving_up_does_not_leave_the_waiter_blocked():
    ctrl = _makeController(setPoint=10.0, timeoutS=0.0)

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 15.0, n=ctrl.reacquireSampleCount)

    assert FocusLockController.waitForFocusReacquired(ctrl, 0.01) is False


def test_a_successful_reacquisition_releases_the_waiter():
    ctrl = _makeController(setPoint=10.0)

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 10.0, n=ctrl.reacquireSampleCount)

    assert FocusLockController.waitForFocusReacquired(ctrl, 0.01) is True


def test_the_barrier_needs_a_full_sample_window_before_deciding():
    ctrl = _makeController(setPoint=10.0, samples=5)

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 10.0, n=4)

    assert ctrl.locked is False, 'must not decide on a partly filled window'

    _feed(ctrl, 10.0, n=1)
    assert ctrl.locked is True


def test_a_new_scan_during_reacquisition_re_suspends_cleanly():
    ctrl = _makeController(setPoint=10.0)

    _suspend(ctrl)
    _end(ctrl)
    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRING

    _suspend(ctrl)

    assert FocusLockController.focusLockState(ctrl) == STATE_SUSPENDED
    assert ctrl.aboutToLock is False
    assert ctrl._preScanSetPoint == 10.0, (
        'the setpoint to return to must survive a scan arriving mid-barrier'
    )


# --------------------------------------------------------------------------
# Calibration interlock
# --------------------------------------------------------------------------

class _CalibThread:
    def __init__(self, running=True):
        self.running = running
        self.stopped = False
        self.yielded = False

    def isRunning(self):
        return self.running

    def stopAndYieldActuator(self):
        self.stopped = True
        self.yielded = True

    def configure(self, *args):  # pragma: no cover - unused here
        pass

    def start(self):  # pragma: no cover - unused here
        pass


def _withCalibThread(ctrl, thread):
    ctrl._FocusLockController__focusCalibThread = thread
    return thread


def test_a_scan_cancels_a_calibration_sweep_in_flight():
    """Calibration drove absolute Z moves straight through a scan waveform."""
    ctrl = _makeController()
    thread = _withCalibThread(ctrl, _CalibThread(running=True))

    _suspend(ctrl)

    assert thread.stopped is True


def test_the_cancelled_sweep_does_not_restore_its_own_position():
    """Restoring would be one more command fighting the scan."""
    ctrl = _makeController()
    thread = _withCalibThread(ctrl, _CalibThread(running=True))

    _suspend(ctrl)

    assert thread.yielded is True


def test_an_idle_calibration_thread_is_left_alone():
    ctrl = _makeController()
    thread = _withCalibThread(ctrl, _CalibThread(running=False))

    _suspend(ctrl)

    assert thread.stopped is False


def test_calibration_refuses_to_start_while_a_scan_owns_the_axis():
    ctrl = _makeController()
    _withCalibThread(ctrl, _CalibThread(running=False))
    ctrl._widget.calibFromEdit = _Edit('-1')
    ctrl._widget.calibToEdit = _Edit('1')
    ctrl._widget.focusCalibButton = _Button()
    ctrl._focusCalibrationActive = False

    _suspend(ctrl)
    FocusLockController.focusCalibrationStart(ctrl)

    assert ctrl._focusCalibrationActive is False
    assert ctrl._logger.warnings


def test_calibration_starts_normally_when_no_scan_is_running():
    ctrl = _makeController()
    thread = _withCalibThread(ctrl, _CalibThread(running=False))
    started = []
    thread.start = lambda: started.append(True)
    ctrl._widget.calibFromEdit = _Edit('-1')
    ctrl._widget.calibToEdit = _Edit('1')
    ctrl._widget.focusCalibButton = _Button()
    ctrl._focusCalibrationActive = False

    FocusLockController.focusCalibrationStart(ctrl)

    assert started == [True]
    assert ctrl._focusCalibrationActive is True


# --------------------------------------------------------------------------
# Interactions that must not defeat the suspension
# --------------------------------------------------------------------------

def test_editing_gains_during_a_scan_keeps_the_pending_restore():
    """Regression: kp/ki edits were wired straight to unlockFocus.

    That cleared the suspension bookkeeping, so the lock never came back after
    the scan -- while the button went on claiming it was engaged.
    """
    ctrl = _makeController(setPoint=10.0)

    _suspend(ctrl)
    FocusLockController.gainsChanged(ctrl)
    _end(ctrl)

    assert FocusLockController.focusLockState(ctrl) == STATE_REACQUIRING
    assert ctrl._preScanSetPoint == 10.0


def test_editing_gains_while_locked_still_drops_the_lock():
    """The historical behaviour, so new gains take effect on the next lock."""
    ctrl = _makeController()

    FocusLockController.gainsChanged(ctrl)

    assert ctrl.locked is False


def test_the_lock_button_does_not_engage_into_a_running_scan():
    """Clicking Lock mid-scan used to arm the loop against the waveform."""
    ctrl = _makeController(locked=False)
    ctrl._widget.lockButton.checked = True

    _suspend(ctrl)
    FocusLockController.toggleFocus(ctrl)

    assert ctrl.locked is False
    assert ctrl._suspendedLock is True, 'the intent must be remembered'
    assert ctrl.positionerReads == 0, 'no hardware read while a scan owns it'


def test_a_lock_requested_mid_scan_engages_once_the_scan_releases():
    ctrl = _makeController(locked=False)
    ctrl._widget.lockButton.checked = True

    _suspend(ctrl)
    FocusLockController.toggleFocus(ctrl)
    _end(ctrl)
    # No pre-scan setpoint to return to, so the barrier only waits for settle.
    _feed(ctrl, 4.0, n=ctrl.reacquireSampleCount)

    assert ctrl.locked is True
    assert ctrl.pi.setPoint == 4.0


def test_unlocking_mid_scan_cancels_the_pending_restore():
    ctrl = _makeController()
    ctrl._widget.lockButton.checked = False

    _suspend(ctrl)
    FocusLockController.toggleFocus(ctrl)
    _end(ctrl)

    assert ctrl.locked is False
    assert FocusLockController.focusLockState(ctrl) == STATE_UNLOCKED


def test_a_second_scan_re_suspends_a_lock_an_earlier_one_handed_back():
    """The early release is per-scan, not a latch."""
    ctrl = _makeController()

    _suspend(ctrl)
    FocusLockController.scanActuatorsResolved(ctrl, ['ND-GalvoX'])
    assert ctrl.locked is True

    _suspend(ctrl)          # a second, unknown scan

    assert ctrl.locked is False
    assert ctrl._scanOwnsFocusActuator is True


def test_no_early_release_while_more_than_one_scan_is_active():
    """One flag cannot prove every concurrent scan is harmless."""
    ctrl = _makeController()

    _suspend(ctrl)
    _suspend(ctrl)
    FocusLockController.scanActuatorsResolved(ctrl, ['ND-GalvoX'])

    assert ctrl.locked is False


def test_a_scan_with_no_lock_is_not_reported_as_suspended():
    ctrl = _makeController(locked=False)

    _suspend(ctrl)

    assert FocusLockController.focusLockState(ctrl) == STATE_UNLOCKED


def test_re_engaging_does_not_query_the_positioner():
    """lockPosition is write-only state; reading it would cost an RS232
    round trip on the GUI thread for every tile in a run."""
    ctrl = _makeController(setPoint=10.0)

    _suspend(ctrl)
    _end(ctrl)
    _feed(ctrl, 10.0, n=ctrl.reacquireSampleCount)

    assert ctrl.locked is True
    assert ctrl.positionerReads == 0
