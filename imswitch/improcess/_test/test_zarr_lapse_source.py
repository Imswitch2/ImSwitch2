"""Tests for ZarrLapseSource with single-file scan{N} timelapse layout."""

from pathlib import Path

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import ZarrLapseSource, make_live_source


@pytest.fixture
def tmp_zarr_path(tmp_path):
    """Fixture providing a temporary Zarr path."""
    return tmp_path / "test_lapse.zarr"


def test_zarr_lapse_source_three_timepoints(tmp_zarr_path):
    """Test ZarrLapseSource with 3 scan{N} groups, verifying global frame indices."""
    frames_per_stack = 4
    num_timepoints = 3
    frame_shape = (3, 5)
    
    # Create root store
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs['timestamp'] = 123456.0
    root.attrs['rec_mode'] = 'recording'
    
    # Create 3 scan groups, each with frames_per_stack frames
    for scan_idx in range(num_timepoints):
        scan_group = root.create_group(f'scan{scan_idx}')
        det_group = scan_group.create_group('CAM')
        
        # Create data for this timepoint
        data = np.arange(
            frames_per_stack * frame_shape[0] * frame_shape[1],
            dtype=np.uint16
        ).reshape(frames_per_stack, *frame_shape) + (scan_idx * 1000)
        
        array = ZarrStorer._create_array(
            det_group, 'data', data=data, chunks=(2, *frame_shape)
        )
        array.attrs['detector_name'] = 'CAM'
        array.attrs['writing'] = False
        array.attrs['axes'] = ['T', 'Y', 'X']
        array.attrs['recording:frames_per_stack'] = frames_per_stack
        array.attrs['recording:num_timepoints'] = num_timepoints
        array.attrs['recording:single_lapse_file'] = True
        array.attrs['recording:dataset_path'] = f'/scan{scan_idx}/CAM/data'
        
        # Add ScanStage metadata
        meta_group = det_group.create_group('metadata')
        scan_stage = meta_group.create_group('ScanStage')
        scan_stage.attrs['position'] = [0.0, 0.0, float(scan_idx)]
    
    # Open with ZarrLapseSource
    source = ZarrLapseSource(detector_name='CAM', chunk_size=2)
    info = source.open(tmp_zarr_path)
    
    # Verify StackInfo
    assert info.frame_shape == frame_shape
    assert info.dtype == np.uint16
    assert info.detector_name == 'CAM'
    assert info.frames_per_stack == frames_per_stack
    assert info.expected_frames == num_timepoints * frames_per_stack  # 12 total frames
    assert info.source_format == 'ZARR'
    assert info.attrs['recording:num_timepoints'] == num_timepoints
    assert info.attrs['recording:frames_per_stack'] == frames_per_stack
    assert info.attrs['rec_mode'] == 'recording'
    
    # Poll all chunks
    all_chunks = []
    while not source.is_complete():
        chunks = source.poll()
        all_chunks.extend(chunks)
    
    # Verify we got chunks with GLOBAL indices
    assert len(all_chunks) == 6  # 12 frames / chunk_size=2
    
    # Verify global indexing
    expected_ranges = [
        (0, 2),   # scan0 chunk 0
        (2, 4),   # scan0 chunk 1
        (4, 6),   # scan1 chunk 0
        (6, 8),   # scan1 chunk 1
        (8, 10),  # scan2 chunk 0
        (10, 12), # scan2 chunk 1
    ]
    
    for i, (chunk, expected_range) in enumerate(zip(all_chunks, expected_ranges)):
        assert chunk.start == expected_range[0], f"Chunk {i}: expected start {expected_range[0]}, got {chunk.start}"
        assert chunk.end == expected_range[1], f"Chunk {i}: expected end {expected_range[1]}, got {chunk.end}"
    
    # Verify data continuity
    reconstructed = np.concatenate([c.data for c in all_chunks], axis=0)
    assert reconstructed.shape == (12, *frame_shape)
    
    # Verify each timepoint's data is correct
    for scan_idx in range(num_timepoints):
        start = scan_idx * frames_per_stack
        end = (scan_idx + 1) * frames_per_stack
        expected_data = np.arange(
            frames_per_stack * frame_shape[0] * frame_shape[1],
            dtype=np.uint16
        ).reshape(frames_per_stack, *frame_shape) + (scan_idx * 1000)
        np.testing.assert_array_equal(reconstructed[start:end], expected_data)
    
    assert source.is_complete()
    source.close()


def test_zarr_lapse_source_auto_detect_detector(tmp_zarr_path):
    """Test auto-detection of detector name in scan groups."""
    frames_per_stack = 2
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    
    for scan_idx in range(2):
        scan_group = root.create_group(f'scan{scan_idx}')
        det_group = scan_group.create_group('DETECTOR1')
        data = np.zeros((frames_per_stack, 2, 3), dtype=np.uint16)
        array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 2, 3))
        array.attrs['detector_name'] = 'DETECTOR1'
        array.attrs['writing'] = False
        array.attrs['recording:frames_per_stack'] = frames_per_stack
    
    source = ZarrLapseSource()  # No detector_name specified
    info = source.open(tmp_zarr_path)
    
    assert info.detector_name == 'DETECTOR1'
    source.close()


