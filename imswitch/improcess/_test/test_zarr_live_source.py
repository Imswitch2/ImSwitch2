"""Tests for ZarrLiveSource with structured and legacy Zarr layouts."""

from pathlib import Path

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import ZarrLiveSource


@pytest.fixture
def tmp_zarr_path(tmp_path):
    """Fixture providing a temporary Zarr path."""
    return tmp_path / "test.zarr"


def test_zarr_live_source_structured_layout(tmp_zarr_path):
    """Test ZarrLiveSource with structured detector layout."""
    data = np.arange(6 * 4 * 5, dtype=np.uint16).reshape(6, 4, 5)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs["rec_mode"] = "recording"
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(2, 4, 5))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    array.attrs["axes"] = ["T", "Y", "X"]
    
    source = ZarrLiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_zarr_path)
    
    assert info.frame_shape == (4, 5)
    assert info.dtype == np.uint16
    assert info.detector_name == "CAM"
    assert info.source_format == "ZARR"
    assert info.attrs["detector_name"] == "CAM"
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


def test_zarr_live_source_legacy_layout(tmp_zarr_path):
    """Test ZarrLiveSource with legacy root array layout."""
    data = np.arange(4 * 3 * 4, dtype=np.float32).reshape(4, 3, 4)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    
    source = ZarrLiveSource(detector_name="CAM")
    info = source.open(tmp_zarr_path)
    
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


def test_zarr_live_source_reads_root_ngff_full_resolution_array(tmp_zarr_path):
    data = np.arange(4 * 3 * 5, dtype=np.uint16).reshape(4, 3, 5)

    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    array = ZarrStorer._create_array(root, "0", data=data, chunks=(2, 3, 5))
    array.attrs["writing"] = False
    root.attrs["multiscales"] = [
        {
            "axes": [
                {"name": "t", "type": "time"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"},
            ],
            "datasets": [{"path": "0"}],
        }
    ]

    source = ZarrLiveSource(chunk_size=2)
    info = source.open(tmp_zarr_path)

    assert info.frame_shape == (3, 5)
    assert info.dataset_path == "/0"
    assert info.attrs["ngff:axes"][1]["name"] == "y"

    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())

    reconstructed = np.concatenate([chunk.data for chunk in chunks], axis=0)
    np.testing.assert_array_equal(reconstructed, data)

    source.close()


def test_zarr_live_source_reads_detector_group_ngff_and_polls(tmp_zarr_path):
    """Detector group carrying OME-NGFF ``multiscales`` (recording output).

    RecordingManager writes ``ome.multiscales`` onto the *detector group* with
    ``datasets[].path = "data"`` (a group-relative path). The image resolver
    must rebase that to the store-relative ``CAM/data`` so the live source's
    per-poll re-traversal from the root actually reaches the array — otherwise
    poll() raises KeyError('data') and Zarr live reconstruction silently dies.
    """
    data = np.arange(6 * 4 * 5, dtype=np.uint16).reshape(6, 4, 5)

    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(2, 4, 5))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    det_group.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [
            {
                "name": "CAM",
                "axes": [
                    {"name": "t", "type": "time"},
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"},
                ],
                "datasets": [{"path": "data"}],
            }
        ],
    }
    root.attrs["ome"] = {"version": "0.5", "series": [{"path": "CAM"}]}

    source = ZarrLiveSource(chunk_size=2)
    info = source.open(tmp_zarr_path)

    assert info.frame_shape == (4, 5)
    assert info.detector_name == "CAM"

    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())

    reconstructed = np.concatenate([c.data for c in chunks], axis=0)
    np.testing.assert_array_equal(reconstructed, data)

    source.close()


def test_zarr_live_source_auto_detect_detector(tmp_zarr_path):
    """Test auto-detection of detector name."""
    data = np.zeros((3, 2, 4), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    det_group = root.create_group("DETECTOR1")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(1, 2, 4))
    array.attrs["detector_name"] = "DETECTOR1"
    array.attrs["writing"] = False
    
    source = ZarrLiveSource()
    info = source.open(tmp_zarr_path)
    
    assert info.detector_name == "DETECTOR1"
    
    source.close()


