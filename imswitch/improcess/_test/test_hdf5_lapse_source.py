"""Tests for HDF5 lapse sources (single-file scan{N} and multi-file)."""

from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.live import Hdf5LapseSource, Hdf5MultiFileLapseSource, make_live_source


@pytest.fixture
def tmp_hdf5_path(tmp_path):
    """Fixture providing a temporary HDF5 path."""
    return tmp_path / "test_lapse.h5"


def _write_hdf5_scan_group(file, scan_idx, detector_name, frames_per_stack, frame_shape, 
                            num_timepoints=None, is_writing=False):
    """Helper to write a single scan{N} group."""
    scan_group = file.create_group(f'scan{scan_idx}')
    det_group = scan_group.create_group(detector_name)
    
    data = np.arange(
        frames_per_stack * frame_shape[0] * frame_shape[1],
        dtype=np.uint16
    ).reshape(frames_per_stack, *frame_shape) + (scan_idx * 1000)
    
    dataset = det_group.create_dataset('data', data=data, chunks=(2, *frame_shape))
    dataset.attrs['detector_name'] = detector_name
    dataset.attrs['writing'] = is_writing
    dataset.attrs['recording:frames_per_stack'] = frames_per_stack
    if num_timepoints is not None:
        dataset.attrs['recording:num_timepoints'] = num_timepoints
    dataset.attrs['recording:dataset_path'] = f'/scan{scan_idx}/{detector_name}/data'
    
    # Add metadata group
    meta_group = det_group.create_group('metadata')
    scan_stage = meta_group.create_group('ScanStage')
    scan_stage.attrs['position'] = [0.0, 0.0, float(scan_idx)]
    
    return dataset


def test_hdf5_lapse_source_three_timepoints(tmp_hdf5_path):
    """Test Hdf5LapseSource with 3 scan{N} groups, verifying global frame indices."""
    frames_per_stack = 4
    num_timepoints = 3
    frame_shape = (3, 5)
    
    # Create HDF5 file with SWMR-compatible layout
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs['timestamp'] = 123456.0
        f.attrs['rec_mode'] = 'recording'
        
        # Create 3 scan groups
        for scan_idx in range(num_timepoints):
            _write_hdf5_scan_group(f, scan_idx, 'CAM', frames_per_stack, frame_shape, 
                                   num_timepoints=num_timepoints, is_writing=False)
        
        f.flush()
        f.swmr_mode = True
    
    # Open with Hdf5LapseSource
    source = Hdf5LapseSource(detector_name='CAM', chunk_size=2)
    info = source.open(tmp_hdf5_path)
    
    # Verify StackInfo
    assert info.frame_shape == frame_shape
    assert info.dtype == np.uint16
    assert info.detector_name == 'CAM'
    assert info.frames_per_stack == frames_per_stack
    assert info.expected_frames == num_timepoints * frames_per_stack  # 12 total frames
    assert info.source_format == 'HDF5'
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


def test_hdf5_lapse_source_auto_detect_detector(tmp_hdf5_path):
    """Test auto-detection of detector name in scan groups."""
    frames_per_stack = 2
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        for scan_idx in range(2):
            _write_hdf5_scan_group(f, scan_idx, 'DETECTOR1', frames_per_stack, (2, 3))
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource()  # No detector_name specified
    info = source.open(tmp_hdf5_path)
    
    assert info.detector_name == 'DETECTOR1'
    source.close()


def test_hdf5_lapse_source_metadata_flattening(tmp_hdf5_path):
    """Test that ScanStage and other metadata is flattened correctly."""
    frames_per_stack = 2
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs['rec_mode'] = 'recording'
        
        scan_group = f.create_group('scan0')
        det_group = scan_group.create_group('CAM')
        data = np.zeros((frames_per_stack, 3, 4), dtype=np.uint16)
        dataset = det_group.create_dataset('data', data=data, chunks=(1, 3, 4))
        dataset.attrs['detector_name'] = 'CAM'
        dataset.attrs['writing'] = False
        
        # Add structured metadata
        meta_group = det_group.create_group('metadata')
        scan_stage = meta_group.create_group('ScanStage')
        scan_stage.attrs['axis_startpos'] = [0.0, 0.0, 0.0]
        scan_stage.attrs['axis_step_size'] = [1.0, 1.0, 1.0]
        
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource(detector_name='CAM')
    info = source.open(tmp_hdf5_path)
    
    # Verify flattened attributes
    assert info.attrs['rec_mode'] == 'recording'
    assert info.attrs['detector_name'] == 'CAM'
    assert 'ScanStage:axis_startpos' in info.attrs
    assert 'ScanStage:axis_step_size' in info.attrs
    np.testing.assert_array_equal(info.attrs['ScanStage:axis_startpos'], [0.0, 0.0, 0.0])
    
    source.close()


