import numpy as np

from imswitch_zhinst_devices.detectors import MockZhinstLockinDetectorManager


class FakeDetectorInfo:
    forAcquisition = True
    forFocusLock = False
    managerProperties = {
        "signal": "r",
        "sampleCount": 64,
        "frameShape": [8, 8],
        "pollDurationS": 0.001,
        "useMock": True,
        "mockSeed": 1,
    }


def test_mock_detector_returns_float_frame():
    manager = MockZhinstLockinDetectorManager(FakeDetectorInfo(), "ZI Lock-in")

    frame = manager.getLatestFrame()

    assert frame.shape == (8, 8)
    assert frame.dtype == np.float32
    assert np.isfinite(frame).all()


def test_mock_detector_buffers_chunks_while_running():
    manager = MockZhinstLockinDetectorManager(FakeDetectorInfo(), "ZI Lock-in")

    manager.startAcquisition()
    manager.getLatestFrame()
    manager.getLatestFrame()
    chunk = manager.getChunk()
    manager.stopAcquisition()

    assert chunk.shape == (2, 8, 8)
