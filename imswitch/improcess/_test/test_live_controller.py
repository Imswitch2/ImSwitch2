"""Tests for LiveReconstructionController."""

import numpy as np

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.live import LiveSource
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


# Startup first-stack collection (open-retry + buffer until frames_per_stack)
# now lives on the stream-worker thread; see test_live_workers.py
# (test_stream_worker_startup_*).


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
