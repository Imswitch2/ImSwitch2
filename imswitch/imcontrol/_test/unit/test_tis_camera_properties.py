"""TIS camera properties reach the hardware, and the GUI shows what it holds.

``CameraTIS.setPropertyValue`` used to assign ``self.cam.gain = value``.
pyicic's ``IC_Camera`` has its ``__setattr__`` commented out, so that only
created an attribute on the Python object: gain, brightness and exposure never
reached the camera, at startup or from the GUI (rig report: editing them in
the Focus Lock Camera tab "simply takes no effect").
"""
import importlib
import sys
import types

import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.TISManager import TISManager

_PYICIC = 'imswitch.imcontrol.model.interfaces.pyicic'
_TISCAMERA = 'imswitch.imcontrol.model.interfaces.tiscamera'


class _Property:
    """pyicic's IC_Property: the camera only changes through ``.value``."""

    def __init__(self, value, low, high):
        self._value = value
        self.range = (low, high)
        self.writes = []

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        # IC_Property passes the value through ctypes.c_long, which rejects
        # floats.
        assert isinstance(value, int), value
        self.writes.append(value)
        self._value = value


class _ICCamera:
    """pyicic's IC_Camera: properties come from ``__getattr__``; there is no
    ``__setattr__``, so assigning ``cam.gain = x`` never reaches them."""

    def __init__(self):
        self.properties = {
            'gain': _Property(10, 0, 480),
            'brightness': _Property(0, 0, 4095),
        }
        self.exposureS = 0.01
        self.exposureRangeS = (1e-4, 2.0)

    def __getattr__(self, name):
        properties = self.__dict__.get('properties', {})
        if name in properties:
            return properties[name]
        raise AttributeError(name)

    def get_exposure_abs_range(self):
        return self.exposureRangeS

    def get_exposure_abs(self):
        return self.exposureS

    def set_exposure_abs(self, seconds):
        self.exposureS = seconds


class _Logger:
    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


@pytest.fixture
def camera(monkeypatch):
    """A CameraTIS around a fake IC_Camera, importable without the TIS DLL."""
    imagingControl = types.ModuleType(f'{_PYICIC}.IC_ImagingControl')
    imagingControl.IC_ImagingControl = object
    icCamera = types.ModuleType(f'{_PYICIC}.IC_Camera')
    icCamera.IC_NoFrameAvailable = type('IC_NoFrameAvailable', (Exception,), {})
    monkeypatch.setitem(sys.modules, f'{_PYICIC}.IC_ImagingControl', imagingControl)
    monkeypatch.setitem(sys.modules, f'{_PYICIC}.IC_Camera', icCamera)
    monkeypatch.delitem(sys.modules, _TISCAMERA, raising=False)
    module = importlib.import_module(_TISCAMERA)
    # Undo removes this stubbed import again.
    monkeypatch.setitem(sys.modules, _TISCAMERA, module)

    cam = module.CameraTIS.__new__(module.CameraTIS)
    cam._closed = True  # nothing to release in __del__
    cam._CameraTIS__logger = _Logger()
    cam.shape = (640, 480)
    cam.cam = _ICCamera()
    return cam


def test_gain_and_brightness_are_written_through_the_camera_property(camera):
    assert camera.setPropertyValue('gain', 5.4) == 5
    assert camera.setPropertyValue('brightness', 300) == 300

    assert camera.cam.properties['gain'].writes == [5]
    assert camera.cam.properties['brightness'].writes == [300]
    assert camera.getPropertyValue('gain') == 5


def test_gain_outside_the_camera_range_is_clamped(camera):
    assert camera.setPropertyValue('gain', 1000) == 480
    assert camera.setPropertyValue('gain', -3) == 0


def test_exposure_is_set_in_milliseconds_through_the_absolute_interface(camera):
    assert camera.setPropertyValue('exposure', 25) == pytest.approx(25.0)

    assert camera.cam.exposureS == pytest.approx(0.025)
    assert camera.getPropertyValue('exposure') == pytest.approx(25.0)


def test_exposure_outside_the_camera_range_is_clamped(camera):
    assert camera.setPropertyValue('exposure', 0) == pytest.approx(0.1)
    assert camera.cam.exposureS == pytest.approx(1e-4)


# --- TISManager -------------------------------------------------------------


class _ClampingCamera:
    """A CameraTIS stand-in that reports back what it accepted."""

    def __init__(self):
        self.model = 'fake'
        self.values = {'exposure': 12.5, 'gain': 34, 'brightness': 56}
        self.forwarded = []
        self.dialogEdits = {}

    def start_live(self):
        pass

    def stop_live(self):
        pass

    def suspend_live(self):
        pass

    def setROI(self, hpos, vpos, hsize, vsize):
        pass

    def openPropertiesGUI(self):
        self.values.update(self.dialogEdits)

    def setPropertyValue(self, name, value):
        self.forwarded.append((name, value))
        if name == 'gain':
            value = min(int(round(value)), 480)
        if name in self.values:
            self.values[name] = value
        return value

    def getPropertyValue(self, name):
        return {'image_width': 640, 'image_height': 480, **self.values}[name]


def _manager(monkeypatch, tis):
    monkeypatch.setattr(TISManager, '_getTISObj', lambda self, index: _ClampingCamera())
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='TISManager',
        managerProperties={'cameraListIndex': 0, 'tis': tis},
        forAcquisition=False,
        forFocusLock=True,
    )
    return TISManager(info, 'FocusLockCamera')


def test_startup_does_not_push_placeholder_camera_settings(monkeypatch):
    """Shipped setups carry exposure/gain/brightness = 0. They never reached
    the camera before, and applying them now would start it dark."""
    mgr = _manager(monkeypatch, {
        'exposure': 0.0, 'gain': 0.0, 'brightness': 0.0,
        'image_width': 1280, 'image_height': 1024,
    })

    assert [name for name, _ in mgr._camera.forwarded] == ['image_width', 'image_height']


def test_parameters_start_from_the_camera_readback(monkeypatch):
    mgr = _manager(monkeypatch, {})

    assert mgr.parameters['exposure'].value == 12.5
    assert mgr.parameters['gain'].value == 34
    assert mgr.parameters['brightness'].value == 56


def test_set_parameter_keeps_the_value_the_camera_accepted(monkeypatch):
    mgr = _manager(monkeypatch, {})

    result = mgr.setParameter('gain', 999.7)

    assert result is mgr.parameters
    assert mgr.parameters['gain'].value == 480


def test_properties_dialog_updates_the_parameters_it_changed(monkeypatch):
    mgr = _manager(monkeypatch, {})
    mgr._camera.dialogEdits = {'exposure': 40.0, 'gain': 7}

    mgr.actions['More properties'].func()

    assert mgr.parameters['exposure'].value == 40.0
    assert mgr.parameters['gain'].value == 7
    assert mgr.parameters['brightness'].value == 56
