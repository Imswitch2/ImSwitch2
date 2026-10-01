"""Runs one processor on a worker thread for a panel, with cancel and live output.

A processor used to run on the GUI thread, so a slow one froze the window. This
moves the run to a thread of its own and gives the panel three things: the
window stays alive, **Cancel** works, and what the processor prints can be shown
as it happens.

Stop semantics are ImScripting's (``imcommon.model.cancellation``), applied in
the same order:

* :meth:`ProcessorRunner.cancel` never blocks. It asks the run's
  :class:`CancelToken` to stop; a processor that calls ``checkpoint()`` stops at
  its next one.
* One that does not is interrupted: after ``escalateAfterMs`` the watchdog
  injects ``OperationCancelled`` into the worker thread, and again every
  ``reinjectAfterMs`` until the run ends, in case the code caught the first.
  The injection lands at the next bytecode, so a long call into C code ends
  first.
* A cancelled run publishes nothing.
* :meth:`ProcessorRunner.shutdown` is the only bounded *wait*, for application
  exit. A thread that will not stop is kept referenced rather than destroyed
  while running, which Qt answers by aborting the process.
"""

from __future__ import annotations

import threading
import time

from qtpy import QtCore

from imswitch.imcommon.model import CancelToken, OperationCancelled, interruptThread
from imswitch.improcess.processors.execution import RunOutcome, execute_run

#: Seconds after cancellation was requested before the thread is interrupted.
DEFAULT_ESCALATE_AFTER_MS = 1500
#: Interval between repeated interruptions of a run that keeps going.
DEFAULT_REINJECT_AFTER_MS = 1000
#: Cancelled runs get this long to clean up before checkpoints raise again.
_CLEANUP_BUDGET_S = 5.0
_WATCHDOG_INTERVAL_MS = 100

#: Runs that did not stop within the shutdown wait, parked so they are not
#: destroyed while running.
_ORPHANED: list = []


class _Worker(QtCore.QObject):
    """Runs the processor on its thread and reports once how it ended."""

    sigOutput = QtCore.Signal(str)
    sigFinished = QtCore.Signal(object)     # RunOutcome

    def __init__(self, processor, inputs, params, logger, token, stream_output):
        super().__init__()
        self._processor = processor
        self._inputs = inputs
        self._params = params
        self._logger = logger
        self._token = token
        self._stream = bool(stream_output)
        self._ident = None
        self._inRun = False

    @QtCore.Slot()
    def run(self) -> None:
        # An interruption can land a moment after the run itself ended; it must
        # not escape this slot (Qt would abort), and the run must report once.
        outcome = None
        for _attempt in range(3):
            try:
                if outcome is None:
                    outcome = self._execute()
                self.sigFinished.emit(outcome)
                return
            except OperationCancelled:
                if outcome is None:
                    outcome = RunOutcome(cancelled=True)

    def _execute(self) -> RunOutcome:
        self._ident = threading.get_ident()
        self._inRun = True
        try:
            return execute_run(
                self._processor, self._inputs, self._params, self._logger,
                token=self._token,
                on_output=self.sigOutput.emit if self._stream else None,
            )
        except OperationCancelled:
            return RunOutcome(cancelled=True)
        except Exception as exc:  # noqa: BLE001 - the panel must not be left waiting
            self._logger.exception("A processor run failed outside the processor")
            return RunOutcome(failures=[(self._inputs[0] if self._inputs else None, str(exc))])
        finally:
            self._inRun = False

    def interrupt(self) -> bool:
        """Inject ``OperationCancelled`` while the processor is running."""
        ident = self._ident
        if ident is None or not self._inRun:
            return False
        return interruptThread(ident)


