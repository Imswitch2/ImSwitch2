"""Regression tests for ThorCamTSIManager.getChunk() recording contract.

Background: ThorCamTSIManager.getChunk() used to return a single 2-D (H, W)
frame instead of the documented 3-D (numFrames, height, width) contract. When
DetectorManager.readChunk() calls list.extend(self.getChunk()), a 2-D array
is unpacked along the first axis, so one (H, W) frame becomes H separate (W,)
row vectors. This corrupts ALL ThorCam TSI recordings in every mode.

Second defect: in hardware-trigger mode, getLatestFrame() fabricates zero
frames when nothing is pending. Because getChunk() delegated to
getLatestFrame(), recordings waiting on hardware triggers received fabricated
black frames instead of empty chunks.

These tests pin the TARGET contract that getChunk() MUST satisfy:
- Always return 3-D (n, H, W), never 2-D
- Return empty 3-D chunk when no frames are pending (hardware mode)
- Never fabricate zero frames for recording

See DetectorManager.getChunk() docstring and readChunk() implementation.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.ThorCamTSIManager import (
    ThorCamTSIManager,
)
from imswitch.imcontrol.model.interfaces.thorcamera_tsi import (
    MockThorTSICamera, ThorTSICamera,
)


def _make_manager(**kwargs):
    """Build a ThorCamTSIManager backed by MockThorTSICamera.
    
    Args:
        **kwargs: override managerProperties (e.g. defaults={'operation_mode': 'Hardware'})
    """
    props = {
        'cameraSerial': 'MOCK_TSI',
        'defaults': kwargs.get('defaults', {}),
    }
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='ThorCamTSIManager',
        managerProperties=props,
        forAcquisition=True,
        forFocusLock=False,
    )
    return ThorCamTSIManager(info, 'ThorCam')


def test_hardware_mode_no_trigger_returns_empty_3d_chunk():
    """Hardware mode, no trigger → empty 3-D chunk (not 2-D, not fabricated zeros)."""
    mgr = _make_manager(defaults={'operation_mode': 'Hardware'})
    
    chunk = mgr.getChunk()
    
    assert chunk.ndim == 3, "getChunk must return 3-D, not 2-D"
    assert len(chunk) == 0, "No trigger → empty chunk, not fabricated frames"


def test_hardware_mode_n_triggers_returns_n_frames():
    """Hardware mode, N triggers → N real frames in a 3-D chunk."""
    mgr = _make_manager(defaults={'operation_mode': 'Hardware'})
    
    # Simulate 3 hardware triggers
    mgr._camera.simulate_hardware_trigger(3)
    
    chunk1 = mgr.getChunk()
    assert chunk1.ndim == 3
    assert chunk1.shape[0] == 3, "3 triggers should produce 3 frames"
    
    # Subsequent call with no new triggers → empty
    chunk2 = mgr.getChunk()
    assert chunk2.ndim == 3
    assert len(chunk2) == 0, "No new triggers → empty chunk"


def test_software_mode_returns_3d_single_frame_chunk():
    """Software mode → 3-D single-frame chunks, never 2-D."""
    mgr = _make_manager(defaults={'operation_mode': 'Software'})
    
    chunk = mgr.getChunk()
    
    assert chunk.ndim == 3, "Must return 3-D even for single frame"
    assert chunk.shape[0] == 1, "Software mode should produce 1 frame per call"
    # Verify it's a real frame (H, W), not a row vector
    h = mgr._camera.image_height_pixels
    w = mgr._camera.image_width_pixels
    assert chunk.shape == (1, h, w)


def test_readchunk_distributes_whole_frames_not_rows():
    """readChunk returns whole (H, W) frames, not unpacked (W,) row vectors."""
    mgr = _make_manager(defaults={'operation_mode': 'Hardware'})
    h = mgr._camera.image_height_pixels
    w = mgr._camera.image_width_pixels
    
    # Feed 2 hardware triggers
    mgr._camera.simulate_hardware_trigger(2)
    
    frames = mgr.readChunk('test_consumer')
    
    assert len(frames) == 2, "Should get 2 whole frames"
    for frame in frames:
        assert frame.shape == (h, w), f"Each frame must be (H, W), got {frame.shape}"
        assert frame.ndim == 2, "Individual frames are 2-D"


def test_no_fabricated_frames_in_hardware_idle():
    """Hardware mode, no triggers → readChunk returns empty list, not black frames."""
    mgr = _make_manager(defaults={'operation_mode': 'Hardware'})
    
    # Poll several times with no triggers
    for _ in range(5):
        frames = mgr.readChunk('recording')
        assert frames == [], "No triggers → no frames, never fabricated zeros"


def test_small_roi_still_returns_3d():
    """Regression: small ROI shouldn't change chunk dimensionality."""
    mgr = _make_manager(defaults={'operation_mode': 'Software'})
    
    # Set a small ROI (e.g. 128x128)
    mgr.setParameter('ROI X0', 0)
    mgr.setParameter('ROI Y0', 0)
    mgr.setParameter('ROI X1', 127)
    mgr.setParameter('ROI Y1', 127)
    
    chunk = mgr.getChunk()
    
    assert chunk.ndim == 3
    assert chunk.shape == (1, 128, 128)


