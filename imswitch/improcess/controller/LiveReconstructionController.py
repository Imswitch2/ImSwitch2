"""Generic live reconstruction controller: drives LiveSource + any Reconstructor."""

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live.buffer import RawDataBuffer
from imswitch.improcess.live.workers import LiveProcessWorker, LiveStreamWorker
from imswitch.improcess.model.processing_config import (
    live_stall_timeout_s,
    live_viewer_update_interval_s,
    load_processing_config,
)
from imswitch.improcess.reconstructors.base import StreamInit


class LiveReconstructionController(QtCore.QObject):
    """
    Controller for live reconstruction that works with any Reconstructor.
    
    For StreamingReconstructor: creates a session and runs the streaming path.
    Streaming only: the session consumes frames as they arrive.
    """

    sigFinished = QtCore.Signal()
    # Emitted on the GUI thread, received on the process thread: a queued
    # marker that lands behind every frame already posted (see stop()).
    sigRequestDrain = QtCore.Signal()

    def __init__(self, comm_channel):
        super().__init__()
        self._commChannel = comm_channel
        self._logger = initLogger(self, tryInheritParent=False)
        self._processing_config = load_processing_config(self._logger)
        self._viewer_update_interval_s = live_viewer_update_interval_s(
            self._processing_config
        )
        # Upper bound on how long a graceful stop waits for the timepoint in
        # flight before giving up and tearing down anyway.
        self._drain_timeout_ms = 30_000

        self._reconstructor = None
        self._source = None
        self._source_arg = None
        self._params = {}
        self._run_name = None

        self._stream_thread = None
        self._process_thread = None
        self._stream_worker = None
        self._process_worker = None
        self._session = None
        self._raw_buffer = None
        self._frames_per_stack = None

        self._stack_info = None
        self._is_streaming = False
        self._running = False
        self._finishing = False
        self._stopping = False
        self._notify_on_stop = False

    @property
    def is_running(self) -> bool:
        """Whether a live reconstruction is in progress."""
        return bool(self._running)

    def start(
        self,
        reconstructor,
        source,
        params: dict | None = None,
        source_arg=None,
        name: str | None = None,
    ) -> bool:
        """
        Start live reconstruction with the given reconstructor and source.

        Args:
            reconstructor: A Reconstructor or StreamingReconstructor instance.
            source: A LiveSource instance (not yet opened).
            params: Reconstruction parameters dict.
            source_arg: Path or handle passed to ``source.open(...)`` — e.g. the
                Zarr/HDF5 recording path for ZarrLiveSource/Hdf5LiveSource.
                ``None`` for sources pre-configured with their target.
            name: Display name for this run's result, and therefore for the
                viewer entry it creates. A queue driver passes the job's name
                (e.g. the timelapse folder) so each job lands in its own data
                object instead of every run sharing one. Falls back to the
                source's ``name`` attribute, then to ``"live"``.

        Returns:
            ``True`` if the worker threads were started. For the streaming path
            this is always ``True`` — the source is opened (with bounded retry)
            on the worker thread, so a recording discovered before its data
            array exists is waited on, not skipped; ``sigFinished`` fires when
            the stack completes (or if startup ultimately fails, so a queue
            driver still advances). ``False`` only when the streaming path
            fails to open the source synchronously.
        """
        if self._running:
            self._logger.warning("Live reconstruction already running, stopping first")
            self.stop(graceful=False)

        self._reconstructor = reconstructor
        self._source = source
        self._source_arg = source_arg
        self._params = params or {}
        self._run_name = name
        self._stack_info = None
        self._finishing = False

        # Streaming only. The directory watcher offers itself solely to
        # reconstructors declaring supports_streaming, so anything else
        # reaching here is a wiring mistake rather than a fallback case.
        # Read off the reconstructor, so the branch costs no I/O -- the
        # streaming path opens the source lazily on its worker thread.
        if not getattr(self._reconstructor, "supports_streaming", False):
            self._logger.error(
                "Live mode is only compatible with a reconstructor that "
                "supports streaming."
            )
            return False

        self._is_streaming = True
        started = self._start_streaming_path()

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
        self._raw_buffer = None
        self._frames_per_stack = None
        self._finishing = False
        self._stopping = False
        self._notify_on_stop = False

    def stop(self, *, graceful: bool = True, notify_finished: bool = False) -> None:
        """Stop live reconstruction and clean up threads.

        Args:
            notify_finished: Emit ``sigFinished`` once teardown completes, as a
                normal end-of-run would. A queue driver skipping the current job
                needs this: a graceful stop returns immediately and tears down
                later, so the driver must not free its slot until the run is
                really gone -- otherwise the next ``start()`` would force-kill
                the run that is still draining, losing the very timepoint the
                graceful stop was preserving. Leave ``False`` when the caller is
                shutting the whole mode down and no queue should advance.
            graceful: Let the timepoint in flight finish first. The frames for
                it are normally already queued on the process thread (the stream
                worker blocks on the frame gate once it has dispatched
                a whole stack), so this just drains that queue instead of
                killing the thread mid-timepoint and leaving the viewer with a
                half-reconstructed last timepoint. Teardown then happens
                asynchronously, so the UI never blocks waiting for it.
                ``False`` tears down immediately -- used when a new run has to
                start and cannot wait.
        """
        if not self._running or self._stopping:
            return

        self._notify_on_stop = notify_finished

        # Draining only means something while the process thread is alive to
        # run the marker; without this the watchdog would stall the teardown for
        # its full timeout on an already-finished run.
        can_drain = (
            graceful
            and not self._finishing
            and self._process_worker is not None
            and self._process_thread is not None
            and self._process_thread.isRunning()
        )
        if can_drain:
            self._logger.debug("Stopping after the current timepoint")
            self._begin_graceful_stop()
            return

        self._logger.debug("Stopping live reconstruction")
        self._finish_run(emit_finished=notify_finished)

    def _begin_graceful_stop(self) -> None:
        """Stop feeding frames, drain the timepoint in flight, then tear down.

        Ordering is what makes this work: the stream worker is stopped *first*,
        so it posts no further frames, and only then is the drain marker posted.
        Qt processes a thread's queued calls in post order, so the marker runs
        after every frame already queued -- the remainder of the current
        timepoint. ``sigDrained`` then triggers the real teardown.

        A watchdog covers the case where the process thread never gets there
        (wedged in a long push, or already gone), so a stop can never hang.
        """
        self._stopping = True
        if self._stream_worker is not None:
            self._stream_worker.stop()

        self._process_worker.sigDrained.connect(self._on_drained)
        self.sigRequestDrain.connect(self._process_worker.finish_pending)
        self.sigRequestDrain.emit()

        QtCore.QTimer.singleShot(self._drain_timeout_ms, self._on_drain_timeout)

    @QtCore.Slot()
    def _on_drained(self) -> None:
        """The in-flight timepoint finished -- now tear the run down."""
        self._stopping = False
        self._finish_run(emit_finished=self._notify_on_stop)

    @QtCore.Slot()
    def _on_drain_timeout(self) -> None:
        """Watchdog: tear down anyway if the drain never reported back."""
        if not self._stopping:
            return
        self._logger.warning(
            f"Timepoint did not finish within {self._drain_timeout_ms / 1000:.0f}s; "
            f"stopping with a partial timepoint"
        )
        self._stopping = False
        self._finish_run(emit_finished=self._notify_on_stop)

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

    def _finish_run(
        self,
        result=None,
        *,
        emit_result: bool = False,
        emit_finished: bool = True,
    ) -> None:
        """Complete or cancel the current run after all worker threads are stopped."""
        if self._finishing:
            return

        self._finishing = True

        try:
            if emit_result and result is not None:
                if self._is_streaming:
                    # The streaming run already owns a viewer entry, created on
                    # its first result and updated ever since. Publishing on
                    # sigResultProduced here would append a *second* entry for
                    # the same job, so deliver the final snapshot down the same
                    # live path and let it update that entry in place.
                    self._commChannel.sigLiveResultUpdated.emit(result)
                else:
                    self._commChannel.sigResultProduced.emit(
                        result, self._run_name or "Live Reconstruction"
                    )

            if (
                self._is_streaming
                and self._session is not None
                and hasattr(self._session, 'close')
            ):
                self._session.close()

            self._shutdown_workers()
        finally:
            self._running = False
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

        # The process worker is built later, in _on_init_stack_ready, once the
        # stack shape is known (it needs the RawDataBuffer + frames_per_stack, both
        # derived from the StackInfo the stream worker produces on open).
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

        self._stream_thread.started.connect(self._stream_worker.run)
        self._stream_thread.start()
        return True

    @QtCore.Slot(object)
    def _on_opened(self, stack_info) -> None:
        """Record the stack metadata and allocate the shared raw buffer."""
        if self._finishing:
            return

        self._stack_info = stack_info
        self._frames_per_stack = self._stream_worker.frames_per_stack

        try:
            # Sized for this job: a whole timepoint must fit, with a floor so
            # a recording whose timepoint is one frame does not end up with a
            # one-slot buffer and lock-step producer/consumer.
            self._raw_buffer = RawDataBuffer.for_job(
                self._frames_per_stack,
                stack_info.frame_shape,
                stack_info.dtype,
            )
        except ValueError as e:
            self._logger.error(f"Cannot allocate raw buffer: {e}")
            self._finish_without_result()
            return

        self._logger.info(
            f"Allocated RawDataBuffer: {self._raw_buffer.capacity} x "
            f"{tuple(stack_info.frame_shape)} ({self._raw_buffer.nbytes / 1e6:.1f} MB)"
        )

    @QtCore.Slot(object)
    def _on_init_stack_ready(self, init_data) -> None:
        """Begin the session, build the process worker, then resume streaming.

        Runs on the controller thread (queued from the worker). The expensive
        WAITING already happened off the UI thread; ``begin()`` itself is a
        one-time localize/allocate step.
        """
        if self._finishing:
            return
        stack_info = self._stack_info
        init_obj = StreamInit(
            name=self._run_name or getattr(self._source, "name", "live"),
            dataset_name=(stack_info.detector_name if stack_info else None) or "detector",
            data=init_data,
            attrs=stack_info.attrs if stack_info else {},
            stack_info=stack_info,
        )
        # Build the process worker now that the ring + frames_per_stack exist.
        # session.begin() runs ON the process thread (it can be seconds of
        # localization for real data) -- wired to the thread's `started` signal.
        self._process_thread = QtCore.QThread()
        self._process_worker = LiveProcessWorker(
            self._session,
            self._raw_buffer,
            self._stream_worker.frame_gate,
            self._frames_per_stack,
            viewer_update_interval_s=self._viewer_update_interval_s,
        )
        self._process_worker.set_init(init_obj, self._params)
        # Provenance for every snapshot and the final result: what was
        # reconstructed, with which settings, from which stream. The first
        # stack is consumed by begin() and never arrives as a frame; it counts
        # towards the committed frames all the same.
        init_shape = getattr(init_data, "shape", None)
        self._process_worker.setProvenance(
            self._reconstructor, self._params, init_obj,
            expected_frames=getattr(stack_info, "expected_frames", None),
            initial_frames=int(init_shape[0]) if init_shape else 0,
        )
        self._process_worker.moveToThread(self._process_thread)
        self._process_worker.sigResultUpdated.connect(self._on_result_updated)
        self._process_worker.sigTimepointUpdated.connect(self._on_timepoint_updated)
        self._process_worker.sigStackFinished.connect(self._on_stack_finished)
        self._process_worker.sigSessionBegun.connect(self._on_session_begun)
        self._process_worker.sigTimepointDone.connect(self._on_timepoint_done)
        self._process_worker.sigFailed.connect(self._on_process_failed)
        self._process_thread.started.connect(self._process_worker.begin_session)
        self._process_thread.start()

    @QtCore.Slot(object)
    def _on_session_begun(self, plan) -> None:
        """Session is ready (off-thread) -- wire the stream->process hot path."""
        if self._finishing:
            return
        self._logger.debug(
            f"Session initialized with output shape {getattr(plan, 'out_shape', '?')}"
        )
        # A stall is forwarded so the final record says "stalled", not
        # "complete".
        self._stream_worker.sigStalled.connect(self._process_worker.markStalled)
        # begin() consumed stack 0, so tell the gate those frames are done --
        # otherwise the producer waits for a consumer that will never report
        # them. Then release the stream worker.
        self._stream_worker.sigFramesReady.connect(self._process_worker.process_chunk)
        self._stream_worker.sigStackComplete.connect(self._process_worker.finalize)
        self._stream_worker.frame_gate.consumed_through(self._frames_per_stack - 1)
        self._stream_worker.resume(self._raw_buffer)

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

    @QtCore.Slot(int)
    def _on_timepoint_done(self, timepoint: int) -> None:
        """Relay a completed-timepoint index so the viewer can move its slider."""
        self._commChannel.sigLiveTimepointDone.emit(timepoint)

    @QtCore.Slot(object)
    def _on_result_updated(self, result) -> None:
        """Emit live result update to the comm channel."""
        self._commChannel.sigLiveResultUpdated.emit(result)

    @QtCore.Slot(int, object)
    def _on_timepoint_updated(self, index: int, plane) -> None:
        """Relay one reconstructed timepoint plane to the viewer."""
        self._commChannel.sigLiveTimepointUpdated.emit(index, plane)

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
