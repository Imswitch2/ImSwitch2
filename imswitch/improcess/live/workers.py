"""Generic live-reconstruction workers: stream polling and chunk processing."""

import time

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.reconstructors.base import Chunk, StreamingSession


class LiveStreamWorker(QtCore.QObject):
    """Polls a LiveSource on its thread and emits new chunks."""

    sigChunkReady = QtCore.Signal(object)
    sigStackComplete = QtCore.Signal()

    def __init__(self, source, poll_interval_ms: int = 200, max_retries: int = 10):
        """
        Args:
            source: Opened LiveSource instance.
            poll_interval_ms: Delay between poll attempts.
            max_retries: Max consecutive empty polls before assuming completion.
        """
        super().__init__()
        self._source = source
        self._poll_interval_ms = poll_interval_ms
        self._max_retries = max_retries
        self._logger = initLogger(self, tryInheritParent=False)
        self._running = False

    @QtCore.Slot()
    def run(self) -> None:
        """Main loop: poll the source until complete or interrupted."""
        self._running = True
        empty_count = 0

        while self._running:
            if QtCore.QThread.currentThread().isInterruptionRequested():
                self._logger.debug("Interruption requested, stopping stream worker")
                break

            try:
                chunks = self._source.poll()
            except Exception as e:
                self._logger.error(f"Error polling source: {e}")
                break

            if chunks:
                empty_count = 0
                for chunk in chunks:
                    self.sigChunkReady.emit(chunk)
            else:
                empty_count += 1
                if self._source.is_complete():
                    self._logger.debug("Source marked complete")
                    self.sigStackComplete.emit()
                    break

                if empty_count > self._max_retries:
                    self._logger.warning(
                        f"No new chunks after {self._max_retries} polls, assuming complete"
                    )
                    self.sigStackComplete.emit()
                    break

            time.sleep(self._poll_interval_ms / 1000.0)

        self._running = False

    def stop(self) -> None:
        """Request the worker loop to stop."""
        self._running = False


class LiveProcessWorker(QtCore.QObject):
    """Owns a StreamingSession and processes incoming chunks."""

    sigResultUpdated = QtCore.Signal(object)
    sigStackFinished = QtCore.Signal(object)

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

    @QtCore.Slot(object)
    def processChunk(self, chunk: Chunk) -> None:
        """Process one chunk and emit updates at a sensible cadence."""
        if QtCore.QThread.currentThread().isInterruptionRequested():
            return

        try:
            self._session.push(chunk.data, chunk.start, chunk.end)
            self._chunk_count += 1

            if self._chunk_count % self._update_cadence == 0:
                result = self._session.result()
                self.sigResultUpdated.emit(result)

        except Exception as e:
            self._logger.error(f"Error processing chunk [{chunk.start}:{chunk.end}]: {e}")

    @QtCore.Slot()
    def finalize(self) -> None:
        """Finalize the session and emit the final result."""
        try:
            final_result = self._session.finish()
            self.sigStackFinished.emit(final_result)
        except Exception as e:
            self._logger.error(f"Error finalizing session: {e}")


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