def test_startacquisition_arms_disarmed_camera():
    """startAcquisition arms a disarmed camera."""
    mgr = _make_manager()
    
    # Disarm the camera (it starts armed from __init__)
    mgr._camera.disarm()
    assert not mgr._camera.is_armed, "Camera should be disarmed"
    
    # startAcquisition should re-arm it
    mgr.startAcquisition()
    assert mgr._camera.is_armed, "startAcquisition should arm the camera"


def test_startacquisition_idempotent_when_armed():
    """startAcquisition is idempotent when already armed."""
    mgr = _make_manager()
    
    # Camera is armed from __init__
    assert mgr._camera.is_armed, "Camera should start armed"
    
    # Calling startAcquisition twice should not raise
    mgr.startAcquisition()
    assert mgr._camera.is_armed, "Camera should still be armed"
    
    mgr.startAcquisition()
    assert mgr._camera.is_armed, "Camera should still be armed after second call"


def test_bulb_mode_drains_hardware_triggers():
    """Bulb mode drains hardware triggers, no software trigger."""
    mgr = _make_manager(defaults={'operation_mode': 'Bulb'})
    
    # Simulate 2 hardware triggers
    mgr._camera.simulate_hardware_trigger(2)
    
    chunk = mgr.getChunk()
    
    assert chunk.ndim == 3, "Must return 3-D chunk"
    assert chunk.shape[0] == 2, "2 hardware triggers should produce 2 frames"
    
    # Verify no pending software triggers were created
    assert mgr._camera._pending_software_triggers == 0, "Bulb mode should not issue software triggers"
    
    # A second getChunk() with no new triggers returns empty
    chunk2 = mgr.getChunk()
    assert chunk2.ndim == 3
    assert len(chunk2) == 0, "No new triggers → empty chunk"


def test_bulb_mode_no_trigger_empty_chunk():
    """Bulb mode, no trigger → empty 3-D chunk (never fabricated zeros)."""
    mgr = _make_manager(defaults={'operation_mode': 'Bulb'})
    
    chunk = mgr.getChunk()
    
    assert chunk.ndim == 3, "Must return 3-D"
    assert len(chunk) == 0, "No trigger → empty chunk, not fabricated frames"



class _FakeSdkCamera:
    """The thorlabs_tsi_sdk surface ThorTSICamera's re-arm path touches."""

    def __init__(self):
        self.is_armed = False
        self.frames_per_trigger_zero_for_unlimited = 1
        self.operation_mode = 0
        self.arm_depths = []

    def arm(self, frames_to_buffer):
        self.is_armed = True
        self.arm_depths.append(int(frames_to_buffer))

    def disarm(self):
        self.is_armed = False


def _wrapper_over(sdk):
    from imswitch.imcontrol.model.interfaces.thorcamera_tsi import ThorTSICamera
    wrapper = ThorTSICamera.__new__(ThorTSICamera)
    wrapper._camera = sdk
    return wrapper


def test_a_trigger_mode_change_rearms_at_the_depth_the_camera_was_armed_with():
    """The re-arm used to read frames-per-trigger (1) as the ring depth, so
    the first Operation Mode change -- which every startup makes while
    restoring detector state -- shrank the SDK ring from four frames to one
    for the rest of the session."""
    sdk = _FakeSdkCamera()
    wrapper = _wrapper_over(sdk)
    wrapper.arm(buffer_size=4)
    assert sdk.arm_depths == [4]

    wrapper.set_trigger_mode('hardware')
    assert sdk.is_armed and sdk.operation_mode == 1
    assert sdk.arm_depths == [4, 4]

    wrapper.set_trigger_mode('software')
    assert sdk.arm_depths == [4, 4, 4]


