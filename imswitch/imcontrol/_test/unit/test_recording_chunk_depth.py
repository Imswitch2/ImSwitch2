"""The on-disk chunk is sized in bytes, and the arm wait contains the open.

Two numbers from the magic-number audit. The streaming datasets were chunked
32 frames deep because the writer batches 32 frames: fine for a small ROI,
a 256 MiB chunk for a 2048x2048 camera (bigger than any default chunk cache,
so scrubbing a slider re-inflated the whole chunk for every frame), and 32
whole assembled volumes for a point detector -- refused outright by the old
HDF5 file format once a chunk reaches 4 GiB. And a scan controller waited
5 s for the recording to arm while the writer inside that wait was allowed
30 s to create the file.
"""

import h5py
import numpy as np
import zarr

from imswitch.imcontrol.model import DetectorsManager, SaveMode
from imswitch.imcontrol.model.managers.RecordingManager import (
    DETECTOR_ARM_TIMEOUT_S, HDF5Storer, RECORDING_ARM_TIMEOUT,
    TARGET_CHUNK_BYTES, WRITE_BATCH_FRAMES, WRITER_OPEN_TIMEOUT_S, ZarrStorer,
    _chunkFrames,
)
from . import detectorInfosBasic


def _frame_bytes(dtype, shape):
    return np.dtype(dtype).itemsize * int(np.prod(shape))


def test_a_small_roi_gets_a_full_write_batch_per_chunk():
    assert _chunkFrames(np.uint16, (64, 64)) == WRITE_BATCH_FRAMES


def test_a_large_camera_frame_gets_one_or_two_frames_per_chunk():
    assert _chunkFrames(np.uint16, (2048, 2048)) == 1
    assert _chunkFrames(np.uint16, (1024, 1024)) == 2


def test_a_point_detector_volume_is_one_chunk():
    """Its one raw frame is the assembled volume; 32 of them was the 4 GiB cliff."""
    assert _chunkFrames(np.float32, (4, 128, 512, 512)) == 1
    assert _chunkFrames(np.uint16, (256, 512, 512)) == 1


def test_chunks_pack_to_the_byte_target():
    """Between the two limits the chunk fills the target without exceeding it."""
    for shape in [(100, 100), (300, 300), (512, 512), (700, 900), (1000, 1500)]:
        depth = _chunkFrames(np.uint16, shape)
        chunk_bytes = depth * _frame_bytes(np.uint16, shape)
        assert chunk_bytes <= TARGET_CHUNK_BYTES, shape
        if depth < WRITE_BATCH_FRAMES:
            assert chunk_bytes + _frame_bytes(np.uint16, shape) > TARGET_CHUNK_BYTES, shape


def test_a_degenerate_frame_still_gets_a_chunk():
    assert _chunkFrames(np.uint16, ()) == WRITE_BATCH_FRAMES
    assert _chunkFrames(np.uint16, (0, 4)) == 1


def _stream(storer, detectorName, dest, frames):
    storer.openStream(
        fileDests={detectorName: dest}, detectorNames=[detectorName],
        shapes={detectorName: frames.shape[-2:]}, attrs={detectorName: {}},
        singleMultiDetectorFile=False, singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream({detectorName: len(frames)}, {detectorName: dest},
                          None, SaveMode.Disk)


def test_hdf5_streaming_dataset_is_chunked_by_size(tmp_path):
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]

    big = str(tmp_path / 'big.h5')
    frames = np.zeros((3, 512, 2048), dtype=np.uint16)  # 2 MiB per frame
    _stream(HDF5Storer(big, detectorsManager), detectorName, big, frames)
    with h5py.File(big, 'r') as f:
        depth = f[detectorName]['data'].chunks[0]
    assert depth == 2
    assert depth * _frame_bytes(np.uint16, (512, 2048)) == TARGET_CHUNK_BYTES

    small = str(tmp_path / 'small.h5')
    frames = np.zeros((3, 8, 8), dtype=np.uint16)
    _stream(HDF5Storer(small, detectorsManager), detectorName, small, frames)
    with h5py.File(small, 'r') as f:
        assert f[detectorName]['data'].chunks[0] == WRITE_BATCH_FRAMES


def test_zarr_streaming_array_is_chunked_by_size(tmp_path):
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]

    big = str(tmp_path / 'big.zarr')
    frames = np.zeros((3, 512, 2048), dtype=np.uint16)
    _stream(ZarrStorer(str(tmp_path / 'unused'), detectorsManager),
            detectorName, big, frames)
    assert zarr.open(big, mode='r')[detectorName]['data'].chunks[0] == 2

    small = str(tmp_path / 'small.zarr')
    frames = np.zeros((3, 8, 8), dtype=np.uint16)
    _stream(ZarrStorer(str(tmp_path / 'unused'), detectorsManager),
            detectorName, small, frames)
    assert (zarr.open(small, mode='r')[detectorName]['data'].chunks[0]
            == WRITE_BATCH_FRAMES)


def test_the_arm_wait_contains_the_writer_open_budget():
    """A scan controller waits for the worker to open the file and then arm
    the detectors; the wait must outlast both, or a slow file open is
    reported as detectors that never armed."""
    assert RECORDING_ARM_TIMEOUT == WRITER_OPEN_TIMEOUT_S + DETECTOR_ARM_TIMEOUT_S
    assert RECORDING_ARM_TIMEOUT > WRITER_OPEN_TIMEOUT_S