class ProcessorRunner(QtCore.QObject):
    """One panel's runs, one at a time, each on a thread of its own."""

    sigOutput = QtCore.Signal(str)
    """What the running processor printed, as it printed it (only when asked for)."""
    sigFinished = QtCore.Signal(object)
    """``RunOutcome``, on the thread this object lives on (the GUI thread)."""

    def __init__(self, parent=None, *, escalateAfterMs=DEFAULT_ESCALATE_AFTER_MS,
                 reinjectAfterMs=DEFAULT_REINJECT_AFTER_MS):
        super().__init__(parent)
        self._escalateAfterS = int(escalateAfterMs) / 1000
        self._reinjectAfterS = int(reinjectAfterMs) / 1000
        self._thread = None
        self._worker = None
        self._token = None
        self._cancelRequestedAt = None
        self._lastInjectionAt = None
        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(_WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._onWatchdogTick)
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._onAboutToQuit)

    # -- running -----------------------------------------------------------------

    def isRunning(self) -> bool:
        return self._thread is not None

    def isStopping(self) -> bool:
        return self._token is not None and self._token.isStopRequested()

    def start(self, processor, inputs, params: dict, logger, *, stream_output: bool = False) -> bool:
        """Run ``processor`` over ``inputs`` on a new thread; False if one is running.

        With ``stream_output`` what the processor prints is emitted on
        :attr:`sigOutput` as it happens; otherwise it goes where it always did.
        """
        if self._thread is not None:
            return False
        token = CancelToken(cleanupBudgetS=_CLEANUP_BUDGET_S)
        worker = _Worker(processor, list(inputs), params, logger, token, stream_output)
        thread = QtCore.QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.sigOutput.connect(self.sigOutput)
        worker.sigFinished.connect(self._onWorkerFinished)
        worker.sigFinished.connect(thread.quit)
        # A method of this object, not a lambda: a lambda would run on the
        # emitting (worker) thread, and the release belongs on this one.
        thread.finished.connect(self._onThreadFinished)
        self._thread, self._worker, self._token = thread, worker, token
        self._cancelRequestedAt = self._lastInjectionAt = None
        thread.start()
        return True

    def _onWorkerFinished(self, outcome) -> None:
        # Delivered on this object's thread. Only the current run may report, and
        # only once (a late interruption can make the worker say it twice).
        if self.sender() is not self._worker or self._thread is None:
            return
        self._watchdog.stop()
        thread, worker = self._thread, self._worker
        self._thread = self._worker = self._token = None
        self._cancelRequestedAt = self._lastInjectionAt = None
        # The thread ends by itself (its worker asked it to quit); until it has,
        # both stay referenced, and are released then. The run is over for
        # callers already, so one of them may start the next from its handler.
        _ORPHANED.append((thread, worker))
        self.sigFinished.emit(outcome)

    def _onThreadFinished(self) -> None:
        thread = self.sender()
        for entry in list(_ORPHANED):
            if entry[0] is thread:
                _ORPHANED.remove(entry)
                entry[1].deleteLater()
                thread.deleteLater()

    # -- stopping ----------------------------------------------------------------

    def cancel(self) -> bool:
        """Ask the running processor to stop; returns whether one was running."""
        token = self._token
        if token is None:
            return False
        if token.requestStop():
            self._cancelRequestedAt = time.monotonic()
            self._lastInjectionAt = None
            self._watchdog.start()
        return True

    def _onWatchdogTick(self) -> None:
        worker = self._worker
        if worker is None or self._cancelRequestedAt is None:
            self._watchdog.stop()
            return
        now = time.monotonic()
        if now - self._cancelRequestedAt < self._escalateAfterS:
            return
        if self._lastInjectionAt is None or now - self._lastInjectionAt >= self._reinjectAfterS:
            self._lastInjectionAt = now
            worker.interrupt()

    def shutdown(self, wait_ms: int = 3000) -> bool:
        """Stop a running processor and wait for its thread, at most ``wait_ms``.

        For application exit. Escalates the way :meth:`cancel` does, but without
        the event loop (this blocks). Returns whether the thread ended; one that
        did not is parked, not destroyed.
        """
        thread, worker, token = self._thread, self._worker, self._token
        if thread is None:
            return True
        self._watchdog.stop()
        token.requestStop(cleanupBudgetS=wait_ms / 1000)
        # Asked for directly: the worker's own "finished" would queue the quit onto
        # this thread, which is blocked in the loop below until the deadline.
        thread.requestInterruption()
        thread.quit()
        deadline = time.monotonic() + wait_ms / 1000
        escalateAt = time.monotonic() + min(self._escalateAfterS, wait_ms / 2000)
        while thread.isRunning() and time.monotonic() < deadline:
            if time.monotonic() >= escalateAt:
                worker.interrupt()
            thread.wait(50)
        finished = not thread.isRunning()
        if finished:
            self._thread = self._worker = self._token = None
            worker.deleteLater()
            thread.deleteLater()
        elif (thread, worker) not in _ORPHANED:
            _ORPHANED.append((thread, worker))
            self._thread = self._worker = self._token = None
        return finished

    def _onAboutToQuit(self) -> None:
        self.shutdown()


__all__ = ["DEFAULT_ESCALATE_AFTER_MS", "DEFAULT_REINJECT_AFTER_MS", "ProcessorRunner"]