def test_the_ring_depth_is_a_manager_property():
    from imswitch.imcontrol.model.interfaces.thorcamera_tsi import DEFAULT_FRAME_BUFFER_DEPTH

    assert _make_manager()._camera.armed_buffer_size == DEFAULT_FRAME_BUFFER_DEPTH == 4

    info = DetectorInfo(
        analogChannel=None, digitalLine=None, managerName='ThorCamTSIManager',
        managerProperties={'cameraSerial': 'MOCK_TSI', 'frameBufferDepth': 8},
        forAcquisition=True, forFocusLock=False,
    )
    mgr = ThorCamTSIManager(info, 'ThorCam')
    assert mgr._camera.armed_buffer_size == 8


def _make_software_only_manager(monkeypatch, defaults=None):
    """Build a manager around a CS165MU-like software-only camera."""
    def _software_only_camera(_self, serial, dll_location):
        return MockThorTSICamera(
            serial=serial,
            supported_trigger_modes=('software',),
            supports_trigger_polarity=False,
        )

    monkeypatch.setattr(ThorCamTSIManager, '_initCamera', _software_only_camera)
    return _make_manager(defaults=defaults or {})


def test_software_only_camera_needs_no_config_migration(monkeypatch):
    """Legacy trigger_polarity config is harmless on a software-only camera."""
    mgr = _make_software_only_manager(
        monkeypatch,
        defaults={
            'operation_mode': 'Software',
            'trigger_polarity': 'Active High',
        },
    )
    try:
        assert mgr.parameters['Operation Mode'].options == ['Software']
        assert 'Trigger Polarity' not in mgr.parameters
        assert mgr.getChunk().shape[0] == 1
    finally:
        mgr.finalize()


def test_software_only_camera_rejects_explicit_hardware_default(monkeypatch):
    """Do not silently downgrade a requested hardware-synchronised setup."""
    with pytest.raises(ValueError, match="Hardware.*not supported"):
        _make_software_only_manager(
            monkeypatch,
            defaults={'operation_mode': 'Hardware'},
        )


def test_trigger_mode_rejection_does_not_corrupt_parameter_state():
    """A rejected SDK write must leave the manager/UI value unchanged."""
    mgr = _make_manager(defaults={'operation_mode': 'Software'})
    try:
        previous = mgr.parameters['Operation Mode'].value

        def reject_mode(_mode):
            raise RuntimeError('SDK rejected mode')

        mgr._camera.set_trigger_mode = reject_mode
        with pytest.raises(RuntimeError, match='SDK rejected mode'):
            mgr.setParameter('Operation Mode', 'Hardware')
        assert mgr.parameters['Operation Mode'].value == previous
    finally:
        mgr.finalize()


def test_interface_probe_detects_software_only_camera():
    """Capability probing is SDK-behaviour based, not model-name based."""
    class SoftwareOnlyVendorCamera:
        model = 'Any software-only TSI camera'

        def __init__(self):
            self._operation_mode = 0

        @property
        def operation_mode(self):
            return self._operation_mode

        @operation_mode.setter
        def operation_mode(self, value):
            if value != 0:
                raise RuntimeError('Command is not supported')
            self._operation_mode = value

    camera = ThorTSICamera.__new__(ThorTSICamera)
    camera._camera = SoftwareOnlyVendorCamera()
    assert camera._detect_supported_trigger_modes() == ('software',)
    camera._supported_trigger_modes = ('software',)
    assert camera._detect_trigger_polarity_support() is False


def test_trigger_changes_restore_actual_arm_buffer_size():
    """Runtime trigger edits disarm/re-arm with the last arm() buffer size."""
    class VendorCamera:
        model = 'Trigger-capable TSI camera'

        def __init__(self):
            self.is_armed = True
            self.operation_mode = 0
            self.trigger_polarity = 0
            self.arm_calls = []

        def disarm(self):
            self.is_armed = False

        def arm(self, buffer_size):
            self.arm_calls.append(buffer_size)
            self.is_armed = True

    camera = ThorTSICamera.__new__(ThorTSICamera)
    camera._camera = VendorCamera()
    camera._supported_trigger_modes = ('software', 'hardware', 'bulb')
    camera._supports_trigger_polarity = True
    camera._last_arm_buffer_size = 4

    camera.set_trigger_mode('hardware')
    camera.set_trigger_polarity('active_low')

    assert camera._camera.operation_mode == 1
    assert camera._camera.trigger_polarity == 1
    assert camera._camera.arm_calls == [4, 4]
