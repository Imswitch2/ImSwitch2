"""Generic live reconstruction controller: drives LiveSource + any Reconstructor."""

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live import InMemoryStackWrapper
from imswitch.improcess.live.workers import LiveProcessWorker, LiveStreamWorker
from imswitch.improcess.model.processing_config import (
    live_stall_timeout_s,
    load_processing_config,
)
from imswitch.improcess.reconstructors.base import Chunk, StreamInit


class LiveReconstructionController(QtCore.QObject):
    """
    Controller for live reconstruction that works with any Reconstructor.
    
    For StreamingReconstructor: creates a session and runs the streaming path.
    For regular Reconstructor: buffers chunks and runs batch fallback.
    """

    sigFinished = QtCore.Signal()

    def __init__(self, comm_channel):
        super().__init__()
        self._commChannel = comm_channel
        self._logger = initLogger(self, tryInheritParent=False)
        self._processing_config = load_processing_config(self._logger)

        self._reconstructor = None
        self._source = None
        self._source_arg = None
        self._params = {}

        self._stream_thread = None
        self._process_thread = None
        self._stream_worker = None
        self._process_worker = None
        self._session = None

        self._buffer = []
        self._stack_info = None
        self._is_streaming = False
        self._running = False
        self._finishing = False

    def start(self, reconstructor, source, params: dict | None = None,
              source_arg=None) -> bool:
        """
        Start live reconstruction with the given reconstructor and source.

        Args:
            reconstructor: A Reconstructor or StreamingReconstructor instance.
            source: A LiveSource instance (not yet opened).
            params: Reconstruction parameters dict.
            source_arg: Path or handle passed to ``source.open(...)`` — e.g. the
                Zarr/HDF5 recording path for ZarrLiveSource/Hdf5LiveSource.
                ``None`` for sources pre-configured with their target.

        Returns:
            ``True`` if the worker threads were started. For the streaming path
            this is always ``True`` — the source is opened (with bounded retry)
            on the worker thread, so a recording discovered before its data
            array exists is waited on, not skipped; ``sigFinished`` fires when
            the stack completes (or if startup ultimately fails, so a queue
            driver still advances). ``False`` only when the batch-fallback path
            fails to open the source synchronously.
        """
        if self._running:
            self._logger.warning("Live reconstruction already running, stopping first")
            self.stop()

        self._reconstructor = reconstructor
        self._source = source
        self._source_arg = source_arg
        self._params = params or {}
        self._buffer = []
        self._stack_info = None
        self._finishing = False

        # supports_streaming lives on the reconstructor, so we can branch
        # without opening the source first — the streaming path opens lazily on
        # its worker thread.
        self._is_streaming = getattr(self._reconstructor, "supports_streaming", False)

        if self._is_streaming:
            started = self._start_streaming_path()
        else:
            try:
                self._stack_info = self._source.open(source_arg)
            except Exception as e:
                self._logger.error(f"Failed to open source: {e}")
                return False
            started = self._start_batch_fallback_path()

        if not started:
            self._reset_workers()
            return False

        self._running = True
        return True

    def _effective_stall_timeout(self) -> float | None:
        """Compute the effective stall timeout for the current source.
        
        When the source has ``idles_between_stacks == True`` (single-file lapse
        sources that present multiple timepoints through one long-lived stream)
        and the config key was not explicitly set, return None to disable the
        watchdog (avoid false triggers on slow timelapses). An explicitly
        configured value applies everywhere.
        """
        timeout, was_explicit = live_stall_timeout_s(self._processing_config)
        if not was_explicit and getattr(self._source, "idles_between_stacks", False):
            return None
        return timeout

    def _reset_workers(self) -> None:
        """Drop worker/thread references after a failed or finished startup."""
        self._stream_thread = None
        self._process_thread = None
        self._stream_worker = None
        self._process_worker = None
        self._session = None
        self._finishing = False

    def stop(self) -> None:
        """Stop live reconstruction and clean up threads."""
        if not self._running:
            return

        self._logger.debug("Stopping live reconstruction")
        self._finish_run(emit_finished=False)

    def _shutdown_workers(self) -> None:
        """Stop live workers, quit Qt thread event loops, and release the source."""
        if self._stream_worker:
            self._stream_worker.stop()

        current_thread = QtCore.QThread.currentThread()
        threads = (
            ("Stream", self._stream_thread),
            ("Process", self._process_thread),
        )
        for _, thread in threads:
            if thread and thread.isRunning():
                thread.requestInterruption()
                thread.quit()

        for name, thread in threads:
            if thread and thread.isRunning() and thread is not current_thread:
                if not thread.wait(2000):
                    self._logger.warning(f"{name} thread did not stop in time")

        if self._source:
            self._source.close()

    def _finish_run(self, result=None, *, emit_result: bool = False,
                    emit_finished: bool = True) -> None:
        """Complete or cancel the current run after all worker threads are stopped."""
        if self._finishing:
            return

        self._finishing = True

        try:
            if emit_result and result is not None:
                self._commChannel.sigResultProduced.emit(result, "Live Reconstruction")

            if (
                self._is_streaming
                and self._session is not None
                and hasattr(self._session, 'close')
            ):
                self._session.close()

            self._shutdown_workers()
        finally:
            self._running = False
            self._buffer = []
            self._stack_info = None
            self._source = None
            self._source_arg = None
            self._reset_workers()

        if emit_finished:
            self.sigFinished.emit()

    def _start_streaming_path(self) -> bool:
        """Start the streaming path.

        The stream worker opens the source (with bounded retry) and collects the
        first stack ON ITS THREAD, then emits ``sigInitStackReady``; the session
        is begun in ``_on_init_stack_ready`` and the worker is resumed to stream
        the remainder. Always returns ``True`` — startup waiting/failure is
        handled asynchronously (a ``sigFinished`` still fires on failure so a
        queue driver advances).
        """
        self._logger.debug("Starting streaming reconstruction path")

        self._session = self._reconstructor.make_session()

        self._process_thread = QtCore.QThread()
        self._process_worker = LiveProcessWorker(self._session)
        self._process_worker.moveToThread(self._process_thread)
        self._process_worker.sigResultUpdated.connect(self._on_result_updated)
        self._process_worker.sigStackFinished.connect(self._on_stack_finished)
        self._process_worker.sigFailed.connect(self._on_process_failed)

        self._stream_thread = QtCore.QThread()
        self._stream_worker = LiveStreamWorker(
            self._source,
            source_arg=self._source_arg,
            do_open=True,
            stall_timeout_s=self._effective_stall_timeout(),
        )
        self._stream_worker.moveToThread(self._stream_thread)
        self._stream_worker.sigOpened.connect(self._on_opened)
        self._stream_worker.sigInitStackReady.connect(self._on_init_stack_ready)
        self._stream_worker.sigFailed.connect(self._on_stream_failed)
        self._stream_worker.sigStalled.connect(self._on_stalled)

        self._process_thread.start()
        self._stream_thread.started.connect(self._stream_worker.run)
        self._stream_thread.start()
        return True

    @QtCore.Slot(object)
    def _on_opened(self, stack_info) -> None:
        """Record the stack metadata once the worker has opened the source."""
        self._stack_info = stack_info

    @QtCore.Slot(object)
    def _on_init_stack_ready(self, init_data) -> None:
        """Begin the session with the first stack, then resume streaming.

        Runs on the controller thread (queued from the worker). The expensive
        WAITING already happened off the UI thread; ``begin()`` itself is a
        one-time localize/allocate step.
        """
        stack_info = self._stack_info
        init_obj = StreamInit(
            name=getattr(self._source, "name", "live"),
            dataset_name=(stack_info.detector_name if stack_info else None) or "detector",
            data=init_data,
            attrs=stack_info.attrs if stack_info else {},
            stack_info=stack_info,
        )
        try:
            plan = self._session.begin(init_obj, self._params)
            self._logger.debug(f"Session initialized with output shape {plan.out_shape}")
        except Exception as e:
            self._logger.error(f"Failed to initialize session: {e}")
            self._finish_without_result()
            return

        # Wire the remainder only after begin() succeeds, then release the gate.
        self._stream_worker.sigChunkReady.connect(self._process_worker.processChunk)
        self._stream_worker.sigStackComplete.connect(self._process_worker.finalize)
        self._stream_worker.resume()

    @QtCore.Slot(str)
    def _on_stream_failed(self, message: str) -> None:
        """Startup ultimately failed (store never became readable)."""
        self._logger.warning(f"Live stream did not start: {message}")
        self._finish_without_result()

    @QtCore.Slot(str)
    def _on_process_failed(self, message: str) -> None:
        """Streaming session processing/finalization failed."""
        self._logger.warning(f"Live stream processing failed: {message}")
        self._finish_without_result()

    @QtCore.Slot(float)
    def _on_stalled(self, seconds_waited: float) -> None:
        """Writer appears to have crashed (no progress and no completion marker).
        
        The worker has already emitted sigStackComplete to finalize with partial
        data, so just log the event here. The partial result flows through the
        normal completion path.
        """
        self._logger.warning(
            f"Live stream stalled for {seconds_waited:.0f}s; "
            f"finalizing with partial data"
        )

    def _finish_without_result(self) -> None:
        """Signal completion without a result so a queue driver advances."""
        self._finish_run()

    def _start_batch_fallback_path(self) -> bool:
        """Start batch fallback: buffer chunks, run process() on complete stack."""
        self._logger.debug("Starting batch fallback path")

        self._stream_thread = QtCore.QThread()
        self._stream_worker = LiveStreamWorker(
            self._source, stall_timeout_s=self._effective_stall_timeout()
        )
        self._stream_worker.moveToThread(self._stream_thread)

        self._stream_worker.sigChunkReady.connect(self._on_chunk_for_buffer)
        self._stream_worker.sigStackComplete.connect(self._on_batch_stack_complete)
        self._stream_worker.sigStalled.connect(self._on_stalled)

        self._stream_thread.started.connect(self._stream_worker.run)
        self._stream_thread.start()
        return True

    @QtCore.Slot(object)
    def _on_chunk_for_buffer(self, chunk: Chunk) -> None:
        """Buffer a chunk for batch fallback."""
        self._buffer.append(chunk)

    @QtCore.Slot()
    def _on_batch_stack_complete(self) -> None:
        """Process the buffered stack with the batch reconstructor."""
        if not self._buffer:
            self._logger.warning("Stack complete but no chunks buffered")
            self._finish_without_result()
            return

        self._logger.debug(f"Stack complete, processing {len(self._buffer)} buffered chunks")

        full_data = np.concatenate([c.data for c in self._buffer], axis=0)

        wrapper = InMemoryStackWrapper(
            name=getattr(self._source, "name", "live"),
            dataset_name=self._stack_info.detector_name or "detector",
            data=full_data,
            attrs=self._stack_info.attrs,
        )

        try:
            result = self._reconstructor.process(wrapper, self._params)
            self._on_stack_finished(result)
        except Exception as e:
            self._logger.error(f"Batch reconstruction failed: {e}")
            self._finish_without_result()

    @QtCore.Slot(object)
    def _on_result_updated(self, result) -> None:
        """Emit live result update to the comm channel."""
        self._commChannel.sigLiveResultUpdated.emit(result)

    @QtCore.Slot(object)
    def _on_stack_finished(self, result) -> None:
        """Emit final result to the comm channel."""
        self._finish_run(result, emit_result=True)


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
