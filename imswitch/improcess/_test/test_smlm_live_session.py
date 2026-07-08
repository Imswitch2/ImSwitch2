"""Tests for SMLM live streaming localization."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.base import StreamInit, StreamingSession
from imswitch.improcess.reconstructors.smlm import SmlmLocalizer
from imswitch.improcess.reconstructors.smlm.localizer import localize_stack


def _gaussian_spot(frame, y, x, amplitude=200.0, sigma=1.3):
    """Add a Gaussian spot to a frame at (row=y, col=x)."""
    rows, cols = np.indices(frame.shape)
    frame += amplitude * np.exp(
        -((cols - x) ** 2 + (rows - y) ** 2) / (2 * sigma ** 2)
    )
    return frame


def _synthetic_blinking_stack(
    num_frames=30,
    shape=(64, 64),
    emitters=None,
    background=10.0,
    amplitude=300.0,
):
    """
    Create a synthetic blinking stack with spots appearing at different frames.
    
    Args:
        num_frames: Total number of frames.
        shape: Frame shape (height, width).
        emitters: List of (frame_index, y, x) tuples for spot positions.
        background: Background intensity.
        amplitude: Spot peak intensity.
    
    Returns:
        ndarray of shape (num_frames, height, width).
    """
    if emitters is None:
        # Default: a few blinking emitters across different frames
        emitters = [
            (0, 16.0, 20.0),
            (0, 40.0, 48.0),
            (5, 30.0, 10.0),
            (10, 50.0, 30.0),
            (15, 20.0, 50.0),
            (20, 45.0, 15.0),
            (25, 35.0, 40.0),
        ]
    
    stack = np.full((num_frames, shape[0], shape[1]), background, dtype=np.float32)
    for frame_idx, y, x in emitters:
        if 0 <= frame_idx < num_frames:
            _gaussian_spot(stack[frame_idx], y, x, amplitude=amplitude)
    
    return stack


def test_smlm_localizer_supports_streaming():
    """Test that SmlmLocalizer declares streaming support."""
    localizer = SmlmLocalizer()
    assert hasattr(localizer, "supports_streaming")
    assert localizer.supports_streaming is True
    assert hasattr(localizer, "make_session")


def test_make_session_returns_streaming_session():
    """Test that make_session returns a StreamingSession instance."""
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    assert isinstance(session, StreamingSession)
    assert hasattr(session, "begin")
    assert hasattr(session, "push")
    assert hasattr(session, "result")
    assert hasattr(session, "finish")
    assert hasattr(session, "close")


def test_live_session_begin():
    """Test StreamingSession.begin with synthetic blinking data."""
    stack = _synthetic_blinking_stack(num_frames=10)
    
    init_obj = StreamInit(
        name="test_blink",
        dataset_name="cam",
        data=stack,
        attrs={},
    )
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    plan = session.begin(init_obj, params)
    
    # Check plan structure
    assert plan.out_shape == (256, 256)  # placeholder preview shape
    assert plan.axis_labels == ["Y", "X"]
    assert plan.scale_unit == "nm"
    assert plan.dtype == np.dtype(np.float32)
    
    # Check that result() returns a valid LocalizationResult
    result = session.result()
    assert result.name == "test_blink localizations"
    assert result.pixel_size_nm == 100.0
    assert result.dims == "2D"
    assert result.source_shape == (64, 64)


def test_live_session_batch_equivalence():
    """
    Batch equivalence: feeding all frames through begin + push yields the same
    recarray as localize_stack over the whole array.
    """
    num_frames = 30
    stack = _synthetic_blinking_stack(num_frames=num_frames)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    # Batch reference: localize the whole stack at once
    batch_locs = localize_stack(
        stack,
        threshold=params["threshold"],
        roi=params["roi"],
        sigma=params["sigma"],
        method=params["method"],
        pixel_size_nm=params["pixel_size_nm"],
    )
    
    # Live path: begin with first 10 frames, push rest in 2 chunks
    init_frames = 10
    chunk1_frames = 10
    chunk2_frames = 10
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="test_blink",
        dataset_name="cam",
        data=stack[:init_frames],
        attrs={},
    )
    session.begin(init_obj, params)
    
    # Push remaining frames in two chunks
    session.push(
        stack[init_frames : init_frames + chunk1_frames],
        start=init_frames,
        end=init_frames + chunk1_frames,
    )
    session.push(
        stack[init_frames + chunk1_frames :],
        start=init_frames + chunk1_frames,
        end=num_frames,
    )
    
    live_result = session.result()
    live_locs = live_result.locs
    
    # Check that counts match
    assert len(live_locs) == len(batch_locs), (
        f"Live localization count {len(live_locs)} != "
        f"batch count {len(batch_locs)}"
    )
    
    # Check that frame indices match (order might differ but frames should be identical)
    assert set(live_locs["frame"]) == set(batch_locs["frame"])
    
    # Check that x_nm and y_nm values are close (allowing for floating-point differences)
    # Sort both by frame then x_nm for comparison
    live_sorted = np.sort(live_locs, order=["frame", "x_nm"])
    batch_sorted = np.sort(batch_locs, order=["frame", "x_nm"])
    
    np.testing.assert_allclose(live_sorted["x_nm"], batch_sorted["x_nm"], rtol=1e-5)
    np.testing.assert_allclose(live_sorted["y_nm"], batch_sorted["y_nm"], rtol=1e-5)
    np.testing.assert_allclose(live_sorted["photons"], batch_sorted["photons"], rtol=1e-4)


def test_live_session_monotonic_growth():
    """
    Localization count after each push is non-decreasing; result() after each
    push reflects the accumulated count.
    """
    num_frames = 20
    stack = _synthetic_blinking_stack(num_frames=num_frames)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="test_blink",
        dataset_name="cam",
        data=stack[:5],
        attrs={},
    )
    session.begin(init_obj, params)
    
    count_after_begin = session.result().count
    assert count_after_begin >= 0
    
    # Push chunk 1
    session.push(stack[5:10], start=5, end=10)
    count_after_chunk1 = session.result().count
    assert count_after_chunk1 >= count_after_begin
    
    # Push chunk 2
    session.push(stack[10:15], start=10, end=15)
    count_after_chunk2 = session.result().count
    assert count_after_chunk2 >= count_after_chunk1
    
    # Push chunk 3
    session.push(stack[15:20], start=15, end=20)
    count_after_chunk3 = session.result().count
    assert count_after_chunk3 >= count_after_chunk2


def test_live_session_stable_name():
    """result().name is identical across successive calls."""
    stack = _synthetic_blinking_stack(num_frames=15)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="stable_test",
        dataset_name="cam",
        data=stack[:5],
        attrs={},
    )
    session.begin(init_obj, params)
    
    name1 = session.result().name
    
    session.push(stack[5:10], start=5, end=10)
    name2 = session.result().name
    
    session.push(stack[10:15], start=10, end=15)
    name3 = session.result().name
    
    assert name1 == name2 == name3
    assert name1 == "stable_test localizations"


def test_live_session_global_frame_indices():
    """
    A spot only in the last chunk gets frame == its global index, not a
    chunk-local index.
    """
    num_frames = 25
    # Create a stack where only frame 20 has a spot
    emitters = [(20, 30.0, 30.0)]
    stack = _synthetic_blinking_stack(
        num_frames=num_frames, emitters=emitters, amplitude=500.0
    )
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    # Begin with frames 0-9 (no spots)
    init_obj = StreamInit(
        name="frame_test",
        dataset_name="cam",
        data=stack[:10],
        attrs={},
    )
    session.begin(init_obj, params)
    assert session.result().count == 0
    
    # Push frames 10-19 (no spots)
    session.push(stack[10:20], start=10, end=20)
    assert session.result().count == 0
    
    # Push frames 20-24 (spot in frame 20)
    session.push(stack[20:25], start=20, end=25)
    result = session.result()
    assert result.count == 1
    
    # The spot's frame index should be 20 (global), not 0 (chunk-local)
    assert result.locs["frame"][0] == 20


def test_live_session_empty_chunks():
    """Pushing all-zero frames neither crashes nor adds rows."""
    stack = np.zeros((10, 64, 64), dtype=np.float32)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="empty_test",
        dataset_name="cam",
        data=stack[:3],
        attrs={},
    )
    session.begin(init_obj, params)
    assert session.result().count == 0
    
    # Push more empty chunks
    session.push(stack[3:6], start=3, end=6)
    assert session.result().count == 0
    
    session.push(stack[6:10], start=6, end=10)
    assert session.result().count == 0


def test_live_session_finish_returns_result():
    """finish() returns a LocalizationResult with the final count."""
    stack = _synthetic_blinking_stack(num_frames=10)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="finish_test",
        dataset_name="cam",
        data=stack,
        attrs={},
    )
    session.begin(init_obj, params)
    
    final_result = session.finish()
    assert final_result.count > 0
    assert final_result.name == "finish_test localizations"


def test_live_session_close_frees_memory():
    """close() can be called without crashing."""
    stack = _synthetic_blinking_stack(num_frames=5)
    
    params = {
        "threshold": 50.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    
    localizer = SmlmLocalizer()
    session = localizer.make_session()
    
    init_obj = StreamInit(
        name="close_test",
        dataset_name="cam",
        data=stack,
        attrs={},
    )
    session.begin(init_obj, params)
    session.close()  # Should not crash


def test_batch_process_unchanged():
    """
    Batch process() is unchanged: existing tests in test_smlm_localizer.py
    should still pass. This is a smoke test to verify the interface.
    """
    from imswitch.improcess._test.test_smlm_localizer import (
        _FakeDataObj,
        _synthetic_frame,
    )
    
    frame = _synthetic_frame(emitters=[(16.0, 20.0), (40.0, 48.0)])
    stack = np.stack([frame, frame, frame])
    data_obj = _FakeDataObj(stack)
    
    localizer = SmlmLocalizer()
    params = {
        "threshold": 20.0,
        "roi": 9,
        "sigma": 1.0,
        "method": "gausslq",
        "pixel_size_nm": 100.0,
    }
    result = localizer.process(data_obj, params)
    
    assert result.count == 6
    assert result.pixel_size_nm == pytest.approx(100.0)
    assert result.dims == "2D"


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
