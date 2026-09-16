"""Generic live-reconstruction workers: stream polling and chunk processing."""

import threading
import time

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.reconstructors.base import Chunk, StreamingSession


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
    already-opened source (used by the batch-fallback path).

    Two hand-off modes, chosen per :meth:`resume`:

    * **ring mode** (a ``StackRing`` was passed to ``resume``): each frame is
      written into the ring and its ring index emitted on ``sigFramesReady``.
      Between stacks the loop blocks on the ``stack_consumed`` event until the
      process worker has drained the previous stack (option (b) barrier).
    * **chunk mode** (no ring, e.g. the batch-fallback path): whole ``Chunk``
      objects are emitted on ``sigChunkReady`` as today.
    """

    sigOpened = QtCore.Signal(object)          # StackInfo
    sigInitStackReady = QtCore.Signal(object)  # first-stack ndarray
    sigChunkReady = QtCore.Signal(object)      # Chunk (chunk mode)
    sigFramesReady = QtCore.Signal(int)        # ring index (ring mode)
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
        # worker to this same Event. Only waited on in ring mode.
        self._stack_consumed = threading.Event()
        # Set by resume() for ring mode.
        self._ring = None

    def resume(self, ring=None) -> None:
        """Release the post-``begin()`` gate so the remainder starts streaming.

        Args:
            ring: A ``StackRing`` to write frames into (ring mode). ``None``
                keeps chunk mode (``sigChunkReady``). The ``_stack_consumed``
                barrier event is owned by this worker; the controller wires the
                process worker to it before calling ``resume``.

        Storing ``ring`` *before* ``_resume_event.set()`` makes the set the
        happens-before edge, so the poll loop sees a fully-built ref.
        """
        self._ring = ring
        self._resume_event.set()

    @property
    def frames_per_stack(self) -> int | None:
        """Frames per stack -- ``None`` until :meth:`_run_startup` opens the source."""
        return self._frames_per_stack

    @property
    def stack_consumed(self) -> threading.Event:
        """The inter-stack barrier event (owned here; wire the process worker to it)."""
        return self._stack_consumed

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
                            f"No new frames and no completion marker for {elapsed:.0f}s — "
                            f"assuming the writer crashed; finalizing with the frames received so far"
                        )
                        self.sigStalled.emit(elapsed)
                        self.sigStackComplete.emit()
                        break
                self._sleep()

    def _dispatch_chunks(self, chunks: list[Chunk]) -> bool:
        """Hand a batch of freshly-polled chunks to the consumer.

        Chunk mode (``self._ring is None``): emit each ``Chunk`` on
        ``sigChunkReady`` — unchanged behaviour.

        Ring mode: unroll every chunk into individual frames. For each ring
        index ``ring_idx`` in ``[chunk.start, chunk.end)`` (``ring_idx =
        chunk.start + offset``):
          * if ``ring_idx`` is the first frame of a new stack
            (``ring_idx > 0 and ring_idx % self._frames_per_stack == 0``), first
            block on :meth:`_await_stack_consumed` so the previous stack's ring
            slots are free;
          * ``self._ring.write(ring_idx, chunk.data[offset])``;
          * ``self.sigFramesReady.emit(ring_idx)``.

        Returns:
            ``True`` to keep looping; ``False`` if the caller should stop
            (barrier stalled or interruption during the wait).
        """
        # non-ring mode
        if self._ring is None:
            for chunk in chunks:
                self.sigChunkReady.emit(chunk)
            return True

        # ring mode
        if self._frames_per_stack is None:
            self.sigFailed.emit(
                "Ring mode requires 'frames_per_stack', which currently is 'None'."
            )
            return False

        frames_per_stack = self._frames_per_stack
        for chunk in chunks:
            if self._interrupted():
                return False

            frames_in_chunk = chunk.data.shape[0]
            for frame_idx in range(frames_in_chunk):
                ring_idx = chunk.start + frame_idx
                if ring_idx > 0 and ring_idx % frames_per_stack == 0:
                    stack_consumed = self._await_stack_consumed()
                    if not stack_consumed:
                        return False
                self._ring.write(ring_idx, chunk.data[frame_idx])
                self.sigFramesReady.emit(ring_idx)

        return True

    def _await_stack_consumed(self) -> bool:
        """Block until the process worker has drained the current stack.

        Polls ``self._stack_consumed`` in ``poll_interval_ms`` slices so an
        interruption stays responsive, then clears the event before returning
        so the *next* boundary actually waits. Bounded by
        ``self._stall_timeout_s`` when it is set: on timeout, log, emit
        ``sigStalled`` and ``sigStackComplete`` (tell the process worker to
        finalize with partial data), and return ``False``. A ``None`` timeout
        means wait indefinitely -- only an interruption breaks out.

        Returns:
            ``True`` once the stack has drained; ``False`` on stall-timeout or
            interruption.
        """
        step = self._poll_interval_ms / 1000.0
        start = time.monotonic()
        while not self._stack_consumed.wait(step):
            if self._interrupted():
                return False

            if self._stall_timeout_s is not None:
                elapsed = time.monotonic() - start
                if elapsed > self._stall_timeout_s:
                    self._logger.warning(
                        f"Process worker did not drain the stack within "
                        f"{self._stall_timeout_s:.0f}s; finalizing with partial data"
                    )
                    self.sigStalled.emit(elapsed)
                    self.sigStackComplete.emit()
                    return False
        self._stack_consumed.clear()
        return not self._interrupted()

    def stop(self) -> None:
        """Request the worker loop to stop and release any gate it may block on."""
        self._running = False
        self._resume_event.set()
        self._stack_consumed.set()


class LiveProcessWorker(QtCore.QObject):
    """Owns a StreamingSession: begins it, then reconstructs frames from a StackRing.

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
        ring,
        stack_consumed: "threading.Event",
        frames_per_stack: int,
        viewer_update_interval_s: float = 0.2,
    ):
        """
        Args:
            session: A StreamingSession instance (not yet begun -- see
                :meth:`begin_session`).
            ring: The shared ``StackRing`` the stream worker writes into.
            stack_consumed: Event to ``set()`` after the last frame of each
                stack, releasing the stream worker's inter-stack barrier.
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
        self._ring = ring
        self._stack_consumed = stack_consumed
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
        """Reconstruct the frame at ring ``index`` and drive result cadence + barrier.

        Steps:
          1. bail if interruption was requested (the stream worker's own
             ``stop`` / ``_interrupted`` handles the barrier in that case);
          2. ``frames = self._ring.read(index)`` — a ``(1, H, W)`` view; must be
             consumed synchronously here (``session.push`` must not retain it);
          3. ``self._session.push(frames, index, index + 1)``;
          4. publish a viewer update -- at the last frame of a stack, or once
             ``viewer_update_interval_s`` has elapsed since the previous one
             (whichever comes first). See :meth:`_publish_update`;
          5. if ``(index + 1) % self._frames_per_stack == 0`` this was the last
             frame of a stack -> ``self._stack_consumed.set()``.

        Steps 2–4 run in a try/except (a bad frame is logged, not raised — it
        must not kill the stream); **step 5 runs outside that try**, so an
        exception in 2–4 still releases the barrier and the stream worker never
        wedges.
        """
        if QtCore.QThread.currentThread().isInterruptionRequested():
            return

        # The LiveProcessorWorker tries to read raw frame data from the StackRing.
        # It then pushes the raw data to the StreamingSession instance, for example,
        # the MonalisaSession, which performs the processing. The session instance
        # then stores the result in a buffer: self.reconstructed.
        stack_end = (index + 1) % self._frames_per_stack == 0
        try:
            frames = self._ring.read(index)
            self._session.push(frames, index, index + 1)
            now = time.monotonic()
            if stack_end or now - self._last_result_emit >= self._viewer_update_interval_s:
                self._publish_update()
                self._last_result_emit = now
        except Exception as e:
            self._logger.error(f"Error processing frame {index}: {e}")

        if stack_end:
            self.sigTimepointDone.emit(index // self._frames_per_stack)
            self._stack_consumed.set()

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

        self.sigResultUpdated.emit(self._session.result())
        self._sent_initial_result = True

    @QtCore.Slot()
    def finish_pending(self) -> None:
        """Marker slot: everything queued before this call has been processed.

        Qt delivers queued invocations to a thread in the order they were
        posted, so by the time this runs every ``process_chunk`` posted earlier
        has already returned -- i.e. the timepoint that was in flight is
        finished rather than cut off mid-way.

        That is what makes a graceful stop cheap: with reconstruction slower
        than streaming, the stream worker is normally blocked on the inter-stack
        barrier having already dispatched the whole timepoint, so those frames
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
            self.sigStackFinished.emit(final_result)
        except Exception as e:
            self._logger.error(f"Error finalizing session: {e}")
            self.sigFailed.emit(str(e))


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
