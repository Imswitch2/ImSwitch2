"""Tests for Hdf5LiveSource with SWMR protocol and structured HDF5 layouts."""

from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.live import Hdf5LiveSource


@pytest.fixture
def tmp_hdf5_path(tmp_path):
    """Fixture providing a temporary HDF5 path."""
    return tmp_path / "test.h5"


def test_hdf5_live_source_structured_layout(tmp_hdf5_path):
    """Test Hdf5LiveSource with structured detector layout."""
    data = np.arange(6 * 4 * 5, dtype=np.uint16).reshape(6, 4, 5)
    
    # Create HDF5 file with SWMR-compatible layout
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs["rec_mode"] = "recording"
        det_group = f.create_group("CAM")
        dataset = det_group.create_dataset("data", data=data, chunks=(2, 4, 5))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["recording:detector_name"] = "CAM"
        dataset.attrs["recording:dataset_path"] = "/CAM/data"
        dataset.attrs["recording:source_format"] = "HDF5"
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_hdf5_path)
    
    assert info.frame_shape == (4, 5)
    assert info.dtype == np.uint16
    assert info.detector_name == "CAM"
    assert info.source_format == "HDF5"
    assert info.dataset_path == "/CAM/data"
    assert info.attrs["rec_mode"] == "recording"
    
    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())
    
    assert len(chunks) == 3
    assert chunks[0].start == 0
    assert chunks[0].end == 2
    assert chunks[1].start == 2
    assert chunks[1].end == 4
    assert chunks[2].start == 4
    assert chunks[2].end == 6
    
    reconstructed = np.concatenate([c.data for c in chunks], axis=0)
    np.testing.assert_array_equal(reconstructed, data)
    
    source.close()


def test_hdf5_live_source_legacy_layout(tmp_hdf5_path):
    """Test Hdf5LiveSource with legacy root dataset layout."""
    data = np.arange(4 * 3 * 4, dtype=np.float32).reshape(4, 3, 4)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        dataset = f.create_dataset("CAM", data=data, chunks=(1, 3, 4))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM")
    info = source.open(tmp_hdf5_path)
    
    assert info.frame_shape == (3, 4)
    assert info.dtype == np.float32
    assert info.detector_name == "CAM"
    
    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())
    
    assert len(chunks) == 4
    for i, chunk in enumerate(chunks):
        assert chunk.start == i
        assert chunk.end == i + 1
    
    reconstructed = np.concatenate([c.data for c in chunks], axis=0)
    np.testing.assert_array_equal(reconstructed, data)
    
    source.close()


