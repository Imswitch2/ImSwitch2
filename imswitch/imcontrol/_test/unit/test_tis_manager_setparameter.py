"""Regression test for TISManager.setParameter forwarding only real camera
properties to the hardware driver.

Background: TISManager used to forward EVERY parameter unconditionally to
CameraTIS.setPropertyValue, including 'Camera pixel size' -- a synthetic,
DetectorManager-level bookkeeping value with no corresponding TIS hardware
property. The real CameraTIS rejects unknown property names (logs a warning,
returns False); MockCameraTIS does not, which is why this was invisible against
the mock and only surfaced against a real camera. SettingsController.
setDetectorParameter also does `c.setParameter(...) and updateParamsFromDetector(...)`,
so the resulting falsy return silently skipped the GUI refresh -- not just a
log warning. See TISManager.py setParameter / _CAMERA_PROPERTIES.
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.TISManager import TISManager


class StrictFakeCamera:
    """Mimics the REAL CameraTIS's setPropertyValue: only a fixed allow-list of
    hardware properties is recognized; anything else is rejected (here: raises,
    so the test fails loudly if TISManager ever forwards a non-hardware name).
    """

    KNOWN = {'gain', 'brightness', 'exposure', 'image_height', 'image_width'}

    def __init__(self):
        self.model = 'fake'
        self.colorenable = 0
        self.forwarded = []

    def start_live(self):
        pass

    def stop_live(self):
        pass

    def suspend_live(self):
        pass

    def setROI(self, hpos, vpos, hsize, vsize):
        pass

    def openPropertiesGUI(self):
        pass

    def setPropertyValue(self, property_name, property_value):
        if property_name not in self.KNOWN:
            raise AssertionError(
                f'TISManager forwarded non-hardware property "{property_name}" '
                f'to the camera driver'
            )
        self.forwarded.append((property_name, property_value))
        if property_name == 'image_height':
            return property_value
        if property_name == 'image_width':
            return property_value
        return property_value

    def getPropertyValue(self, property_name):
        return {'image_width': 800, 'image_height': 800}.get(property_name, 0)


def _make_manager(monkeypatch):
    monkeypatch.setattr(TISManager, '_getTISObj', lambda self, cameraId: StrictFakeCamera())
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='TISManager',
        managerProperties={
            'cameraListIndex': 0,
            'tis': {},
            'cameraPixelSizeUm': 0.5,
        },
        forAcquisition=True,
        forFocusLock=False,
    )
    return TISManager(info, 'WidefieldCamera')


def test_set_camera_pixel_size_does_not_touch_hardware(monkeypatch):
    """The synthetic 'Camera pixel size' parameter must never reach the driver."""
    mgr = _make_manager(monkeypatch)
    mgr._camera.forwarded.clear()

    result = mgr.setParameter('Camera pixel size', 0.3)

    assert mgr._camera.forwarded == []
    assert mgr.parameters['Camera pixel size'].value == 0.3
    # Must return the parameters dict (truthy), not the camera's return value,
    # so SettingsController's `setParameter(...) and updateParamsFromDetector(...)`
    # never short-circuits on this parameter.
    assert result is mgr.parameters
    assert bool(result)


def test_set_real_hardware_parameter_is_forwarded(monkeypatch):
    mgr = _make_manager(monkeypatch)
    mgr._camera.forwarded.clear()

    mgr.setParameter('gain', 5)

    assert mgr._camera.forwarded == [('gain', 5)]
    assert mgr.parameters['gain'].value == 5


def test_set_parameter_return_value_is_truthy_even_when_value_is_zero(monkeypatch):
    """Before the fix, setting gain/brightness to 0 returned the camera's raw
    (falsy) return value, which also short-circuited the GUI refresh in
    SettingsController.setDetectorParameter."""
    mgr = _make_manager(monkeypatch)

    result = mgr.setParameter('brightness', 0)

    assert bool(result)
    assert result is mgr.parameters
