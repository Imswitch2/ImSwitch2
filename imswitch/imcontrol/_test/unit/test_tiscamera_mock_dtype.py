"""Recording dtype contract for the TIS camera mock (recording_dataflow_plan,
ROADMAP M10 Phase 1): DetectorManager.dtype is the single source of truth for
the storer's dataset dtype, and frames must match it. MockCameraTIS used to
emit float64 frames while the manager declared uint16, so every mock recording
logged an HDF5Storer dtype-mismatch warning and cast each frame. MockCameraTIS
is also the fallback mock for Basler/AV/PiCam/ESP32Cam/JetsonCam/GXPIPY
managers, so this contract covers those too.
"""
import numpy as np

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.interfaces.tiscamera_mock import MockCameraTIS
from imswitch.imcontrol.model.managers.detectors.TISManager import TISManager


def _make_manager(monkeypatch):
    monkeypatch.setattr(TISManager, '_getTISObj', lambda self, cameraId: MockCameraTIS())
    info = DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='TISManager',
        managerProperties={
            'cameraListIndex': 'mock',
            'tis': {},
            'cameraPixelSizeUm': 0.5,
        },
        forAcquisition=True,
        forFocusLock=False,
    )
    return TISManager(info, 'MockTISCam')


def test_mock_grabFrame_is_uint16():
    cam = MockCameraTIS()
    frame = cam.grabFrame()
    assert frame.dtype == np.uint16
    assert frame.shape == cam.shape


def test_mock_getLastChunk_is_uint16():
    cam = MockCameraTIS()
    chunk = cam.getLastChunk()
    assert chunk.dtype == np.uint16
    assert chunk.ndim == 3


def test_manager_frames_match_declared_dtype(monkeypatch):
    mgr = _make_manager(monkeypatch)

    assert mgr.dtype == np.dtype(np.uint16)

    frame = mgr.getLatestFrame()
    assert frame.dtype == mgr.dtype, (
        f'Mock frame dtype {frame.dtype} does not match declared '
        f'detector dtype {mgr.dtype}; recordings would cast every frame'
    )

    chunk = mgr.getChunk()
    assert chunk.dtype == mgr.dtype

    # dtype must stay stable after frames have been grabbed (DetectorManager
    # derives it from the latest frame once one exists).
    assert mgr.dtype == np.dtype(np.uint16)
    assert mgr.bitDepth == 16


def test_mock_frames_span_plausible_camera_range():
    """Frames should carry non-trivial integer signal (noise floor around the
    Poisson mean), not an all-zero image collapsed by float->uint casting."""
    cam = MockCameraTIS()
    frame = cam.grabFrame()
    assert frame.max() > 0
    assert frame.max() <= 65535


def test_mock_tis_properties_persist_for_reconnect_replay():
    cam = MockCameraTIS()

    cam.setPropertyValue('exposure', 17)
    cam.setPropertyValue('gain', 4)
    cam.setPropertyValue('brightness', 2)

    assert cam.getPropertyValue('exposure') == 17
    assert cam.getPropertyValue('gain') == 4
    assert cam.getPropertyValue('brightness') == 2
    assert cam.exposure == 17
    assert cam.gain == 4
    assert cam.brightness == 2


def test_mock_tis_roi_updates_reported_dimensions():
    cam = MockCameraTIS()

    cam.setROI(11, 13, 320, 240)

    assert cam.shape == (240, 320)
    assert cam.getPropertyValue('image_width') == 320
    assert cam.getPropertyValue('image_height') == 240
