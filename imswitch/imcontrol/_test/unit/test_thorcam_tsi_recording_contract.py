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