def test_zarr_lapse_source_metadata_flattening(tmp_zarr_path):
    """Test that ScanStage and other metadata is flattened correctly."""
    frames_per_stack = 2
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs['rec_mode'] = 'recording'
    
    scan_group = root.create_group('scan0')
    det_group = scan_group.create_group('CAM')
    data = np.zeros((frames_per_stack, 3, 4), dtype=np.uint16)
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 3, 4))
    array.attrs['detector_name'] = 'CAM'
    array.attrs['writing'] = False
    
    # Add structured metadata
    meta_group = det_group.create_group('metadata')
    scan_stage = meta_group.create_group('ScanStage')
    scan_stage.attrs['axis_startpos'] = [0.0, 0.0, 0.0]
    scan_stage.attrs['axis_step_size'] = [1.0, 1.0, 1.0]
    
    source = ZarrLapseSource(detector_name='CAM')
    info = source.open(tmp_zarr_path)
    
    # Verify flattened attributes
    assert info.attrs['rec_mode'] == 'recording'
    assert info.attrs['detector_name'] == 'CAM'
    assert 'ScanStage:axis_startpos' in info.attrs
    assert 'ScanStage:axis_step_size' in info.attrs
    np.testing.assert_array_equal(info.attrs['ScanStage:axis_startpos'], [0.0, 0.0, 0.0])
    
    source.close()


def test_zarr_lapse_source_factory_routing(tmp_zarr_path):
    """Test that make_live_source returns ZarrLapseSource for scan{N} stores."""
    frames_per_stack = 2
    
    # Create a single-file lapse store
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs['recording:single_lapse_file'] = True
    
    scan_group = root.create_group('scan0')
    det_group = scan_group.create_group('CAM')
    data = np.zeros((frames_per_stack, 2, 3), dtype=np.uint16)
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 2, 3))
    array.attrs['writing'] = False
    
    # Use factory
    source = make_live_source(tmp_zarr_path, detector_name='CAM')
    
    # Verify we got a ZarrLapseSource
    assert isinstance(source, ZarrLapseSource)
    
    info = source.open(tmp_zarr_path)
    assert info.frame_shape == (2, 3)
    source.close()


def test_zarr_lapse_source_vs_regular_source(tmp_zarr_path, tmp_path):
    """Test that regular stores use ZarrLiveSource, not ZarrLapseSource."""
    from imswitch.improcess.live import ZarrLiveSource
    
    # Create a regular (non-lapse) store
    regular_path = tmp_path / "regular.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(regular_path)), overwrite=True)
    det_group = root.create_group('CAM')
    data = np.zeros((4, 2, 3), dtype=np.uint16)
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 2, 3))
    array.attrs['writing'] = False
    
    # Use factory - should return ZarrLiveSource
    source = make_live_source(regular_path, detector_name='CAM')
    assert isinstance(source, ZarrLiveSource)
    assert not isinstance(source, ZarrLapseSource)
    source.close()


def test_zarr_lapse_source_partial_completion(tmp_zarr_path):
    """Test polling behavior when not all groups are written yet."""
    frames_per_stack = 3
    num_timepoints = 3
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    
    # Create only scan0 and scan1 initially
    for scan_idx in range(2):
        scan_group = root.create_group(f'scan{scan_idx}')
        det_group = scan_group.create_group('CAM')
        data = np.ones((frames_per_stack, 2, 3), dtype=np.uint16) * (scan_idx + 1)
        array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(2, 2, 3))
        array.attrs['detector_name'] = 'CAM'
        array.attrs['writing'] = False
        array.attrs['recording:frames_per_stack'] = frames_per_stack
        array.attrs['recording:num_timepoints'] = num_timepoints
    
    source = ZarrLapseSource(detector_name='CAM', chunk_size=2)
    info = source.open(tmp_zarr_path)
    
    assert info.expected_frames == 9  # 3 timepoints * 3 frames each
    
    # Poll should get scan0 and scan1 only
    chunks = []
    for _ in range(10):  # Multiple polls
        new_chunks = source.poll()
        chunks.extend(new_chunks)
        if not new_chunks:
            break
    
    # Should have 6 frames (scan0 + scan1)
    total_frames = sum(c.end - c.start for c in chunks)
    assert total_frames == 6
    
    # Not complete yet (scan2 missing)
    assert not source.is_complete()
    
    # Add scan2
    scan_group = root.create_group('scan2')
    det_group = scan_group.create_group('CAM')
    data = np.ones((frames_per_stack, 2, 3), dtype=np.uint16) * 3
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(2, 2, 3))
    array.attrs['detector_name'] = 'CAM'
    array.attrs['writing'] = False
    array.attrs['recording:frames_per_stack'] = frames_per_stack
    
    # Now we should be able to poll scan2
    new_chunks = source.poll()
    chunks.extend(new_chunks)
    
    # Verify global indices span all 3 groups
    assert chunks[0].start == 0
    assert chunks[-1].end == 9
    
    assert source.is_complete()
    source.close()


