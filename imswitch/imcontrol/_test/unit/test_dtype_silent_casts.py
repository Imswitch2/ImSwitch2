"""
Tests for Phase 1, Task 2: elimination of silent dtype casts on the
acquisition/recording path (APDManager pixel writes and RecordingWorker._getNewFrames).
"""
import numpy as np
import pytest
from unittest.mock import Mock, MagicMock, patch

from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model import DetectorInfo, DetectorsManager, RecordingManager


@pytest.fixture
def mock_nidaq_manager():
    """Mock NidaqManager for APDManager tests."""
    nidaq = Mock()
    nidaq.sigScanBuilt = Mock()
    nidaq.sigScanBuilt.connect = Mock()
    nidaq.sigScanStarted = Mock()
    nidaq.sigScanStarted.connect = Mock()
    return nidaq


@pytest.fixture
def apd_detector_info():
    """Create a basic APDManager detector info."""
    return DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='APDManager',
        managerProperties={
            'ctrInputLine': 0,
            'terminal': '/Dev1/PFI0',
            'deviceName': 'Dev1'
        },
        forAcquisition=True
    )


class TestAPDManagerExplicitCasts:
    """Test that APDManager pixel writes use explicit dtype conversion."""

    def test_updateImage_2D_uint16_rounds_floats(self, apd_detector_info, mock_nidaq_manager):
        """Verify non-TTL (uint16) path rounds float pixels and preserves declared dtype."""
        apd = APDManager(apd_detector_info, 'APD', mock_nidaq_manager)
        
        # Non-TTL mode: dtype should be uint16
        assert apd._ttlmultiplying is False
        assert apd.dtype == np.dtype(np.uint16)
        
        # Allocate a 2D buffer (simulates initiateImage allocation)
        apd._image = np.zeros((100, 100), dtype=np.uint16)
        apd._APDManager__shape = (100, 100)
        apd._linestep = 1
        
        # updateImage with float-valued pixels (e.g., from photon counts)
        float_pixels = np.array([1.3, 2.7, 3.5, 4.2, 5.9], dtype=np.float64)
        apd.updateImage(float_pixels, pos=(0,))
        
        # Assert buffer dtype unchanged
        assert apd._image.dtype == np.uint16, "Buffer dtype changed from uint16"
        
        # Assert values are ROUNDED (not truncated): 1.3→1, 2.7→3, 3.5→4, 4.2→4, 5.9→6
        expected = np.array([1, 3, 4, 4, 6], dtype=np.uint16)
        np.testing.assert_array_equal(apd._image[0, :5], expected,
                                      err_msg="Float pixels not correctly rounded to uint16")

    def test_updateImage_2D_float32_preserves_nan(self, apd_detector_info, mock_nidaq_manager):
        """Verify TTL (float32) path preserves NaN markers."""
        apd = APDManager(apd_detector_info, 'APD', mock_nidaq_manager)
        
        # TTL mode: dtype should be float32
        apd._ttlmultiplying = True
        assert apd.dtype == np.dtype(np.float32)
        
        # Allocate a 2D buffer with float32
        apd._image = np.zeros((100, 100), dtype=np.float32)
        apd._APDManager__shape = (100, 100)
        apd._linestep = 1
        
        # updateImage with float pixels including NaN
        float_pixels = np.array([1.0, np.nan, 3.0, np.nan, 5.0], dtype=np.float64)
        apd.updateImage(float_pixels, pos=(0,))
        
        # Assert buffer dtype unchanged
        assert apd._image.dtype == np.float32, "Buffer dtype changed from float32"
        
        # Assert NaN markers are preserved
        result = apd._image[0, :5]
        assert np.isnan(result[1]), "NaN marker at index 1 not preserved"
        assert np.isnan(result[3]), "NaN marker at index 3 not preserved"
        np.testing.assert_array_equal(result[[0, 2, 4]], [1.0, 3.0, 5.0],
                                      err_msg="Non-NaN values incorrectly converted")

    def test_updateImage_3D_uint16_explicit_cast(self, apd_detector_info, mock_nidaq_manager):
        """Verify 3D/higher-dim path also uses explicit dtype conversion."""
        apd = APDManager(apd_detector_info, 'APD', mock_nidaq_manager)
        
        # Non-TTL mode
        assert apd.dtype == np.dtype(np.uint16)
        
        # Allocate a 3D buffer (Z stack)
        apd._image = np.zeros((10, 100, 100), dtype=np.uint16)  # (Nz, Ny, Nx)
        apd._APDManager__shape = (10, 100, 100)
        apd._linestep = 1
        
        # updateImage with float pixels into a specific Z slice
        float_pixels = np.array([10.6, 20.3, 30.9], dtype=np.float64)
        apd.updateImage(float_pixels, pos=(5, 0))  # z=5, y=0
        
        # Assert buffer dtype unchanged
        assert apd._image.dtype == np.uint16
        
        # Assert values are rounded: 10.6→11, 20.3→20, 30.9→31
        expected = np.array([11, 20, 31], dtype=np.uint16)
        np.testing.assert_array_equal(apd._image[5, 0, :3], expected,
                                      err_msg="3D pixel write not correctly rounded")

    def test_samples_to_pixels_empty_returns_manager_dtype(self, apd_detector_info, mock_nidaq_manager):
        """Verify samples_to_pixels returns empty array with manager's dtype, not float."""
        apd = APDManager(apd_detector_info, 'APD', mock_nidaq_manager)
        
        # Test both uint16 (non-TTL) and float32 (TTL) modes
        for ttl_mode, expected_dtype in [(False, np.uint16), (True, np.float32)]:
            apd._ttlmultiplying = ttl_mode
            assert apd.dtype == np.dtype(expected_dtype)
            
            # Create a minimal mock ScanWorker instance to test the method in isolation
            from imswitch.imcontrol.model.managers.detectors.APDManager import ScanWorker
            mock_worker = Mock(spec=ScanWorker)
            mock_worker._manager = apd
            mock_worker._frac_det_dwell = 10
            
            # Call the actual samples_to_pixels method with insufficient data
            result = ScanWorker.samples_to_pixels(mock_worker, np.array([1, 2, 3]))
            
            # Assert empty result has manager's dtype, NOT float
            assert result.dtype == expected_dtype, \
                f"Empty result dtype is {result.dtype}, expected {expected_dtype} (TTL={ttl_mode})"
            assert len(result) == 0, f"Expected empty array (TTL={ttl_mode})"


