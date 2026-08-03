"""Regression test for HamamatsuManager.crop trusting the camera's actually
applied ROI rather than the requested one.

Background: DCAM's dcam_setgetpropertyvalue (wrapped by HamamatsuCamera.
setPropertyValue) sets AND reads back the value in one call, returning what
the driver actually applied. The subarray_hpos/vpos/hsize/vsize properties
have a hardware-defined step granularity (DCAM_PARAM_PROPERTYATTR.valuestep)
-- a requested value that isn't aligned to that step gets silently snapped
by the driver to the nearest valid value. HamamatsuManager.crop() used to
ignore the returned (applied) value and store the *requested* numbers in
self._frameStart/self._shape, which then disagreed with the real hardware
ROI whenever snapping occurred -- only for off-step ROIs, which is why the
mismatch looked intermittent/"sometimes weird" rather than a hard,
every-time failure. See HamamatsuManager.crop.
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)


class SnappingFakeCamera:
    """Mimics the real HamamatsuCamera: setPropertyValue both sets AND
    returns the value the "driver" actually applied, which may differ from
    what was requested due to hardware step/range constraints. Here hpos and
    hsize snap to multiples of 4; vpos and vsize snap to multiples of 8
    (deliberately different from hpos/vpos's step, like real DCAM cameras
    that have asymmetric H/V granularity), to make sure the fix doesn't rely
    on assuming a single uniform step everywhere.
    """

    HSTEP = 4
    VSTEP = 8

    def __init__(self):
        self.camera_model = b'fake-hamamatsu'
        self.properties = {
            'image_width': 2048,
            'image_height': 2048,
            'subarray_hpos': 0,
            'subarray_vpos': 0,
            'subarray_hsize': 2048,
            'subarray_vsize': 2048,
            'subarray_mode': b'OFF',
            'exposure_time': 0.01,
            'internal_frame_interval': 0.01,
            'timing_readout_time': 0.0,
            'internal_frame_rate': 100.0,
            'trigger_source': 1,
            'trigger_mode': 1,
        }
        self.set_calls = []

    @staticmethod
    def _snap(value, step):
        return step * round(value / step)

    def getPropertyValue(self, name):
        return self.properties.get(name, 0), 'LONG'

    def setPropertyValue(self, name, value):
        self.set_calls.append((name, value))
        if name in ('subarray_hpos', 'subarray_hsize'):
            value = self._snap(value, self.HSTEP)
        elif name in ('subarray_vpos', 'subarray_vsize'):
            value = self._snap(value, self.VSTEP)
        self.properties[name] = value
        return value

    def startAcquisition(self):
        pass

    def stopAcquisition(self):
        pass


def _make_manager(monkeypatch, cameraClass=SnappingFakeCamera):
    monkeypatch.setattr(HamamatsuManager, '_getCameraObj', lambda self, cameraId: cameraClass())
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='HamamatsuManager',
        managerProperties={
            'cameraListIndex': 0,
            'hamamatsu': {},
            'cameraPixelSizeUm': 0.15,
        },
        forAcquisition=True,
        forFocusLock=False,
    )
    return HamamatsuManager(info, 'OrcaStraight')


def test_crop_with_off_step_roi_stores_applied_not_requested_values(monkeypatch):
    """A ROI that isn't aligned to the camera's native step gets silently
    snapped by the driver; frameStart/shape must reflect what the hardware
    actually did, not the raw request."""
    mgr = _make_manager(monkeypatch)

    # hpos=137 -> snapped to 136 (nearest multiple of 4)
    # vpos=101 -> snapped to 104 (nearest multiple of 8)
    # hsize=301 -> snapped to 300 (nearest multiple of 4)
    # vsize=199 -> snapped to 200 (nearest multiple of 8)
    mgr.crop(hpos=137, vpos=101, hsize=301, vsize=199)

    assert mgr.frameStart == (
        SnappingFakeCamera._snap(137, 4),
        SnappingFakeCamera._snap(101, 8),
    )
    assert mgr.shape == (
        SnappingFakeCamera._snap(301, 4),
        SnappingFakeCamera._snap(199, 8),
    )
    # Sanity: the snapped values are NOT simply the requested ones, i.e. this
    # test would have failed before the fix.
    assert mgr.frameStart != (137, 101)
    assert mgr.shape != (301, 199)


class ModeSensitiveFakeCamera(SnappingFakeCamera):
    """Models DCAM's cold-start ranges: while subarray mode is OFF every
    subarray write reads back as the full chip at position zero."""

    def setPropertyValue(self, name, value):
        self.set_calls.append((name, value))
        if name == 'subarray_mode':
            self.properties[name] = 2 if value in (b'ON', 'ON', 2) else 1
            return self.properties[name]
        if name.startswith('subarray_') and self.properties['subarray_mode'] != 2:
            value = 0 if name.endswith('pos') else 2048
            self.properties[name] = value
            return value
        if name in ('subarray_hpos', 'subarray_hsize'):
            value = self._snap(value, self.HSTEP)
        elif name in ('subarray_vpos', 'subarray_vsize'):
            value = self._snap(value, self.VSTEP)
        self.properties[name] = value
        return value


class RefusingSubarrayModeCamera(ModeSensitiveFakeCamera):
    def setPropertyValue(self, name, value):
        if name == 'subarray_mode' and value in (b'ON', 'ON', 2):
            self.set_calls.append((name, value))
            return self.properties[name]
        return super().setPropertyValue(name, value)


def test_crop_enables_subarray_mode_before_cold_start_roi_writes(monkeypatch):
    mgr = _make_manager(monkeypatch, ModeSensitiveFakeCamera)
    assert mgr._camera.properties['subarray_mode'] != 2
    mgr._camera.set_calls.clear()

    mgr.crop(hpos=512, vpos=256, hsize=400, vsize=300)

    assert mgr._camera.set_calls[0] == ('subarray_mode', b'ON')
    assert mgr._camera.properties['subarray_mode'] == 2
    assert mgr.frameStart == (512, 256)
    assert mgr.shape == (400, 304)  # vsize snaps to the camera's 8-pixel step


def test_crop_reports_full_chip_clamp_as_rejection(monkeypatch):
    mgr = _make_manager(monkeypatch, RefusingSubarrayModeCamera)

    with pytest.raises(RuntimeError, match='rejected requested ROI'):
        mgr.crop(hpos=512, vpos=256, hsize=400, vsize=300)

    assert mgr.frameStart == (0, 0)
    assert mgr.shape == mgr.fullShape


class MinimalPropertyCamera(SnappingFakeCamera):
    """A camera whose property table omits the subarray positions and mode.

    ``HamamatsuCamera.getPropertyValue`` returns a bare ``False`` -- not a
    ``(value, type)`` pair -- for anything the camera does not expose, and
    HamamatsuManager._syncSubarrayState documents that some cameras omit
    ``subarray_hpos``/``subarray_vpos`` while subarray mode is off. Subscripting
    that ``False`` raises TypeError, which would take down every crop --
    including the full-chip crop SettingsController performs during __init__,
    leaving ImSwitch unable to start.
    """

    OMITTED = ('subarray_hpos', 'subarray_vpos', 'subarray_mode')

    def __init__(self):
        super().__init__()
        for name in self.OMITTED:
            self.properties.pop(name, None)

    def getPropertyValue(self, name):
        if name not in self.properties:
            return False
        return super().getPropertyValue(name)

    def setPropertyValue(self, name, value):
        if name not in self.properties:
            self.set_calls.append((name, value))
            return False
        return super().setPropertyValue(name, value)


def test_custom_roi_survives_camera_without_subarray_position_properties(monkeypatch):
    """A camera that reports no positions/mode must still crop, not raise."""
    mgr = _make_manager(monkeypatch, MinimalPropertyCamera)

    mgr.crop(hpos=512, vpos=256, hsize=400, vsize=300)

    # Sizes are all this camera can report; positions fall back to 0 rather
    # than corrupting the geometry or blowing up.
    assert mgr.shape == (400, 304)
    assert mgr.frameStart == (0, 0)


def test_full_frame_survives_camera_without_subarray_mode_property(monkeypatch):
    """The full-chip crop run during startup must never raise."""
    mgr = _make_manager(monkeypatch, MinimalPropertyCamera)

    mgr.crop(hpos=0, vpos=0, hsize=2048, vsize=2048)

    assert mgr.frameStart == (0, 0)
    assert mgr.shape == (2048, 2048)


def test_crop_to_full_frame_explicitly_disables_subarray_mode(monkeypatch):
    mgr = _make_manager(monkeypatch, ModeSensitiveFakeCamera)
    mgr.crop(hpos=128, vpos=96, hsize=512, vsize=512)

    mgr.crop(hpos=0, vpos=0, hsize=2048, vsize=2048)

    assert mgr._camera.properties['subarray_mode'] == 1
    assert mgr._camera.set_calls[-1] == ('subarray_mode', b'OFF')


def test_crop_with_on_step_roi_is_unaffected(monkeypatch):
    """When the requested ROI already lands exactly on the hardware step,
    nothing should be snapped and the previous (correct) behaviour is
    preserved."""
    mgr = _make_manager(monkeypatch)

    mgr.crop(hpos=128, vpos=96, hsize=512, vsize=512)

    assert mgr.frameStart == (128, 96)
    assert mgr.shape == (512, 512)


def test_crop_to_full_frame_resets_cleanly(monkeypatch):
    mgr = _make_manager(monkeypatch)
    mgr.crop(hpos=128, vpos=96, hsize=512, vsize=512)

    mgr.crop(hpos=0, vpos=0, hsize=2048, vsize=2048)

    assert mgr.frameStart == (0, 0)
    assert mgr.shape == (2048, 2048)


# --- trigger-source read-back --------------------------------------------
#
# _updatePropertiesFromCamera compared getPropertyValue('trigger_source')
# against an int, but that method returns (value, type). Every branch was
# therefore dead and 'Trigger source' was never refreshed from the camera: the
# settings tree kept showing whatever ImSwitch had last written, even when the
# camera was in a different trigger mode. The camera-reported timings around it
# used [0] correctly, which is what made the omission easy to miss.

def _setCameraTrigger(mgr, source, mode):
    mgr._camera.properties['trigger_source'] = source
    mgr._camera.properties['trigger_mode'] = mode


def test_trigger_source_is_read_back_from_the_camera(monkeypatch):
    mgr = _make_manager(monkeypatch)
    _setCameraTrigger(mgr, source=2, mode=1)

    mgr._updatePropertiesFromCamera()

    assert mgr.parameters['Trigger source'].value == 'External "frame-trigger"'


def test_start_trigger_is_read_back_from_the_camera(monkeypatch):
    mgr = _make_manager(monkeypatch)
    _setCameraTrigger(mgr, source=2, mode=6)

    mgr._updatePropertiesFromCamera()

    assert mgr.parameters['Trigger source'].value == 'External "start-trigger"'


def test_internal_trigger_is_read_back_from_the_camera(monkeypatch):
    mgr = _make_manager(monkeypatch)
    mgr.setParameter('Trigger source', 'External "frame-trigger"')
    _setCameraTrigger(mgr, source=1, mode=1)

    mgr._updatePropertiesFromCamera()

    assert mgr.parameters['Trigger source'].value == 'Internal trigger'


def test_trigger_read_back_does_not_rewrite_the_camera(monkeypatch):
    """The read-back must not route back through _setTriggerSource: re-writing
    the properties it just read would make a plain exposure change poke the
    camera's trigger configuration."""
    mgr = _make_manager(monkeypatch)
    _setCameraTrigger(mgr, source=2, mode=6)
    mgr._camera.set_calls.clear()

    mgr._updatePropertiesFromCamera()

    triggerWrites = [call for call in mgr._camera.set_calls
                     if call[0] in ('trigger_source', 'trigger_mode')]
    assert triggerWrites == []


