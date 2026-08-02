"""Focus-lock safety guard: an oversized correction must not reach the piezo.

``updatePI`` drops the lock when the computed step exceeds the safety
threshold, but it used to ``return move`` afterwards -- and ``update`` applies
whatever it returns. The guard therefore unlocked *and then issued the very
step it existed to prevent*, driving the piezo by more than 3 um in one tick on
the way out of the lock.

That matters most right after a Z scan: the first tick sees an error built from
the entire scan-induced spot excursion, which is exactly the case that trips
the guard.
"""

import threading

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
    PI,
)


SAFETY_THRESHOLD_UM = 3.0
DEADBAND_UM = 0.002


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):  # pragma: no cover - unused
        pass


class _Line:
    def setValue(self, value):
        self.value = value


class _Image:
    def setImage(self, image):
        self.image = image


class _Curve:
    def setData(self, *args, **kwargs):
        self.data = args


class _Plot:
    def removeItem(self, item):
        self.removed = item


class _Graph:
    lineLock = object()


class _Button:
    def __init__(self):
        self.checked = True

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        self.checked = value


class _Widget:
    def __init__(self):
        self.center = _Line()
        self.camImg = _Image()
        self.focusPlotCurve = _Curve()
        self.focusPlot = _Plot()
        self.focusLockGraph = _Graph()
        self.lockButton = _Button()


class _ProcessDataThread:
    """Publishes one canned estimate, the way the real worker does."""

    def __init__(self, result):
        self._result = result

    def takeResult(self):
        result = self._result
        self._result = None
        return result


def _makeController(*, currentValue, kp, ki=0.0):
    """A FocusLockController wired for one ``update`` tick, no hardware.

    The real ``PI`` is used rather than a stub so the threshold arithmetic
    under test is the arithmetic that ships.
    """
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._shutdownComplete = False
    ctrl._focusCalibrationActive = False
    ctrl._logger = _Logger()
    ctrl._widget = _Widget()

    ctrl.locked = True
    ctrl.aboutToLock = False
    ctrl.noStepVar = True
    ctrl.zStackVar = False
    ctrl.lastPosition = 0.0
    ctrl.currentPosition = 0.0
    ctrl._lastPIUpdate = None

    # Scan-arbitration state: unlockFocus abandons any reacquisition in flight,
    # and the safety trip reaches it through updatePI.
    ctrl._scanSuspendDepth = 0
    ctrl._scanOwnsFocusActuator = True
    ctrl._suspendedLock = False
    ctrl._preScanSetPoint = None
    ctrl._reacquireDeadline = None
    ctrl._reacquireSamples = None
    ctrl._reacquireFailed = False
    ctrl._reacquireDone = threading.Event()
    ctrl._reacquireDone.set()

    # Set point 0 with a feedback value of `currentValue` gives error
    # `-currentValue`; the PI's first step is out = kp * error.
    ctrl.setPointSignal = currentValue
    ctrl.pi = PI(0.0, multiplier=1, kp=kp, ki=ki, nominalDt=0.1)

    ctrl.buffer = 40
    ctrl.currPoint = 0
    ctrl.setPointData = np.zeros(ctrl.buffer)
    ctrl.timeData = np.zeros(ctrl.buffer)
    ctrl.startTime = 0.0

    ctrl._FocusLockController__processDataThread = _ProcessDataThread(
        (np.zeros((4, 4)), currentValue, 1.0)
    )

    ctrl.moves = []
    ctrl.movePositioner = ctrl.moves.append
    return ctrl


# --------------------------------------------------------------------------
# updatePI in isolation
# --------------------------------------------------------------------------

def test_oversized_step_reports_no_motion_and_drops_the_lock():
    ctrl = _makeController(currentValue=100.0, kp=1.0)

    move = FocusLockController.updatePI(ctrl)

    assert move == 0.0, 'the tripping step must not be handed back to the caller'
    assert ctrl.locked is False
    assert ctrl._logger.warnings, 'the safety trip must be logged'


def test_step_within_the_threshold_is_reported_unchanged():
    ctrl = _makeController(currentValue=1.0, kp=1.0)

    move = FocusLockController.updatePI(ctrl)

    assert move == pytest.approx(-1.0)
    assert ctrl.locked is True


def test_threshold_is_on_magnitude_not_sign():
    """A large *negative* step trips the guard just as a positive one does."""
    ctrl = _makeController(currentValue=-100.0, kp=1.0)

    move = FocusLockController.updatePI(ctrl)

    assert move == 0.0
    assert ctrl.locked is False


# --------------------------------------------------------------------------
# The guard as seen through a real update() tick
# --------------------------------------------------------------------------

def test_safety_trip_moves_the_piezo_nowhere():
    """The regression: unlock fired, and the oversized move went out anyway."""
    ctrl = _makeController(currentValue=100.0, kp=1.0)

    FocusLockController.update(ctrl)

    assert ctrl.moves == []
    assert ctrl.locked is False


def test_normal_correction_still_reaches_the_piezo():
    """The guard must not have been bought by disabling the loop."""
    ctrl = _makeController(currentValue=1.0, kp=1.0)

    FocusLockController.update(ctrl)

    assert ctrl.moves == [pytest.approx(-1.0)]
    assert ctrl.locked is True


def test_correction_inside_the_deadband_is_not_issued():
    ctrl = _makeController(currentValue=DEADBAND_UM / 2, kp=1.0)

    FocusLockController.update(ctrl)

    assert ctrl.moves == []
    assert ctrl.locked is True


def test_update_does_not_correct_a_lock_dropped_mid_tick():
    """``update`` must re-read ``locked`` rather than trust its own branch.

    Belt-and-braces for the same defect: even if some future ``updatePI`` path
    unlocks while still returning a non-zero step, no correction may be sent.
    """
    ctrl = _makeController(currentValue=1.0, kp=1.0)

    def unlockButStillAskForAMove(timestamp=None):
        ctrl.locked = False
        return 2.5

    ctrl.updatePI = unlockButStillAskForAMove

    FocusLockController.update(ctrl)

    assert ctrl.moves == []
