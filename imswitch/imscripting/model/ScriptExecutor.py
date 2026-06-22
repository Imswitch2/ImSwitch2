import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Worker
from imswitch.imcommon.model import initLogger
from .actions import getActionsScope
from .ScriptRunResult import ScriptRunResult, ScriptRunStatus


class ScriptExecutor(SignalInterface):
    """Handles execution and state of scripts with cooperative cancellation."""

    sigOutputAppended = Signal(str)  # (outputText)
    _sigExecute = Signal(str, str, object)  # (scriptPath, code, result)
    sigExecutionFinished = Signal(object)  # (ScriptRunResult)

    def __init__(self, scriptScope):
        super().__init__()
        self.__logger = initLogger(self)

        self._executionWorker = ExecutionThread(scriptScope)
        self._executionWorker.sigOutputAppended.connect(self.sigOutputAppended)
        self._executionThread = Thread()
        self._executionWorker.moveToThread(self._executionThread)
        self._executionWorker.sigExecutionFinished.connect(self.sigExecutionFinished)
        self._sigExecute.connect(self._executionWorker.execute)
        self._currentResult = None

    def __del__(self):
        self._executionThread.quit()
        self._executionThread.wait()
        if hasattr(super(), '__del__'):
            super().__del__()

    def execute(self, scriptPath, code):
        """Executes the specified script code. scriptPath is the path to the
        script file if it exists, or None if the script has not been saved to a
        file. Returns the ScriptRunResult object."""
        self.cancel()
        self._currentResult = ScriptRunResult(script_path=scriptPath)
        self._executionThread.start()
        self._sigExecute.emit(scriptPath, code, self._currentResult)
        return self._currentResult

    def cancel(self):
        """Cooperatively cancels the currently running script. Does nothing if no script
        is running. Waits for the thread to finish cleanly."""
        if self.isExecuting():
            print()  # Blank line
            self.__logger.info('Cancelling script...')
            self._executionWorker.requestStop()
            self._executionThread.quit()
            self._executionThread.wait()

    def isExecuting(self):
        """Returns whether a script is currently being executed."""
        return self._executionThread.isRunning() and self._executionWorker.isWorking()
    
    def getCurrentResult(self):
        """Returns the current ScriptRunResult, or None if no script is executing."""
        return self._currentResult


class ExecutionThread(Worker):
    """Worker that executes scripts with cooperative cancellation support.
    
    Uses per-run stdout/stderr capture instead of global process-wide redirection.
    Execution can be stopped cooperatively via requestStop(), which sets a flag
    that the script can check. The thread will complete its current operation and
    exit cleanly without using QThread.terminate().
    """
    sigOutputAppended = Signal(str)  # (outputText)
    sigExecutionFinished = Signal(object)  # (ScriptRunResult)

    def __init__(self, scriptScope):
        super().__init__()
        self.__logger = initLogger(self, tryInheritParent=True)
        self._scriptScope = scriptScope
        self._isWorking = False
        self._stopRequested = False
        self._currentResult = None

    def requestStop(self):
        """Request cooperative cancellation of the current execution.
        The script will complete its current operation and exit cleanly."""
        self._stopRequested = True

    def execute(self, scriptPath, code, result):
        """Execute script code with per-run output capture and structured result."""
        scriptScope = {}
        scriptScope.update(self._scriptScope)
        scriptScope.update(getActionsScope(self._scriptScope, scriptPath))

        self._isWorking = True
        self._stopRequested = False
        self._currentResult = result
        
        # Per-run output capture: SignaledStringIO emits output live and captures it
        outputIO = SignaledStringIO(self.sigOutputAppended, result)
        
        try:
            # Use context managers for thread-safe, per-run stdout/stderr capture
            with redirect_stdout(outputIO), redirect_stderr(outputIO):
                self.__logger.info('Started script')
                print()  # Blank line
                
                try:
                    if not self._stopRequested:
                        exec(code, scriptScope)
                        result.mark_succeeded()
                    else:
                        result.mark_cancelled()
                except Exception as e:
                    error_msg = traceback.format_exc()
                    self.__logger.error(error_msg)
                    result.mark_failed(error_msg)
                
                if self._stopRequested and result.status == ScriptRunStatus.RUNNING:
                    result.mark_cancelled()
                
                print()  # Blank line
                if result.status == ScriptRunStatus.SUCCEEDED:
                    self.__logger.info('Finished script')
                elif result.status == ScriptRunStatus.CANCELLED:
                    self.__logger.info('Script cancelled')
        finally:
            self.sigExecutionFinished.emit(result)
            self._isWorking = False
            self._currentResult = None

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


# Copyright (C) 2020-2021 ImSwitch developers
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
