"""Tests for batch fallback path driving arbitrary (non-streaming) reconstructors."""

import numpy as np
import pytest

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.live import LiveSource
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import Chunk, Reconstructor, StackInfo
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
)


class _Result(ProcessingResult):
    """Minimal ProcessingResult for testing."""

    def save(self, path, fmt):
        return None


class _StubRecon(Reconstructor):
    """Stub non-streaming reconstructor for batch fallback tests."""

    name = "Stub Batch"
    id = "stub-batch"

    def __init__(self):
        self.process_calls = []

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        self.process_calls.append((data_obj, params))
        data = np.asarray(data_obj.data)
        return _Result("stub-batch", data.copy(), ["T", "Y", "X"])


class _TestSource(LiveSource):
    """Synthetic live source for testing."""

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


def test_batch_fallback_stub_reconstructor():
    """Batch fallback buffers chunks and calls process() once with full stack."""
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _TestSource(stack, chunk_size=2)
    reconstructor = _StubRecon()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {"test_param": 42}
    controller._running = False
    controller._is_streaming = False
    controller._buffer = []
    controller._stack_info = source.open(None)

    chunk1 = source.poll()[0]
    chunk2 = source.poll()[0]
    chunk3 = source.poll()[0]

    controller._on_chunk_for_buffer(chunk1)
    controller._on_chunk_for_buffer(chunk2)
    controller._on_chunk_for_buffer(chunk3)

    assert len(controller._buffer) == 3

    controller._on_batch_stack_complete()

    assert len(reconstructor.process_calls) == 1
    data_obj, params = reconstructor.process_calls[0]
    assert params == {"test_param": 42}
    assert data_obj.name == "test-source"
    assert data_obj.datasetName == "CAM"
    assert np.array_equal(data_obj.data, stack)


def test_batch_fallback_view_only_reconstructor():
    """Batch fallback works with real ViewOnlyReconstructor."""
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    reconstructor = ViewOnlyReconstructor()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {}
    controller._running = False
    controller._is_streaming = False
    controller._buffer = []
    controller._stack_info = source.open(None)

    results = []

    def capture_result(result):
        results.append(result)

    controller._on_stack_finished = capture_result

    chunk1 = source.poll()[0]
    chunk2 = source.poll()[0]

    controller._on_chunk_for_buffer(chunk1)
    controller._on_chunk_for_buffer(chunk2)

    controller._on_batch_stack_complete()

    assert len(results) == 1
    result = results[0]
    assert result.name == "test-source"
    assert result.axis_labels == ["C", "Y", "X"]
    assert np.array_equal(result.data, stack)


def test_batch_fallback_empty_buffer_logs_warning(caplog):
    """Batch fallback with empty buffer logs a warning and does not crash."""
    source = _TestSource(np.zeros((2, 3, 3), dtype=np.float32), chunk_size=1)
    reconstructor = _StubRecon()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {}
    controller._running = False
    controller._is_streaming = False
    controller._buffer = []
    controller._stack_info = source.open(None)

    controller._on_batch_stack_complete()

    assert len(reconstructor.process_calls) == 0
    assert "Stack complete but no chunks buffered" in caplog.text


def test_batch_fallback_concatenates_multiple_chunks():
    """Batch fallback correctly concatenates chunks of varying sizes."""
    stack = np.arange(10 * 2 * 2, dtype=np.float32).reshape(10, 2, 2)
    source = _TestSource(stack, chunk_size=3)
    reconstructor = _StubRecon()

    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)

    controller._reconstructor = reconstructor
    controller._source = source
    controller._params = {}
    controller._running = False
    controller._is_streaming = False
    controller._buffer = []
    controller._stack_info = source.open(None)

    for _ in range(4):
        chunks = source.poll()
        for chunk in chunks:
            controller._on_chunk_for_buffer(chunk)

    assert len(controller._buffer) == 4

    controller._on_batch_stack_complete()

    assert len(reconstructor.process_calls) == 1
    data_obj, _ = reconstructor.process_calls[0]
    assert data_obj.data.shape == (10, 2, 2)
    assert np.array_equal(data_obj.data, stack)


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
