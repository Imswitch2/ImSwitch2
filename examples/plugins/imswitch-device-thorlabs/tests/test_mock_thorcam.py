"""The extracted ThorCam manager runs hardware-free via its mock camera."""

import numpy as np

from imswitch.pluginapi import DetectorInfo
from imswitch_device_thorlabs.detectors import ThorCamTSIManager


def _info(**props):
    base = {"cameraSerial": "MOCK_KIRALUX"}
    base.update(props)
    return DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName="thorlabs.tsi-camera",
        managerProperties=base,
        forAcquisition=True,
    )


def test_mock_camera_instantiates_and_yields_frames():
    mgr = ThorCamTSIManager(_info(), "ThorCam")
    try:
        frame = mgr.getLatestFrame()
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 2
        # Lifecycle must be safe.
        mgr.startAcquisition()
        mgr.stopAcquisition()
    finally:
        mgr.finalize()


def test_mock_camera_reports_a_model_and_full_shape():
    mgr = ThorCamTSIManager(_info(), "ThorCam")
    try:
        assert mgr.model  # non-empty mock model string
        assert len(mgr.fullShape) == 2
        assert all(dim > 0 for dim in mgr.fullShape)
    finally:
        mgr.finalize()
