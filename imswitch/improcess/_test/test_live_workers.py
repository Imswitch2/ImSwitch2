"""Tests for LiveStreamWorker and LiveProcessWorker."""

import threading

import numpy as np

from imswitch.improcess.live import LiveProcessWorker, LiveSource, LiveStreamWorker
from imswitch.improcess.live.buffer import StackRing
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    Chunk,
    StackInfo,
    StreamInit,
    StreamPlan,
    StreamingSession,
)


class _Result(ProcessingResult):
    def save(self, path, fmt):
        return None


class _TestSession(StreamingSession):
    def __init__(self):
        self.buffer = None
        self.pushed_chunks = []

    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        frames = init_obj.stack_info.expected_frames if init_obj.stack_info else 10
        self.buffer = np.zeros((frames, *init_obj.data.shape[-2:]), dtype=np.float32)
        self.push(init_obj.data, 0, init_obj.data.shape[0])
        return StreamPlan(
            out_shape=self.buffer.shape,
            axis_labels=["T", "Y", "X"],
            view_modes=[ViewMode("Standard", (0, 1, 2))],
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        self.buffer[start:end] = chunk
        self.pushed_chunks.append((start, end))

    def result(self) -> ProcessingResult:
        return _Result("test", self.buffer.copy(), ["T", "Y", "X"])

    def finish(self) -> ProcessingResult:
        return self.result()


class _TestSource(LiveSource):
    def __init__(self, stack: np.ndarray, chunk_size: int):
        self.stack = stack
        self.chunk_size = chunk_size
        self.cursor = 0
        self.closed = False

    def open(self, path_or_handle) -> StackInfo:
        return StackInfo(
            frame_shape=self.stack.shape[-2:],
            dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self.stack.shape[0],
        )

    def poll(self) -> list[Chunk]:
        if self.cursor >= self.stack.shape[0]:
            return []
        start = self.cursor
        end = min(start + self.chunk_size, self.stack.shape[0])
        self.cursor = end
        return [Chunk(self.stack[start:end], start, end)]

    def is_complete(self) -> bool:
        return self.cursor >= self.stack.shape[0]

    def close(self) -> None:
        self.closed = True


def test_live_stream_worker_logic():
    """LiveStreamWorker polls the source and emits chunks and completion."""
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _TestSource(stack, chunk_size=2)
    source.open("test")

    worker = LiveStreamWorker(source, poll_interval_ms=1)

    chunks_received = []
    complete_called = [False]

    def on_chunk(chunk):
        chunks_received.append(chunk)

    def on_complete():
        complete_called[0] = True

    worker.sigChunkReady.connect(on_chunk)
    worker.sigStackComplete.connect(on_complete)

    worker.run()

    assert len(chunks_received) == 3
    assert chunks_received[0].start == 0
    assert chunks_received[0].end == 2
    assert chunks_received[-1].end == 6
    assert complete_called[0] is True


def test_live_stream_worker_stops_cleanly():
    """LiveStreamWorker stops when stop() is called."""
    stack = np.ones((20, 4, 5), dtype=np.float32)
    source = _TestSource(stack, chunk_size=5)
    source.open("test")

    worker = LiveStreamWorker(source, poll_interval_ms=1)

    chunks_received = []

    def on_chunk(chunk):
        chunks_received.append(chunk)
        if len(chunks_received) >= 2:
            worker.stop()

    worker.sigChunkReady.connect(on_chunk)

    worker.run()

    assert len(chunks_received) == 2
    assert worker._running is False


def _init_obj(init_frames: int, expected_frames: int):
    return StreamInit(
        name="test",
        dataset_name="CAM",
        data=np.zeros((init_frames, 4, 5), dtype=np.float32),
        stack_info=StackInfo(
            frame_shape=(4, 5),
            dtype=np.dtype(np.float32),
            expected_frames=expected_frames,
        ),
    )


# --- LiveProcessWorker: ring path ------------------------------------------

def test_process_worker_reads_ring_and_pushes_per_frame():
    """process_chunk reads a frame from the ring and pushes it as [idx, idx+1)."""
    session = _TestSession()
    session.begin(_init_obj(init_frames=2, expected_frames=6), {})  # push (0, 2)

    ring = StackRing(2, (4, 5), np.float32)  # frames_per_stack = 2
    barrier = threading.Event()
    worker = LiveProcessWorker(session, ring, barrier, frames_per_stack=2,
                               viewer_update_interval_s=0.0)  # emit every frame

    ring.write(2, np.ones((4, 5), np.float32))       # stack 1, frame 0
    worker.process_chunk(2)
    ring.write(3, np.ones((4, 5), np.float32) * 2)   # stack 1, frame 1 (last)
    worker.process_chunk(3)

    assert session.pushed_chunks == [(0, 2), (2, 3), (3, 4)]
    np.testing.assert_array_equal(session.buffer[2], 1.0)
    np.testing.assert_array_equal(session.buffer[3], 2.0)
    assert barrier.is_set()  # (3 + 1) % 2 == 0 -> last frame of the stack


def test_process_worker_result_emit_is_throttled_but_always_fires_at_stack_end():
    session = _TestSession()
    session.begin(_init_obj(init_frames=2, expected_frames=30), {})

    ring = StackRing(10, (4, 5), np.float32)
    # Large interval -> mid-stack frames are throttled out; only stack ends emit.
    worker = LiveProcessWorker(session, ring, threading.Event(),
                               frames_per_stack=10, viewer_update_interval_s=1e6)
    updates = []
    worker.sigResultUpdated.connect(updates.append)

    for i in range(2, 30):  # frames 2..29 -> stack ends at 9 and 19 and 29
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert len(updates) == 3  # index 9, 19, 29


def test_process_worker_result_emits_every_frame_when_interval_zero():
    session = _TestSession()
    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    ring = StackRing(10, (4, 5), np.float32)
    worker = LiveProcessWorker(session, ring, threading.Event(),
                               frames_per_stack=10, viewer_update_interval_s=0.0)
    updates = []
    worker.sigResultUpdated.connect(updates.append)

    for i in range(2, 11):  # 9 frames
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert len(updates) == 9


def test_process_worker_releases_barrier_even_when_push_raises():
    """A bad frame is logged, not raised -- and the barrier is still set."""

    class _BadPush(_TestSession):
        def push(self, chunk, start, end):
            raise RuntimeError("bad frame")

    session = _BadPush()
    ring = StackRing(2, (4, 5), np.float32)
    barrier = threading.Event()
    worker = LiveProcessWorker(session, ring, barrier, frames_per_stack=2)

    ring.write(1, np.zeros((4, 5), np.float32))
    worker.process_chunk(1)  # (1 + 1) % 2 == 0 -> last frame

    assert barrier.is_set()


def test_live_process_worker_finalize_failure_emits_failed_signal():
    """A finish() exception must notify the controller instead of wedging."""

    class _FinishFailsSession(_TestSession):
        def finish(self) -> ProcessingResult:
            raise RuntimeError("finish failed")

    session = _FinishFailsSession()
    session.begin(_init_obj(init_frames=1, expected_frames=1), {})
    worker = LiveProcessWorker(
        session, StackRing(1, (4, 5), np.float32), threading.Event(), frames_per_stack=1
    )

    failed = []
    finished = []
    worker.sigFailed.connect(lambda message: failed.append(message))
    worker.sigStackFinished.connect(lambda result: finished.append(result))

    worker.finalize()

    assert finished == []
    assert failed == ["finish failed"]


# --- LiveStreamWorker: ring path ------------------------------------------

def _idle_worker(n_frames=1):
    src = _TestSource(np.zeros((n_frames, 4, 5), np.float32), chunk_size=1)
    w = LiveStreamWorker(src, poll_interval_ms=1)
    w._running = True
    return w


def test_dispatch_chunks_ring_mode_unrolls_frames_to_the_ring():
    worker = _idle_worker()
    worker._frames_per_stack = 4
    ring = StackRing(4, (4, 5), np.float32)
    worker._ring = ring
    worker._stack_consumed.set()  # so the boundary wait returns immediately

    emitted = []
    worker.sigFramesReady.connect(emitted.append)

    stack = np.arange(4 * 4 * 5, dtype=np.float32).reshape(4, 4, 5)
    assert worker._dispatch_chunks([Chunk(stack, 4, 8)]) is True  # stack 1

    assert emitted == [4, 5, 6, 7]
    np.testing.assert_array_equal(ring.read(4)[0], stack[0])
    np.testing.assert_array_equal(ring.read(7)[0], stack[3])
    assert not worker._stack_consumed.is_set()  # the boundary wait cleared it


def test_dispatch_chunks_chunk_mode_emits_whole_chunks():
    worker = _idle_worker()  # no ring -> chunk mode
    got = []
    worker.sigChunkReady.connect(got.append)

    c = Chunk(np.zeros((2, 4, 5), np.float32), 0, 2)
    assert worker._dispatch_chunks([c]) is True
    assert got == [c]


def test_await_stack_consumed_true_when_preset_and_clears_event():
    worker = _idle_worker()
    worker._stack_consumed.set()
    assert worker._await_stack_consumed() is True
    assert not worker._stack_consumed.is_set()


def test_await_stack_consumed_times_out_and_signals_stall():
    worker = _idle_worker()
    worker._stall_timeout_s = 0.05
    stalled, complete = [], []
    worker.sigStalled.connect(stalled.append)
    worker.sigStackComplete.connect(lambda: complete.append(True))

    assert worker._await_stack_consumed() is False
    assert stalled and complete


def test_await_stack_consumed_false_when_stopped():
    worker = _idle_worker()  # stall_timeout_s is None -> would block forever
    worker._running = False   # simulate stop()
    assert worker._await_stack_consumed() is False


class _RetryOpenSource(_TestSource):
    """Source whose open() fails a few times before succeeding (store created
    before its data array exists), with a configurable frames_per_stack."""

    def __init__(self, stack, chunk_size, fail_opens=0, frames_per_stack=None):
        super().__init__(stack, chunk_size)
        self._fail_opens = fail_opens
        self._fps = frames_per_stack if frames_per_stack is not None else stack.shape[0]
        self.open_attempts = 0

    def open(self, path_or_handle) -> StackInfo:
        self.open_attempts += 1
        if self.open_attempts <= self._fail_opens:
            raise RuntimeError("data array not ready yet")
        return StackInfo(
            frame_shape=self.stack.shape[-2:],
            dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self._fps,
        )


def test_stream_worker_startup_retries_open_then_collects_first_stack():
    """do_open startup retries open until the store is readable, collects the
    first stack, then waits for begin (resume) before streaming the rest."""
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _RetryOpenSource(stack, chunk_size=2, fail_opens=2, frames_per_stack=4)
    worker = LiveStreamWorker(source, source_arg="x", do_open=True,
                              poll_interval_ms=1, open_max_attempts=10)

    opened, init_data, chunks = [], [], []
    worker.sigOpened.connect(lambda si: opened.append(si))
    worker.sigInitStackReady.connect(lambda d: (init_data.append(d), worker.resume()))
    worker.sigChunkReady.connect(lambda c: chunks.append(c))

    worker.run()

    assert source.open_attempts == 3  # two failures + one success
    assert len(opened) == 1 and opened[0].frames_per_stack == 4
    # First stack == first frames_per_stack frames; the remainder streams after resume.
    assert init_data[0].shape[0] == 4
    np.testing.assert_array_equal(init_data[0], stack[:4])
    streamed = np.concatenate([c.data for c in chunks], axis=0) if chunks else np.empty((0,))
    np.testing.assert_array_equal(streamed, stack[4:])


def test_stream_worker_startup_splits_oversized_first_chunk():
    """A source may emit more than one logical stack in its first poll."""
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _RetryOpenSource(stack, chunk_size=6, frames_per_stack=4)
    worker = LiveStreamWorker(source, source_arg="x", do_open=True,
                              poll_interval_ms=1, open_max_attempts=10)

    init_data, chunks = [], []
    worker.sigInitStackReady.connect(lambda d: (init_data.append(d), worker.resume()))
    worker.sigChunkReady.connect(lambda c: chunks.append(c))

    worker.run()

    assert init_data[0].shape[0] == 4
    np.testing.assert_array_equal(init_data[0], stack[:4])
    assert len(chunks) == 1
    assert chunks[0].start == 4
    assert chunks[0].end == 6
    np.testing.assert_array_equal(chunks[0].data, stack[4:])


def test_stream_worker_startup_fails_when_store_never_readable():
    """If the store never becomes readable, startup gives up after bounded
    attempts and emits sigFailed (so a queue driver can advance)."""
    stack = np.zeros((4, 4, 5), dtype=np.float32)
    source = _RetryOpenSource(stack, chunk_size=2, fail_opens=999)
    worker = LiveStreamWorker(source, source_arg="x", do_open=True,
                              poll_interval_ms=1, open_max_attempts=3)

    opened, failed = [], []
    worker.sigOpened.connect(lambda si: opened.append(si))
    worker.sigFailed.connect(lambda msg: failed.append(msg))

    worker.run()

    assert source.open_attempts == 3
    assert opened == []
    assert len(failed) == 1



def test_stream_worker_retries_transient_poll_errors():
    """A transient poll error (e.g. recorder still holds the store) is retried,
    not fatal — the stream recovers and finishes."""
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)

    class _FlakySource(_TestSource):
        def __init__(self, stack, chunk_size, fail_polls):
            super().__init__(stack, chunk_size)
            self._fail_polls = fail_polls
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls <= self._fail_polls:
                raise PermissionError("store locked by recorder")
            return super().poll()

    source = _FlakySource(stack, chunk_size=2, fail_polls=2)
    source.open("x")
    worker = LiveStreamWorker(source, poll_interval_ms=1, max_poll_error_retries=10)

    chunks, complete = [], [False]
    worker.sigChunkReady.connect(lambda c: chunks.append(c))
    worker.sigStackComplete.connect(lambda: complete.__setitem__(0, True))
    worker.run()

    assert source.poll_calls > 2  # retried past the failures
    assert sum(c.data.shape[0] for c in chunks) == 4  # all frames streamed
    assert complete[0] is True


