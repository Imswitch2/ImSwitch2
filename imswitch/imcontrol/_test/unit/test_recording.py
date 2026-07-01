import os
import time
import pytest

import h5py
import numpy as np
import zarr

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, SaveFormat, DetectorInfo
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.imcontrol.model.managers.recording_metadata import MODE_SCAN, MODE_TIMELAPSE
from . import detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare


class _Param:
    def __init__(self, value, valueUnits):
        self.value = value
        self.valueUnits = valueUnits


class _OmeMetaDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [9.9, 0.2, 0.1]
    parameters = {
        'Internal frame interval': _Param(25, 'ms'),
    }


class _OmeMetaDetectors:
    def __getitem__(self, name):
        return _OmeMetaDetector()

    def execOnAll(self, func, *, condition=None):
        return {}


def test_build_ome_meta_uses_scan_step_and_parameter_units():
    manager = RecordingManager(_OmeMetaDetectors())

    scan_meta = manager.buildOmeMeta(
        'Cam', MODE_SCAN, 5, scanDims=(32, 32, 5), scanStepSizes=(0.1, 0.2, 0.75))
    assert scan_meta.axes_string == 'ZYX'
    assert scan_meta.scale == [0.75, 0.2, 0.1]

    time_meta = manager.buildOmeMeta('Cam', MODE_TIMELAPSE, 4)
    assert time_meta.axes_string == 'TYX'
    assert time_meta.scale[0] == 0.025


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
        assert dataset.attrs['recording:detector_name'] == detectorName
        assert dataset.attrs['recording:source_format'] == 'HDF5'
        assert dataset.attrs['recording:expected_frames'] == numFrames
        assert dataset.attrs['recording:frames_per_stack'] == numFrames
        assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
        # Non-lapse recordings should have default lapse metadata
        assert dataset.attrs['recording:num_timepoints'] == 1
        assert dataset.attrs['recording:lapse_index'] == 0
        assert dataset.attrs['recording:single_lapse_file'] == False
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


@pytest.mark.parametrize('recMode,kwargs',
                         [(RecMode.SpecFrames, {'recFrames': 10}),
                          (RecMode.SpecTime, {'recTime': 2})])
def test_recording_emits_recording_ended(qtbot, recMode, kwargs):
    """Non-scan recordings must emit sigRecordingEnded when they self-terminate.

    Regression: SpecFrames suppressed sigRecordingEnded (it was lumped in with
    the scan-driven modes, which finish via sigScanDone instead). With no scan
    to drive RecordingController.recordingCycleEnded(), the REC button stayed
    stuck checked after a frame-count recording. SpecTime always emitted it;
    both non-scan modes must now behave the same.
    """
    detectorInfos = detectorInfosBasic
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    with qtbot.waitSignal(recordingManager.sigRecordingEnded, timeout=30000):
        recordingManager.startRecording(
            detectorNames=list(detectorInfos.keys()),
            recMode=recMode,
            savename='test_recording_ended',
            saveMode=SaveMode.RAM,
            attrs={detectorName: {} for detectorName in detectorInfos.keys()},
            **kwargs,
        )

    qtbot.wait(200)  # let the worker thread fully wind down
    assert not recordingManager.record, "Recording should have stopped on its own"


