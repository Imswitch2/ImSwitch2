import importlib.util
import logging
import os
import time
from typing import Any, Callable, Optional

from imswitch.imcommon.framework import Signal, FrameworkUtils
from imswitch.imcommon.model import (
    APIExport, cancellableSleep, checkpoint, generateAPI, initLogger,
)


class _Actions:
    """ Additional functions intended to be made available through the
    scripting API. """

    def __init__(self, scriptScope, scriptPath=None):
        self._scriptScope = scriptScope
        self._scriptPath = scriptPath
        self._scriptLogger = initLogger('script')

    @APIExport()
    def importScript(self, path: str) -> Any:
        """ Imports the script at the specified path (either absolute or
        relative to the main script) and returns it as a module variable. """

        # Convert to absolute path
        if self._scriptPath is not None:
            path = os.path.join(os.path.dirname(self._scriptPath), path)

        # Load module
        spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        # Set script/actions scope
        script.__dict__.update(self._scriptScope)
        script.__dict__.update(getActionsScope(self._scriptScope, self._scriptPath))

        # Return
        return script

    @APIExport()
    def getScriptDirPath(self) -> str:
        """ Returns the path to the directory containing the running script.
        """
        return os.path.dirname(self._scriptPath)

    @APIExport()
    def getLogger(self) -> logging.LoggerAdapter:
        """ Returns a logger instance that can be used to print formatted
        messages to the console. """
        return self._scriptLogger

    @APIExport()
    def getWaitForSignal(self, signal: Signal,
                         pollIntervalSeconds: float = 0.05,
                         timeout: Optional[float] = None) -> Callable[[], None]:
        """ Returns a function that will wait for the specified signal to emit.
        The returned function will wait until the signal has been emitted
        since its creation, so **create it before triggering the action** that
        emits the signal (e.g. before ``api.imcontrol.runScan()``); an
        emission that happened before creation is not seen.

        The returned function raises ``TimeoutError`` if ``timeout`` seconds
        pass without an emission (``None`` waits indefinitely) and
        ``OperationCancelled`` if the script is stopped. It accepts an
        optional ``timeout`` argument of its own that overrides the one given
        here. The polling interval defaults to 50 ms. """

        emitted = False

        def setEmitted(*_args, **_kwargs):
            nonlocal emitted
            emitted = True

        signal.connect(setEmitted)
        connected = True

        def wait(timeout: Optional[float] = timeout) -> None:
            nonlocal connected
            deadline = None if timeout is None else time.monotonic() + float(timeout)
            try:
                while not emitted:
                    checkpoint()
                    FrameworkUtils.processPendingEventsCurrThread()
                    if emitted:
                        break
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f'Timed out after {timeout:g} s waiting for the '
                                f'signal to be emitted'
                            )
                        time.sleep(min(pollIntervalSeconds, remaining))
                    else:
                        time.sleep(pollIntervalSeconds)
            finally:
                close()

        def close() -> None:
            """ Stops listening without waiting. """
            nonlocal connected
            if connected:
                connected = False
                try:
                    signal.disconnect(setEmitted)
                except (TypeError, RuntimeError):
                    pass  # Already disconnected or the emitter is gone

        wait.close = close
        return wait

    @APIExport()
    def callAndWaitForSignal(self, signal: Signal, func: Callable, *args,
                             timeout: Optional[float] = None, **kwargs) -> Any:
        """ Creates a waiter for ``signal``, then calls ``func(*args,
        **kwargs)``, then waits for the signal. Returns what ``func``
        returned. This is the safe way to call an API function and wait for
        the event it causes, because the waiter exists before the call: a
        signal emitted synchronously inside the call is caught too.

        Example: ``callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
        api.imcontrol.stopRecording, timeout=60)``. Raises ``TimeoutError``
        after ``timeout`` seconds and ``OperationCancelled`` if the script is
        stopped. """
        wait = self.getWaitForSignal(signal, timeout=timeout)
        try:
            result = func(*args, **kwargs)
        except BaseException:
            wait.close()
            raise
        wait()
        return result

    @APIExport()
    def sleep(self, seconds: float) -> None:
        """ Sleeps for the specified number of seconds. Unlike ``time.sleep``,
        this returns immediately (raising ``OperationCancelled``) when the
        script is stopped. """
        cancellableSleep(seconds)

    @APIExport()
    def waitUntil(self, predicate: Callable[[], bool],
                  timeout: Optional[float] = None,
                  pollIntervalSeconds: float = 0.05) -> None:
        """ Waits until ``predicate()`` returns a true value. Raises
        ``TimeoutError`` after ``timeout`` seconds (``None`` waits
        indefinitely) and ``OperationCancelled`` if the script is stopped. """
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            checkpoint()
            if predicate():
                return
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f'Timed out after {timeout:g} s waiting for the condition'
                    )
                time.sleep(min(pollIntervalSeconds, remaining))
            else:
                time.sleep(pollIntervalSeconds)


def getActionsScope(otherScope, scriptPath=None):
    """ Returns the script scope for the actions. """
    return generateAPI([_Actions(otherScope, scriptPath)])._asdict()


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
