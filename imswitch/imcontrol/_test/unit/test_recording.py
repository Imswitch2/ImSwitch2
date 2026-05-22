import os
import pytest

import h5py
import numpy as np

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, SaveFormat, DetectorInfo
from . import detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare


def record(qtbot, detectorInfos, *args, **kwargs):
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    filePerDetector, savedToDiskPerDetector = {}, {}

    def memoryRecordingAvailable(_, file, __, savedToDisk, detectorName):
        nonlocal filePerDetector, savedToDiskPerDetector
        filePerDetector[detectorName], savedToDiskPerDetector[detectorName] = file, savedToDisk
        return True

    recordingManager.startRecording(*args, **kwargs)
    with qtbot.waitSignals(
            [recordingManager.sigMemoryRecordingAvailable for _ in detectorInfos],
            check_params_cbs=[(lambda *args, detectorName=detectorName, **kwargs:
                               memoryRecordingAvailable(*args, detectorName=detectorName, **kwargs))
                              for detectorName in detectorInfos],
            timeout=30000
    ):
        pass

    return filePerDetector, savedToDiskPerDetector


@pytest.mark.parametrize('detectorInfos,numFrames',
                         [(detectorInfosBasic, 10), (detectorInfosNonSquare, 53)])
def test_recording_spec_frames(qtbot, detectorInfos, numFrames):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecFrames,
        savename='test_spec_frames',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recFrames=numFrames
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, f"Detector group '{detectorName}' not found"
        dataset = detector_group.get('data')
        assert dataset is not None, f"Data dataset not found in group '{detectorName}'"
        assert dataset.shape[0] == numFrames
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


@pytest.mark.parametrize('detectorInfos',
                         [detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare])
def test_recording_spec_time(qtbot, detectorInfos):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecTime,
        savename='test_spec_time',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recTime=5
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, f"Detector group '{detectorName}' not found"
        dataset = detector_group.get('data')
        assert dataset is not None, f"Data dataset not found in group '{detectorName}'"
        assert dataset.shape[0] > 0
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


def test_recording_dtype_preservation(qtbot):
    """HDF5 datasets must derive their dtype from the detector frames.

    Regression test: the old code hardcoded dtype='i2' (signed int16), which
    corrupted unsigned-16-bit values >32767 (wrap-around to negative) and
    needlessly widened 8-bit data. The dataset dtype must instead match the
    dtype of the frames the detector actually produces.

    Note: this asserts the *invariant* (dtype derived from frames, never the
    hardcoded 'i2'). Verifying a specific bit depth (e.g. uint8 stays uint8)
    would require a mock camera whose output dtype is configurable — see the
    recording-manager milestone in ROADMAP.md.
    """
    detectorInfos = detectorInfosBasic
    detectorName = next(iter(detectorInfos))

    filePerDetector, _ = record(
        qtbot,
        detectorInfos,
        detectorNames=[detectorName],
        recMode=RecMode.SpecFrames,
        savename='test_dtype',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {'testAttr1': 2, 'testAttr2': 'value'}},
        recFrames=5
    )

    file = filePerDetector[detectorName]
    with h5py.File(file) as h5pyFile:
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, 'recorded detector group missing'
        dataset = detector_group.get('data')
        assert dataset is not None, 'recorded dataset missing'
        # The bug: dtype was hardcoded to signed int16 ('i2'). The fix derives
        # it from the frame array, so it must NOT be 'i2' here (the mock
        # detector yields wider integer frames).
        assert dataset.dtype != np.dtype('i2'), \
            "HDF5 dtype must be derived from detector frames, not hardcoded 'i2'"
        assert np.issubdtype(dataset.dtype, np.integer), \
            f'expected an integer dataset dtype, got {dataset.dtype}'
    file.close()