def test_stream_worker_gives_up_after_persistent_poll_errors():
    """A persistently unreadable source stops the stream (bounded) via sigFailed."""
    class _DeadSource(_TestSource):
        def poll(self):
            raise OSError("gone")

    source = _DeadSource(np.zeros((2, 2, 2), dtype=np.float32), chunk_size=1)
    source.open("x")
    worker = LiveStreamWorker(source, poll_interval_ms=1, max_poll_error_retries=3)
    failed = []
    worker.sigFailed.connect(lambda msg: failed.append(msg))
    worker.run()
    assert len(failed) == 1


def test_poll_loop_stall_detection():
    """Worker detects stalls (no frames, no completion marker) and finalizes with partial data."""
    class _StallSource(_TestSource):
        def __init__(self, stack, chunk_size, chunks_before_stall):
            super().__init__(stack, chunk_size)
            self._chunks_before_stall = chunks_before_stall
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.cursor >= self._chunks_before_stall * self.chunk_size:
                return []  # stall forever
            return super().poll()

        def is_complete(self):
            return False  # never marks complete

    stack = np.arange(10 * 4 * 5, dtype=np.float32).reshape(10, 4, 5)
    source = _StallSource(stack, chunk_size=3, chunks_before_stall=1)
    source.open("test")

    worker = LiveStreamWorker(source, poll_interval_ms=10, stall_timeout_s=0.05)
    chunks, stalled, complete = [], [], [False]
    worker.sigChunkReady.connect(lambda c: chunks.append(c))
    worker.sigStalled.connect(lambda s: stalled.append(s))
    worker.sigStackComplete.connect(lambda: complete.__setitem__(0, True))

    worker.run()

    assert len(chunks) == 1  # only got one chunk before stall
    assert chunks[0].data.shape[0] == 3
    assert len(stalled) == 1
    assert stalled[0] >= 0.05  # waited at least timeout duration
    assert complete[0] is True  # finalized with partial data