def test_hdf5_live_source_auto_detect_detector(tmp_hdf5_path):
    """Test auto-detection of detector name."""
    data = np.zeros((3, 2, 4), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        det_group = f.create_group("DETECTOR1")
        dataset = det_group.create_dataset("data", data=data, chunks=(1, 2, 4))
        dataset.attrs["detector_name"] = "DETECTOR1"
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource()
    info = source.open(tmp_hdf5_path)
    
    assert info.detector_name == "DETECTOR1"
    
    source.close()


def test_hdf5_live_source_expected_frames(tmp_hdf5_path):
    """Test completion with expected_frames metadata."""
    data = np.zeros((5, 3, 4), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        det_group = f.create_group("CAM")
        dataset = det_group.create_dataset("data", data=data, chunks=(2, 3, 4))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["recording:expected_frames"] = 10
        dataset.attrs["writing"] = True
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_hdf5_path)
    
    assert info.expected_frames == 10
    
    chunks = source.poll()
    assert len(chunks) == 3
    assert chunks[0].end == 2
    assert chunks[1].end == 4
    assert chunks[2].end == 5
    
    assert not source.is_complete()
    assert source.poll() == []
    
    source.close()


def test_hdf5_live_source_derives_frames_per_stack_from_scan_geometry(tmp_hdf5_path):
    """MoNaLISA live files may omit recording:frames_per_stack."""
    data = np.zeros((18 * 18, 3, 4), dtype=np.uint16)

    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        det_group = f.create_group("CAM")
        dataset = det_group.create_dataset("data", data=data, chunks=(50, 3, 4))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False

        metadata = det_group.create_group("metadata")
        scan_stage = metadata.create_group("ScanStage")
        scan_stage.attrs["axis_startpos"] = [0.0, 0.0, 0.0]
        scan_stage.attrs["axis_length"] = [950.0, 950.0, 1.0]
        scan_stage.attrs["axis_step_size"] = [50.0, 50.0, 1.0]
        scan_ttl = metadata.create_group("ScanTTL")
        scan_ttl.attrs["Nx"] = 18
        scan_ttl.attrs["Ny"] = 18
        f.flush()
        f.swmr_mode = True

    source = Hdf5LiveSource(detector_name="CAM")
    info = source.open(tmp_hdf5_path)

    assert info.frames_per_stack == 18 * 18
    source.close()


def test_hdf5_live_source_writing_false_completes(tmp_hdf5_path):
    """Test that writing=False completes when all frames read."""
    data = np.zeros((3, 2, 3), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        dataset = f.create_dataset("CAM", data=data, chunks=(1, 2, 3))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM")
    source.open(tmp_hdf5_path)
    
    assert not source.is_complete()
    source.poll()
    assert source.is_complete()
    
    source.close()


def test_hdf5_live_source_growing_dataset_simulation(tmp_hdf5_path):
    """Test simulated growing dataset by manually extending shape (SWMR protocol)."""
    # Writer: create file with SWMR mode
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        det_group = f.create_group("CAM")
        dataset = det_group.create_dataset("data", shape=(0, 3, 4), maxshape=(None, 3, 4), 
                                          chunks=(2, 3, 4), dtype=np.uint16)
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["recording:detector_name"] = "CAM"
        dataset.attrs["writing"] = True
        f.flush()
        f.swmr_mode = True
        
        # Simulate batched writes
        dataset.resize((3, 3, 4))
        dataset[:3] = np.arange(3 * 3 * 4, dtype=np.uint16).reshape(3, 3, 4)
        f.flush()
    
    # Reader: open in SWMR mode
    source = Hdf5LiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_hdf5_path)
    
    assert info.frame_shape == (3, 4)
    
    chunks = source.poll()
    assert len(chunks) == 2
    assert chunks[0].start == 0
    assert chunks[0].end == 2
    assert chunks[1].start == 2
    assert chunks[1].end == 3
    
    assert not source.is_complete()
    
    # Close the reader before reopening as writer (HDF5 SWMR limitation)
    source.close()
    
    # Writer: append more frames
    with h5py.File(tmp_hdf5_path, 'r+', libver='latest') as f:
        f.swmr_mode = True
        dataset = f['CAM/data']
        dataset.resize((6, 3, 4))
        dataset[3:6] = np.arange(3 * 3 * 4, dtype=np.uint16).reshape(3, 3, 4) + 100
        f.flush()
    
    # Reopen the reader
    source = Hdf5LiveSource(detector_name="CAM", chunk_size=2)
    source.open(tmp_hdf5_path)
    
    chunks = source.poll()
    assert len(chunks) == 3  # Will read all 6 frames since we reopened
    assert chunks[0].start == 0
    assert chunks[0].end == 2
    assert chunks[1].start == 2
    assert chunks[1].end == 4
    assert chunks[2].start == 4
    assert chunks[2].end == 6
    
    # Close and mark as complete
    source.close()
    
    with h5py.File(tmp_hdf5_path, 'r+', libver='latest') as f:
        f.swmr_mode = True
        dataset = f['CAM/data']
        dataset.attrs["writing"] = False
        f.flush()
    
    # Reopen and verify complete
    source = Hdf5LiveSource(detector_name="CAM", chunk_size=2)
    source.open(tmp_hdf5_path)
    source.poll()  # Read all frames
    assert source.is_complete()
    
    source.close()


def test_hdf5_live_source_metadata_flattening(tmp_hdf5_path):
    """Test that structured metadata is flattened correctly."""
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs["rec_mode"] = "recording"
        det_group = f.create_group("CAM")
        dataset = det_group.create_dataset("data", data=data, chunks=(1, 3, 4))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False
        
        metadata = det_group.create_group("metadata")
        detector = metadata.create_group("detector")
        detector.attrs["exposure"] = 0.01
        detector.attrs["gain"] = 2.5
        metadata.attrs["uncategorized"] = "value"
        
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM")
    info = source.open(tmp_hdf5_path)
    
    assert info.attrs["rec_mode"] == "recording"
    assert info.attrs["detector_name"] == "CAM"
    assert info.attrs["detector:exposure"] == 0.01
    assert info.attrs["detector:gain"] == 2.5
    assert info.attrs["uncategorized"] == "value"
    
    source.close()


def test_hdf5_live_source_chunk_size_fallback(tmp_hdf5_path):
    """Test fallback when array chunks[0] spans entire time dimension."""
    data = np.arange(5 * 2 * 3, dtype=np.uint16).reshape(5, 2, 3)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        dataset = f.create_dataset("CAM", data=data, chunks=(5, 2, 3))
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM")
    source.open(tmp_hdf5_path)
    
    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())
    
    assert len(chunks) == 1
    assert chunks[0].start == 0
    assert chunks[0].end == 5
    
    source.close()


