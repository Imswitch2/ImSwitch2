"""Generic live reconstruction controller: drives LiveSource + any Reconstructor."""

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live import InMemoryStackWrapper
from imswitch.improcess.live.workers import LiveProcessWorker, LiveStreamWorker
from imswitch.improcess.reconstructors.base import Chunk, StreamInit, StreamingReconstructor


class LiveReconstructionController(QtCore.QObject):
    """
    Controller for live reconstruction that works with any Reconstructor.
    
    For StreamingReconstructor: creates a session and runs the streaming path.
    For regular Reconstructor: buffers chunks and runs batch fallback.
    """

    def __init__(self, comm_channel):
        super().__init__()
        self._commChannel = comm_channel
        self._logger = initLogger(self, tryInheritParent=False)

        self._reconstructor = None
        self._source = None
        self._params = {}

        self._stream_thread = None
        self._process_thread = None
        self._stream_worker = None
        self._process_worker = None

        self._buffer = []
        self._stack_info = None
        self._is_streaming = False
        self._running = False

    def start(self, reconstructor, source, params: dict | None = None) -> None:
        """
        Start live reconstruction with the given reconstructor and source.
        
        Args:
            reconstructor: A Reconstructor or StreamingReconstructor instance.
            source: A LiveSource instance (not yet opened).
            params: Reconstruction parameters dict.
        """
        if self._running:
            self._logger.warning("Live reconstruction already running, stopping first")
            self.stop()

        self._reconstructor = reconstructor
        self._source = source
        self._params = params or {}
        self._buffer = []

        try:
            self._stack_info = self._source.open(None)
        except Exception as e:
            self._logger.error(f"Failed to open source: {e}")
            return

        self._is_streaming = getattr(self._reconstructor, "supports_streaming", False)

        if self._is_streaming:
            self._start_streaming_path()
        else:
            self._start_batch_fallback_path()

        self._running = True

    def stop(self) -> None:
        """Stop live reconstruction and clean up threads."""
        if not self._running:
            return

        self._logger.debug("Stopping live reconstruction")
        self._running = False

        if self._stream_worker:
            self._stream_worker.stop()

        if self._stream_thread and self._stream_thread.isRunning():
            self._stream_thread.requestInterruption()
            if not self._stream_thread.wait(2000):
                self._logger.warning("Stream thread did not stop in time")

        if self._process_thread and self._process_thread.isRunning():
            self._process_thread.requestInterruption()
            if not self._process_thread.wait(2000):
                self._logger.warning("Process thread did not stop in time")

        if self._source:
            self._source.close()

        self._stream_thread = None
        self._process_thread = None
        self._stream_worker = None
        self._process_worker = None
        self._buffer = []

    def _start_streaming_path(self) -> None:
        """Start streaming reconstruction with a StreamingSession."""
        self._logger.debug("Starting streaming reconstruction path")

        session = self._reconstructor.make_session()

        self._stream_thread = QtCore.QThread()
        self._stream_worker = LiveStreamWorker(self._source)
        self._stream_worker.moveToThread(self._stream_thread)

        self._process_thread = QtCore.QThread()
        self._process_worker = LiveProcessWorker(session)
        self._process_worker.moveToThread(self._process_thread)

        first_chunk = None
        chunks = self._source.poll()
        if chunks:
            first_chunk = chunks[0]
        else:
            self._logger.error("No initial chunk available from source")
            return

        init_obj = StreamInit(
            name=getattr(self._source, "name", "live"),
            dataset_name=self._stack_info.detector_name or "detector",
            data=first_chunk.data,
            attrs=self._stack_info.attrs,
            stack_info=self._stack_info,
        )

        try:
            plan = session.begin(init_obj, self._params)
            self._logger.debug(f"Session initialized with output shape {plan.out_shape}")
        except Exception as e:
            self._logger.error(f"Failed to initialize session: {e}")
            return

        self._stream_worker.sigChunkReady.connect(self._process_worker.processChunk)
        self._stream_worker.sigStackComplete.connect(self._process_worker.finalize)

        self._process_worker.sigResultUpdated.connect(self._on_result_updated)
        self._process_worker.sigStackFinished.connect(self._on_stack_finished)

        self._stream_thread.started.connect(self._stream_worker.run)
        self._stream_thread.start()
        self._process_thread.start()

    def _start_batch_fallback_path(self) -> None:
        """Start batch fallback: buffer chunks, run process() on complete stack."""
        self._logger.debug("Starting batch fallback path")

        self._stream_thread = QtCore.QThread()
        self._stream_worker = LiveStreamWorker(self._source)
        self._stream_worker.moveToThread(self._stream_thread)

        self._stream_worker.sigChunkReady.connect(self._on_chunk_for_buffer)
        self._stream_worker.sigStackComplete.connect(self._on_batch_stack_complete)

        self._stream_thread.started.connect(self._stream_worker.run)
        self._stream_thread.start()

    @QtCore.Slot(object)
    def _on_chunk_for_buffer(self, chunk: Chunk) -> None:
        """Buffer a chunk for batch fallback."""
        self._buffer.append(chunk)

    @QtCore.Slot()
    def _on_batch_stack_complete(self) -> None:
        """Process the buffered stack with the batch reconstructor."""
        if not self._buffer:
            self._logger.warning("Stack complete but no chunks buffered")
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

    @QtCore.Slot(object)
    def _on_result_updated(self, result) -> None:
        """Emit live result update to the comm channel."""
        self._commChannel.sigLiveResultUpdated.emit(result)

    @QtCore.Slot(object)
    def _on_stack_finished(self, result) -> None:
        """Emit final result to the comm channel."""
        self._commChannel.sigResultProduced.emit(result, "Live Reconstruction")
        if self._is_streaming and self._process_worker and hasattr(self._process_worker._session, 'close'):
            self._process_worker._session.close()


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