def test_no_stall_when_complete():
    """Normal completion (source marks itself complete) should not trigger stall."""
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    source.open("test")

    worker = LiveStreamWorker(source, poll_interval_ms=1, stall_timeout_s=0.1)
    chunks, stalled, complete = [], [], [False]
    worker.sigChunkReady.connect(lambda c: chunks.append(c))
    worker.sigStalled.connect(lambda s: stalled.append(s))
    worker.sigStackComplete.connect(lambda: complete.__setitem__(0, True))

    worker.run()

    assert len(chunks) == 2
    assert stalled == []  # no stall signal
    assert complete[0] is True


def test_disabled_stall_timeout():
    """Worker with stall_timeout_s=None should poll indefinitely without stalling."""
    class _NeverCompleteSource(_TestSource):
        def __init__(self, stack, chunk_size, emit_chunks):
            super().__init__(stack, chunk_size)
            self._emit_chunks = emit_chunks

        def poll(self):
            if self.cursor < self._emit_chunks * self.chunk_size:
                return super().poll()
            return []  # stall after emitting specified chunks

        def is_complete(self):
            return False  # never complete

    stack = np.arange(10 * 4 * 5, dtype=np.float32).reshape(10, 4, 5)
    source = _NeverCompleteSource(stack, chunk_size=2, emit_chunks=2)
    source.open("test")

    worker = LiveStreamWorker(source, poll_interval_ms=5, stall_timeout_s=None)
    chunks, stalled = [], []
    worker.sigChunkReady.connect(lambda c: chunks.append(c))
    worker.sigStalled.connect(lambda s: stalled.append(s))

    # Drive a bounded number of iterations then stop
    def stop_after_chunks():
        if len(chunks) >= 2:
            import time
            time.sleep(0.05)  # give it time to stall if it would
            worker.stop()

    worker.sigChunkReady.connect(lambda _: stop_after_chunks())
    worker.run()

    assert len(chunks) == 2
    assert stalled == []  # no stall with timeout disabled


