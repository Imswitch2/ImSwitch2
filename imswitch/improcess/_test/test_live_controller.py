"""Tests for LiveReconstructionController."""

import time

import numpy as np
from types import SimpleNamespace
from qtpy import QtCore, QtWidgets

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
        # Mirrors MonalisaLiveSession: the run's name comes in on StreamInit and
        # is carried by every result, which is what names the viewer entry.
        self.name = "stream"

    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        self.name = init_obj.name
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
        return _Result(self.name, self.buffer.copy(), ["T", "Y", "X"])


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


class _FinishFailsSession(_StreamingSession):
    def finish(self) -> ProcessingResult:
        raise RuntimeError("finish failed")


class _FinishFailsStreamingRecon(_StreamingRecon):
    def make_session(self) -> StreamingSession:
        return _FinishFailsSession()


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


class _FakeThread:
    def __init__(self):
        self.running = True
        self.request_interruption_called = False
        self.quit_called = False
        self.wait_called_with = None

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.request_interruption_called = True

    def quit(self):
        self.quit_called = True

    def wait(self, timeout_ms):
        self.wait_called_with = timeout_ms
        self.running = False
        return True


class _Closeable:
    def __init__(self, events=None, label="close"):
        self.closed = False
        self._events = events
        self._label = label

    def close(self):
        self.closed = True
        if self._events is not None:
            self._events.append(self._label)


def _wait_for_finished(controller, timeout_ms=3000):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    loop = QtCore.QEventLoop()
    finished = []

    def on_finished():
        finished.append(True)
        loop.quit()

    controller.sigFinished.connect(on_finished)
    QtCore.QTimer.singleShot(timeout_ms, loop.quit)
    if hasattr(loop, "exec"):
        loop.exec()
    else:
        loop.exec_()
    controller.sigFinished.disconnect(on_finished)
    return bool(finished)


def test_controller_streaming_path():
    """start() takes the streaming branch for a StreamingReconstructor and
    builds the process worker lazily once the stack shape is known."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    stack = np.arange(6 * 4 * 5, dtype=np.float32).reshape(6, 4, 5)
    source = _TestSource(stack, chunk_size=2)

    controller = LiveReconstructionController(CommunicationChannel())

    assert controller.start(_StreamingRecon(), source, {}, source_arg="synthetic") is True
    assert controller._is_streaming is True
    assert controller._stream_worker is not None
    assert controller._process_worker is None  # not built until _on_init_stack_ready

    assert _wait_for_finished(controller)
    assert controller._running is False
    assert controller._stream_thread is None
    assert controller._process_thread is None
    assert controller._ring is None


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


def test_controller_stop_quits_thread_event_loops():
    """Stopping a run must quit Qt event-loop threads, not only interrupt them."""
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    stream_thread = _FakeThread()
    process_thread = _FakeThread()
    stream_worker = SimpleNamespace(stop=lambda: setattr(stream_worker, "stopped", True))
    stream_worker.stopped = False
    source = _Closeable()

    controller._running = True
    controller._stream_thread = stream_thread
    controller._process_thread = process_thread
    controller._stream_worker = stream_worker
    controller._process_worker = object()
    controller._source = source

    controller.stop(graceful=False)      # teardown mechanics, not the drain path

    assert stream_worker.stopped is True
    assert stream_thread.request_interruption_called is True
    assert stream_thread.quit_called is True
    assert stream_thread.wait_called_with == 2000
    assert process_thread.request_interruption_called is True
    assert process_thread.quit_called is True
    assert process_thread.wait_called_with == 2000
    assert source.closed is True
    assert controller._running is False
    assert controller._stream_thread is None
    assert controller._process_thread is None
    assert controller._source is None


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


def test_stack_finished_emits_queue_signal_after_cleanup():
    """The live queue should advance only after the previous run is torn down."""
    events = []
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    controller.sigFinished.connect(lambda: events.append("finished"))
    # A streaming run publishes its final snapshot on the live path, so it
    # updates the entry it already owns instead of appending a second one.
    comm_channel.sigLiveResultUpdated.connect(lambda *_a: events.append("result"))
    comm_channel.sigResultProduced.connect(
        lambda *_a: events.append("UNEXPECTED_sigResultProduced")
    )

    controller._running = True
    controller._is_streaming = True
    controller._session = _Closeable(events, "session_close")
    controller._source = _Closeable(events, "source_close")

    result = _Result("stream", np.zeros((1, 2, 3), dtype=np.float32), ["T", "Y", "X"])
    controller._on_stack_finished(result)

    assert events == ["result", "session_close", "source_close", "finished"]
    assert controller._running is False
    assert controller._session is None
    assert controller._source is None


def test_controller_processes_two_streaming_sources_sequentially():
    """Sequential live files should not leave the previous QThreads running."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    results = []
    comm_channel.sigLiveResultUpdated.connect(results.append)

    for offset in (0, 1000):
        stack = (
            np.arange(offset, offset + 4 * 3 * 3, dtype=np.float32)
            .reshape(4, 3, 3)
        )
        source = _TestSource(stack, chunk_size=2)

        results.clear()
        assert controller.start(_StreamingRecon(), source, {}, source_arg="synthetic")
        assert _wait_for_finished(controller)
        # A run emits several live updates; what matters is that each run
        # produced results and tore its threads down before the next started.
        assert results, f"run with offset {offset} produced no result"
        assert controller._running is False
        assert controller._stream_thread is None
        assert controller._process_thread is None


