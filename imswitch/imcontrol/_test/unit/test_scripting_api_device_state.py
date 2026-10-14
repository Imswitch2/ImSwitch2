"""The scripting API's device-state calls added in October 2026: what a
script can read back (rotators, lasers, positioners, detector pixel size,
the live frame, the focus lock) and the scan parameters as a dict. Each
controller is built without Qt around fakes of its widget and managers."""
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
)
from imswitch.imcontrol.controller.controllers.LaserController import LaserController
from imswitch.imcontrol.controller.controllers.PositionerController import (
    PositionerController,
)
from imswitch.imcontrol.controller.controllers.RotatorController import (
    RotatorController,
)
from imswitch.imcontrol.controller.controllers.SettingsController import (
    SettingsController,
)
from imswitch.imcontrol.controller.controllers.ViewController import ViewController


def _bare(cls, **attrs):
    ctrl = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(ctrl, name, value)
    return ctrl


# --- rotators ---------------------------------------------------------------

class _Rotator:
    def __init__(self, position=0.0):
        self.position = position

    def move_rel(self, degrees):
        self.position += degrees

    def move_abs(self, degrees):
        self.position = degrees


class _Rotators(dict):
    def __iter__(self):
        return iter(self.items())


def _rotator_controller():
    shown = {}
    widget = SimpleNamespace(updatePosition=lambda name, pos: shown.__setitem__(name, pos))
    ctrl = _bare(RotatorController, _widget=widget,
                 _master=SimpleNamespace(rotatorsManager=_Rotators(HWP=_Rotator(10.0), QWP=_Rotator())))
    return ctrl, shown


def test_rotator_names_position_and_moves():
    ctrl, shown = _rotator_controller()
    assert ctrl.getRotatorNames() == ['HWP', 'QWP']
    assert ctrl.getRotatorPosition('HWP') == 10.0
    ctrl.moveRotator('HWP', -2.5)
    assert ctrl.getRotatorPosition('HWP') == 7.5
    ctrl.setRotatorPosition('HWP', 45.0)
    assert ctrl.getRotatorPosition('HWP') == 45.0
    ctrl.moveAbs('QWP', 90.0)                      # the older name still turns
    assert ctrl.getRotatorPosition('QWP') == 90.0
    assert shown == {'HWP': 45.0, 'QWP': 90.0}     # the widget follows every call


# --- lasers -----------------------------------------------------------------

def test_laser_value_state_and_info():
    widget = SimpleNamespace(getValue=lambda n: {'488': 12.5}[n],
                             isLaserActive=lambda n: {'488': True}[n])
    laser = SimpleNamespace(wavelength=488, valueUnits='mW', valueRangeMin=0,
                            valueRangeMax=100, valueRangeStep=0.5, isBinary=False)
    ctrl = _bare(LaserController, _widget=widget,
                 _master=SimpleNamespace(lasersManager={'488': laser}))
    assert ctrl.getLaserValue('488') == 12.5
    assert ctrl.isLaserActive('488') is True
    assert ctrl.getLaserInfo('488') == {'wavelength': 488, 'units': 'mW', 'min': 0,
                                        'max': 100, 'step': 0.5, 'binary': False}


# --- positioners ------------------------------------------------------------

def test_positioner_axis_position_and_axes():
    stage = SimpleNamespace(position={'X': 1.0, 'Y': -2.0}, axes=['X', 'Y'])
    ctrl = _bare(PositionerController,
                 _master=SimpleNamespace(positionersManager={'XY': stage}))
    assert ctrl.getPositionerPosition('XY', 'Y') == -2.0
    assert ctrl.getPositionerAxes('XY') == ['X', 'Y']


# --- detectors --------------------------------------------------------------

def test_detector_pixel_size_is_z_y_x_in_um():
    camera = SimpleNamespace(pixelSizeUm=[1, 0.15, 0.15])
    ctrl = _bare(SettingsController,
                 _master=SimpleNamespace(detectorsManager={'Camera': camera}))
    assert ctrl.getDetectorPixelSize('Camera') == [1.0, 0.15, 0.15]