def test_startup_stall_detection():
    """do_open startup that stalls before collecting first stack emits sigFailed."""
    class _StartupStallSource(_TestSource):
        def poll(self):
            return []  # never yields frames

        def is_complete(self):
            return False

    stack = np.zeros((4, 3, 3), dtype=np.float32)
    source = _StartupStallSource(stack, chunk_size=2)

    worker = LiveStreamWorker(
        source,
        source_arg="test",
        do_open=True,
        poll_interval_ms=5,
        stall_timeout_s=0.05,
    )
    opened, init_data, failed = [], [], []
    worker.sigOpened.connect(lambda si: opened.append(si))
    worker.sigInitStackReady.connect(lambda d: init_data.append(d))
    worker.sigFailed.connect(lambda msg: failed.append(msg))

    worker.run()

    assert len(opened) == 1  # source opened
    assert init_data == []  # no init stack collected
    assert len(failed) == 1
    assert "stall" in failed[0].lower()


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


# --- incremental (per-timepoint) viewer updates ----------------------------
#
# The first viewer update is a whole ProcessingResult -- that is the buffer the
# viewer keeps. Every update after it is a single timepoint plane written into
# that buffer, so the per-refresh cost stays flat instead of growing with the
# timelapse. Sessions without live_plane() keep receiving whole results.