def test_hdf5_lapse_source_factory_routing(tmp_hdf5_path):
    """Test that make_live_source returns Hdf5LapseSource for scan{N} HDF5 files."""
    frames_per_stack = 2
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs['recording:single_lapse_file'] = True
        _write_hdf5_scan_group(f, 0, 'CAM', frames_per_stack, (2, 3))
        f.flush()
        f.swmr_mode = True
    
    # Use factory
    source = make_live_source(tmp_hdf5_path, detector_name='CAM')
    
    # Verify we got an Hdf5LapseSource
    assert isinstance(source, Hdf5LapseSource)
    
    info = source.open(tmp_hdf5_path)
    assert info.frame_shape == (2, 3)
    source.close()


def test_hdf5_lapse_source_vs_regular_source(tmp_hdf5_path, tmp_path):
    """Test that regular HDF5 files use Hdf5LiveSource, not Hdf5LapseSource."""
    from imswitch.improcess.live import Hdf5LiveSource
    
    # Create a regular (non-lapse) HDF5 file
    regular_path = tmp_path / "regular.h5"
    with h5py.File(regular_path, 'w', libver='latest') as f:
        det_group = f.create_group('CAM')
        data = np.zeros((4, 2, 3), dtype=np.uint16)
        dataset = det_group.create_dataset('data', data=data, chunks=(1, 2, 3))
        dataset.attrs['writing'] = False
        f.flush()
        f.swmr_mode = True
    
    # Use factory - should return Hdf5LiveSource
    source = make_live_source(regular_path, detector_name='CAM')
    assert isinstance(source, Hdf5LiveSource)
    assert not isinstance(source, Hdf5LapseSource)
    source.close()


def test_hdf5_lapse_source_no_scan_groups_error(tmp_hdf5_path):
    """Test error when no scan{N} groups found."""
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        det_group = f.create_group('CAM')
        data = np.zeros((2, 3, 4), dtype=np.uint16)
        det_group.create_dataset('data', data=data, chunks=(1, 3, 4))
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource(detector_name='CAM')
    
    with pytest.raises(ValueError, match="No scan.*groups found"):
        source.open(tmp_hdf5_path)


def test_hdf5_lapse_source_missing_detector_in_scan(tmp_hdf5_path):
    """Test error when detector not found in scan group."""
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        scan_group = f.create_group('scan0')
        det_group = scan_group.create_group('CAM1')
        data = np.zeros((2, 3, 4), dtype=np.uint16)
        det_group.create_dataset('data', data=data, chunks=(1, 3, 4))
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource(detector_name='CAM2')
    
    with pytest.raises(ValueError, match="not found"):
        source.open(tmp_hdf5_path)


def test_hdf5_lapse_source_frames_per_stack_fallback(tmp_hdf5_path):
    """Test fallback to dataset length when recording:frames_per_stack is missing."""
    actual_frames = 5
    
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        scan_group = f.create_group('scan0')
        det_group = scan_group.create_group('CAM')
        data = np.zeros((actual_frames, 2, 3), dtype=np.uint16)
        dataset = det_group.create_dataset('data', data=data, chunks=(2, 2, 3))
        dataset.attrs['detector_name'] = 'CAM'
        dataset.attrs['writing'] = False
        # Note: no recording:frames_per_stack attribute
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource(detector_name='CAM')
    info = source.open(tmp_hdf5_path)
    
    assert info.frames_per_stack == actual_frames
    source.close()


def test_hdf5_lapse_source_poll_after_close(tmp_hdf5_path):
    """Test that poll returns empty after close."""
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        _write_hdf5_scan_group(f, 0, 'CAM', 2, (3, 4))
        f.flush()
        f.swmr_mode = True
    
    source = Hdf5LapseSource(detector_name='CAM')
    source.open(tmp_hdf5_path)
    source.close()
    
    assert source.poll() == []
    assert source.is_complete() is True


