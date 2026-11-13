"""M1 regression: metadata negotiation and finite/growing incremental handoff.

The core tests require NumPy only; the QThread smoke test requires pytest-qt.
No ImControl hardware or acquisition directory observer is involved.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.execution.contracts import Contract, Incompatible, raw_info_from_stack
from imswitch.improcess.execution.frame_inbox import FrameInbox
from imswitch.improcess.execution.reader import IncrementalReader


@dataclass
class _Chunk:
    data: np.ndarray
    start: int
    end: int


class _Source:
    def __init__(self, frames, *, available=0, chunk=2, attrs=None, fps=None):
        self.frames = frames
        self.available = available
        self.chunk = chunk
        self.cursor = 0
        self.closed = False
        self.complete = False
        self.info = SimpleNamespace(
            frame_shape=frames.shape[-2:], dtype=frames.dtype,
            attrs=attrs or {"Rec:Mode": "ScanOnce"},
            frames_per_stack=fps or len(frames), expected_frames=len(frames),
            planned_timepoints=1, detector_name="CAM", source_format="ZARR",
            acquisition_layout=None,
        )

    def open(self, _path):
        return self.info

    def poll(self):
        if self.cursor >= self.available:
            return []
        start = self.cursor
        self.cursor = min(start + self.chunk, self.available)
        return [_Chunk(self.frames[start:self.cursor].copy(), start, self.cursor)]

    def is_complete(self):
        return self.complete and self.cursor == self.available

    def close(self):
        self.closed = True


class _ScanReconstructor:
    @classmethod
    def contract(cls, info, params):
        if not info.S:
            raise Incompatible("not a scan")
        return Contract(info.frames_per_timepoint, 2, dict(params))


class _ViewReconstructor:
    @classmethod
    def contract(cls, info, params):
        return Contract(0, 1, dict(params))


def _step(reader):
    reader.prefetch()
    return reader.next_item()


def test_mode_is_not_inferred_from_reader_stack_fallback():
    """12-frame SpecFrames is T=12, S=(), not a scan stack of 12."""
    info = SimpleNamespace(
        frame_shape=(4, 5), dtype=np.dtype('uint16'),
        frames_per_stack=12, expected_frames=12, planned_timepoints=1,
        attrs={"Rec:Mode": "SpecFrames", "recording:num_timepoints": 1},
    )
    raw = raw_info_from_stack(info, name="camera")
    assert raw.T == 12 and raw.S == () and raw.frames_per_timepoint == 1
    with pytest.raises(Incompatible):
        _ScanReconstructor.contract(raw, {})
    scan = raw_info_from_stack(SimpleNamespace(
        **{**vars(info), "attrs": {"Rec:Mode": "ScanLapse"},
           "planned_timepoints": 20, "frames_per_stack": 576}
    ))
    assert scan.T == 20 and scan.S == (576,)
    assert _ScanReconstructor.contract(scan, {}).init_frames == 576


def test_replayed_scanlapse_keeps_declared_timepoint_count():
    """Recorder mode must describe the *replayed lapse*, not the input scan."""
    info = SimpleNamespace(
        frame_shape=(456, 452), dtype=np.dtype("uint16"),
        frames_per_stack=242, expected_frames=5 * 242, planned_timepoints=5,
        attrs={"recording:mode": "ScanLapse", "Rec:Mode": "ScanLapse",
               "recording:num_timepoints": 5, "recording:frames_per_stack": 242},
        detector_name="CAM", source_format="ZARR", acquisition_layout=None,
    )
    raw = raw_info_from_stack(info)
    assert raw.mode == "ScanLapse"
    assert raw.T == raw.planned_timepoints == 5
    assert raw.frames_per_timepoint == 242


def test_finish_finite_once_and_never_push_begin_frames():
    frames = np.arange(7 * 2 * 2, dtype=np.uint16).reshape(7, 2, 2)
    src = _Source(frames, available=len(frames), chunk=2, fps=3)
    src.complete = True
    reader = IncrementalReader(src, _ScanReconstructor, {"test": 1}, name="finite")
    reader.open()
    items = []
    for _ in range(30):
        item = _step(reader)
        if item is None:
            continue
        items.append(item)
        reader.acknowledge()
        if reader.done:
            break
    assert [item.kind for item in items] == ["begin", "push", "push", "finish"]
    assert [(it.frames.start, it.frames.end) for it in items[:-1]] == [
        (0, 3), (3, 5), (5, 7)
    ]
    assert np.array_equal(np.concatenate([item.frames.data for item in items[:-1]]), frames)
    reader.close()
    assert src.closed


def test_growing_source_waits_and_ack_gate_limits_inflight():
    frames = np.arange(6 * 2 * 2, dtype=np.uint16).reshape(6, 2, 2)
    src = _Source(frames, available=1, chunk=2, fps=3)
    reader = IncrementalReader(src, _ScanReconstructor, {}, name="growing")
    reader.open()
    assert _step(reader) is None
    src.available = 3
    begin = _step(reader)
    assert begin.kind == "begin" and begin.frames.end == 3
    assert _step(reader) is None  # strictly one in flight until ack
    reader.acknowledge()
    reader.request_pause(True)
    src.available = 6
    assert _step(reader) is None  # paused at a timepoint boundary
    reader.request_pause(False)
    first = _step(reader)
    assert first.kind == "push" and (first.frames.start, first.frames.end) == (3, 5)
    reader.acknowledge()
    # Only the last partial chunk remains after the source closes.
    assert _step(reader) is None
    src.complete = True
    final = _step(reader)
    assert final.kind == "push" and (final.frames.start, final.frames.end) == (5, 6)
    reader.acknowledge()
    finish = _step(reader)
    assert finish.kind == "finish"
    reader.acknowledge()
    assert reader.done
    assert np.array_equal(np.concatenate([begin.frames.data, first.frames.data, final.frames.data]), frames)


def test_first_frame_start_and_incomplete_initialization_failure():
    frames = np.ones((2, 2, 2), np.uint16)
    src = _Source(frames, available=2, attrs={"Rec:Mode": "SpecFrames"})
    reader = IncrementalReader(src, _ViewReconstructor, {}, name="camera")
    reader.open()
    assert reader.info.S == () and reader.info.T == 2
    assert _step(reader).frames.end == 1
    reader.acknowledge()
    assert _step(reader).frames.end == 2
    reader.acknowledge()
    src.complete = True
    assert _step(reader).kind == "finish"

    short = _Source(frames, available=2, fps=3)
    short.complete = True
    reader = IncrementalReader(short, _ScanReconstructor, {}, name="too-short")
    reader.open()
    with pytest.raises(ValueError, match="before 3 initialization frames"):
        _step(reader)


def test_frame_inbox_rejects_gaps_overbudget_and_invalid_ranges_atomically():
    inbox = FrameInbox(max_bytes=20)
    a = _Chunk(np.ones((2, 2, 2), np.uint8), 0, 2)
    inbox.append([a])
    with pytest.raises(ValueError, match="Non-contiguous"):
        inbox.append([_Chunk(np.ones((1, 2, 2), np.uint8), 3, 4)])
    assert inbox.available == 2
    with pytest.raises(MemoryError, match="budget"):
        inbox.append([_Chunk(np.ones((4, 2, 2), np.uint8), 2, 6)])
    assert inbox.available == 2
    assert inbox.take(3) is None
    taken = inbox.take(1)
    assert (taken.start, taken.end) == (0, 1)
    assert inbox.take(1).start == 1
    assert inbox.available == 0


def test_contract_validation():
    with pytest.raises(ValueError):
        Contract(-1, 1, {})
    with pytest.raises(ValueError):
        Contract(0, 0, {})


def test_two_thread_runner_finishes_without_duplicate_frames(qtbot):
    from imswitch.improcess.execution.runner import QtIncrementalRunner

    frames = np.arange(7 * 2 * 2, dtype=np.uint16).reshape(7, 2, 2)
    src = _Source(frames, available=7, chunk=2, fps=3)
    # The real LiveSource contract returns the dataclass StackInfo, not a
    # SimpleNamespace used by the pure metadata tests above.
    from imswitch.improcess.reconstructors.base import StackInfo
    src.info = StackInfo(**vars(src.info))
    src.complete = True

    class _Session:
        def __init__(self):
            self.ranges = []
            self.closed = False
        def begin(self, init, _params):
            self.ranges.append((0, len(init.data), init.data.copy()))
        def push(self, frames, start, end):
            self.ranges.append((start, end, frames.copy()))
        def live_plane(self):
            return None
        def result(self):
            return np.concatenate([x[2] for x in self.ranges])
        def finish(self):
            return self.result()
        def close(self):
            self.closed = True

    class _Recon(_ScanReconstructor):
        def __init__(self):
            self.session = _Session()
        def make_session(self):
            return self.session

    recon = _Recon()
    runner = QtIncrementalRunner(recon, src, {}, name="run", poll_ms=2)
    results = []
    runner.sigResult.connect(results.append)
    with qtbot.waitSignal(runner.sigFinished, timeout=10000) as done:
        runner.start()
    assert done.args == [True]
    assert len(results) == 1
    np.testing.assert_array_equal(results[0], frames)
    assert recon.session.closed and src.closed


def test_two_thread_cancel_discards_partial_and_releases_resources(qtbot):
    from imswitch.improcess.execution.runner import QtIncrementalRunner
    from imswitch.improcess.reconstructors.base import StackInfo

    frames = np.ones((3, 2, 2), dtype=np.uint16)
    src = _Source(frames, available=3, fps=3)
    src.info = StackInfo(**vars(src.info))
    src.complete = False  # a growing source with no terminal marker yet

    class _Session:
        closed = False
        def begin(self, _init, _params):
            return None
        def live_plane(self):
            return None
        def result(self):
            return np.ones((1, 2, 2), dtype=np.uint16)
        def close(self):
            self.closed = True

    class _Recon(_ScanReconstructor):
        def __init__(self):
            self.session = _Session()
        def make_session(self):
            return self.session

    recon = _Recon()
    runner = QtIncrementalRunner(recon, src, {}, name="cancel", poll_ms=2)
    results = []
    runner.sigResult.connect(results.append)
    with qtbot.waitSignal(runner.sigPreview, timeout=10000):
        runner.start()
    with qtbot.waitSignal(runner.sigFinished, timeout=10000) as done:
        runner.cancel()  # MUST NOT call QThread.wait() on the GUI thread
    assert done.args == [False]
    assert not results and recon.session.closed and src.closed