class _PlaneSession(_TestSession):
    """A session that supports incremental plane updates."""

    def live_plane(self):
        # One "timepoint" per frame here; keep all axes with T length 1.
        index = max(0, len(self.pushed_chunks) - 1)
        return index, self.buffer[index:index + 1].copy()


def _worker(session, frames_per_stack=10, interval=0.0):
    ring = StackRing(frames_per_stack, (4, 5), np.float32)
    return LiveProcessWorker(session, ring, threading.Event(),
                             frames_per_stack=frames_per_stack,
                             viewer_update_interval_s=interval), ring


def test_first_update_is_a_whole_result_then_planes():
    session = _PlaneSession()
    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    worker, ring = _worker(session)

    results, planes = [], []
    worker.sigResultUpdated.connect(results.append)
    worker.sigTimepointUpdated.connect(lambda i, p: planes.append((i, p)))

    for i in range(2, 8):
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert len(results) == 1, "only the first update should be a whole result"
    assert len(planes) == 5, "every later update should be a plane"


def test_planes_are_copies_not_views_of_session_state():
    """The viewer must never be handed memory the process thread keeps writing."""
    session = _PlaneSession()
    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    worker, ring = _worker(session)

    planes = []
    worker.sigTimepointUpdated.connect(lambda i, p: planes.append(p))
    for i in range(2, 6):
        ring.write(i, np.ones((4, 5), np.float32))
        worker.process_chunk(i)

    assert planes
    for plane in planes:
        assert not np.shares_memory(plane, session.buffer)