class TestRecordingWorkerDtypePreservation:
    """Test that RecordingWorker._getNewFrames preserves detector's native dtype."""

    def test_getNewFrames_preserves_uint16(self, qtbot):
        """Verify _getNewFrames preserves uint16 dtype for normal chunk."""
        # Use the standard test fixture from __init__.py
        from imswitch.imcontrol._test.unit import detectorInfosBasic
        
        detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
        recordingManager = RecordingManager(detectorsManager)
        
        # Inject mock frames into detector's chunk consumer queue
        detector = detectorsManager['CAM']
        assert detector.dtype == np.dtype(np.uint16), "Expected mock detector to be uint16"
        
        # Simulate frames in the detector's queue
        frame1 = np.ones((1024, 1024), dtype=np.uint16) * 100
        frame2 = np.ones((1024, 1024), dtype=np.uint16) * 200
        detector._chunkConsumers = {'RecordingManager': [frame1, frame2]}
        
        # Create a recording worker and call _getNewFrames
        from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker
        worker = RecordingWorker(recordingManager)
        
        result = worker._getNewFrames('CAM')
        
        # Assert dtype preserved (not promoted to float64)
        assert result.dtype == np.uint16, f"dtype promoted to {result.dtype}, expected uint16"
        assert result.shape == (2, 1024, 1024), f"Unexpected shape {result.shape}"

    def test_getNewFrames_empty_chunk_returns_detector_dtype(self, qtbot):
        """Verify _getNewFrames returns empty array with detector's dtype (not float64)."""
        # Use the standard test fixture from __init__.py
        from imswitch.imcontrol._test.unit import detectorInfosBasic
        
        detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
        recordingManager = RecordingManager(detectorsManager)
        
        detector = detectorsManager['CAM']
        assert detector.dtype == np.dtype(np.uint16)
        
        # Empty chunk: no frames queued
        detector._chunkConsumers = {'RecordingManager': []}
        
        from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker
        worker = RecordingWorker(recordingManager)
        
        result = worker._getNewFrames('CAM')
        
        # Assert empty array has detector's dtype (uint16), NOT float64
        assert result.dtype == np.uint16, f"Empty chunk dtype is {result.dtype}, expected uint16"
        assert result.shape == (0,), f"Expected shape (0,), got {result.shape}"


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
