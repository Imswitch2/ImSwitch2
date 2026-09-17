import ctypes
import threading
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

from imswitch.imcommon.framework import (
    FrameworkUtils, Signal, SignalInterface, Thread, Timer, Worker
)
from imswitch.imcommon.model import (
    CancelToken, OperationCancelled, clearCurrentCancelToken, initLogger,
    setCurrentCancelToken,
)
from .actions import getActionsScope
from .ScriptRunResult import ScriptRunResult, ScriptRunStatus

#: Seconds a cancelled script gets to run its cleanup (``finally`` blocks)
#: after cancellation has been delivered, before cancellation re-arms.
DEFAULT_CLEANUP_BUDGET_S = 30.0
#: Milliseconds after a stop request without the script reaching a
#: cooperative checkpoint before an asynchronous exception is injected.
DEFAULT_ESCALATE_AFTER_MS = 2000
#: Interval between repeated injections once the cleanup budget has expired.
REINJECT_INTERVAL_S = 1.0
#: Default bound for :meth:`ScriptExecutor.shutdown`.
DEFAULT_SHUTDOWN_TIMEOUT_MS = 3000
_WATCHDOG_INTERVAL_MS = 100


class ScriptExecutor(SignalInterface):
    """Runs scripts on a dedicated thread with cooperative cancellation.

    Stop semantics (see ``imcommon.model.cancellation``):

    * :meth:`cancel` never blocks the calling (GUI) thread. It requests a
      stop; the script receives ``OperationCancelled`` at its next
      cooperative wait or, failing that, by asynchronous exception injection
      after ``escalateAfterMs``.
    * Cancellation is delivered **once**. Afterwards the script's ``finally``
      blocks run inside a cleanup window of ``cleanupBudgetS`` seconds in
      which waits and API calls work normally. When that budget expires,
      cancellation re-arms and injection repeats until the thread ends.
    * :meth:`execute` while a run is still winding down defers the new run
      until :attr:`sigExecutionFinished` has fired for the old one, so two
      scripts never overlap.
    * :meth:`shutdown` is the only bounded *wait*; it is meant for
      application exit and pumps this thread's events while waiting so that
      queued cleanup calls can still be delivered.
    """

    sigOutputAppended = Signal(str)  # (outputText)
    _sigExecute = Signal(str, str, object)  # (scriptPath, code, result)
    sigExecutionFinished = Signal(object)  # (ScriptRunResult)

    def __init__(self, scriptScope, *, cleanupBudgetS=DEFAULT_CLEANUP_BUDGET_S,
                 escalateAfterMs=DEFAULT_ESCALATE_AFTER_MS):
        super().__init__()
        self.__logger = initLogger(self)
        self._cleanupBudgetS = float(cleanupBudgetS)
        self._escalateAfterMs = int(escalateAfterMs)

        self._executionWorker = ExecutionThread(
            scriptScope, cleanupBudgetS=self._cleanupBudgetS
        )
        self._executionWorker.sigOutputAppended.connect(self.sigOutputAppended)
        self._executionThread = Thread()
        self._executionWorker.moveToThread(self._executionThread)
        self._executionWorker.sigExecutionFinished.connect(self._onWorkerFinished)
        self._sigExecute.connect(self._executionWorker.execute)

        self._currentResult = None
        self._pendingRun = None  # (scriptPath, code, result) deferred until the current run ends
        self._cancelRequestedAt = None
        self._escalated = False
        self._lastInjectionAt = None

        self._watchdog = Timer()
        self._watchdog.setInterval(_WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._onWatchdogTick)

    def __del__(self):
        try:
            self.shutdown(timeoutMs=1000)
        except Exception:
            pass
        if hasattr(super(), '__del__'):
            super().__del__()

    # ------------------------------------------------------------------ #
    # Running                                                             #
    # ------------------------------------------------------------------ #
    def execute(self, scriptPath, code):
        """Executes the specified script code. scriptPath is the path to the
        script file if it exists, or None if the script has not been saved to
        a file. Returns the ScriptRunResult object.

        If a script is still running (or still stopping), it is cancelled and
        the new run starts once it has finished; the returned result then
        stays ``RUNNING`` until that moment."""
        result = ScriptRunResult(script_path=scriptPath)
        if self.isExecuting():
            self.cancel()
            replaced = self._pendingRun
            self._pendingRun = (scriptPath, code, result)
            if replaced is not None:
                replaced[2].mark_cancelled()
                self.sigExecutionFinished.emit(replaced[2])
            self.__logger.info(
                'The previous script is still stopping; the new run will '
                'start as soon as it has ended.'
            )
            return result
        self._start(scriptPath, code, result)
        return result

    def _start(self, scriptPath, code, result):
        self._currentResult = result
        result.mark_started()
        self._executionThread.start()
        self._sigExecute.emit(scriptPath, code, result)

    def _onWorkerFinished(self, result):
        # Delivered on this object's (GUI) thread after the worker's execute
        # slot has returned.
        self._watchdog.stop()
        self._cancelRequestedAt = None
        self._escalated = False
        self._lastInjectionAt = None
        if result is self._currentResult:
            self._currentResult = None
        self.sigExecutionFinished.emit(result)
        pending = self._pendingRun
        self._pendingRun = None
        if pending is not None:
            self._start(*pending)

    # ------------------------------------------------------------------ #
    # Stopping                                                            #
    # ------------------------------------------------------------------ #
    def cancel(self):
        """Request cooperative cancellation of the running script and return
        immediately. Returns True if a script was running. Does nothing if
        no script is running."""
        if not self.isExecuting():
            return False
        if self._executionWorker.requestStop():
            print()  # Blank line
            self.__logger.info('Cancelling script...')
            self._cancelRequestedAt = time.monotonic()
            self._escalated = False
            self._lastInjectionAt = None
            self._watchdog.start()
        return True

    def _onWatchdogTick(self):
        if not self.isExecuting():
            self._watchdog.stop()
            return
        token = self._executionWorker.currentToken()
        if token is None or self._cancelRequestedAt is None:
            return
        now = time.monotonic()
        state = token.state
        if state == CancelToken.REQUESTED:
            if (
                not self._escalated
                and now - self._cancelRequestedAt >= self._escalateAfterMs / 1000
            ):
                self._escalated = True
                if self._executionWorker.interrupt():
                    token.markDelivered()
                    self.__logger.warning(
                        'The script did not reach a cooperative wait within '
                        f'{self._escalateAfterMs} ms; interrupting it.'
                    )
        elif state == CancelToken.EXPIRED:
            if (
                self._lastInjectionAt is None
                or now - self._lastInjectionAt >= REINJECT_INTERVAL_S
            ):
                self._lastInjectionAt = now
                if self._executionWorker.interrupt():
                    self.__logger.warning(
                        'The script exceeded its cleanup budget of '
                        f'{self._cleanupBudgetS:g} s; interrupting it again.'
                    )

    def shutdown(self, timeoutMs=DEFAULT_SHUTDOWN_TIMEOUT_MS):
        """Stop any running script and join the execution thread, blocking
        for at most roughly ``timeoutMs``. Returns whether the thread ended.

        Meant for application/module close. Events of the calling thread are
        pumped while waiting so that queued cleanup calls and the finished
        signal can still be delivered."""
        self._pendingRun = None
        timeoutS = max(0.0, float(timeoutMs) / 1000)
        deadline = time.monotonic() + timeoutS
        if self.isExecuting():
            worker = self._executionWorker
            worker.requestStop(cleanupBudgetS=timeoutS)
            self._watchdog.stop()
            escalateAt = time.monotonic() + min(
                self._escalateAfterMs / 1000, timeoutS / 2
            )
            escalated = False
            lastInjection = None
            while self.isExecuting() and time.monotonic() < deadline:
                try:
                    FrameworkUtils.processPendingEventsCurrThread()
                except Exception:
                    pass
                token = worker.currentToken()
                now = time.monotonic()
                if token is not None:
                    if not escalated and now >= escalateAt and token.state == CancelToken.REQUESTED:
                        escalated = True
                        if worker.interrupt():
                            token.markDelivered()
                    elif token.state == CancelToken.EXPIRED and (
                        lastInjection is None or now - lastInjection >= 0.5
                    ):
                        lastInjection = now
                        worker.interrupt()
                time.sleep(0.02)
        finished = not self.isExecuting()
        if finished:
            self._executionThread.quit()
            remainingMs = max(200, int((deadline - time.monotonic()) * 1000))
            self._executionThread.wait(remainingMs)
        else:
            self.__logger.error(
                'The running script did not stop within '
                f'{timeoutS:g} s; its thread is left running.'
            )
        return finished

    # ------------------------------------------------------------------ #
    # State                                                               #
    # ------------------------------------------------------------------ #
    def isExecuting(self):
        """Returns whether a script is currently being executed (including
        one that is still stopping)."""
        return self._executionThread.isRunning() and self._executionWorker.isWorking()

    def isStopping(self):
        """Returns whether the running script has been asked to stop."""
        token = self._executionWorker.currentToken()
        return self.isExecuting() and token is not None and token.isStopRequested()

    def getCurrentResult(self):
        """Returns the current ScriptRunResult, or None if no script is executing."""
        return self._currentResult


