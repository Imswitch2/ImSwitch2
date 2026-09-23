"""Tests for ``ViewOnlyLiveSession``.

Drives the session exactly as ``LiveProcessWorker`` does -- ``begin()`` with the
first stack, then one frame per ``push()`` at its global index -- so the
contract is exercised rather than mocked. No Qt, no files.

    pytest imswitch/improcess/_test/test_view_only_live_session.py -v
"""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.base import (
    StackInfo,
    StreamingReconstructor,
    StreamInit,
)
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
)

H, W = 5, 6


def _frames(count, dtype=np.uint16):
    return (np.arange(count * H * W, dtype=dtype) % 997).reshape(count, H, W)


def _init(frames, *, frames_per_stack, num_timepoints=None, attrs=None,
          expected_frames=None):
    attrs = dict(attrs or {})
    if num_timepoints is not None:
        attrs["recording:num_timepoints"] = num_timepoints
    info = StackInfo(
        frame_shape=(H, W),
        dtype=frames.dtype,
        attrs=attrs,
        frames_per_stack=frames_per_stack,
        expected_frames=expected_frames if expected_frames is not None
        else frames_per_stack,
    )
    return StreamInit(
        name="measA", dataset_name="Cam",
        data=frames[:frames_per_stack], attrs=attrs, stack_info=info,
    )


def _run(frames, *, frames_per_stack, num_timepoints=None, **kwargs):
    """begin() with stack 0, then push the rest one frame at a time."""
    session = ViewOnlyReconstructor().make_session()
    init = _init(frames, frames_per_stack=frames_per_stack,
                 num_timepoints=num_timepoints, **kwargs)
    plan = session.begin(init, {})
    for i in range(frames_per_stack, len(frames)):
        session.push(frames[i:i + 1], i, i + 1)
    return session, plan


# --- the reconstructor now streams -----------------------------------------

def test_view_only_is_a_streaming_reconstructor():
    reconstructor = ViewOnlyReconstructor()

    assert isinstance(reconstructor, StreamingReconstructor)
    assert reconstructor.supports_streaming
    assert reconstructor.make_session() is not reconstructor.make_session()


# --- pass-through ----------------------------------------------------------

def test_frames_arrive_unchanged(recwarn):
    """The whole point: what the viewer gets is what was recorded."""
    frames = _frames(12)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=3)

    result = session.finish()

    assert result.data.shape == (3, 4, H, W)
    assert np.array_equal(result.data.reshape(12, H, W), frames)


def test_dtype_is_the_source_dtype_not_float32():
    """Promoting a uint16 pass-through would double a buffer that is already
    the whole recording, and buys nothing."""
    frames = _frames(8, dtype=np.uint16)
    session, plan = _run(frames, frames_per_stack=4, num_timepoints=2)

    assert plan.dtype == np.dtype(np.uint16)
    assert session.finish().data.dtype == np.uint16


def test_begin_consumes_the_first_stack():
    """The stream worker only pushes what follows stack 0, so begin() has to
    write it or timepoint 0 stays blank for the whole run."""
    frames = _frames(8)
    session = ViewOnlyReconstructor().make_session()

    session.begin(_init(frames, frames_per_stack=4, num_timepoints=2), {})

    assert np.array_equal(session.result().data[0].reshape(4, H, W), frames[:4])


# --- shapes the watcher will actually meet ---------------------------------

def test_camera_lapse_has_one_frame_per_timepoint():
    frames = _frames(6)
    session, _ = _run(frames, frames_per_stack=1, num_timepoints=6)

    result = session.finish()

    assert result.data.shape == (6, 1, H, W)
    assert np.array_equal(result.data.reshape(6, H, W), frames)


def test_a_single_snap_is_one_timepoint():
    frames = _frames(1)
    session, _ = _run(frames, frames_per_stack=1, num_timepoints=1)

    assert session.finish().data.shape == (1, 1, H, W)


def test_no_lapse_metadata_means_a_single_timepoint():
    """Right for a snap, a non-lapse recording, and a store carrying nothing."""
    frames = _frames(4)
    session, _ = _run(frames, frames_per_stack=4)

    assert session.finish().data.shape == (1, 4, H, W)


