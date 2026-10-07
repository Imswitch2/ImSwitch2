"""Generic live-reconstruction workers: stream polling and chunk processing."""

import threading
import time

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.reconstructors.base import Chunk, StreamingSession
from .buffer import FrameGate


class LiveStreamWorker(QtCore.QObject):
    """Polls a LiveSource on its own thread and hands new frames to the consumer.

    With ``do_open=True`` the worker also runs the startup sequence on this
    thread, so the UI never blocks and a store discovered before its data
    array exists is *waited on*, not skipped:
      1. open the source with bounded retry,
      2. collect the first logical stack (``frames_per_stack`` frames),
      3. wait for the controller to ``begin()`` the session (resume gate),
      4. stream the remaining frames.
    With ``do_open=False`` it behaves as a plain poll loop over an
    already-opened source.

    Two hand-off modes, chosen per :meth:`resume`:

    * **buffer mode** (a ``RawDataBuffer`` was passed to ``resume``): each
      frame is written into the buffer and its global index emitted on
      ``sigFramesReady``. Before reusing a slot the loop waits on the frame
      gate for the frame that slot still holds.
    * **chunk mode** (no buffer): whole ``Chunk`` objects are emitted on
      ``sigChunkReady``, for a consumer that wants them unsplit.
    """

    sigOpened = QtCore.Signal(object)          # StackInfo
    sigInitStackReady = QtCore.Signal(object)  # first-stack ndarray
    sigChunkReady = QtCore.Signal(object)      # Chunk (chunk mode)
    sigFramesReady = QtCore.Signal(int)        # global frame index (buffer mode)
    sigStackComplete = QtCore.Signal()
    sigFailed = QtCore.Signal(str)
    sigStalled = QtCore.Signal(float)          # seconds waited

    def __init__(
        self,
        source,
        source_arg=None,
        do_open: bool = False,
        poll_interval_ms: int = 200,
        open_max_attempts: int = 50,
        max_poll_error_retries: int = 50,
        stall_timeout_s: float | None = None,
    ):
        """
        Args:
            source: A LiveSource. Opened already when ``do_open`` is False.
            source_arg: Path/handle forwarded to ``source.open`` when do_open.
            do_open: Run the open + first-stack + begin-gate startup here.
            poll_interval_ms: Delay between poll attempts / retries.
            open_max_attempts: Bounded retries while the store is unreadable.
            max_poll_error_retries: Consecutive transient poll errors (e.g. a
                PermissionError while the recorder still holds a live store)
                tolerated before giving up, rather than killing the stream on
                the first error.
            stall_timeout_s: Optional timeout for detecting stalled writers. If
                no new frames arrive and no completion marker appears for this
                many seconds, the worker finalizes with partial data. ``None``
                disables the watchdog.
        """
        super().__init__()
        self._source = source
        self._source_arg = source_arg
        self._do_open = do_open
        self._poll_interval_ms = poll_interval_ms
        self._open_max_attempts = open_max_attempts
        self._max_poll_error_retries = max_poll_error_retries
        self._stall_timeout_s = stall_timeout_s
        self._frames_per_stack = None
        self._logger = initLogger(self, tryInheritParent=False)
        self._running = False
        self._resume_event = threading.Event()
        self._pending_chunks: list[Chunk] = []
        self._last_progress = None
        # Owned here (like _resume_event); the controller wires the process
        # worker to this same gate. Only waited on in buffer mode.
        self._frame_gate = FrameGate()
        # Set by resume() for buffer mode.
        self._raw_buffer = None
        # Which frame index each buffer slot currently holds, or None while
        # the slot has never been written. Producer-side and single-threaded:
        # only this worker writes, so only it knows what it would overwrite.
        self._slot_owner: list[int | None] = []

    def resume(self, raw_buffer=None) -> None:
        """Release the post-``begin()`` gate so the remainder starts streaming.

        Args:
            raw_buffer: A ``RawDataBuffer`` to write frames into (buffer mode).
                ``None`` keeps chunk mode (``sigChunkReady``). The frame gate
                is owned by this worker; the controller wires the process
                worker to it before calling ``resume``.

        Store ``_raw_buffer`` *before* ``_resume_event.set()`` makes the set the
        happens-before edge, so the poll loop sees a fully-built ref.
        """
        self._raw_buffer = raw_buffer
        self._slot_owner = (
            [None] * raw_buffer.capacity if raw_buffer is not None else []
        )
        self._resume_event.set()

    @property
    def frames_per_stack(self) -> int | None:
        """Frames per stack -- ``None`` until :meth:`_run_startup` opens the source."""
        return self._frames_per_stack

    @property
    def frame_gate(self) -> FrameGate:
        """The producer/consumer gate (owned here; wire the process worker to it).

        Bounds how far ahead the producer may write. Replaces a per-stack
        event: the bound is the same when the buffer holds one stack, but
        stated per frame it also fits a buffer sized for the job.
        """
        return self._frame_gate

    @QtCore.Slot()
    def run(self) -> None:
        """Run startup (if requested) then the steady poll loop."""
        self._running = True
        if self._do_open and not self._run_startup():
            self._running = False
            return
        self._poll_loop()
        self._running = False

    def _interrupted(self) -> bool:
        return (
            not self._running
            or QtCore.QThread.currentThread().isInterruptionRequested()
        )

    def _sleep(self) -> None:
        time.sleep(self._poll_interval_ms / 1000.0)

    def _run_startup(self) -> bool:
        """Open (with retry) + collect first stack + wait for begin. Off the UI thread."""
        # 1. Open — the recorder may create the store before its data array,
        #    so retry instead of giving up (which would skip the recording).
        stack_info = None
        attempts = 0
        while not self._interrupted():
            try:
                stack_info = self._source.open(self._source_arg)
                break
            except Exception as e:
                attempts += 1
                if attempts >= self._open_max_attempts:
                    self._logger.error(f"Source did not become readable: {e}")
                    self.sigFailed.emit(str(e))
                    return False
                self._sleep()

        if stack_info is None:
            return False

        self._frames_per_stack = max(1, int(stack_info.frames_per_stack or 1))
        self.sigOpened.emit(stack_info)
        self._last_progress = time.monotonic()

        # 2. Collect the first logical stack (needed whole for localize/orient).
        buffered = []
        total = 0
        while total < self._frames_per_stack and not self._interrupted():
            chunks = self._source.poll()
            if chunks:
                self._last_progress = time.monotonic()
                for chunk in chunks:
                    remaining = self._frames_per_stack - total
                    if remaining <= 0:
                        break

                    frame_count = int(chunk.data.shape[0])
                    if frame_count <= remaining:
                        buffered.append(chunk.data)
                        total += frame_count
                        continue

                    buffered.append(chunk.data[:remaining])
                    total += remaining
                    overflow = Chunk(
                        data=chunk.data[remaining:],
                        start=chunk.start + remaining,
                        end=chunk.end,
                    )
                    self._pending_chunks.append(overflow)

            elif self._source.is_complete():
                break

            else:
                if self._stall_timeout_s is not None:
                    elapsed = time.monotonic() - self._last_progress
                    if elapsed > self._stall_timeout_s:
                        self._logger.warning(
                            f"No new frames and no completion marker for {elapsed:.0f}s "
                            f"during startup — assuming the writer crashed; cannot salvage "
                            f"incomplete first stack"
                        )
                        self.sigFailed.emit(
                            f"writer stalled for {elapsed:.0f}s before first stack complete"
                        )
                        return False
                self._sleep()

        if self._interrupted():
            return False

        if not buffered:
            self._logger.error("Source completed without yielding any frames")
            self.sigFailed.emit("source yielded no frames")
            return False

        self.sigInitStackReady.emit(np.concatenate(buffered, axis=0))

        # 3. Wait for the controller to begin() the session before streaming the rest.
        while not self._resume_event.is_set():
            if self._interrupted():
                return False
            self._resume_event.wait(0.05)

        return True

    def _poll_loop(self) -> None:
        """Emit remaining chunks until the source is complete or interrupted.

        Transient source errors (e.g. a PermissionError/OSError while the
        recorder still holds a live Zarr/HDF5 store mid-write) are retried with
        backoff instead of killing the stream — matching the upstream live
        watcher's lock-retry behaviour. Only a persistent failure (more than
        ``max_poll_error_retries`` consecutive errors) stops the stream.
        """
        consecutive_errors = 0
        if self._last_progress is None:
            self._last_progress = time.monotonic()
        while not self._interrupted():
            if self._pending_chunks:
                chunks = self._pending_chunks
                self._pending_chunks = []
                self._last_progress = time.monotonic()
                if not self._dispatch_chunks(chunks):
                    break
                continue

            try:
                chunks = self._source.poll()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    self._logger.warning(f"Transient source poll error, retrying: {e}")
                if consecutive_errors > self._max_poll_error_retries:
                    self._logger.error(
                        f"Source unreadable after {self._max_poll_error_retries} "
                        f"retries; stopping stream: {e}"
                    )
                    self.sigFailed.emit(str(e))
                    break
                self._sleep()
                continue

            if chunks:
                self._last_progress = time.monotonic()
                if not self._dispatch_chunks(chunks):
                    break
            elif self._source.is_complete():
                self._logger.debug("Source marked complete")
                self.sigStackComplete.emit()
                break
            else:
                if self._stall_timeout_s is not None:
                    elapsed = time.monotonic() - self._last_progress
                    if elapsed > self._stall_timeout_s:
                        self._logger.warning(
                            f"No new frames and no completion marker for "
                            f"{elapsed:.0f}s; finalizing with the frames "
                            f"received so far. The recording was either "
                            f"stopped between timepoints or its writer "
                            f"failed — this cannot be told apart from here, "
                            f"so check the file's completion outcome before "
                            f"treating the result as short."
                        )
                        self.sigStalled.emit(elapsed)
                        self.sigStackComplete.emit()
                        break
                self._sleep()

    def _dispatch_chunks(self, chunks: list[Chunk]) -> bool:
        """Hand a batch of freshly-polled chunks to the consumer.

        Chunk mode (``self._raw_buffer is None``): emit each ``Chunk`` whole
        on ``sigChunkReady``.

        Buffer mode: unroll every chunk into individual frames. For each
        global frame index ``frame_index`` in ``[chunk.start, chunk.end)``:
          * look up the frame that currently occupies that index's slot; if
            there is one and it has not been consumed, block on
            :meth:`_await_free_slot` until it has;
          * ``self._raw_buffer.write(frame_index, chunk.data[offset])`` and
            record the slot's new occupant;
          * ``self.sigFramesReady.emit(frame_index)``.

        The wait is on the slot's *occupant*, not on ``frame_index -
        capacity``: a timelapse that skips a timepoint leaves a hole in the
        index sequence, and the arithmetic form would wait on a frame that
        is never produced.

        Returns:
            ``True`` to keep looping; ``False`` if the caller should stop
            (barrier stalled or interruption during the wait).
        """
        # chunk mode
        if self._raw_buffer is None:
            for chunk in chunks:
                self.sigChunkReady.emit(chunk)
            return True

        # buffer mode
        if self._frames_per_stack is None:
            self.sigFailed.emit(
                "Buffer mode requires 'frames_per_stack', which currently is 'None'."
            )
            return False

        frames_per_stack = self._frames_per_stack
        for chunk in chunks:
            if self._interrupted():
                return False

            frames_in_chunk = chunk.data.shape[0]
            for frame_idx in range(frames_in_chunk):
                raw_buffer_idx = chunk.start + frame_idx
                slot = raw_buffer_idx % self._raw_buffer.capacity
                owner = self._slot_owner[slot]
                # None means the slot has never been written, so there is
                # nothing to overwrite. Asking for the occupant by name is
                # what survives a timelapse skipping a timepoint: the global
                # index jumps, and 'index - capacity' would name a frame that
                # is never produced and so never consumed.
                if owner is not None and not self._frame_gate.reached(owner):
                    if not self._await_free_slot(owner):
                        return False
                self._raw_buffer.write(raw_buffer_idx, chunk.data[frame_idx])
                self._slot_owner[slot] = raw_buffer_idx
                self.sigFramesReady.emit(raw_buffer_idx)

        return True

    def _await_free_slot(self, owner_index: int) -> bool:
        """Block until ``owner_index`` -- the frame currently in the slot we are
        about to reuse -- has been consumed.

        Polls in ``poll_interval_ms`` slices so an interruption stays
        responsive. Bounded by ``self._stall_timeout_s`` when it is set: on
        timeout, log, emit ``sigStalled`` and ``sigStackComplete`` (tell the
        process worker to finalize with partial data), and return ``False``. A
        ``None`` timeout means wait indefinitely -- only an interruption breaks
        out.

        Returns:
            ``True`` once the slot is free; ``False`` on stall-timeout or
            interruption.
        """
        step = self._poll_interval_ms / 1000.0
        start = time.monotonic()
        while not self._frame_gate.wait_until(owner_index, step):
            if self._interrupted():
                return False

            if self._stall_timeout_s is not None:
                elapsed = time.monotonic() - start
                if elapsed > self._stall_timeout_s:
                    self._logger.warning(
                        f"Process worker did not keep up within "
                        f"{self._stall_timeout_s:.0f}s; finalizing with partial data"
                    )
                    self.sigStalled.emit(elapsed)
                    self.sigStackComplete.emit()
                    return False
        return not self._interrupted()

    def stop(self) -> None:
        """Request the worker loop to stop and release any gate it may block on."""
        self._running = False
        self._resume_event.set()
        self._frame_gate.release()