def test_next_lapse_defers_start_while_previous_recording_active(monkeypatch):
    """A ScanLapse timepoint must not startRecording while the previous one is
    still finalizing.

    Regression: ScanLapse drives its cycle off sigScanDone and suppresses
    sigRecordingEnded, so sigScanDone can advance the lapse before the recording
    worker clears the record flag. With Freq=0 the lapse timer fires
    immediately, startRecording hits its "a recording is already active" guard,
    and the raised error escaped the timer callback and wedged the widget.
    nextLapse must instead re-arm a short retry until the recording drains.
    """
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
        _LAPSE_RECORDING_DRAIN_RETRY_MS,
    )

    class _FakeSignal:
        def __init__(self):
            self.cb = None

        def connect(self, cb):
            self.cb = cb

    class _FakeTimer:
        created = []

        def __init__(self, singleShot=False):
            self.singleShot = singleShot
            self.timeout = _FakeSignal()
            self.started_ms = None
            _FakeTimer.created.append(self)

        def start(self, ms):
            self.started_ms = ms

        def stop(self):
            pass

    monkeypatch.setattr(rc_mod, "Timer", _FakeTimer)

    class _FakeRecMgr:
        def __init__(self):
            self._record = True
            self.start_calls = 0

        @property
        def record(self):
            return self._record

        def startRecording(self, **kwargs):
            self.start_calls += 1

    # RecordingController is a QObject subclass, so it can't be built with
    # object.__new__; nextLapse's deferral path only reads a few attributes, so
    # drive it via a duck-typed self with the method bound onto it (it re-arms
    # the retry timer onto self.nextLapse).
    recMgr = _FakeRecMgr()
    ctrl = types.SimpleNamespace(
        stopRequested=False,
        timer=None,
        _widget=type("W", (), {"isRecButtonChecked": lambda self: True})(),
        _master=type("M", (), {"recordingManager": recMgr})(),
    )
    ctrl.nextLapse = types.MethodType(RecordingController.nextLapse, ctrl)

    ctrl.nextLapse()

    # Deferred: no recording started, a retry timer re-armed onto nextLapse.
    assert recMgr.start_calls == 0
    assert len(_FakeTimer.created) == 1
    timer = _FakeTimer.created[0]
    assert timer.started_ms == _LAPSE_RECORDING_DRAIN_RETRY_MS
    assert timer.timeout.cb == ctrl.nextLapse


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
        self.aborted = False
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

    def abortStream(self, filePaths, fileDests, saveMode):
        self.aborted = True


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


def test_writerthread_abort_calls_abortstream_not_finalize():
    """abort() discards via abortStream and never finalizes the stream."""
    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    for i in range(WRITE_BATCH_FRAMES * 2):
        writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
    writer.abort()

    writer.join(timeout=5.0)
    assert not writer.is_alive(), "Writer must terminate after abort"
    assert storer.aborted is True, "abortStream must be called on abort"
    assert storer.finalized is False, "finalizeStream must NOT be called on abort"


def test_hdf5storer_abort_removes_partial_file(tmp_path):
    """HDF5Storer.abortStream closes handles and deletes the partial file."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'aborted.h5')

    storer = HDF5Storer(path, detectorsManager)
    storer.openStream(
        fileDests={detectorName: path}, detectorNames=[detectorName],
        shapes={detectorName: (8, 8)}, attrs={detectorName: {}},
        singleMultiDetectorFile=False, singleLapseFile=False, saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, np.random.randint(0, 100, (3, 8, 8), dtype=np.uint16))
    assert os.path.exists(path), "file should exist before abort"

    storer.abortStream({detectorName: path}, {detectorName: path}, SaveMode.Disk)
    assert not os.path.exists(path), "abortStream must delete the partial file"


def test_recording_abort_discards_file(qtbot, tmp_path):
    """End-to-end: aborting an UntilStop recording leaves no file on disk."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    savename = str(tmp_path / 'aborted')
    detectorName = list(detectorInfosBasic.keys())[0]

    recordingManager.startRecording(
        detectorNames=[detectorName], recMode=RecMode.UntilStop, savename=savename,
        saveMode=SaveMode.Disk, saveFormat=SaveFormat.HDF5, attrs={detectorName: {}},
    )
    qtbot.wait(300)  # let some frames stream through the writer
    recordingManager.abortRecording(wait=True)

    assert not os.path.exists(f'{savename}_{detectorName}.hdf5'), \
        "aborted recording must not leave a file on disk"


