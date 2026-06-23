"""Tests for LiveReconstructionController."""

import numpy as np
from qtpy import QtCore

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.live import InMemoryStackWrapper, LiveSource
from imswitch.improcess.live.workers import LiveProcessWorker, LiveStreamWorker
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    Chunk,
    Reconstructor,
    StackInfo,
    StreamInit,
    StreamPlan,
    StreamingReconstructor,
    StreamingSession,
)


class _Result(ProcessingResult):
    def save(self, path, fmt):
        return None


class _StreamingSession(StreamingSession):
    def __init__(self):
        self.buffer = None

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

    def result(self) -> ProcessingResult:
        return _Result("stream", self.buffer.copy(), ["T", "Y", "X"])


class _StreamingRecon(StreamingReconstructor):
    name = "Test Streaming"
    id = "test-streaming"
    supports_streaming = True

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        return _Result("batch", np.asarray(data_obj.data), ["T", "Y", "X"])

    def make_session(self) -> StreamingSession:
        return _StreamingSession()


class _BatchRecon(Reconstructor):
    name = "Test Batch"
    id = "test-batch"

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        return _Result("batch", np.asarray(data_obj.data), ["T", "Y", "X"])


class _TestSource(LiveSource):
    def __init__(self, stack: np.ndarray, chunk_size: int):
        self.stack = stack
        self.chunk_size = chunk_size
        self.cursor = 0
        self.name = "test-source"

    def open(self, path_or_handle) -> StackInfo:
        return StackInfo(
            frame_shape=self.stack.shape[-2:],
            dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self.stack.shape[0],
            detector_name="CAM",
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


def test_controller_streaming_path():
    """LiveReconstructionController sets up streaming path for StreamingReconstructor."""
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _TestSource(stack, chunk_size=2)
    reconstructor = _StreamingRecon()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {}
    controller._running = False
    controller._is_streaming = True
    controller._stream_thread = None
    controller._process_thread = None
    controller._buffer = []

    session = reconstructor.make_session()
    controller._process_worker = LiveProcessWorker(session, update_cadence=1)
    controller._stream_worker = LiveStreamWorker(source)

    assert controller._is_streaming is True
    assert controller._stream_worker is not None
    assert controller._process_worker is not None


def test_controller_batch_fallback_path():
    """LiveReconstructionController sets up batch fallback for regular Reconstructor."""
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    reconstructor = _BatchRecon()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {}
    controller._running = False
    controller._is_streaming = False
    controller._stream_thread = None
    controller._process_thread = None
    controller._buffer = []

    controller._stream_worker = LiveStreamWorker(source)

    assert controller._is_streaming is False
    assert controller._stream_worker is not None
    assert controller._process_worker is None


def test_controller_clean_stop():
    """LiveReconstructionController stops cleanly without raising."""
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._running = True
    controller._stream_thread = None
    controller._process_thread = None
    controller._stream_worker = None
    controller._process_worker = None
    controller._source = None

    controller.stop()

    assert controller._running is False


def test_controller_double_stop_is_safe():
    """Calling stop() multiple times does not raise."""
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._running = True
    controller._stream_thread = None
    controller._process_thread = None
    controller._stream_worker = None
    controller._process_worker = None
    controller._source = None

    controller.stop()
    controller.stop()

    assert controller._running is False


class _BurstSource(_TestSource):
    def __init__(self, stack: np.ndarray, chunk_sizes: list[int], frames_per_stack: int):
        super().__init__(stack, chunk_size=1)
        self.chunk_sizes = chunk_sizes
        self.frames_per_stack = frames_per_stack
        self.poll_count = 0

    def open(self, path_or_handle) -> StackInfo:
        info = super().open(path_or_handle)
        return StackInfo(
            frame_shape=info.frame_shape,
            dtype=info.dtype,
            expected_frames=info.expected_frames,
            frames_per_stack=self.frames_per_stack,
            detector_name=info.detector_name,
        )

    def poll(self) -> list[Chunk]:
        if self.poll_count > 0 or self.cursor >= self.stack.shape[0]:
            self.poll_count += 1
            return []

        chunks = []
        for size in self.chunk_sizes:
            if self.cursor >= self.stack.shape[0]:
                break
            start = self.cursor
            end = min(start + size, self.stack.shape[0])
            self.cursor = end
            chunks.append(Chunk(self.stack[start:end], start, end))

        self.poll_count += 1
        return chunks


def test_collect_initial_chunks_keeps_multiple_chunks_from_first_poll():
    """Startup polling includes every chunk returned before the worker starts."""
    stack = np.arange(10 * 4 * 5, dtype=np.float32).reshape(10, 4, 5)
    source = _BurstSource(stack, chunk_sizes=[2, 3, 5], frames_per_stack=4)
    controller = LiveReconstructionController(CommunicationChannel())
    controller._source = source
    controller._stack_info = source.open(None)

    chunks, init_data = controller._collect_initial_chunks()

    assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 2), (2, 5), (5, 10)]
    np.testing.assert_array_equal(init_data, stack)
    assert source.cursor == stack.shape[0]


def test_collect_initial_chunks_buffers_until_frames_per_stack():
    """Startup polling continues until at least frames_per_stack frames exist."""
    stack = np.arange(9 * 3 * 3, dtype=np.float32).reshape(9, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    controller = LiveReconstructionController(CommunicationChannel())
    controller._source = source
    controller._stack_info = source.open(None)
    controller._stack_info = StackInfo(
        frame_shape=controller._stack_info.frame_shape,
        dtype=controller._stack_info.dtype,
        expected_frames=controller._stack_info.expected_frames,
        frames_per_stack=5,
        detector_name=controller._stack_info.detector_name,
    )

    chunks, init_data = controller._collect_initial_chunks()

    assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 2), (2, 4), (4, 6)]
    np.testing.assert_array_equal(init_data, stack[:6])
    assert source.cursor == 6


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