def test_run_name_names_the_result_so_each_job_gets_its_own_entry():
    """``start(name=...)`` flows to StreamInit and out on every result.

    ``ReconstructionViewController`` creates a new viewer entry whenever an
    incoming live result's name differs from the selected one's, so a per-job
    name is what gives each timelapse folder its own data object.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    names = []
    comm_channel.sigLiveResultUpdated.connect(lambda r: names.append(r.name))

    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)  # source.name == "test-source"

    assert controller.start(
        _StreamingRecon(), source, {}, source_arg="synthetic", name="timelapse_07"
    )
    assert _wait_for_finished(controller)

    assert names, "no live results were emitted"
    # The explicit run name wins over the source's own name.
    assert set(names) == {"timelapse_07"}


def test_run_name_falls_back_to_the_source_name():
    """Without an explicit name, the source's ``name`` still names the run."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    names = []
    comm_channel.sigLiveResultUpdated.connect(lambda r: names.append(r.name))

    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)

    assert controller.start(_StreamingRecon(), source, {}, source_arg="synthetic")
    assert _wait_for_finished(controller)

    assert set(names) == {"test-source"}


def test_controller_clears_streaming_run_when_session_finish_fails():
    """A finish() exception should advance the live queue without a result."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=4)
    comm_channel = CommunicationChannel()
    controller = LiveReconstructionController(comm_channel)
    results = []
    comm_channel.sigResultProduced.connect(lambda result, _title: results.append(result))

    assert controller.start(_FinishFailsStreamingRecon(), source, {}, source_arg="synthetic")
    assert _wait_for_finished(controller)

    assert results == []
    assert controller._running is False
    assert controller._stream_thread is None
    assert controller._process_thread is None
    assert controller._source is None


# Startup first-stack collection (open-retry + buffer until frames_per_stack)
# now lives on the stream-worker thread; see test_live_workers.py
# (test_stream_worker_startup_*).


def test_controller_effective_stall_timeout_with_idle_source(qtbot, tmpdir, monkeypatch):
    """Source with idles_between_stacks=True + absent config → timeout disabled.
    Explicit config value applies even to idle sources."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    comm = CommunicationChannel()

    # Patch to return custom config
    def mock_load_config(logger):
        return {}  # absent key → default 300.0, was_explicit=False

    monkeypatch.setattr(
        "imswitch.improcess.controller.LiveReconstructionController.load_processing_config",
        mock_load_config,
    )

    controller = LiveReconstructionController(comm)
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    source.idles_between_stacks = True  # simulate lapse source

    controller._source = source
    assert controller._effective_stall_timeout() is None  # disabled for idle source

    # Explicit config should apply even to idle sources
    def mock_load_config_explicit(logger):
        return {"liveStallTimeoutS": 120}

    monkeypatch.setattr(
        "imswitch.improcess.controller.LiveReconstructionController.load_processing_config",
        mock_load_config_explicit,
    )
    controller2 = LiveReconstructionController(comm)
    controller2._source = source
    assert controller2._effective_stall_timeout() == 120.0  # explicit value applies