class LiveProcessWorker(QtCore.QObject):
    """Owns a StreamingSession: begins it, then reconstructs frames from the buffer.

    Everything here runs on the process thread — including ``session.begin()``,
    which for MoNaLISA localizes the whole first stack and must not block the UI.
    """

    sigResultUpdated = QtCore.Signal(object)
    sigTimepointUpdated = QtCore.Signal(int, object)  # (timepoint index, plane copy)
    sigStackFinished = QtCore.Signal(object)
    sigSessionBegun = QtCore.Signal(object)  # StreamPlan
    sigTimepointDone = QtCore.Signal(int)    # 0-based index of a just-completed stack
    sigDrained = QtCore.Signal()             # queued frames processed; safe to tear down
    sigFailed = QtCore.Signal(str)

    def __init__(
        self,
        session: StreamingSession,
        raw_buffer,
        frame_gate,
        frames_per_stack: int,
        viewer_update_interval_s: float = 0.2,
    ):
        """
        Args:
            session: A StreamingSession instance (not yet begun -- see
                :meth:`begin_session`).
            raw_buffer: The shared ``RawDataBuffer`` the stream worker writes into.
            frame_gate: Gate to report each finished frame to, freeing its
                buffer slot for the stream worker to reuse.
            frames_per_stack: Frames per stack (for the stack-boundary test).
            viewer_update_interval_s: Lower bound on wall-clock time between
                viewer updates, so a fast process loop cannot flood the GUI
                thread with redraws. In the GPU path this is also the
                GPU->host (D2H) transfer cadence -- the session accumulates on
                the device and only copies back here. A stack's last frame
                always emits regardless. ``0`` emits every frame.

        Emits the first update as a whole ``ProcessingResult``
        (``sigResultUpdated``), which gives the viewer a buffer of its own;
        every update after that is a single timepoint plane
        (``sigTimepointUpdated``) written into that buffer, so the per-refresh
        cost stays flat instead of growing with the timelapse. Sessions that
        do not implement ``live_plane()`` keep getting whole results.
        """
        super().__init__()
        self._session = session
        self._raw_buffer = raw_buffer
        self._frame_gate = frame_gate
        self._frames_per_stack = frames_per_stack
        self._viewer_update_interval_s = viewer_update_interval_s
        # Seed from the clock, not 0.0: time.monotonic() is an absolute reading
        # (system uptime), so a 0.0 seed makes `now - last` enormous and lets
        # the very first frame bypass the throttle -- on a machine whose uptime
        # happens to exceed the configured interval. Seeding here measures the
        # first interval from construction, identically on every machine.
        self._last_result_emit = time.monotonic()
        # The viewer needs one whole result before incremental planes mean
        # anything -- that first result is the buffer they get written into.
        self._sent_initial_result = False
        self._init_obj = None
        self._init_params: dict = {}
        self._logger = initLogger(self, tryInheritParent=False)
        # Provenance context: set once the controller has begun the session,
        # so every snapshot and the final result record where they came from.
        self._provenance = None
        self._frames_committed = 0
        self._stalled = False
        self._failed_chunks: list[tuple[int, int, str]] = []

    def setProvenance(self, reconstructor, params: dict, source, expected_frames=None,
                      initial_frames: int | None = None) -> None:
        """Tell the worker what it is reconstructing, for the provenance record.

        ``initial_frames`` is how many frames ``session.begin()`` already
        consumed from the first stack: they never arrive as chunks, so
        without this the count would start at zero and a complete
        acquisition with no further chunks would be recorded as partial.
        """
        self._provenance = (reconstructor, dict(params or {}), source, expected_frames)
        if initial_frames is None:
            data = getattr(source, "data", None)
            shape = getattr(data, "shape", None)
            initial_frames = int(shape[0]) if shape else 0
        self._frames_committed = max(self._frames_committed, int(initial_frames or 0))

    @QtCore.Slot(float)
    def markStalled(self, _seconds_waited: float = 0.0) -> None:
        """The stream worker gave up waiting for frames; the final result is partial."""
        self._stalled = True

    def _record(self, result, status: str) -> None:
        """Attach the provenance record to ``result``; raises when it cannot.

        A result that leaves this worker without provenance would be the one
        producing path that records nothing, so a recording failure is a
        failure of the result, not a debug line.
        """
        if self._provenance is None or result is None:
            return
        reconstructor, params, source, expected = self._provenance
        from imswitch.improcess.reconstructors.run import record_snapshot

        record_snapshot(
            result, reconstructor, params, source,
            session=self._session, status=status,
            frames_committed=self._frames_committed, expected_frames=expected,
        )

    def set_init(self, init_obj, params: dict | None) -> None:
        """Stash the first-stack payload for :meth:`begin_session` (call before
        ``moveToThread``)."""
        self._init_obj = init_obj
        self._init_params = params or {}

    @QtCore.Slot()
    def begin_session(self) -> None:
        """Run ``session.begin()`` here, off the GUI thread.

        Wired to the process thread's ``started`` signal, so it runs the moment
        the thread's event loop comes up. Emits ``sigSessionBegun(plan)`` on
        success, ``sigFailed`` on error.
        """
        try:
            plan = self._session.begin(self._init_obj, self._init_params)
            self.sigSessionBegun.emit(plan)
        except Exception as e:
            self._logger.error(f"Session begin failed: {e}")
            self.sigFailed.emit(str(e))

    @QtCore.Slot(int)
    def process_chunk(self, index: int) -> None:
        """Reconstruct the frame at raw_buffer ``index`` and drive result cadence + barrier.

        Steps:
          1. bail if interruption was requested (the stream worker's own
             ``stop`` / ``_interrupted`` handles the barrier in that case);
          2. ``frames = self._raw_buffer.read(index)`` — a ``(1, H, W)`` view; must be
             consumed synchronously here (``session.push`` must not retain it);
          3. ``self._session.push(frames, index, index + 1)``;
          4. publish a viewer update -- at the last frame of a stack, or once
             ``viewer_update_interval_s`` has elapsed since the previous one
             (whichever comes first). See :meth:`_publish_update`;
          5. report the frame to the gate, freeing its buffer slot; if
             ``(index + 1) % self._frames_per_stack == 0`` this was the last
             frame of a stack -> ``sigTimepointDone``.

        Steps 2–4 run in a try/except (a bad frame is logged, not raised — it
        must not kill the stream); **step 5 runs outside that try**, so an
        exception in 2–4 still releases the barrier and the stream worker never
        wedges.
        """
        if QtCore.QThread.currentThread().isInterruptionRequested():
            return

        # Reads the raw frame out of the shared RawDataBuffer and pushes it to
        # the StreamingSession, which accumulates the reconstruction.
        stack_end = (index + 1) % self._frames_per_stack == 0
        try:
            frames = self._raw_buffer.read(index)
            self._session.push(frames, index, index + 1)
            self._frames_committed = max(self._frames_committed, index + 1)
        except Exception as e:
            self._logger.error(f"Error processing frame {index}: {e}")
            # A dropped frame is missing from the result; the final record
            # must not call that complete.
            self._failed_chunks.append((index, index + 1, str(e)))
        else:
            now = time.monotonic()
            if stack_end or now - self._last_result_emit >= self._viewer_update_interval_s:
                try:
                    self._publish_update()
                except Exception as e:
                    # The frame is in; only the viewer update could not be
                    # made or recorded, which does not make the result partial.
                    self._logger.error(f"Could not publish the live update: {e}")
                self._last_result_emit = now

        # Outside the try, and for every frame rather than every stack: the
        # frame has been read either way, and a slot never reported free is a
        # slot the producer waits on forever. Reporting only at stack ends is
        # what the per-stack barrier did, and it wedges a gate counting frames.
        self._frame_gate.consumed_through(index)

        if stack_end:
            self.sigTimepointDone.emit(index // self._frames_per_stack)

    def _publish_update(self) -> None:
        """Send the viewer either the whole result or just the newest plane.

        The first update has to be a whole ``ProcessingResult``: it is what the
        viewer stores and keeps, and the buffer that later planes are written
        into. After that, ``live_plane()`` gives one timepoint per refresh --
        a fixed cost, rather than a copy of the whole volume that grows with
        every timepoint added.

        A session that does not implement ``live_plane()`` (the base class
        returns ``None``) simply keeps receiving whole results.
        """
        if self._sent_initial_result:
            plane = self._session.live_plane()
            if plane is not None:
                self.sigTimepointUpdated.emit(int(plane[0]), plane[1])
                return

        # A whole result is a snapshot, and leaves with its provenance record;
        # if that cannot be made, nothing is emitted for it.
        result = self._session.result()
        self._record(result, "partial")
        self.sigResultUpdated.emit(result)
        self._sent_initial_result = True

    def _final_status(self) -> str:
        if self._stalled:
            return "stalled"
        if self._failed_chunks:
            return "partial"
        expected = self._provenance[3] if self._provenance is not None else None
        if expected is not None and self._frames_committed < int(expected):
            return "partial"
        return "complete"

    @QtCore.Slot()
    def finish_pending(self) -> None:
        """Marker slot: everything queued before this call has been processed.

        Qt delivers queued invocations to a thread in the order they were
        posted, so by the time this runs every ``process_chunk`` posted earlier
        has already returned -- i.e. the timepoint that was in flight is
        finished rather than cut off mid-way.

        That is what makes a graceful stop cheap: with reconstruction slower
        than streaming, the stream worker is normally blocked on the frame
        gate having already dispatched the whole timepoint, so those frames
        are sitting in this thread's queue. Draining them costs at most the rest
        of one timepoint and needs no extra synchronisation.

        Publishes a final update so the viewer shows the completed timepoint,
        then reports back on ``sigDrained``.
        """
        try:
            self._publish_update()
        except Exception as e:
            self._logger.error(f"Error publishing the final update: {e}")
        self.sigDrained.emit()

    @QtCore.Slot()
    def finalize(self) -> None:
        """Finalize the session and emit the final result."""
        try:
            final_result = self._session.finish()
        except Exception as e:
            self._logger.error(f"Error finalizing session: {e}")
            self.sigFailed.emit(str(e))
            return
        try:
            self._record(final_result, self._final_status())
        except Exception as e:
            # Fail closed: a final result without its provenance record is
            # a failed stream, never a finished one.
            self._logger.error(f"Could not record the live result's provenance: {e}")
            self.sigFailed.emit(f"provenance could not be recorded: {e}")
            return
        self.sigStackFinished.emit(final_result)


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