def test_zarr_lapse_source_no_scan_groups_error(tmp_zarr_path):
    """Test error when no scan{N} groups found."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    det_group = root.create_group('CAM')
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 3, 4))
    
    source = ZarrLapseSource(detector_name='CAM')
    
    with pytest.raises(ValueError, match="No scan.*groups found"):
        source.open(tmp_zarr_path)


def test_zarr_lapse_source_missing_detector_in_scan(tmp_zarr_path):
    """Test error when detector not found in scan group."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    scan_group = root.create_group('scan0')
    det_group = scan_group.create_group('CAM1')
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 3, 4))
    
    source = ZarrLapseSource(detector_name='CAM2')
    
    with pytest.raises(ValueError, match="not found"):
        source.open(tmp_zarr_path)


def test_zarr_lapse_source_frames_per_stack_fallback(tmp_zarr_path):
    """Test fallback to array length when recording:frames_per_stack is missing."""
    actual_frames = 5
    
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    scan_group = root.create_group('scan0')
    det_group = scan_group.create_group('CAM')
    data = np.zeros((actual_frames, 2, 3), dtype=np.uint16)
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(2, 2, 3))
    array.attrs['detector_name'] = 'CAM'
    array.attrs['writing'] = False
    # Note: no recording:frames_per_stack attribute
    
    source = ZarrLapseSource(detector_name='CAM')
    info = source.open(tmp_zarr_path)
    
    assert info.frames_per_stack == actual_frames
    source.close()


def test_zarr_lapse_source_poll_after_close(tmp_zarr_path):
    """Test that poll returns empty after close."""
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    scan_group = root.create_group('scan0')
    det_group = scan_group.create_group('CAM')
    data = np.zeros((2, 3, 4), dtype=np.uint16)
    array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(1, 3, 4))
    array.attrs['writing'] = False
    
    source = ZarrLapseSource(detector_name='CAM')
    source.open(tmp_zarr_path)
    source.close()
    
    assert source.poll() == []
    assert source.is_complete() is True


def test_zarr_lapse_poll_stops_at_the_byte_budget_and_resumes(tmp_zarr_path, monkeypatch):
    """A poll used to walk every timepoint in one call, so a finished lapse
    was materialised whole before its first chunk was processed. The budget
    ends the walk; the cursor stays put; the next poll carries on."""
    from imswitch.improcess.live import sources

    frames_per_stack, num_timepoints, frame_shape = 4, 3, (3, 5)
    root = zarr.group(store=ZarrStorer._make_store(str(tmp_zarr_path)), overwrite=True)
    root.attrs['timestamp'] = 123456.0
    root.attrs['rec_mode'] = 'recording'
    for scan_idx in range(num_timepoints):
        det_group = root.create_group(f'scan{scan_idx}').create_group('CAM')
        data = np.arange(
            frames_per_stack * frame_shape[0] * frame_shape[1], dtype=np.uint16
        ).reshape(frames_per_stack, *frame_shape) + scan_idx * 1000
        array = ZarrStorer._create_array(det_group, 'data', data=data, chunks=(2, *frame_shape))
        array.attrs['detector_name'] = 'CAM'
        array.attrs['writing'] = False
        array.attrs['axes'] = ['T', 'Y', 'X']
        array.attrs['recording:frames_per_stack'] = frames_per_stack
        array.attrs['recording:num_timepoints'] = num_timepoints
        array.attrs['recording:single_lapse_file'] = True
        array.attrs['recording:dataset_path'] = f'/scan{scan_idx}/CAM/data'
        det_group.create_group('metadata').create_group('ScanStage').attrs['position'] = [0.0, 0.0, float(scan_idx)]

    chunk_bytes = 2 * frame_shape[0] * frame_shape[1] * 2
    monkeypatch.setattr(sources, 'LIVE_POLL_MAX_BYTES', 2 * chunk_bytes)

    source = ZarrLapseSource(detector_name='CAM', chunk_size=2)
    source.open(tmp_zarr_path)
    polls = []
    while not source.is_complete():
        polls.append(source.poll())

    assert [len(chunks) for chunks in polls] == [2, 2, 2]
    chunks = [chunk for poll in polls for chunk in poll]
    assert [(c.start, c.end) for c in chunks] == [(i, i + 2) for i in range(0, 12, 2)]
    assert int(chunks[2].data[0, 0, 0]) == 1000  # the second timepoint, read on the second poll