def test_zarr_streaming_recording_metadata(tmp_path):
    """Zarr streaming datasets expose live-source recording metadata."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'live_metadata.zarr')
    storer = ZarrStorer(path, detectorsManager)
    frames = np.random.randint(0, 100, (4, 8, 8), dtype=np.uint16)

    storer.openStream(
        fileDests={detectorName: path},
        detectorNames=[detectorName],
        shapes={detectorName: (8, 8)},
        attrs={detectorName: {
            'recording:expected_frames': 4,
            'recording:frames_per_stack': 4,
            'recording:num_timepoints': 1,
            'recording:lapse_index': 0,
            'recording:single_lapse_file': False,
            'custom:note': 'kept-in-metadata-group',
        }},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream(
        currentFrames={detectorName: 4},
        filePaths={detectorName: path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.attrs['recording:detector_name'] == detectorName
    assert dataset.attrs['recording:source_format'] == 'ZARR'
    assert dataset.attrs['recording:expected_frames'] == 4
    assert dataset.attrs['recording:frames_per_stack'] == 4
    assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
    # Non-lapse recordings should have default lapse metadata
    assert dataset.attrs['recording:num_timepoints'] == 1
    assert dataset.attrs['recording:lapse_index'] == 0
    assert dataset.attrs['recording:single_lapse_file'] == False
    assert root[detectorName]['metadata']['custom'].attrs['note'] == 'kept-in-metadata-group'


def test_hdf5_streaming_swmr_readable(tmp_path):
    """Test that HDF5 streaming recordings are SWMR-readable with recording:* metadata."""
    from imswitch.improcess.live import Hdf5LiveSource
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer
    
    detectorName = 'TestCam'
    detectorInfos = {detectorName: DetectorInfo(
        analogChannel=None,
        digitalLine=3,
        managerName='HamamatsuManager',
        managerProperties={
            'cameraListIndex': 'mock',
            'hamamatsu': {
                'readout_speed': 3,
                'trigger_global_exposure': 5,
                'trigger_active': 2,
                'trigger_polarity': 2,
                'exposure_time': 0.01,
                'trigger_source': 1,
                'subarray_hpos': 0,
                'subarray_vpos': 0,
                'subarray_hsize': 5,
                'subarray_vsize': 4,
                'image_width': 5,
                'image_height': 4
            }
        },
        forAcquisition=True
    )}
    
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    path = tmp_path / f'swmr_test_{detectorName}.h5'
    
    storer = HDF5Storer(str(path), detectorsManager)
    
    attrs = {
        detectorName: {
            'recording:frames_per_stack': 10,
            'recording:expected_frames': 10,
            'custom:metadata': 'test_value'
        }
    }
    
    storer.openStream(
        fileDests={detectorName: str(path)},
        detectorNames=[detectorName],
        shapes={detectorName: (4, 5)},
        attrs=attrs,
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    
    # Write frames in batches
    frames1 = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    storer.writeFrames(detectorName, frames1)
    
    frames2 = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5) + 100
    storer.writeFrames(detectorName, frames2)
    
    # Verify SWMR-readable before finalize
    with h5py.File(path, 'r', libver='latest', swmr=True) as f:
        assert f.swmr_mode is True
        dataset = f[detectorName]['data']
        assert dataset.shape[0] == 6
        assert dataset.attrs['writing'] == True
        assert dataset.attrs['recording:detector_name'] == detectorName
        assert dataset.attrs['recording:source_format'] == 'HDF5'
        assert dataset.attrs['recording:expected_frames'] == 10
        assert dataset.attrs['recording:frames_per_stack'] == 10
        assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
    
    # Test with Hdf5LiveSource while writing=True
    source = Hdf5LiveSource(detector_name=detectorName)
    info = source.open(path)
    assert info.expected_frames == 10
    assert info.detector_name == detectorName
    assert info.source_format == 'HDF5'
    
    chunks = source.poll()
    assert len(chunks) > 0
    total_frames = sum(c.end - c.start for c in chunks)
    assert total_frames == 6
    assert not source.is_complete()  # writing=True and cursor < expected_frames
    source.close()
    
    # Finalize
    storer.finalizeStream(
        currentFrames={detectorName: 6},
        filePaths={detectorName: str(path)},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Verify writing=False after finalize
    with h5py.File(path, 'r', libver='latest', swmr=True) as f:
        dataset = f[detectorName]['data']
        assert dataset.attrs['writing'] == False
        assert dataset.shape[0] == 6
    
    # Test with Hdf5LiveSource after finalize
    source2 = Hdf5LiveSource(detector_name=detectorName)
    source2.open(path)
    chunks2 = source2.poll()
    total_frames2 = sum(c.end - c.start for c in chunks2)
    assert total_frames2 == 6
    assert source2.is_complete()  # writing=False and all frames read
    source2.close()


def test_worker_lapse_metadata_augmentation():
    """Test that RecordingWorker._augment_attrs_with_recording_metadata adds lapse fields."""
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    worker = recordingManager._RecordingManager__recordingWorker
    
    # Configure worker as if it were a ScanLapse recording
    worker.saveFormat = SaveFormat.ZARR
    worker.recLapseTotal = 3
    worker.recLapseIndex = 1
    worker.singleLapseFile = True
    
    detectorName = list(detectorInfosBasic.keys())[0]
    input_attrs = {detectorName: {'user_key': 'user_value'}}
    expected_frames = {detectorName: 10}
    
    augmented = worker._augment_attrs_with_recording_metadata(input_attrs, expected_frames)
    
    assert detectorName in augmented
    attrs = augmented[detectorName]
    
    # Check that lapse metadata was added
    assert attrs['recording:num_timepoints'] == 3
    assert attrs['recording:lapse_index'] == 1
    assert attrs['recording:single_lapse_file'] == True
    
    # Check that other metadata was also added
    assert attrs['recording:detector_name'] == detectorName
    assert attrs['recording:source_format'] == 'ZARR'
    assert attrs['recording:expected_frames'] == 10
    assert attrs['recording:frames_per_stack'] == 10
    
    # Check that user attrs were preserved
    assert attrs['user_key'] == 'user_value'
    
    # Test with default values (non-lapse)
    worker.recLapseTotal = None
    worker.recLapseIndex = None
    worker.singleLapseFile = False
    
    augmented_default = worker._augment_attrs_with_recording_metadata(input_attrs, expected_frames)
    attrs_default = augmented_default[detectorName]
    
    assert attrs_default['recording:num_timepoints'] == 1
    assert attrs_default['recording:lapse_index'] == 0
    assert attrs_default['recording:single_lapse_file'] == False


def test_lapse_metadata_end_to_end(qtbot):
    """Test that lapse metadata flows through to HDF5 file via RecordingManager."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    filePerDetector, savedToDiskPerDetector = {}, {}

    def memoryRecordingAvailable(_, file, __, savedToDisk, detName):
        nonlocal filePerDetector, savedToDiskPerDetector
        filePerDetector[detName], savedToDiskPerDetector[detName] = file, savedToDisk
        return True

    # Start a recording with explicit lapse parameters
    recordingManager.startRecording(
        detectorNames=[detectorName],
        recMode=RecMode.SpecFrames,
        savename='test_lapse_metadata',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {'test_attr': 'test_value'}},
        recFrames=5,
        recLapseTotal=4,
        recLapseIndex=2,
        singleLapseFile=True,
    )
    
    with qtbot.waitSignals(
        [recordingManager.sigMemoryRecordingAvailable],
        check_params_cbs=[lambda *args, **kwargs: memoryRecordingAvailable(*args, detName=detectorName, **kwargs)],
        timeout=30000
    ):
        pass

    assert detectorName in filePerDetector
    file = filePerDetector[detectorName]
    h5pyFile = h5py.File(file)
    
    detector_group = h5pyFile.get(detectorName)
    assert detector_group is not None
    dataset = detector_group.get('data')
    assert dataset is not None
    
    # Verify lapse metadata
    assert dataset.attrs['recording:num_timepoints'] == 4
    assert dataset.attrs['recording:lapse_index'] == 2
    assert dataset.attrs['recording:single_lapse_file'] == True
    
    # Verify other recording metadata still works
    assert dataset.attrs['recording:detector_name'] == detectorName
    assert dataset.attrs['recording:expected_frames'] == 5
    
    h5pyFile.close()
    file.close()


