"""Tests for LiveStreamWorker and LiveProcessWorker."""

import numpy as np

from imswitch.improcess.live import LiveProcessWorker, LiveSource, LiveStreamWorker
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


def test_live_process_worker_pushes_chunks_to_session():
    """LiveProcessWorker processes chunks via session.push()."""
    session = _TestSession()
    init_obj = StreamInit(
        name="test",
        dataset_name="CAM",
        data=np.zeros((2, 4, 5), dtype=np.float32),
        stack_info=StackInfo(frame_shape=(4, 5), dtype=np.dtype(np.float32), expected_frames=6),
    )
    session.begin(init_obj, {})

    worker = LiveProcessWorker(session, update_cadence=2)

    results_received = []
    final_result = [None]

    def on_update(result):
        results_received.append(result)

    def on_finish(result):
        final_result[0] = result

    worker.sigResultUpdated.connect(on_update)
    worker.sigStackFinished.connect(on_finish)

    chunk1 = Chunk(np.ones((2, 4, 5), dtype=np.float32), 2, 4)
    chunk2 = Chunk(np.ones((2, 4, 5), dtype=np.float32) * 2, 4, 6)

    worker.processChunk(chunk1)
    worker.processChunk(chunk2)
    worker.finalize()

    assert len(session.pushed_chunks) == 3
    assert session.pushed_chunks[0] == (0, 2)
    assert session.pushed_chunks[1] == (2, 4)
    assert session.pushed_chunks[2] == (4, 6)

    assert len(results_received) == 1
    assert final_result[0] is not None
    np.testing.assert_array_equal(final_result[0].data[2:4], 1.0)
    np.testing.assert_array_equal(final_result[0].data[4:6], 2.0)


def test_live_process_worker_update_cadence():
    """LiveProcessWorker emits updates at the specified cadence."""
    session = _TestSession()
    init_obj = StreamInit(
        name="test",
        dataset_name="CAM",
        data=np.zeros((1, 4, 5), dtype=np.float32),
        stack_info=StackInfo(frame_shape=(4, 5), dtype=np.dtype(np.float32), expected_frames=10),
    )
    session.begin(init_obj, {})

    worker = LiveProcessWorker(session, update_cadence=3)
    updates = []

    def on_update(result):
        updates.append(result)

    worker.sigResultUpdated.connect(on_update)

    for i in range(1, 10):
        worker.processChunk(Chunk(np.zeros((1, 4, 5), dtype=np.float32), i, i + 1))

    assert len(updates) == 3


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
