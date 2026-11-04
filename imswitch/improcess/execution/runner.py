"""Qt lifecycle wrapper for a single independently testable incremental job.

Session/Queue owns publication and result lifecycle; this runner only emits
preview/terminal events. Never waits on a QThread on the GUI thread.
"""

from __future__ import annotations

from qtpy import QtCore

from .reader import IncrementalReader
from .qt_workers import StreamProcessWorker, StreamReaderWorker


# An owner can be closed while cancellation is pending. Keep Python wrappers
# alive until both threads have emitted QThread.finished (not merely worker stop).
_active_runners: set["QtIncrementalRunner"] = set()


class QtIncrementalRunner(QtCore.QObject):
    sigBegin = QtCore.Signal()
    sigCancelReader = QtCore.Signal()
    sigCancelProcessor = QtCore.Signal()
    sigPause = QtCore.Signal(bool)

    sigResult = QtCore.Signal(object)
    sigPreview = QtCore.Signal(object, object)
    sigTimepoint = QtCore.Signal(int)
    sigCoverage = QtCore.Signal(object)
    sigFailed = QtCore.Signal(str)
    sigFinished = QtCore.Signal(bool)  # True iff result finalized successfully

    def __init__(self, reconstructor, source, params: dict, *,
                 name: str, path=None, dataset_name: str | None = None,
                 max_buffer_bytes: int = 256 * 1024 * 1024, poll_ms: int = 100,
                 enable_preview: bool = True):
        super().__init__(None)
        self._reconstructor = reconstructor
        self._source = source
        self._params = dict(params)
        self._name = name
        self._path = path
        self._dataset_name = dataset_name
        self._buffer_bytes = max_buffer_bytes
        self._poll_ms = poll_ms
        self._enable_preview = bool(enable_preview)
        self._started = False
        self._cancelled = False
        self._final_result = False
        self._last_error = None
        self._threads = []
        self._finished_threads = set()
        self._reader_worker = self._process_worker = None

    def start(self):
        if self._started:
            raise RuntimeError("An incremental runner cannot be reused")
        self._started = True
        _active_runners.add(self)
        reader = IncrementalReader(
            self._source, type(self._reconstructor), self._params,
            name=self._name, path=self._path, max_buffer_bytes=self._buffer_bytes,
        )
        self._reader_worker = StreamReaderWorker(1, reader, self._poll_ms)
        self._process_worker = StreamProcessWorker(
            1, self._reconstructor, name=self._name,
            dataset_name=self._dataset_name,
            enable_preview=self._enable_preview,
        )
        r_thread, p_thread = QtCore.QThread(), QtCore.QThread()
        self._threads = [r_thread, p_thread]
        self._reader_worker.moveToThread(r_thread)
        self._process_worker.moveToThread(p_thread)
        self.sigBegin.connect(self._reader_worker.start)
        self.sigCancelReader.connect(self._reader_worker.cancel)
        self.sigCancelProcessor.connect(self._process_worker.cancel)
        self.sigPause.connect(self._reader_worker.set_paused)
        self._reader_worker.sigBegin.connect(self._process_worker.begin)
        self._reader_worker.sigPush.connect(self._process_worker.push)
        self._reader_worker.sigFinish.connect(self._process_worker.finish)
        self._process_worker.sigAcknowledged.connect(self._reader_worker.acknowledge)
        self._reader_worker.sigCoverage.connect(self._on_coverage)
        self._reader_worker.sigFailed.connect(self._on_error)
        self._process_worker.sigFailed.connect(self._on_error)
        self._process_worker.sigResult.connect(self._on_result)
        self._process_worker.sigPreview.connect(self._on_preview)
        self._process_worker.sigTimepoint.connect(self._on_timepoint)
        self._reader_worker.sigStopped.connect(r_thread.quit)
        self._process_worker.sigStopped.connect(p_thread.quit)
        r_thread.finished.connect(self._reader_worker.deleteLater)
        p_thread.finished.connect(self._process_worker.deleteLater)
        for thread in self._threads:
            thread.finished.connect(self._on_thread_finished)
        # Start process event loop first: no begin can target a stopped worker.
        p_thread.start()
        r_thread.start()
        self.sigBegin.emit()

    def pause(self, paused: bool = True):
        if self._started and not self._cancelled:
            self.sigPause.emit(bool(paused))

    def cancel(self):
        """Stop processing without blocking. Already published previews are owned
        by the queue/viewer and remain intact; this runner never deletes them.
        """
        if not self._started or self._cancelled:
            return
        self._cancelled = True
        self.sigCancelReader.emit()
        self.sigCancelProcessor.emit()

    @QtCore.Slot(int, str)
    def _on_error(self, generation: int, message: str):
        if generation != 1:
            return
        if not self._cancelled:
            self._last_error = message
            self.sigFailed.emit(message)
        self.cancel()

    @QtCore.Slot(int, object)
    def _on_result(self, generation, result):
        if generation == 1 and not self._cancelled and not self._final_result:
            self._final_result = True
            self.sigResult.emit(result)

    @QtCore.Slot(int, object, object)
    def _on_preview(self, generation, result, plane):
        if generation == 1 and not self._cancelled:
            self.sigPreview.emit(result, plane)

    @QtCore.Slot(int, object)
    def _on_coverage(self, generation, missing_ranges):
        if generation == 1 and not self._cancelled:
            self.sigCoverage.emit(missing_ranges)

    @QtCore.Slot(int, int)
    def _on_timepoint(self, generation, index):
        if generation == 1 and not self._cancelled:
            self.sigTimepoint.emit(index)

    @QtCore.Slot()
    def _on_thread_finished(self):
        # This is a GUI-thread slot. Do not finalize or release Qt wrappers
        # from an undecorated callback that might run on the worker thread.
        self._finished_threads.add(self.sender())
        if len(self._finished_threads) != 2:
            return
        # A worker can stop on its own before any process finalization if the
        # source fails; report a failure rather than a false successful finish.
        success = bool(self._final_result and not self._cancelled)
        self._reader_worker = self._process_worker = None
        self._threads.clear()
        self.sigFinished.emit(success)
        _active_runners.discard(self)