class ExecutionThread(Worker):
    """Worker that executes scripts with cooperative cancellation support.

    Each run owns a ``CancelToken`` (published thread-locally so that the
    scripting actions and the API layer can honour it). ``requestStop`` moves
    the token; the script observes it at its next cooperative wait.
    ``interrupt`` injects ``OperationCancelled`` asynchronously for scripts
    that never reach one. The thread is never terminated.
    """
    sigOutputAppended = Signal(str)  # (outputText)
    sigExecutionFinished = Signal(object)  # (ScriptRunResult)

    def __init__(self, scriptScope, *, cleanupBudgetS=DEFAULT_CLEANUP_BUDGET_S):
        super().__init__()
        self.__logger = initLogger(self, tryInheritParent=True)
        self._scriptScope = scriptScope
        self._cleanupBudgetS = float(cleanupBudgetS)
        self._isWorking = False
        self._inScript = False
        self._threadIdent = None
        self._token = None
        self._currentResult = None

    def requestStop(self, *, cleanupBudgetS=None):
        """Request cooperative cancellation of the current execution. Returns
        True on the first request of a run."""
        token = self._token
        if token is None:
            return False
        return token.requestStop(cleanupBudgetS=cleanupBudgetS)

    def currentToken(self):
        return self._token

    def interrupt(self):
        """Inject ``OperationCancelled`` into the script thread while it is
        executing script code. Returns whether the injection was accepted."""
        ident = self._threadIdent
        if ident is None or not self._inScript:
            return False
        accepted = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(ident), ctypes.py_object(OperationCancelled)
        )
        if accepted > 1:
            # More than one thread state matched: undo, this must never
            # poison another thread.
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)
            return False
        return accepted == 1

    def execute(self, scriptPath, code, result):
        """Execute script code with per-run output capture and structured result."""
        scriptScope = {}
        scriptScope.update(self._scriptScope)
        scriptScope.update(getActionsScope(self._scriptScope, scriptPath))

        token = CancelToken(cleanupBudgetS=self._cleanupBudgetS)
        self._token = token
        self._threadIdent = threading.get_ident()
        setCurrentCancelToken(token)
        self._isWorking = True
        self._currentResult = result

        # Per-run output capture: SignaledStringIO emits output live and captures it
        outputIO = SignaledStringIO(self.sigOutputAppended, result)

        try:
            with redirect_stdout(outputIO), redirect_stderr(outputIO):
                self.__logger.info('Started script')
                print()  # Blank line

                self._inScript = True
                try:
                    exec(code, scriptScope)
                    result.mark_succeeded()
                except OperationCancelled:
                    result.mark_cancelled()
                except Exception:
                    error_msg = traceback.format_exc()
                    self.__logger.error(error_msg)
                    result.mark_failed(error_msg)
                finally:
                    self._inScript = False

                self._finishRun(token, result)
        finally:
            clearCurrentCancelToken()
            self._isWorking = False
            self._currentResult = None
            self._token = None
            self.sigExecutionFinished.emit(result)

    def _finishRun(self, token, result):
        # An injected OperationCancelled can, in a tiny window, land after the
        # script has already returned; it must not escape this bookkeeping.
        for _attempt in range(3):
            try:
                if result.status == ScriptRunStatus.RUNNING:
                    if token.isStopRequested():
                        result.mark_cancelled()
                    else:
                        result.mark_succeeded()
                if result.status == ScriptRunStatus.CANCELLED and token.isExpired():
                    result.cleanup_timed_out = True
                    self.__logger.warning(
                        'The cancelled script did not finish its cleanup within '
                        f'the budget of {token.cleanupBudgetS:g} s.'
                    )
                print()  # Blank line
                if result.status == ScriptRunStatus.SUCCEEDED:
                    self.__logger.info('Finished script')
                elif result.status == ScriptRunStatus.CANCELLED:
                    self.__logger.info('Script cancelled')
                return
            except OperationCancelled:
                if result.status == ScriptRunStatus.RUNNING:
                    result.mark_cancelled()
                continue

    def isWorking(self):
        return self._isWorking

    def getCurrentResult(self):
        """Returns the current ScriptRunResult, or None if not executing."""
        return self._currentResult


class SignaledStringIO(StringIO):
    """StringIO that emits output via signal and captures it in a ScriptRunResult.

    This provides both live output (via signal) and final captured output (in result),
    while being scoped to a single script run rather than global process-wide."""

    def __init__(self, signal, result):
        super().__init__()
        self._signal = signal
        self._result = result

    def write(self, text):
        super().write(text)
        self._signal.emit(text)
        self._result.append_stdout(text)
        return len(text)


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