def test_waitForAcquisitionStarted_blocks_until_armed(qtbot):
    """Verify that waitForAcquisitionStarted blocks until arming completes."""
    import threading
    from unittest.mock import patch
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    block_event = threading.Event()
    original_startAcquisition = detectorsManager.startAcquisition
    
    def blocking_startAcquisition():
        block_event.wait()
        return original_startAcquisition()
    
    try:
        with patch.object(detectorsManager, 'startAcquisition', side_effect=blocking_startAcquisition):
            recordingManager.startRecording(
                detectorNames=list(detectorInfosBasic.keys()),
                recMode=RecMode.SpecFrames,
                savename='test_arming_block',
                saveMode=SaveMode.RAM,
                attrs={name: {} for name in detectorInfosBasic.keys()},
                recFrames=5
            )
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=0.2)
            assert armed is False, "Should timeout while detector is blocked"
            
            block_event.set()
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=2.0)
            assert armed is True, "Should return True after detector is unblocked"
    finally:
        block_event.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_waitForAcquisitionStarted_waits_for_stream_open():
    """Scan gating must wait until the writer stream and chunk consumer exist."""
    import threading

    open_started = threading.Event()
    release_open = threading.Event()

    class BlockingStorer:
        def __init__(self, filepath, detectorManager):
            self.filepath = filepath
            self.detectorManager = detectorManager

        def openStream(self, *args, **kwargs):
            open_started.set()
            release_open.wait()

        def writeFrames(self, detectorName, frames):
            pass

        def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
            pass

        def abortStream(self, filePaths, fileDests, saveMode):
            pass

    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(
        detectorsManager,
        storerMap={SaveFormat.HDF5: BlockingStorer},
    )

    try:
        recordingManager.startRecording(
            detectorNames=list(detectorInfosBasic.keys()),
            recMode=RecMode.SpecFrames,
            savename='test_stream_ready_block',
            saveMode=SaveMode.RAM,
            attrs={name: {} for name in detectorInfosBasic.keys()},
            recFrames=1,
        )

        assert open_started.wait(2.0)
        assert recordingManager.waitForAcquisitionStarted(timeout=0.1) is False

        release_open.set()
        assert recordingManager.waitForAcquisitionStarted(timeout=2.0) is True
    finally:
        release_open.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_abortRecording_releases_open_writer_without_frames():
    """Abort cleanup must wake a writer waiting on an empty frame queue."""
    import numpy as np

    class EmptyDetector:
        forAcquisition = True
        shape = (4, 4)
        dtype = np.dtype(np.uint16)
        pixelSizeUm = [1, 1, 1]

        def startAcquisition(self):
            pass

        def stopAcquisition(self):
            pass

        def flushBuffers(self):
            pass

        def releaseChunkConsumer(self, consumerKey):
            pass

        def readChunk(self, consumerKey):
            return []

    class EmptyDetectorsManager:
        def __init__(self):
            self.detector = EmptyDetector()

        def __getitem__(self, detectorName):
            return self.detector

        def execOnAll(self, func, *, condition=None):
            condition = condition or (lambda detector: True)
            if condition(self.detector):
                return {"Empty": func(self.detector)}
            return {}

        def startAcquisition(self, liveView=False):
            self.detector.startAcquisition()
            return object()

        def stopAcquisition(self, handle, liveView=False):
            self.detector.stopAcquisition()

    class NoopStorer:
        def __init__(self, filepath, detectorManager):
            self.filepath = filepath
            self.detectorManager = detectorManager

        def openStream(self, *args, **kwargs):
            pass

        def writeFrames(self, detectorName, frames):
            pass

        def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
            pass

        def abortStream(self, filePaths, fileDests, saveMode):
            pass

    recordingManager = RecordingManager(
        EmptyDetectorsManager(),
        storerMap={SaveFormat.HDF5: NoopStorer},
    )

    recordingManager.startRecording(
        detectorNames=["Empty"],
        recMode=RecMode.SpecFrames,
        savename="test_abort_open_writer",
        saveMode=SaveMode.RAM,
        attrs={"Empty": {}},
        recFrames=1,
        stallTimeout=5.0,
    )
    assert recordingManager.waitForAcquisitionStarted(timeout=2.0) is True

    recordingManager.abortRecording(emitSignal=False, wait=True)

    assert recordingManager.record is False


