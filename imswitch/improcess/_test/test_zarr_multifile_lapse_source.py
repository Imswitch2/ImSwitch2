"""Tests for ZarrMultiFileLapseSource (per-file timelapse accumulation)."""

import numpy as np
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import ZarrMultiFileLapseSource


def _write_lapse_file(path, frames, lapse_time, writing=False):
    """Write a legacy-style single-array lapse store (array named 'chunks')."""
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    arr = ZarrStorer._create_array(
        root, "chunks", data=frames, chunks=(1, *frames.shape[-2:])
    )
    arr.attrs["detector_name"] = "CAM"
    arr.attrs["writing"] = writing
    arr.attrs["ImswitchData"] = {
        "ScanStage:axis_startpos": [[0], [0], [0]],
        "ScanStage:axis_length": [0.2, 0.2, 0.0],
        "ScanStage:axis_step_size": [0.1, 0.1, 0.1],
        "Rec:LapseTime": lapse_time,
    }


def test_multifile_lapse_streams_all_timepoints_as_one_stream(tmp_path):
    fps = 9  # 3x3 scan (one stack/timepoint)
    n_tp = 3
    stacks = [
        np.full((fps, 4, 5), t + 1, dtype=np.int16) for t in range(n_tp)
    ]
    for t, stack in enumerate(stacks):
        _write_lapse_file(tmp_path / f"rec_scan__0{t}__CAM.zarr", stack, n_tp)

    seed = str(tmp_path / "rec_scan__00__CAM.zarr")
    src = ZarrMultiFileLapseSource(seed)
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


def test_multifile_lapse_waits_for_missing_later_timepoint(tmp_path):
    """If a later timepoint file is absent, the source streams what's present
    and reports not-complete (waiting), rather than ending early."""
    fps = 9
    _write_lapse_file(tmp_path / "rec_scan__00__CAM.zarr",
                      np.ones((fps, 4, 5), dtype=np.int16), 3)

    seed = str(tmp_path / "rec_scan__00__CAM.zarr")
    src = ZarrMultiFileLapseSource(seed)
    info = src.open(seed)
    assert info.expected_frames == fps * 3  # expects 3 timepoints

    # Drain the only present file.
    for _ in range(5):
        src.poll()
    # Timepoint 1 file doesn't exist yet -> not complete (live: keep waiting).
    assert src.is_complete() is False
    src.close()


def test_multifile_lapse_does_not_advance_onto_writing_store(tmp_path):
    """The source must not open the next timepoint store until it is complete.

    ZarrStorer resizes the array BEFORE writing frame data, so a mid-write
    store's shape is ahead of its committed data; opening it early reads
    uninitialised (zero) frames that get permanently baked into the stream
    (root cause 1 in docs/live_reconstruction_audit.md). Advance must be
    gated on writing=False, not on file existence.
    """
    fps = 9
    n_tp = 2
    _write_lapse_file(tmp_path / "rec_scan__00__CAM.zarr",
                      np.full((fps, 4, 5), 1, dtype=np.int16), n_tp)
    # Timepoint 1 exists but is still being written.
    tp1 = tmp_path / "rec_scan__01__CAM.zarr"
    _write_lapse_file(tp1, np.full((fps, 4, 5), 2, dtype=np.int16), n_tp,
                      writing=True)

    seed = str(tmp_path / "rec_scan__00__CAM.zarr")
    src = ZarrMultiFileLapseSource(seed)
    src.open(seed)

    # Drain timepoint 0 and poll a few extra times: the writing tp1 store
    # must not be entered, so no chunk may carry a global index >= fps.
    chunks = []
    for _ in range(10):
        chunks.extend(src.poll())
    assert chunks and max(c.end for c in chunks) == fps
    assert src.is_complete() is False  # waiting on tp1, not finished early

    # Recorder finishes timepoint 1 -> now the source advances and reads it.
    root = zarr.open(str(tp1), mode="a")
    root["chunks"].attrs["writing"] = False

    chunks2 = []
    guard = 0
    while not src.is_complete() and guard < 10_000:
        chunks2.extend(src.poll())
        guard += 1
    src.close()

    data = np.concatenate([c.data for c in chunks2], axis=0)
    assert chunks2[0].start == fps
    assert chunks2[-1].end == fps * n_tp
    assert np.all(data == 2)  # real frames, not zeros from a mid-write read


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


def test_multifile_lapse_advances_onto_midwrite_store_with_barrier(tmp_path):
    """A next timepoint carrying frames_committed can be followed mid-write.

    With the barrier the inner reader caps reads at the flushed data, so
    advancing before writing=False is safe and restores per-frame liveness
    for later timepoints. Barrier-less stores keep the complete-only gate
    (see test_multifile_lapse_does_not_advance_onto_writing_store).
    """
    fps = 9
    n_tp = 2
    _write_lapse_file(tmp_path / "rec_scan__00__CAM.zarr",
                      np.full((fps, 4, 5), 1, dtype=np.int16), n_tp)
    # Timepoint 1: still writing, but with the committed barrier — 4 of 9
    # frames flushed so far.
    tp1 = tmp_path / "rec_scan__01__CAM.zarr"
    _write_lapse_file(tp1, np.full((fps, 4, 5), 2, dtype=np.int16), n_tp,
                      writing=True)
    root = zarr.open(str(tp1), mode="a")
    root["chunks"].attrs["recording:frames_committed"] = 4

    seed = str(tmp_path / "rec_scan__00__CAM.zarr")
    src = ZarrMultiFileLapseSource(seed)
    src.open(seed)

    chunks = []
    for _ in range(10):
        chunks.extend(src.poll())

    # Advanced onto tp1 and read exactly its committed frames — no more.
    assert max(c.end for c in chunks) == fps + 4
    tp1_frames = np.concatenate(
        [c.data for c in chunks if c.start >= fps], axis=0)
    assert np.all(tp1_frames == 2)  # committed data, not uninitialised zeros
    assert src.is_complete() is False

    # Recorder finishes tp1.
    root = zarr.open(str(tp1), mode="a")
    root["chunks"].attrs["recording:frames_committed"] = fps
    root["chunks"].attrs["writing"] = False

    chunks2 = []
    guard = 0
    while not src.is_complete() and guard < 10_000:
        chunks2.extend(src.poll())
        guard += 1
    src.close()

    assert chunks2[-1].end == fps * n_tp
    assert np.all(np.concatenate([c.data for c in chunks2], axis=0) == 2)