def test_controller_effective_stall_timeout_without_idle_source(qtbot, tmpdir, monkeypatch):
    """Source with idles_between_stacks=False (or absent) uses default config timeout."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    comm = CommunicationChannel()

    def mock_load_config(logger):
        return {}  # absent key → default 300.0

    monkeypatch.setattr(
        "imswitch.improcess.controller.LiveReconstructionController.load_processing_config",
        mock_load_config,
    )

    controller = LiveReconstructionController(comm)
    stack = np.arange(4 * 3 * 3, dtype=np.float32).reshape(4, 3, 3)
    source = _TestSource(stack, chunk_size=2)
    # source.idles_between_stacks not set → defaults to False

    controller._source = source
    assert controller._effective_stall_timeout() == 300.0  # default applies


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

# --- graceful stop ---------------------------------------------------------
#
# Reconstruction is slower than streaming, so the stream worker normally sits
# blocked on the inter-stack barrier having already dispatched a whole
# timepoint: those frames are queued on the process thread. Killing the thread
# there leaves the viewer's last timepoint half reconstructed, so stop() drains
# the queue first.


class _SlowSession(_StreamingSession):
    """Processes one frame at a time, slowly, recording what it got."""

    def __init__(self, delay=0.03):
        super().__init__()
        self.delay = delay
        self.pushed = []

    def push(self, chunk, start, end):
        time.sleep(self.delay)
        super().push(chunk, start, end)
        self.pushed.append(start)


class _SlowRecon(_StreamingRecon):
    def __init__(self):
        self.session = _SlowSession()

    def make_session(self):
        return self.session


class _NeverEndingSource(_TestSource):
    """Two stacks of frames, and never reports completion (a live timelapse)."""

    def __init__(self, stack, chunk_size, frames_per_stack):
        super().__init__(stack, chunk_size)
        self.frames_per_stack = frames_per_stack

    def open(self, path_or_handle):
        return StackInfo(
            frame_shape=self.stack.shape[-2:],
            dtype=self.stack.dtype,
            expected_frames=self.stack.shape[0],
            frames_per_stack=self.frames_per_stack,
            detector_name="CAM",
        )

    def is_complete(self):
        return False


# Anchored module-side on purpose: a QApplication created into a *local* dies
# when that function returns, taking every QObject with it ("wrapped C/C++
# object has been deleted" on the next touch).
_APP = None


def _qapp():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _spin_until(predicate, timeout_ms=8000):
    app = _qapp()
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _start_slow_run(fps=10):
    _qapp()
    stack = np.arange(2 * fps * 3 * 3, dtype=np.float32).reshape(2 * fps, 3, 3)
    source = _NeverEndingSource(stack, chunk_size=fps, frames_per_stack=fps)
    recon = _SlowRecon()
    controller = LiveReconstructionController(CommunicationChannel())

    assert controller.start(recon, source, {}, source_arg="synthetic")
    # Wait until the second stack is under way but nowhere near finished.
    assert _spin_until(lambda: len(recon.session.pushed) >= 2)
    return controller, recon, fps


def test_graceful_stop_finishes_the_timepoint_in_flight():
    """Every frame already queued must be processed before teardown."""
    controller, recon, fps = _start_slow_run()
    assert len(recon.session.pushed) < fps + 1      # genuinely mid-timepoint

    controller.stop()                                # graceful by default
    assert _spin_until(lambda: controller._running is False), "stop never completed"

    # begin() consumed stack 0 as one push; stack 1 arrives frame by frame.
    assert recon.session.pushed == [0] + list(range(fps, 2 * fps))


def test_graceful_stop_does_not_block_the_calling_thread():
    """Teardown is asynchronous -- the UI must not freeze for a whole timepoint."""
    controller, recon, fps = _start_slow_run()

    t0 = time.monotonic()
    controller.stop()
    elapsed = time.monotonic() - t0

    remaining = (fps - len(recon.session.pushed)) * recon.session.delay
    assert elapsed < max(0.05, remaining / 2), f"stop() blocked for {elapsed:.3f}s"
    assert _spin_until(lambda: controller._running is False)


def test_immediate_stop_tears_down_without_draining():
    """graceful=False is still available for when a new run cannot wait."""
    controller, recon, _ = _start_slow_run()

    controller.stop(graceful=False)

    assert controller._running is False
    assert controller._stream_thread is None