def test_waitForAcquisitionStarted_fast_arming(qtbot):
    """Verify that waitForAcquisitionStarted returns True in normal fast-arming path."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    try:
        recordingManager.startRecording(
            detectorNames=list(detectorInfosBasic.keys()),
            recMode=RecMode.SpecFrames,
            savename='test_arming_fast',
            saveMode=SaveMode.RAM,
            attrs={name: {} for name in detectorInfosBasic.keys()},
            recFrames=5
        )
        
        armed = recordingManager.waitForAcquisitionStarted(timeout=2.0)
        assert armed is True, "Should return True for fast-arming detector"
    finally:
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_waitForAcquisitionStarted_timeout(qtbot):
    """Verify that waitForAcquisitionStarted returns False on timeout."""
    import threading
    from unittest.mock import patch
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    block_event = threading.Event()
    original_startAcquisition = detectorsManager.startAcquisition
    
    def never_finishing_startAcquisition():
        block_event.wait()
        return original_startAcquisition()
    
    try:
        with patch.object(detectorsManager, 'startAcquisition', side_effect=never_finishing_startAcquisition):
            recordingManager.startRecording(
                detectorNames=list(detectorInfosBasic.keys()),
                recMode=RecMode.SpecFrames,
                savename='test_arming_timeout',
                saveMode=SaveMode.RAM,
                attrs={name: {} for name in detectorInfosBasic.keys()},
                recFrames=5
            )
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=0.1)
            assert armed is False, "Should return False on timeout"
    finally:
        block_event.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


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