def _write_multifile_hdf5(path, frames, lapse_time, detector_name='CAM'):
    """Write a single-stack HDF5 file for multi-file lapse testing."""
    with h5py.File(path, 'w', libver='latest') as f:
        det_group = f.create_group(detector_name)
        dataset = det_group.create_dataset('data', data=frames, chunks=(1, *frames.shape[-2:]))
        dataset.attrs['detector_name'] = detector_name
        dataset.attrs['writing'] = False
        dataset.attrs['recording:num_timepoints'] = lapse_time
        dataset.attrs['recording:single_lapse_file'] = False
        dataset.attrs['recording:frames_per_stack'] = frames.shape[0]
        dataset.attrs['recording:expected_frames'] = frames.shape[0]
        f.flush()
        f.swmr_mode = True


def test_hdf5_multifile_lapse_streams_all_timepoints_as_one_stream(tmp_path):
    """Test Hdf5MultiFileLapseSource streams all timepoints with global indices."""
    fps = 9  # frames per stack
    n_tp = 3
    stacks = [
        np.full((fps, 4, 5), t + 1, dtype=np.int16) for t in range(n_tp)
    ]
    for t, stack in enumerate(stacks):
        _write_multifile_hdf5(tmp_path / f"rec_scan__0{t}__CAM.h5", stack, n_tp)

    seed = str(tmp_path / "rec_scan__00__CAM.h5")
    src = Hdf5MultiFileLapseSource(seed)
    info = src.open(seed)

    assert info.frames_per_stack == fps
    assert info.expected_frames == fps * n_tp  # all timepoints

    chunks = []
    guard = 0
    while not src.is_complete() and guard < 10_000:
        chunks.extend(src.poll())
        guard += 1
    src.close()

    # Global indices are contiguous across the three files [0, fps*n_tp).
    starts = [c.start for c in chunks]
    assert starts == sorted(starts)
    assert chunks[0].start == 0
    assert chunks[-1].end == fps * n_tp

    data = np.concatenate([c.data for c in chunks], axis=0)
    assert data.shape[0] == fps * n_tp
    # Each timepoint block carries its own constant value (1, 2, 3).
    for t in range(n_tp):
        block = data[t * fps:(t + 1) * fps]
        assert np.all(block == t + 1)


def test_hdf5_multifile_lapse_waits_for_missing_later_timepoint(tmp_path):
    """If a later timepoint file is absent, the source streams what's present
    and reports not-complete (waiting), rather than ending early."""
    fps = 9
    _write_multifile_hdf5(tmp_path / "rec_scan__00__CAM.h5",
                          np.ones((fps, 4, 5), dtype=np.int16), 3)

    seed = str(tmp_path / "rec_scan__00__CAM.h5")
    src = Hdf5MultiFileLapseSource(seed)
    info = src.open(seed)
    assert info.expected_frames == fps * 3  # expects 3 timepoints

    # Drain the only present file.
    for _ in range(5):
        src.poll()
    # Timepoint 1 file doesn't exist yet -> not complete (live: keep waiting).
    assert src.is_complete() is False
    src.close()


# Copyright (C) 2020-2026 ImSwitch developers
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


def test_hdf5_lapse_poll_stops_at_the_byte_budget_and_resumes(tmp_hdf5_path, monkeypatch):
    """A poll used to walk every timepoint in one call, so a finished lapse
    was materialised whole before its first chunk was processed. The budget
    ends the walk; the cursor stays put; the next poll carries on."""
    from imswitch.improcess.live import sources

    frames_per_stack, num_timepoints, frame_shape = 4, 3, (3, 5)
    with h5py.File(tmp_hdf5_path, 'w', libver='latest') as f:
        f.attrs['timestamp'] = 123456.0
        f.attrs['rec_mode'] = 'recording'
        for scan_idx in range(num_timepoints):
            _write_hdf5_scan_group(f, scan_idx, 'CAM', frames_per_stack, frame_shape,
                                   num_timepoints=num_timepoints, is_writing=False)
        f.flush()
        f.swmr_mode = True

    chunk_bytes = 2 * frame_shape[0] * frame_shape[1] * 2
    monkeypatch.setattr(sources, 'LIVE_POLL_MAX_BYTES', 2 * chunk_bytes)

    source = Hdf5LapseSource(detector_name='CAM', chunk_size=2)
    source.open(tmp_hdf5_path)
    polls = []
    while not source.is_complete():
        polls.append(source.poll())

    assert [len(chunks) for chunks in polls] == [2, 2, 2]
    chunks = [chunk for poll in polls for chunk in poll]
    assert [(c.start, c.end) for c in chunks] == [(i, i + 2) for i in range(0, 12, 2)]
    assert int(chunks[2].data[0, 0, 0]) == 1000  # the second timepoint, read on the second poll
