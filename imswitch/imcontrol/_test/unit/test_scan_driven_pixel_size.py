"""Pixel-size contracts for scan-driven detectors and recordings."""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.ScanControllerPointScan import (
    ScanControllerPointScan,
)
from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.managers.recording_metadata import MODE_SCAN


POINT_DETECTOR_MANAGERS = [APDManager, PMTManager, SwabianTimeTaggerManager]


@pytest.mark.parametrize("managerClass", POINT_DETECTOR_MANAGERS)
def test_scan_driven_detector_scale_matches_pixel_size(managerClass):
    manager = SimpleNamespace()
    managerClass.setPixelSize(manager, [0.05, 0.2])

    scale = list(managerClass.scale.fget(manager))
    pixelSizeUm = managerClass.pixelSizeUm.fget(manager)
    assert managerClass.isScanDriven.fget(manager)
    assert scale == [0.2, 0.05]
    assert pixelSizeUm == [1, 0.2, 0.05]
    assert scale == pixelSizeUm[1:]


@pytest.mark.parametrize("managerClass", POINT_DETECTOR_MANAGERS)
def test_scan_driven_pixel_size_is_always_zyx_triple(managerClass):
    """``pixelSizeUm`` is ``[Z, Y, X]`` -- three entries whatever the scan rank.

    A 3-axis scan publishes ``pixel_sizes`` as ``[x, y, z]``. Splatting that
    behind a leading 1 yields a 4-entry list whose ``[1]``/``[2]`` slots are the
    Z and Y steps, so every consumer indexing ``pixelSizeUm[1:]`` as (Y, X)
    silently reads a z-stack one axis off.
    """
    manager = SimpleNamespace()
    managerClass.setPixelSize(manager, [0.05, 0.2, 1.5])

    pixelSizeUm = managerClass.pixelSizeUm.fget(manager)

    assert pixelSizeUm == [1.5, 0.2, 0.05]


@pytest.mark.parametrize("managerClass", POINT_DETECTOR_MANAGERS)
def test_scan_driven_pixel_size_survives_single_axis_scan(managerClass):
    """A 1-axis line scan publishes one step; Y falls back to it, Z stays 1."""
    manager = SimpleNamespace()
    managerClass.setPixelSize(manager, [0.05])

    assert managerClass.pixelSizeUm.fget(manager) == [1.0, 0.05, 0.05]


def test_point_scan_exposes_padded_scan_geometry():
    controller = ScanControllerPointScan.__new__(ScanControllerPointScan)
    controller._analogParameterDict = {
        "axis_length": [10.0, 20.0],
        "axis_step_size": [3.0, 0.2],
    }
    controller.getParameters = lambda: None

    assert controller.getDimsScan() == (3, 100, 0)
    assert controller.getScanStepSizes() == [3.0, 0.2, 0.0]


class _StalePointDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 8.8, 9.9]
    isScanDriven = True
    parameters = {}


class _Camera:
    """Free-running detector: its own pixel size wins over the scan step."""
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.11, 0.11]
    isScanDriven = False
    parameters = {}


class _Detectors:
    def __init__(self, detector):
        self._detector = detector

    def __getitem__(self, name):
        return self._detector

    def execOnAll(self, func, *, condition=None):
        return {}


def test_recording_ome_xy_uses_scan_steps_not_stale_detector_cache():
    manager = RecordingManager(_Detectors(_StalePointDetector()))

    meta = manager.buildOmeMeta(
        "APD",
        MODE_SCAN,
        1,
        scanDims=(200, 100, 0),
        scanStepSizes=(0.05, 0.2, 0.0),
    )

    assert meta.axes_string == "YX"
    assert meta.scale == [0.2, 0.05]
    assert meta.tiff_metadata()["PhysicalSizeY"] == 0.2
    assert meta.tiff_metadata()["PhysicalSizeX"] == 0.05


def test_recording_ome_ignores_zero_steps_of_inactive_scan_axes():
    """An inactive axis reports step 0; 0 is never a valid PhysicalSize."""
    manager = RecordingManager(_Detectors(_StalePointDetector()))

    meta = manager.buildOmeMeta(
        "APD",
        MODE_SCAN,
        8,
        scanDims=(200, 0, 0),
        scanStepSizes=(0.05, 0.0, 0.0),
    )

    assert meta.tiff_metadata()["PhysicalSizeX"] == 0.05
    # Y was not scanned, so it keeps the detector's own value rather than 0.
    assert meta.tiff_metadata()["PhysicalSizeY"] == 8.8
    assert all(scale != 0 for scale in meta.scale)


def test_recording_ome_zstack_takes_all_three_axes_from_scan():
    manager = RecordingManager(_Detectors(_StalePointDetector()))

    meta = manager.buildOmeMeta(
        "APD",
        MODE_SCAN,
        20,
        scanDims=(200, 100, 20),
        scanStepSizes=(0.05, 0.2, 1.5),
    )

    assert meta.axes_string == "ZYX"
    assert meta.scale == [1.5, 0.2, 0.05]


def test_recording_ome_keeps_camera_pixel_size_during_a_scan():
    """A camera triggered by the scan is not calibrated by the scan step."""
    manager = RecordingManager(_Detectors(_Camera()))

    meta = manager.buildOmeMeta(
        "Cam",
        MODE_SCAN,
        20,
        scanDims=(200, 100, 20),
        scanStepSizes=(0.05, 0.2, 1.5),
    )

    assert meta.tiff_metadata()["PhysicalSizeY"] == 0.11
    assert meta.tiff_metadata()["PhysicalSizeX"] == 0.11
