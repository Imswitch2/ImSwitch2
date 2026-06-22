"""Tests for LiveStreamWorker and LiveProcessWorker."""

import numpy as np
from qtpy import QtCore

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

    worker = LiveStreamWorker(source, poll_interval_ms=1, max_retries=1)

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

    worker = LiveStreamWorker(source, poll_interval_ms=1, max_retries=1)

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