def test_latest_frame_is_a_copy_of_the_detectors_frame():
    frame = np.arange(6, dtype=np.uint16).reshape(2, 3)

    class _Detectors(dict):
        def getCurrentDetectorName(self):
            return 'Camera'

    detectors = _Detectors(Camera=SimpleNamespace(getLatestFrame=lambda: frame))
    ctrl = _bare(ViewController, _master=SimpleNamespace(detectorsManager=detectors))
    got = ctrl.getLatestFrame()
    np.testing.assert_array_equal(got, frame)
    got[0, 0] = 99
    assert frame[0, 0] == 0                        # the camera's buffer is untouched
    assert ctrl.getLatestFrame('Camera').shape == (2, 3)


# --- focus lock -------------------------------------------------------------

class _LockButton:
    def __init__(self):
        self.checked = False
        self.text = 'Lock'

    def setChecked(self, value):
        self.checked = value

    def isChecked(self):
        return self.checked

    def setText(self, text):
        self.text = text


def _focus_controller(*, cameraOn=True):
    ctrl = _bare(FocusLockController, locked=False, aboutToLock=False,
                 _widget=SimpleNamespace(lockButton=_LockButton(),
                                         setLockState=lambda state: None))
    ctrl.focusCameraAcquisitionActive = lambda: cameraOn
    ctrl._endReacquire = lambda **kwargs: None
    ctrl.getPositionerAbs = lambda: 3.0
    locks = []
    ctrl.lockFocus = lambda zpos: (locks.append(zpos), setattr(ctrl, 'locked', True))
    ctrl.unlockFocus = lambda: setattr(ctrl, 'locked', False)
    return ctrl, locks


def test_focus_lock_state_lock_and_unlock():
    ctrl, locks = _focus_controller()
    assert ctrl.getFocusLockState() == 'unlocked'
    assert ctrl.isFocusLocked() is False
    ctrl.setFocusLocked(True)
    assert locks == [3.0] and ctrl.isFocusLocked() is True
    assert ctrl.getFocusLockState() == 'locked'
    assert ctrl._widget.lockButton.text == 'Unlock'
    ctrl.setFocusLocked(False)
    assert ctrl.isFocusLocked() is False
    assert ctrl._widget.lockButton.text == 'Lock'


def test_focus_lock_needs_the_focus_camera():
    ctrl, locks = _focus_controller(cameraOn=False)
    with pytest.raises(RuntimeError, match='focus camera is off'):
        ctrl.setFocusLocked(True)
    assert locks == []


def test_wait_for_focus_lock_returns_at_once_when_no_lock_is_wanted():
    ctrl, _locks = _focus_controller()
    assert ctrl.waitForFocusLock(timeout=0.01) is True
    assert FocusLockController.waitForFocusLock._APIRunOnUIThread is False


# --- scan parameters --------------------------------------------------------

def test_scan_parameters_round_trip_and_refusal():
    state = {'analogParameterDict': {'axis_step_size': [0.1]}}
    applied = []

    def apply(parameters, *, applyMode):
        applied.append(parameters)
        return ['Camera2 is not a device of this setup'] if 'bad' in parameters else []

    ctrl = SimpleNamespace(getComponentState=lambda: dict(state), applyComponentState=apply)
    ctrl.getScanParameters = lambda: SuperScanController.getScanParameters(ctrl)
    ctrl.setScanParameters = lambda p: SuperScanController.setScanParameters(ctrl, p)
    params = ctrl.getScanParameters()
    assert params == state
    params['analogParameterDict']['axis_step_size'] = [0.2]
    ctrl.setScanParameters(params)
    assert applied == [params]
    with pytest.raises(ValueError, match='Camera2'):
        ctrl.setScanParameters({'bad': True})
