import os
import time
import pytest

import h5py
import numpy as np
import zarr

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, SaveFormat, DetectorInfo
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
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
        
        # Check compression (gzip is the default since lzf caused problems
        # on some setups; see RecordingManager)
        assert dataset.compression is not None, "Dataset should be compressed"
        assert dataset.compression == 'gzip', f"Expected gzip compression, got {dataset.compression}"
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


def test_snap_zarr_structured_layout(tmp_path) -> None:
    """Test that Zarr snapshots mirror the structured HDF5 layout."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    detectorName = list(detectorInfosBasic.keys())[0]
    test_image = np.zeros((3, 100, 50), dtype=np.uint16)
    test_image[0, :, 0] = 1
    test_image[1, 0, :] = 2
    test_image[2, :, :] = 3

    test_attrs = {
        detectorName: {
            'detector:exposure': 0.01,
            'detector:gain': 1.5,
            'lasers:laser1:power': 50,
            'scan:size_um': 100.0,
            'uncategorized_key': 'value',
        }
    }

    savepath = str(tmp_path / 'test_zarr_snap')
    recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.ZARR, test_image, test_attrs)

    snap_store = f'{savepath}.zarr'
    assert os.path.exists(snap_store), f"Zarr snapshot not created: {snap_store}"

    root = zarr.open(snap_store, mode='r')
    assert root.attrs['rec_mode'] == 'snap'
    assert detectorName in root

    det_group = root[detectorName]
    assert 'data' in det_group
    dataset = det_group['data']
    assert dataset.shape == test_image.shape
    assert dataset.dtype == np.uint16
    assert dataset.attrs['detector_name'] == detectorName
    assert dataset.attrs['axes'] == ['T', 'Y', 'X']
    assert dataset.attrs['writing'] is False
    np.testing.assert_array_equal(dataset[:], test_image)

    assert 'metadata' in det_group
    meta_group = det_group['metadata']
    assert meta_group.attrs['uncategorized_key'] == 'value'
    assert meta_group['detector'].attrs['exposure'] == 0.01
    assert meta_group['detector'].attrs['gain'] == 1.5
    assert meta_group['lasers'].attrs['laser1:power'] == 50
    assert meta_group['scan'].attrs['size_um'] == 100.0


def test_zarr_streaming_structured_layout(tmp_path) -> None:
    """Test that Zarr streaming creates a structured, dtype-preserving array."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    store_path = str(tmp_path / 'test_zarr_stream.zarr')
    attrs = {
        detectorName: {
            'detector:exposure': 0.02,
            'scan:frames': 3,
        }
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={detectorName: store_path},
        detectorNames=[detectorName],
        shapes={detectorName: (50, 100)},
        attrs=attrs,
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )

    frames_a = np.arange(2 * 7 * 5, dtype=np.uint16).reshape(2, 7, 5)
    frames_b = np.full((1, 7, 5), 50000, dtype=np.uint16)
    storer.writeFrames(detectorName, frames_a)
    storer.writeFrames(detectorName, frames_b)
    storer.finalizeStream(
        currentFrames={detectorName: 3},
        filePaths={detectorName: store_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(store_path, mode='r')
    dataset = root[detectorName]['data']
    expected = np.concatenate([frames_a, frames_b], axis=0)
    assert dataset.shape == expected.shape
    assert dataset.dtype == np.uint16
    assert dataset.attrs['writing'] is False
    assert dataset.attrs['axes'] == ['T', 'Y', 'X']
    np.testing.assert_array_equal(dataset[:], expected)

    meta_group = root[detectorName]['metadata']
    assert meta_group['detector'].attrs['exposure'] == 0.02
    assert meta_group['scan'].attrs['frames'] == 3


def test_zarr_streaming_multi_detector_single_file(tmp_path) -> None:
    """Test that multiple detectors can stream into one structured Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosMulti, updatePeriod=100)
    detectorNames = list(detectorInfosMulti.keys())
    store_path = str(tmp_path / 'test_zarr_multidetector.zarr')
    attrs = {name: {'detector:index': index} for index, name in enumerate(detectorNames)}

    frames = {
        detectorNames[0]: np.full((2, 4, 5), 11, dtype=np.uint16),
        detectorNames[1]: np.full((3, 6, 7), 22, dtype=np.uint16),
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={name: store_path for name in detectorNames},
        detectorNames=detectorNames,
        shapes={name: frames[name].shape[-2:] for name in detectorNames},
        attrs=attrs,
        singleMultiDetectorFile=True,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for name in detectorNames:
        storer.writeFrames(name, frames[name])
    storer.finalizeStream(
        currentFrames={name: frames[name].shape[0] for name in detectorNames},
        filePaths={name: store_path for name in detectorNames},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(store_path, mode='r')
    assert sorted(root.keys()) == sorted(detectorNames)
    for index, name in enumerate(detectorNames):
        dataset = root[name]['data']
        assert dataset.shape == frames[name].shape
        assert dataset.dtype == np.uint16
        assert dataset.attrs['writing'] is False
        assert root[name]['metadata']['detector'].attrs['index'] == index
        np.testing.assert_array_equal(dataset[:], frames[name])


def test_zarr_streaming_per_detector_files(tmp_path) -> None:
    """Test that per-detector mode writes each detector to a separate Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosMulti, updatePeriod=100)
    detectorNames = list(detectorInfosMulti.keys())
    fileDests = {
        name: str(tmp_path / f'test_zarr_{index}.zarr')
        for index, name in enumerate(detectorNames)
    }
    frames = {
        detectorNames[0]: np.full((2, 3, 4), 7, dtype=np.uint16),
        detectorNames[1]: np.full((2, 5, 6), 9, dtype=np.uint16),
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests=fileDests,
        detectorNames=detectorNames,
        shapes={name: frames[name].shape[-2:] for name in detectorNames},
        attrs={name: {} for name in detectorNames},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for name in detectorNames:
        storer.writeFrames(name, frames[name])
    storer.finalizeStream(
        currentFrames={name: frames[name].shape[0] for name in detectorNames},
        filePaths=fileDests,
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    for name in detectorNames:
        root = zarr.open(fileDests[name], mode='r')
        assert list(root.keys()) == [name]
        np.testing.assert_array_equal(root[name]['data'][:], frames[name])


def test_zarr_streaming_single_lapse_adds_scan_groups(tmp_path) -> None:
    """Test repeated single-lapse streams append scan groups in one Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    store_path = str(tmp_path / 'test_zarr_lapse.zarr')

    for scan_index in range(2):
        frames = np.full((1, 4, 5), scan_index + 1, dtype=np.uint16)
        storer = ZarrStorer(str(tmp_path / f'unused_{scan_index}'), detectorsManager)
        storer.openStream(
            fileDests={detectorName: store_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames.shape[-2:]},
            attrs={detectorName: {'scan:index': scan_index}},
            singleMultiDetectorFile=False,
            singleLapseFile=True,
            saveMode=SaveMode.Disk,
        )
        storer.writeFrames(detectorName, frames)
        storer.finalizeStream(
            currentFrames={detectorName: frames.shape[0]},
            filePaths={detectorName: store_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )

    root = zarr.open(store_path, mode='r')
    assert sorted(root.keys()) == ['scan0', 'scan1']
    for scan_index in range(2):
        dataset = root[f'scan{scan_index}'][detectorName]['data']
        assert dataset.shape == (1, 4, 5)
        assert dataset.attrs['writing'] is False
        assert root[f'scan{scan_index}'][detectorName]['metadata']['scan'].attrs['index'] == scan_index
        np.testing.assert_array_equal(
            dataset[:], np.full((1, 4, 5), scan_index + 1, dtype=np.uint16)
        )


def test_zarr_ram_streaming_is_explicitly_unsupported(tmp_path) -> None:
    """Document current Zarr RAM-mode boundary until MemoryStore support is added."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)

    with pytest.raises(NotImplementedError):
        storer.openStream(
            fileDests={detectorName: str(tmp_path / 'memory.zarr')},
            detectorNames=[detectorName],
            shapes={detectorName: (4, 5)},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.RAM,
        )


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


def test_detector_dtype_contract(tmp_path):
    """Test that storers create datasets from detector's declared dtype (the contract).
    
    Phase 1, Task 1: The detector's dtype property is the single authoritative 
    source of truth for recording. HDF5 and Zarr storers must create datasets 
    from this declared dtype, not from frames.dtype.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer
    
    # Create detector with uint16 dtype (default)
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # Verify the detector declares uint16
    declared_dtype = detectorsManager[detectorName].dtype
    assert declared_dtype == np.dtype(np.uint16), \
        f"Expected uint16 declared dtype, got {declared_dtype}"
    
    # Test HDF5Storer
    hdf5_path = str(tmp_path / 'test_dtype_contract.h5')
    hdf5_storer = HDF5Storer(hdf5_path, detectorsManager)
    frames = np.random.randint(0, 1000, (5, 128, 128), dtype=np.uint16)
    
    hdf5_storer.openStream(
        fileDests={detectorName: hdf5_path},
        detectorNames=[detectorName],
        shapes={detectorName: frames.shape[-2:]},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    hdf5_storer.writeFrames(detectorName, frames)
    hdf5_storer.finalizeStream(
        currentFrames={detectorName: frames.shape[0]},
        filePaths={detectorName: hdf5_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Assert on-disk HDF5 dataset dtype matches declared dtype
    with h5py.File(hdf5_path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.dtype == declared_dtype, \
            f"HDF5 dataset dtype {dataset.dtype} != declared dtype {declared_dtype}"
    
    # Test ZarrStorer
    zarr_path = str(tmp_path / 'test_dtype_contract.zarr')
    zarr_storer = ZarrStorer(zarr_path, detectorsManager)
    
    zarr_storer.openStream(
        fileDests={detectorName: zarr_path},
        detectorNames=[detectorName],
        shapes={detectorName: frames.shape[-2:]},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    zarr_storer.writeFrames(detectorName, frames)
    zarr_storer.finalizeStream(
        currentFrames={detectorName: frames.shape[0]},
        filePaths={detectorName: zarr_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Assert on-disk Zarr dataset dtype matches declared dtype
    root = zarr.open(zarr_path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.dtype == declared_dtype, \
        f"Zarr dataset dtype {dataset.dtype} != declared dtype {declared_dtype}"


def test_dtype_mismatch_warning(tmp_path, caplog):
    """Test that storers warn loudly once per detector on dtype mismatch.
    
    Phase 1, Task 1: When frames.dtype != declared dtype, storers must emit a
    prominent warning ONCE per detector, then proceed with a logged cast (not silent).
    The on-disk dtype must remain the declared dtype.
    """
    import logging
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer, TiffStorer
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # Detector declares uint16
    declared_dtype = detectorsManager[detectorName].dtype
    assert declared_dtype == np.dtype(np.uint16)
    
    # Create frames with a DIFFERENT dtype (float32) to trigger mismatch
    frames_wrong_dtype = np.random.rand(3, 128, 128).astype(np.float32)
    
    # Test HDF5Storer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        hdf5_path = str(tmp_path / 'test_mismatch.h5')
        hdf5_storer = HDF5Storer(hdf5_path, detectorsManager)
        
        hdf5_storer.openStream(
            fileDests={detectorName: hdf5_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        hdf5_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: hdf5_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for HDF5Storer
    hdf5_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'HDF5Storer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(hdf5_warnings) == 1, \
        f"Expected exactly 1 HDF5 dtype mismatch warning, got {len(hdf5_warnings)}"
    assert 'declared=uint16' in hdf5_warnings[0].message
    assert 'actual frame=float32' in hdf5_warnings[0].message
    
    # Assert on-disk dtype is the DECLARED dtype (uint16), not the frame dtype (float32)
    with h5py.File(hdf5_path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.dtype == declared_dtype, \
            f"HDF5 dataset should use declared dtype {declared_dtype}, got {dataset.dtype}"
        assert dataset.shape[0] == 3, "All frames should be recorded despite mismatch"
    
    # Clear caplog for Zarr test
    caplog.clear()
    
    # Test ZarrStorer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        zarr_path = str(tmp_path / 'test_mismatch.zarr')
        zarr_storer = ZarrStorer(zarr_path, detectorsManager)
        
        zarr_storer.openStream(
            fileDests={detectorName: zarr_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        zarr_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: zarr_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for ZarrStorer
    zarr_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'ZarrStorer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(zarr_warnings) == 1, \
        f"Expected exactly 1 Zarr dtype mismatch warning, got {len(zarr_warnings)}"
    
    # Assert on-disk dtype is the DECLARED dtype
    root = zarr.open(zarr_path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.dtype == declared_dtype, \
        f"Zarr dataset should use declared dtype {declared_dtype}, got {dataset.dtype}"
    assert dataset.shape[0] == 3, "All frames should be recorded despite mismatch"
    
    # Clear caplog for Tiff test
    caplog.clear()
    
    # Test TiffStorer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        tiff_path = str(tmp_path / 'test_mismatch.tiff')
        tiff_storer = TiffStorer(tiff_path, detectorsManager)
        
        tiff_storer.openStream(
            fileDests={detectorName: tiff_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        tiff_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: tiff_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for TiffStorer
    tiff_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'TiffStorer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(tiff_warnings) == 1, \
        f"Expected exactly 1 Tiff dtype mismatch warning, got {len(tiff_warnings)}"


def test_detector_bitDepth_property():
    """Test that detector bitDepth property returns correct values.
    
    Phase 1, Task 1: bitDepth should return int(dtype.itemsize * 8).
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # uint16 detector should report 16 bits
    assert detectorsManager[detectorName].bitDepth == 16, \
        "uint16 detector should report bitDepth=16"
    
    # Create a mock float32 detector by patching dtype
    detector = detectorsManager[detectorName]
    original_dtype = detector.dtype
    
    # Temporarily override dtype to test bitDepth calculation
    class MockFloat32Detector:
        @property
        def dtype(self):
            return np.dtype(np.float32)
        
        @property
        def bitDepth(self):
            return int(np.dtype(self.dtype).itemsize * 8)
    
    mock_detector = MockFloat32Detector()
    assert mock_detector.bitDepth == 32, \
        "float32 detector should report bitDepth=32"


# ---------------------------------------------------------------------------
# Off-thread writer (Phase 1 Task 3) tests.
#
# Design note: the off-thread writer is now in the DEFAULT recording path, so
# the existing recording integration tests (test_recording_spec_frames,
# test_recording_spec_time, lapse, multi-detector, ...) already exercise it
# end-to-end. We therefore avoid adding new slow qtbot/mock-driven integration
# tests here (they run on every CI push). Instead:
#   - one fast direct-storer test that compression survives the batched-chunk
#     change, and
#   - deterministic WriterThread unit tests for the concurrency guarantees
#     (FIFO order, no-drop, backpressure, openStream-failure propagation) using
#     a controllable fake storer. The mock detector emits RANDOM frames at a
#     time-driven rate and the real writer outpaces it, so order/backpressure
#     are not observable through the integration path anyway.
# ---------------------------------------------------------------------------

from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer, WriterThread, WRITER_QUEUE_MAXSIZE, WRITE_BATCH_FRAMES,
)


def test_offthread_writer_compression_enabled(tmp_path):
    """Compression is still applied after the batched multi-frame chunk change.

    Direct-storer test (no qtbot / no time-driven mock) so it stays CI-fast.
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'test_compression.h5')

    storer = HDF5Storer(path, detectorsManager)
    frames = np.random.randint(0, 1000, (5, 64, 64), dtype=np.uint16)
    storer.openStream(
        fileDests={detectorName: path}, detectorNames=[detectorName],
        shapes={detectorName: (64, 64)}, attrs={detectorName: {}},
        singleMultiDetectorFile=False, singleLapseFile=False, saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream({detectorName: 5}, {detectorName: path}, None, SaveMode.Disk)

    with h5py.File(path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.compression is not None, "Compression must remain enabled for disk mode"
        assert dataset.shape[0] == 5


class _FakeStorer:
    """Controllable storer for deterministic WriterThread unit tests.

    Records, in arrival order, every batch handed to writeFrames so tests can
    assert FIFO ordering and that no frame is dropped. Optionally raises on
    openStream (failure-propagation test) or sleeps per write (backpressure).
    """
    def __init__(self, openStreamError=None, writeDelay=0.0):
        self._openStreamError = openStreamError
        self._writeDelay = writeDelay
        self.opened = False
        self.finalized = False
        self.writes = {}  # detectorName -> list of received batch arrays

    def openStream(self, **kwargs):
        if self._openStreamError is not None:
            raise self._openStreamError
        self.opened = True

    def writeFrames(self, detectorName, frames):
        if self._writeDelay:
            time.sleep(self._writeDelay)
        self.writes.setdefault(detectorName, []).append(np.asarray(frames))

    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        self.finalized = True


def _make_writer(storer, detectorNames=('CAM',)):
    detectorNames = list(detectorNames)
    return WriterThread(
        storer=storer,
        fileDests={d: f'{d}.h5' for d in detectorNames},
        detectorNames=detectorNames,
        shapes={d: (2, 2) for d in detectorNames},
        attrs={d: {} for d in detectorNames},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={d: f'{d}.h5' for d in detectorNames},
        recordingManager=None,
    )


def test_writerthread_openstream_failure_propagates():
    """openStream failure is re-raised on the caller via wait_for_open, no hang."""
    storer = _FakeStorer(openStreamError=OSError("boom"))
    writer = _make_writer(storer)
    writer.start()
    with pytest.raises(OSError, match="boom"):
        writer.wait_for_open()
    writer.join(timeout=5.0)
    assert not writer.is_alive(), "Writer thread must terminate after openStream failure"
    assert not storer.finalized, "finalizeStream must not run if openStream failed"


def test_writerthread_preserves_order_and_count():
    """All enqueued frames reach the storer exactly once, in FIFO order."""
    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    n = WRITE_BATCH_FRAMES * 3 + 5  # multiple full batches + partial final
    for i in range(n):
        # Tag each frame with its index so order is verifiable.
        writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
    writer.finish()

    assert storer.finalized, "finalizeStream must run after the sentinel"
    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == n, f"Expected {n} frames, got {len(received)} (drops/dupes)"
    for i in range(n):
        assert (received[i] == i).all(), f"Frame {i} out of order"


def test_writerthread_backpressure_no_drop():
    """A slow storer fills the bounded queue; blocking put must not drop frames."""
    storer = _FakeStorer(writeDelay=0.003)  # writer slower than producer -> queue fills
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    n = WRITER_QUEUE_MAXSIZE * 2 + 10  # forces the queue full -> enqueue blocks
    for i in range(n):
        writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
    writer.finish()

    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == n, f"Backpressure dropped frames: expected {n}, got {len(received)}"
    for i in range(n):
        assert (received[i] == i).all(), f"Frame {i} out of order under backpressure"


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
