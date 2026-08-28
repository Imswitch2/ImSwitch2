from __future__ import annotations

import traceback
import weakref
from typing import Callable

from qtpy import QtCore

from ...cgh.execution.executor import CGHExecutionHandle,CGHExecutor
from ...cgh.execution.job import CGHJob
from ...cgh.execution.result import CGHResult


class CGHExecutorError(RuntimeError):
    """CGH execution failure retaining the worker-side traceback text."""

    def __init__(self,message: str,traceback_text: str="") -> None:
        super().__init__(str(message))
        self.traceback_text = str(traceback_text or "")


class _QtCGHWorker(QtCore.QObject):
    sigComputeRequested = QtCore.Signal(int,object)
    sigComputed = QtCore.Signal(int,object)
    sigFailed = QtCore.Signal(int,object)

    @QtCore.Slot(int,object)
    def compute(self,submission_id: int,job: CGHJob) -> None:
        try:
            if not isinstance(job,CGHJob):
                raise TypeError("QtCGHExecutor requires a CGHJob")
            result = job.run()
            self.sigComputed.emit(int(submission_id),result)
        except Exception as error:
            self.sigFailed.emit(
                int(submission_id),
                CGHExecutorError(str(error),traceback.format_exc()),
            )


class _QtCGHExecutionHandle:
    def __init__(self,executor: "QtCGHExecutor",submission_id: int) -> None:
        self._executor_ref = weakref.ref(executor)
        self._submission_id = int(submission_id)

    def cancel(self) -> None:
        executor = self._executor_ref()
        if executor is not None:
            executor.cancel(self._submission_id)


class QtCGHExecutor(QtCore.QObject):
    """Default Qt-thread implementation of the generic :class:`CGHExecutor`.

    The executor owns only scheduling.  Runtime generation checks, stale-result
    handling and commits remain responsibilities of the toolkit-independent
    ``SLMSession``. Qt presentation synchronization remains in
    ``SLMQtSession``.
    """

    def __init__(self,parent: QtCore.QObject | None=None) -> None:
        super().__init__(parent)
        self._next_submission_id = 0
        self._callbacks: dict[int, tuple[Callable[[CGHResult], None], Callable[[Exception], None]]] = {}
        self._disposed = False

        self._thread = QtCore.QThread()
        self._worker = _QtCGHWorker()
        self._worker.moveToThread(self._thread)
        self._worker.sigComputeRequested.connect(self._worker.compute)
        self._worker.sigComputed.connect(self._on_computed)
        self._worker.sigFailed.connect(self._on_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.dispose)

    def submit(
        self,
        job: CGHJob,
        on_result: Callable[[CGHResult],None],
        on_error: Callable[[Exception],None],
    ) -> CGHExecutionHandle:
        if self._disposed:
            raise RuntimeError("QtCGHExecutor has been disposed")
        if not isinstance(job,CGHJob):
            raise TypeError("QtCGHExecutor requires a CGHJob")
        if not callable(on_result) or not callable(on_error):
            raise TypeError("CGH executor callbacks must be callable")

        self._next_submission_id += 1
        submission_id = self._next_submission_id
        self._callbacks[submission_id] = (on_result,on_error)
        try:
            self._worker.sigComputeRequested.emit(submission_id,job)
        except Exception:
            self._callbacks.pop(submission_id,None)
            raise
        return _QtCGHExecutionHandle(self,submission_id)

    def cancel(self,submission_id: int) -> None:
        # Algorithms are not forcibly interrupted; dropping the callbacks makes
        # the eventual result inert and is sufficient for generation-safe CGH.
        self._callbacks.pop(int(submission_id),None)

    @QtCore.Slot(int,object)
    def _on_computed(self,submission_id: int,result: CGHResult) -> None:
        callbacks = self._callbacks.pop(int(submission_id),None)
        if callbacks is not None:
            callbacks[0](result)

    @QtCore.Slot(int,object)
    def _on_failed(self,submission_id: int,error: Exception) -> None:
        callbacks = self._callbacks.pop(int(submission_id),None)
        if callbacks is not None:
            callbacks[1](error)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._callbacks.clear()
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()

    def __del__(self) -> None:
        try:
            self.dispose()
        except Exception:
            pass
