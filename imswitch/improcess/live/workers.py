"""Generic live-reconstruction workers: stream polling and chunk processing."""

import threading
import time

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.reconstructors.base import Chunk, StreamingSession


class LiveStreamWorker(QtCore.QObject):
    """Polls a LiveSource on its own thread and emits new chunks.

    With ``do_open=True`` the worker also runs the startup sequence on this
    thread, so the UI never blocks and a store discovered before its data
    array exists is *waited on*, not skipped:
      1. open the source with bounded retry,
      2. collect the first logical stack (``frames_per_stack`` frames),
      3. wait for the controller to ``begin()`` the session (resume gate),
      4. stream the remaining frames.
    With ``do_open=False`` it behaves as a plain poll loop over an
    already-opened source (used by the batch-fallback path).
    """

    sigOpened = QtCore.Signal(object)          # StackInfo
    sigInitStackReady = QtCore.Signal(object)  # first-stack ndarray
    sigChunkReady = QtCore.Signal(object)      # Chunk
    sigStackComplete = QtCore.Signal()
    sigFailed = QtCore.Signal(str)
    sigStalled = QtCore.Signal(float)          # seconds waited

    def __init__(self, source, source_arg=None, do_open: bool = False,
                 poll_interval_ms: int = 200, open_max_attempts: int = 50,
                 max_poll_error_retries: int = 50, stall_timeout_s: float | None = None):
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

    def resume(self) -> None:
        """Release the post-``begin()`` gate so the remainder starts streaming."""
        self._resume_event.set()

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
        return (not self._running
                or QtCore.QThread.currentThread().isInterruptionRequested())

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
                for chunk in chunks:
                    self.sigChunkReady.emit(chunk)
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
                for chunk in chunks:
                    self.sigChunkReady.emit(chunk)
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

    def stop(self) -> None:
        """Request the worker loop to stop and release any startup gate."""
        self._running = False
        self._resume_event.set()


class LiveProcessWorker(QtCore.QObject):
    """Owns a StreamingSession and processes incoming chunks."""

    sigResultUpdated = QtCore.Signal(object)
    sigStackFinished = QtCore.Signal(object)
    sigFailed = QtCore.Signal(str)

    def __init__(self, session: StreamingSession, update_cadence: int = 5):
        """
        Args:
            session: A StreamingSession instance (already begun).
            update_cadence: Emit sigResultUpdated every N chunks.
        """
        super().__init__()
        self._session = session
        self._update_cadence = update_cadence
        self._chunk_count = 0
        self._logger = initLogger(self, tryInheritParent=False)
        # Provenance context: set once the controller has begun the session,
        # so every snapshot and the final result record where they came from.
        self._provenance = None
        self._frames_committed = 0
        self._stalled = False

    def setProvenance(self, reconstructor, params: dict, source, expected_frames=None) -> None:
        """Tell the worker what it is reconstructing, for the provenance record."""
        self._provenance = (reconstructor, dict(params or {}), source, expected_frames)

    @QtCore.Slot(float)
    def markStalled(self, _seconds_waited: float = 0.0) -> None:
        """The stream worker gave up waiting for frames; the final result is partial."""
        self._stalled = True

    def _record(self, result, status: str) -> None:
        if self._provenance is None or result is None:
            return
        reconstructor, params, source, expected = self._provenance
        try:
            from imswitch.improcess.reconstructors.run import record_snapshot

            record_snapshot(
                result, reconstructor, params, source,
                session=self._session, status=status,
                frames_committed=self._frames_committed, expected_frames=expected,
            )
        except Exception:
            self._logger.debug("Could not record streaming provenance", exc_info=True)

    @QtCore.Slot(object)
    def processChunk(self, chunk: Chunk) -> None:
        """Process one chunk and emit updates at a sensible cadence."""
        if QtCore.QThread.currentThread().isInterruptionRequested():
            return

        try:
            self._session.push(chunk.data, chunk.start, chunk.end)
            self._chunk_count += 1
            self._frames_committed = max(self._frames_committed, int(chunk.end))

            if self._chunk_count % self._update_cadence == 0:
                result = self._session.result()
                self._record(result, "partial")
                self.sigResultUpdated.emit(result)

        except Exception as e:
            self._logger.error(f"Error processing chunk [{chunk.start}:{chunk.end}]: {e}")

    @QtCore.Slot()
    def finalize(self) -> None:
        """Finalize the session and emit the final result."""
        try:
            final_result = self._session.finish()
            self._record(final_result, "stalled" if self._stalled else "complete")
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