def test_snap_hdf5_structured_layout(tmp_path):
    """Test that HDF5 snapshots use the structured layout with detector groups and metadata."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager, SaveMode, SaveFormat
    
    # Create a simple detector setup
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image and attrs
    detectorName = list(detectorInfosBasic.keys())[0]
    test_image = np.random.randint(0, 1000, (10, 128, 128), dtype=np.uint16)
    
    # Create attrs with categorized keys
    test_attrs = {
        detectorName: {
            'detector:exposure': 0.01,
            'detector:gain': 1.5,
            'lasers:laser1:power': 50,
            'lasers:laser1:wavelength': 488,
            'scan:size_um': 100.0,
            'uncategorized_key': 'value'
        }
    }
    
    # Snap the image
    savepath = str(tmp_path / 'test_snap')
    recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
    
    # Verify structured layout
    snap_file = f'{savepath}_{detectorName}.h5'
    assert os.path.exists(snap_file), f"Snapshot file not created: {snap_file}"
    
    with h5py.File(snap_file, 'r') as f:
        # Check file-level attrs
        assert 'timestamp' in f.attrs
        assert 'rec_mode' in f.attrs
        assert f.attrs['rec_mode'] == 'snap'
        
        # Check detector group exists
        assert detectorName in f, f"Detector group '{detectorName}' not found"
        det_group = f[detectorName]
        
        # Check data dataset
        assert 'data' in det_group, "data dataset not found in detector group"
        dataset = det_group['data']
        
        # Check dataset attrs
        assert 'detector_name' in dataset.attrs
        assert 'element_size_um' in dataset.attrs
        assert dataset.attrs['detector_name'] == detectorName
        
        # Check dtype preservation
        assert dataset.dtype == np.uint16, f"Expected uint16, got {dataset.dtype}"
        
        # Check shape (should be T, Y, X)
        assert dataset.shape == test_image.shape, f"Shape mismatch: {dataset.shape} != {test_image.shape}"
        
        # Check data content
        np.testing.assert_array_equal(dataset[:], test_image)
        
        # Check compression
        assert dataset.compression is not None, "Dataset should be compressed"
        assert dataset.compression == 'lzf', f"Expected lzf compression, got {dataset.compression}"
        assert dataset.shuffle, "Shuffle filter should be enabled"
        
        # Check metadata group structure
        assert 'metadata' in det_group, "metadata group not found"
        meta_group = det_group['metadata']
        
        # Check category subgroups
        assert 'detector' in meta_group, "detector category not found in metadata"
        assert 'lasers' in meta_group, "lasers category not found in metadata"
        assert 'scan' in meta_group, "scan category not found in metadata"
        
        # Check detector category attrs
        det_meta = meta_group['detector']
        assert 'exposure' in det_meta.attrs
        assert 'gain' in det_meta.attrs
        assert det_meta.attrs['exposure'] == 0.01
        assert det_meta.attrs['gain'] == 1.5
        
        # Check lasers category attrs
        laser_meta = meta_group['lasers']
        assert 'laser1:power' in laser_meta.attrs
        assert 'laser1:wavelength' in laser_meta.attrs
        assert laser_meta.attrs['laser1:power'] == 50
        assert laser_meta.attrs['laser1:wavelength'] == 488
        
        # Check scan category attrs
        scan_meta = meta_group['scan']
        assert 'size_um' in scan_meta.attrs
        assert scan_meta.attrs['size_um'] == 100.0
        
        # Check uncategorized attrs (flat in metadata group)
        assert 'uncategorized_key' in meta_group.attrs
        assert meta_group.attrs['uncategorized_key'] == 'value'


def test_snap_axis_ordering():
    """Test that snapshots use (T, Y, X) axis convention without reversal."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image with known pattern to detect axis swaps
    # Shape: (3, 100, 50) = (T, Y, X)
    test_image = np.zeros((3, 100, 50), dtype=np.uint16)
    test_image[0, :, 0] = 1  # Mark first frame, first column
    test_image[1, 0, :] = 2  # Mark second frame, first row
    test_image[2, :, :] = 3  # Mark third frame, all pixels
    
    detectorName = list(detectorInfosBasic.keys())[0]
    test_attrs = {detectorName: {'test': 'value'}}
    
    # Use a unique temporary filename
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        savepath = os.path.join(tmpdir, 'test_axis')
        recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
        
        # Read back and verify no axis transposition
        snap_file = f'{savepath}_{detectorName}.h5'
        with h5py.File(snap_file, 'r') as f:
            dataset = f[detectorName]['data']
            saved_data = dataset[:]
            
            # Should match exactly (T, Y, X) with no reversal
            assert saved_data.shape == test_image.shape
            np.testing.assert_array_equal(saved_data, test_image)
            
            # Verify the pattern markers are in correct positions
            assert saved_data[0, :, 0].sum() == 100  # First column of frame 0
            assert saved_data[1, 0, :].sum() == 100  # First row of frame 1
            assert saved_data[2, :, :].sum() == 3 * 100 * 50  # All of frame 2


def test_recording_stall_watchdog(qtbot, caplog, monkeypatch):
    """Test that the stall watchdog detects and aborts recordings when no frames arrive."""
    import time
    import logging
    
    # Use basic detector configuration
    detectorInfos = detectorInfosBasic
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Mock the detector's getChunk method to return no frames (simulating a stalled camera)
    detectorName = list(detectorInfos.keys())[0]
    original_getChunk = detectorsManager[detectorName].getChunk
    def mock_getChunk():
        return []  # Return empty list to simulate no frames arriving
    monkeypatch.setattr(detectorsManager[detectorName], 'getChunk', mock_getChunk)
    
    # Track signals
    stalled_detector = None
    
    def on_stalled(detector_name):
        nonlocal stalled_detector
        stalled_detector = detector_name
    
    recordingManager.sigRecordingStalled.connect(on_stalled)
    
    # Start recording with a very short stall timeout
    short_timeout = 0.5  # 0.5 seconds for faster test
    start_time = time.time()
    
    with caplog.at_level(logging.ERROR):
        recordingManager.startRecording(
            detectorNames=[detectorName],
            recMode=RecMode.SpecFrames,
            savename='test_stall',
            saveMode=SaveMode.RAM,
            attrs={detectorName: {}},
            recFrames=100,  # Request many frames that will never arrive
            stallTimeout=short_timeout
        )
        
        # Wait for stall signal with a reasonable timeout (stall timeout + margin)
        try:
            with qtbot.waitSignal(recordingManager.sigRecordingStalled, timeout=int((short_timeout + 2) * 1000)):
                pass
        except Exception:
            # If signal not emitted, the test will check the state below
            pass
    
    elapsed = time.time() - start_time
    
    # Verify the watchdog triggered
    assert stalled_detector is not None, "Stall signal should have been emitted"
    assert stalled_detector == detectorName, f"Stalled detector '{stalled_detector}' should match '{detectorName}'"
    
    # Verify error was logged
    error_logs = [r for r in caplog.records if r.levelno == logging.ERROR and 'stalled' in r.message]
    assert len(error_logs) > 0, "Expected ERROR log message about stall"
    assert 'no frames received' in error_logs[0].message
    
    # Verify it triggered within reasonable time (stall timeout + some margin for processing)
    assert elapsed >= short_timeout, f"Watchdog triggered too early: {elapsed:.2f}s < {short_timeout}s"
    assert elapsed < short_timeout + 2, f"Watchdog took too long: {elapsed:.2f}s > {short_timeout + 2}s"
    
    # Verify recording ended cleanly (thread not hung)
    qtbot.wait(200)  # Small delay to let thread finish
    assert not recordingManager.record, "Recording should have stopped after stall"


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