def test_hdf5_live_source_invalid_dimension(tmp_hdf5_path):
    """Test that non-3D datasets raise an error."""
    data = np.zeros((2, 3), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.create_dataset("CAM", data=data, chunks=(1, 3))
    
    source = Hdf5LiveSource(detector_name="CAM")
    
    with pytest.raises(ValueError, match="Expected 3D dataset"):
        source.open(tmp_hdf5_path)


def test_hdf5_live_source_missing_detector(tmp_hdf5_path):
    """Test error when detector not found."""
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.create_dataset("CAM1", data=np.zeros((2, 3, 4)), chunks=(1, 3, 4))
    
    source = Hdf5LiveSource(detector_name="CAM2")
    
    with pytest.raises(ValueError, match="not found"):
        source.open(tmp_hdf5_path)


def test_hdf5_live_source_no_data_in_group(tmp_hdf5_path):
    """Test error when detector group has no data dataset."""
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.create_group("CAM")
    
    source = Hdf5LiveSource(detector_name="CAM")
    
    with pytest.raises(ValueError, match="no 'data' dataset"):
        source.open(tmp_hdf5_path)


def test_hdf5_live_source_poll_after_close(tmp_hdf5_path):
    """Test that poll returns empty after close."""
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        dataset = f.create_dataset("CAM", data=data, chunks=(1, 3, 4))
        dataset.attrs["writing"] = False
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LiveSource(detector_name="CAM")
    source.open(tmp_hdf5_path)
    source.close()
    
    assert source.poll() == []
    assert source.is_complete() is True


def _hdf5_streaming_recorder(path, n_frames_first_batch=3):
    """Drive a real HDF5Storer streaming session (no expected_frames attrs).

    Mirrors a SpecTime/UntilStop recording: the reader cannot rely on
    recording:expected_frames and must terminate via the stream_complete
    marker instead.
    """
    from unittest.mock import MagicMock

    from imswitch.imcontrol.model.managers.RecordingManager import (
        HDF5Storer,
        SaveMode,
    )

    det = MagicMock()
    det.dtype = np.uint16
    det.pixelSizeUm = [1, 0.1, 0.1]
    det_mgr = MagicMock()
    det_mgr.__getitem__ = MagicMock(return_value=det)

    storer = HDF5Storer(str(path), det_mgr)
    storer.omeMeta = {}
    storer.openStream(
        fileDests={"CAM": str(path)}, detectorNames=["CAM"],
        shapes={"CAM": (2, 3)}, attrs={"CAM": {}},
        singleMultiDetectorFile=False, singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames("CAM", np.full((n_frames_first_batch, 2, 3), 1, np.uint16))
    return storer


def test_hdf5_live_source_streams_and_completes_via_stream_complete_marker(tmp_hdf5_path):
    """A live SWMR reader terminates via the stream_complete marker.

    The final writing=False attr is rewritten through a post-close r+ reopen
    that a live SWMR handle can never see, and with no
    recording:expected_frames (SpecTime/UntilStop) the reader previously had
    no way to complete at all (audit root cause 2). The marker dataset is
    written while the recorder's SWMR handle is still open, so the live
    reader sees it.
    """
    from unittest.mock import MagicMock

    from imswitch.imcontrol.model.managers.RecordingManager import SaveMode

    storer = _hdf5_streaming_recorder(tmp_hdf5_path, n_frames_first_batch=3)

    source = Hdf5LiveSource(detector_name="CAM")
    source.open(tmp_hdf5_path)

    chunks = source.poll()
    assert sum(len(c.data) for c in chunks) == 3
    assert source.is_complete() is False

    storer.writeFrames("CAM", np.full((2, 2, 3), 2, np.uint16))
    chunks = source.poll()
    frames = np.concatenate([c.data for c in chunks], axis=0)
    assert frames.shape[0] == 2
    assert np.all(frames == 2)
    assert source.is_complete() is False  # recorder still writing

    storer.finalizeStream(
        currentFrames={"CAM": 5}, filePaths={"CAM": str(tmp_hdf5_path)},
        recordingManager=MagicMock(), saveMode=SaveMode.Disk,
    )
    # No expected_frames and the writing=False rewrite is invisible to this
    # held SWMR handle - completion must come from the marker.
    assert source.is_complete() is True
    source.close()


def test_hdf5_live_source_caps_reads_at_frames_committed(tmp_hdf5_path):
    """Reads are capped by the frames_committed barrier when it lags shape."""
    with h5py.File(tmp_hdf5_path, "w", libver="latest") as f:
        group = f.create_group("CAM")
        data = group.create_dataset(
            "data", shape=(6, 2, 3), maxshape=(None, 2, 3), dtype=np.uint16
        )
        data[:] = 5
        data.attrs["writing"] = True
        committed = group.create_dataset("frames_committed", shape=(1,), dtype=np.int64)
        committed[0] = 4  # two frames not yet committed
        group.create_dataset("stream_complete", shape=(1,), dtype=np.uint8)

    source = Hdf5LiveSource(detector_name="CAM")
    source.open(tmp_hdf5_path)

    chunks = source.poll()
    assert sum(len(c.data) for c in chunks) == 4  # not shape (6)
    assert source.poll() == []
    assert source.is_complete() is False
    source.close()