def test_timepoints_are_derived_from_expected_frames_when_unstated():
    frames = _frames(12)
    session, _ = _run(frames, frames_per_stack=4, expected_frames=12)

    assert session.finish().data.shape == (3, 4, H, W)


@pytest.mark.parametrize("written", ["null", "", "none"])
def test_legacy_null_timepoint_metadata_is_treated_as_absent(written):
    frames = _frames(4)
    session, _ = _run(frames, frames_per_stack=4,
                      attrs={"recording:num_timepoints": written})

    assert session.finish().data.shape == (1, 4, H, W)


def test_metadata_nested_under_imswitchdata_is_found():
    frames = _frames(8)
    session, _ = _run(
        frames, frames_per_stack=4,
        attrs={"ImswitchData": {"recording:num_timepoints": 2}},
    )

    assert session.finish().data.shape == (2, 4, H, W)


# --- the viewer contract ---------------------------------------------------

def test_the_timepoint_axis_is_named_T():
    """The viewer locates the timepoint axis by name -- it writes each
    incremental plane at axis_labels.index("T"). Renaming it silently disables
    live updates rather than failing."""
    frames = _frames(8)
    session, plan = _run(frames, frames_per_stack=4, num_timepoints=2)

    assert plan.axis_labels[0] == "T"
    assert session.finish().axis_labels.index("T") == 0


def test_live_plane_tracks_the_timepoint_being_written():
    frames = _frames(12)
    session = ViewOnlyReconstructor().make_session()
    session.begin(_init(frames, frames_per_stack=4, num_timepoints=3), {})

    assert session.live_plane()[0] == 0
    for i in range(4, 8):
        session.push(frames[i:i + 1], i, i + 1)
    assert session.live_plane()[0] == 1


def test_live_plane_keeps_every_axis_with_T_of_length_one():
    """So the caller can assign it straight into an identically-shaped buffer."""
    frames = _frames(8)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=2)

    index, plane = session.live_plane()

    assert plane.shape == (1, 4, H, W)
    assert np.array_equal(plane[0].reshape(4, H, W), frames[4:8])


def test_live_plane_is_a_copy():
    """The session goes on writing into its buffer; handing out a view would
    let the viewer read memory mutating on the process thread."""
    frames = _frames(4)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=1)

    _, plane = session.live_plane()
    plane[...] = 0

    assert session.result().data.any()


def test_result_is_a_copy():
    frames = _frames(4)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=1)

    snapshot = session.result()
    snapshot.data[...] = 0

    assert session.result().data.any()


# --- robustness ------------------------------------------------------------

def test_frames_beyond_the_allocated_timepoints_are_dropped_not_raised():
    """A recording that outruns its own metadata must not kill the run."""
    frames = _frames(12)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=2)

    result = session.finish()

    assert result.data.shape == (2, 4, H, W)
    assert np.array_equal(result.data.reshape(8, H, W), frames[:8])


def test_a_two_dimensional_chunk_is_accepted():
    frames = _frames(4)
    session = ViewOnlyReconstructor().make_session()
    session.begin(_init(frames, frames_per_stack=4, num_timepoints=2), {})

    session.push(frames[0], 4, 5)          # (Y, X), not (1, Y, X)

    assert np.array_equal(session.result().data[1, 0], frames[0])


def test_push_before_begin_raises():
    session = ViewOnlyReconstructor().make_session()
    with pytest.raises(RuntimeError):
        session.push(_frames(1), 0, 1)


def test_live_plane_before_begin_is_none():
    assert ViewOnlyReconstructor().make_session().live_plane() is None


def test_close_releases_the_buffer():
    frames = _frames(4)
    session, _ = _run(frames, frames_per_stack=4, num_timepoints=1)

    session.close()

    assert session.data is None
    assert session.live_plane() is None


def test_a_non_three_dimensional_first_chunk_is_rejected():
    session = ViewOnlyReconstructor().make_session()
    info = StackInfo(frame_shape=(H, W), dtype=np.dtype(np.uint16), attrs={})
    init = StreamInit(name="x", dataset_name="Cam",
                      data=np.zeros((H, W), np.uint16), stack_info=info)

    with pytest.raises(ValueError):
        session.begin(init, {})


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


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