def test_setting_exposure_refreshes_the_displayed_trigger_source(monkeypatch):
    """The path this actually runs on: the GUI sets an exposure, and the
    manager refreshes every camera-reported value alongside it."""
    mgr = _make_manager(monkeypatch)
    _setCameraTrigger(mgr, source=2, mode=6)

    mgr.setParameter('Set exposure time', 0.02)

    assert mgr.parameters['Trigger source'].value == 'External "start-trigger"'


class RejectingTriggerCamera(SnappingFakeCamera):
    """Accepts the API call but leaves the hardware trigger state unchanged."""

    def setPropertyValue(self, name, value):
        if name in ('trigger_source', 'trigger_mode'):
            self.set_calls.append((name, value))
            return self.properties[name]
        return super().setPropertyValue(name, value)


def test_rejected_trigger_write_keeps_cache_at_hardware_readback(monkeypatch):
    mgr = _make_manager(monkeypatch, RejectingTriggerCamera)

    with pytest.raises(RuntimeError, match='rejected trigger source'):
        mgr.setParameter('Trigger source', 'External "frame-trigger"')

    assert mgr._camera.properties['trigger_source'] == 1
    assert mgr.parameters['Trigger source'].value == 'Internal trigger'


def test_accepted_trigger_write_updates_cache_after_hardware(monkeypatch):
    mgr = _make_manager(monkeypatch)

    mgr.setParameter('Trigger source', 'External "start-trigger"')

    assert mgr._camera.properties['trigger_source'] == 2
    assert mgr._camera.properties['trigger_mode'] == 6
    assert mgr.parameters['Trigger source'].value == 'External "start-trigger"'