def test_session_without_live_plane_keeps_getting_whole_results():
    """The base StreamingSession returns None -- callers must fall back."""
    session = _TestSession()          # no live_plane override
    assert session.live_plane() is None

    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    worker, ring = _worker(session)

    results, planes = [], []
    worker.sigResultUpdated.connect(results.append)
    worker.sigTimepointUpdated.connect(lambda i, p: planes.append((i, p)))

    for i in range(2, 8):
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert planes == []
    assert len(results) == 6


def test_plane_updates_still_honour_the_viewer_throttle():
    session = _PlaneSession()
    session.begin(_init_obj(init_frames=2, expected_frames=30), {})
    worker, ring = _worker(session, interval=1e6)   # only stack ends emit

    updates = []
    worker.sigResultUpdated.connect(lambda r: updates.append("result"))
    worker.sigTimepointUpdated.connect(lambda i, p: updates.append("plane"))

    for i in range(2, 30):                          # stack ends at 9, 19, 29
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert updates == ["result", "plane", "plane"]


def test_publishing_updates_logs_no_errors():
    """process_chunk swallows exceptions -- so assert none were swallowed.

    Regression guard: a stray reference inside _publish_update once raised on
    every whole-result publish. The emission still happened first, so tests
    that only counted signals stayed green while the log filled with
    "Error processing frame N".
    """
    from unittest.mock import MagicMock

    session = _PlaneSession()
    session.begin(_init_obj(init_frames=2, expected_frames=30), {})
    worker, ring = _worker(session, interval=0.0)
    worker._logger = MagicMock()

    for i in range(2, 25):
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)

    assert worker._logger.error.call_args_list == []


def test_barrier_is_released_at_every_stack_end():
    """Removing the duplicated release must not cost the real one."""
    session = _PlaneSession()
    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    ring = StackRing(10, (4, 5), np.float32)
    barrier = threading.Event()
    worker = LiveProcessWorker(session, ring, barrier, frames_per_stack=10,
                               viewer_update_interval_s=0.0)

    for i in range(2, 9):
        ring.write(i, np.zeros((4, 5), np.float32))
        worker.process_chunk(i)
    assert not barrier.is_set()          # mid-stack

    ring.write(9, np.zeros((4, 5), np.float32))
    worker.process_chunk(9)              # (9 + 1) % 10 == 0 -> stack end
    assert barrier.is_set()


def test_barrier_is_released_even_when_publishing_raises():
    """A failure inside the update path must not wedge the stream worker."""
    class _BadPlane(_PlaneSession):
        def live_plane(self):
            raise RuntimeError("boom")

        def result(self):
            raise RuntimeError("boom")

    session = _BadPlane()
    session.begin(_init_obj(init_frames=2, expected_frames=20), {})
    ring = StackRing(10, (4, 5), np.float32)
    barrier = threading.Event()
    worker = LiveProcessWorker(session, ring, barrier, frames_per_stack=10,
                               viewer_update_interval_s=0.0)

    ring.write(9, np.zeros((4, 5), np.float32))
    worker.process_chunk(9)

    assert barrier.is_set()
