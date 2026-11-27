"""Regression test for bounded scoped HDF5 timelapses with physical scan offsets."""

import h5py
import numpy as np

from imswitch.improcess.live import Hdf5LapseSource
from imswitch.improcess.live import sources


def test_nonzero_origin_scope_does_not_finish_between_bounded_polls(tmp_path, monkeypatch):
    """Physical scan5 must not be mistaken for logical final timepoint T2."""
    path = tmp_path / "scoped_lapse.h5"
    frames_per_stack = 4
    frame_shape = (2, 3)
    chunk_size = 2

    with h5py.File(path, "w", libver="latest") as file:
        for scan_index in range(8):
            detector = file.create_group(f"scan{scan_index}").create_group("CAM")
            values = (
                np.arange(frames_per_stack * np.prod(frame_shape), dtype=np.uint16)
                .reshape(frames_per_stack, *frame_shape)
                + 1000 * scan_index
            )
            dataset = detector.create_dataset(
                "data", data=values, chunks=(chunk_size, *frame_shape)
            )
            dataset.attrs["detector_name"] = "CAM"
            dataset.attrs["writing"] = False
            dataset.attrs["recording:frames_per_stack"] = frames_per_stack
            dataset.attrs["recording:num_timepoints"] = 3 if scan_index >= 5 else 5
            dataset.attrs["recording:dataset_path"] = f"/scan{scan_index}/CAM/data"
            stage = detector.create_group("metadata").create_group("ScanStage")
            stage.attrs["position"] = [0.0, 0.0, float(scan_index)]
        file.flush()
        file.swmr_mode = True

    # One 2-frame chunk per poll: completion is evaluated between scan groups.
    chunk_bytes = chunk_size * np.prod(frame_shape) * np.dtype("uint16").itemsize
    monkeypatch.setattr(sources, "LIVE_POLL_MAX_BYTES", chunk_bytes)
    source = Hdf5LapseSource(
        detector_name="CAM",
        chunk_size=chunk_size,
        timepoint_start=5,
        timepoint_stop=8,
    )
    try:
        info = source.open(path)
        assert info.expected_frames == 3 * frames_per_stack
        chunks = []
        for _ in range(6):
            assert not source.is_complete(), "Scoped stream completed too early"
            polled = source.poll()
            assert len(polled) == 1
            chunks.extend(polled)
        assert source.is_complete()
        assert [(chunk.start, chunk.end) for chunk in chunks] == [
            (start, start + chunk_size) for start in range(0, 12, chunk_size)
        ]
        assert [int(chunk.data[0, 0, 0]) for chunk in chunks] == [
            5000, 5012, 6000, 6012, 7000, 7012
        ]
    finally:
        source.close()
