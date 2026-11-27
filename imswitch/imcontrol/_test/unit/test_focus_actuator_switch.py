"""Session-only focus actuator switching never moves the physical Z hardware."""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.FocusLockController import FocusLockController


class _Z:
    axes = ['Z']
    isAvailable = True
    requiresReference = False
    isReferenced = True

    def __init__(self):
        self.moves = []

    def move(self, dz, axis):
        self.moves.append((dz, axis))

    def get_abs(self, axis):
        return 0


class _Thread:
    def isRunning(self):
        return False


class _Widget:
    def __init__(self):
        self.calibrationDisplay = SimpleNamespace(setText=lambda value: None)
        self.calibCurveButton = SimpleNamespace(setEnabled=lambda value: None)
        self.focusActuatorButton = SimpleNamespace(setToolTip=lambda value: None)


def _controller():
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl.positioner = 'FocusPiezoDAC'
    ctrl.positionerAxis = 'Z'
    ctrl.locked = False
    ctrl.aboutToLock = False
    ctrl._scanSuspendDepth = 0
    ctrl._focusCalibrationActive = False
    ctrl._reacquireFailed = False
    ctrl._pendingMove = 0.1
    ctrl._lastPIUpdate = None
    ctrl._lastTiltFocusMeasurement = (2., 0.)
    ctrl._master = SimpleNamespace(positionersManager={
        'FocusPiezoDAC': _Z(), 'Z': _Z(), 'Stage': object(),
    })
    ctrl._setupInfo = SimpleNamespace(
        focusLock=SimpleNamespace(positioner='FocusPiezoDAC'),
        positioners={
            'FocusPiezoDAC': SimpleNamespace(axes=['Z'], forPositioning=True),
            'Z': SimpleNamespace(axes=['Z'], forPositioning=True),
            'Stage': SimpleNamespace(axes=['X', 'Y'], forPositioning=True),
        },
    )
    ctrl._commChannel = SimpleNamespace(_tilingActive=False)
    ctrl._widget = _Widget()
    ctrl._FocusLockController__focusCalibThread = _Thread()
    ctrl._reacquireDone = SimpleNamespace(set=lambda: None)
    ctrl._logger = SimpleNamespace(info=lambda *a, **k: None)
    ctrl._publishFocusLockState = lambda: None
    return ctrl


def test_switch_keeps_hardware_still_and_uses_runtime_selection():
    ctrl = _controller()
    assert set(ctrl._focusZCandidates()) == {'Z', 'FocusPiezoDAC'}
    ctrl.setFocusZActuator('Z')
    assert ctrl.positioner == 'Z'
    assert ctrl.positionerAxis == 'Z'
    assert ctrl._pendingMove == 0
    assert ctrl._lastTiltFocusMeasurement is None
    assert ctrl._setupInfo.focusLock.positioner == 'FocusPiezoDAC'
    for motor in ctrl._master.positionersManager.values():
        if isinstance(motor, _Z):
            assert not motor.moves


def test_switch_disallowed_while_locked_tiling_or_calibrating():
    for flag in ('locked', '_scanSuspendDepth', '_focusCalibrationActive'):
        ctrl = _controller()
        setattr(ctrl, flag, True)
        with pytest.raises(RuntimeError):
            ctrl.setFocusZActuator('Z')
    ctrl = _controller()
    ctrl._commChannel._tilingActive = True
    with pytest.raises(RuntimeError):
        ctrl.setFocusZActuator('Z')


def test_switch_before_any_calibration_thread_has_been_created():
    # The normal startup state has no calibration thread yet. This must not
    # dereference None when the user first changes the focus actuator.
    ctrl = _controller()
    ctrl._FocusLockController__focusCalibThread = None
    ctrl.setFocusZActuator('Z')
    assert ctrl.positioner == 'Z'