def test_zarr_live_source_expected_frames(tmp_zarr_path):
    """Test completion with expected_frames metadata."""
    data = np.zeros((5, 3, 4), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs["recording:expected_frames"] = 10
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(2, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = True
    
    source = ZarrLiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_zarr_path)
    
    assert info.expected_frames == 10
    
    chunks = source.poll()
    assert len(chunks) == 3
    assert chunks[0].end == 2
    assert chunks[1].end == 4
    assert chunks[2].end == 5
    
    assert not source.is_complete()
    assert source.poll() == []
    
    source.close()


def test_zarr_live_source_derives_frames_per_stack_from_scan_geometry(tmp_zarr_path):
    """MoNaLISA live stores can derive startup stack size from scan metadata."""
    data = np.zeros((18 * 18, 3, 4), dtype=np.uint16)

    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(50, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False

    metadata = det_group.create_group("metadata")
    scan_stage = metadata.create_group("ScanStage")
    scan_stage.attrs["axis_startpos"] = [0.0, 0.0, 0.0]
    scan_stage.attrs["axis_length"] = [950.0, 950.0, 1.0]
    scan_stage.attrs["axis_step_size"] = [50.0, 50.0, 1.0]
    scan_ttl = metadata.create_group("ScanTTL")
    scan_ttl.attrs["Nx"] = 18
    scan_ttl.attrs["Ny"] = 18

    source = ZarrLiveSource(detector_name="CAM")
    info = source.open(tmp_zarr_path)

    assert info.frames_per_stack == 18 * 18
    source.close()


def test_zarr_live_source_writing_false_completes(tmp_zarr_path):
    """Test that writing=False completes when all frames read."""
    data = np.zeros((3, 2, 3), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 2, 3))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    
    source = ZarrLiveSource(detector_name="CAM")
    source.open(tmp_zarr_path)
    
    assert not source.is_complete()
    source.poll()
    assert source.is_complete()
    
    source.close()


def test_zarr_live_source_growing_array_simulation(tmp_zarr_path):
    """Test simulated growing array by manually extending shape."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", shape=(0, 3, 4), chunks=(2, 3, 4), dtype=np.uint16)
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = True
    
    source = ZarrLiveSource(detector_name="CAM", chunk_size=2)
    info = source.open(tmp_zarr_path)
    
    assert info.frame_shape == (3, 4)
    assert source.poll() == []
    
    array.resize((3, 3, 4))
    array[:3] = np.arange(3 * 3 * 4, dtype=np.uint16).reshape(3, 3, 4)
    
    chunks = source.poll()
    assert len(chunks) == 2
    assert chunks[0].start == 0
    assert chunks[0].end == 2
    assert chunks[1].start == 2
    assert chunks[1].end == 3
    
    assert not source.is_complete()
    
    array.resize((6, 3, 4))
    array[3:6] = np.arange(3 * 3 * 4, dtype=np.uint16).reshape(3, 3, 4) + 100
    
    chunks = source.poll()
    assert len(chunks) == 2
    assert chunks[0].start == 3
    assert chunks[0].end == 5
    assert chunks[1].start == 5
    assert chunks[1].end == 6
    
    array.attrs["writing"] = False
    assert source.is_complete()
    
    source.close()


def test_zarr_live_source_metadata_flattening(tmp_zarr_path):
    """Test that structured metadata is flattened correctly."""
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs["rec_mode"] = "recording"
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    
    metadata = det_group.create_group("metadata")
    detector = metadata.create_group("detector")
    detector.attrs["exposure"] = 0.01
    detector.attrs["gain"] = 2.5
    metadata.attrs["uncategorized"] = "value"
    
    source = ZarrLiveSource(detector_name="CAM")
    info = source.open(tmp_zarr_path)
    
    assert info.attrs["rec_mode"] == "recording"
    assert info.attrs["detector_name"] == "CAM"
    assert info.attrs["detector:exposure"] == 0.01
    assert info.attrs["detector:gain"] == 2.5
    assert info.attrs["uncategorized"] == "value"
    
    source.close()


def test_zarr_live_source_chunk_size_fallback(tmp_zarr_path):
    """Test fallback to chunk_size=1 when array chunks[0] would span entire time dimension."""
    data = np.arange(5 * 2 * 3, dtype=np.uint16).reshape(5, 2, 3)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=data, chunks=(5, 2, 3))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    
    source = ZarrLiveSource(detector_name="CAM")
    source.open(tmp_zarr_path)
    
    chunks = []
    while not source.is_complete():
        chunks.extend(source.poll())
    
    assert len(chunks) == 1
    assert chunks[0].start == 0
    assert chunks[0].end == 5
    
    source.close()


def test_zarr_live_source_invalid_dimension(tmp_zarr_path):
    """Test that non-3D arrays raise an error."""
    data = np.zeros((2, 3), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 3))
    
    source = ZarrLiveSource(detector_name="CAM")
    
    with pytest.raises(ValueError, match="Expected 3D array"):
        source.open(tmp_zarr_path)


def test_zarr_live_source_missing_detector(tmp_zarr_path):
    """Test error when detector not found."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    ZarrStorer._create_array(root, "CAM1", data=np.zeros((2, 3, 4)), chunks=(1, 3, 4))
    
    source = ZarrLiveSource(detector_name="CAM2")
    
    with pytest.raises(ValueError, match="not found"):
        source.open(tmp_zarr_path)


def test_zarr_live_source_no_data_in_group(tmp_zarr_path):
    """Test error when detector group has no data array."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.create_group("CAM")
    
    source = ZarrLiveSource(detector_name="CAM")
    
    with pytest.raises(ValueError, match="no 'data' array"):
        source.open(tmp_zarr_path)


def test_zarr_live_source_poll_after_close(tmp_zarr_path):
    """Test that poll returns empty after close."""
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 3, 4))
    array.attrs["writing"] = False
    
    source = ZarrLiveSource(detector_name="CAM")
    source.open(tmp_zarr_path)
    source.close()
    
    assert source.poll() == []
    assert source.is_complete() is True
