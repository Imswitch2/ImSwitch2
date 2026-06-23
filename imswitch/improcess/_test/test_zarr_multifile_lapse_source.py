"""Tests for ZarrMultiFileLapseSource (per-file timelapse accumulation)."""

import numpy as np
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import ZarrMultiFileLapseSource


def _write_lapse_file(path, frames, lapse_time):
    """Write a legacy-style single-array lapse store (array named 'chunks')."""
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    arr = ZarrStorer._create_array(
        root, "chunks", data=frames, chunks=(1, *frames.shape[-2:])
    )
    arr.attrs["detector_name"] = "CAM"
    arr.attrs["writing"] = False
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
