import functools
import inspect
import threading

from qtpy import QtCore

from imswitch.imcommon.framework import Signal, SignalInterface
from .cancellation import OperationCancelled, currentCancelToken


class APIExport:
    """ Decorator for methods that should be exported to API. """

    def __init__(self, *, runOnUIThread=False):
        self._APIExport = True
        self._APIRunOnUIThread = runOnUIThread

    def __call__(self, func):
        func._APIExport = self._APIExport
        func._APIRunOnUIThread = self._APIRunOnUIThread
        return func


class _ApiGate:
    """Process-wide switch that refuses API calls once shutdown has begun.

    Closing the gate is a fail-fast line for callers, not hardware
    isolation: that comes from draining the threads that can hold hardware
    references (see ``imcommon.model.shutdown``)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = True
        self._reason = ''

    def isOpen(self):
        return self._open

    def close(self, reason='ImSwitch is shutting down'):
        with self._lock:
            self._open = False
            self._reason = reason

    def reopen(self):
        """For tests and for embedding hosts that restart the API."""
        with self._lock:
            self._open = True
            self._reason = ''

    def checkOpen(self):
        if not self._open:
            raise RuntimeError(f'API call refused: {self._reason}')


apiGate = _ApiGate()


def generateAPI(objs, *, missingAttributeErrorMsg=None):
    """ Generates an API from APIExport-decorated methods in the objects in the
    passed array objs. Must be called from the main thread. """

    from imswitch.imcommon.model import pythontools

    exportedFuncs = {}
    for obj in objs:
        for subObjName in dir(obj):
            subObj = getattr(obj, subObjName)
            if not callable(subObj):
                continue

            if not hasattr(subObj, '_APIExport') or not subObj._APIExport:
                continue

            if subObjName in exportedFuncs:
                raise NameError(f'API method name "{subObjName}" is already in use')

            runOnUIThread = hasattr(subObj, '_APIRunOnUIThread') and subObj._APIRunOnUIThread

            if runOnUIThread:
                exportedFuncs[subObjName] = _UIThreadExecWrapper(subObj)
            else:
                exportedFuncs[subObjName] = _GatedExport(subObj)

    return pythontools.dictToROClass(exportedFuncs,
                                     missingAttributeErrorMsg=missingAttributeErrorMsg)


def _copyMetadata(wrapper, apiFunc):
    functools.update_wrapper(wrapper, apiFunc)
    wrapper.__signature__ = inspect.signature(apiFunc)
    wrapper.module = apiFunc.__module__.split('.')[-1]


class _GatedExport:
    """Export that runs on the caller's thread, refused once the API gate is
    closed. Preserves the wrapped method's metadata for docs and servers."""

    def __init__(self, apiFunc):
        self._apiFunc = apiFunc
        _copyMetadata(self, apiFunc)

    def __call__(self, *args, **kwargs):
        apiGate.checkOpen()
        return self._apiFunc(*args, **kwargs)


class _Invocation:
    """One call handed from a caller thread to the GUI thread.

    States: PENDING (queued) → RUNNING → DONE, or PENDING → CANCELLED (a
    stop was requested before the GUI thread started it) / SKIPPED (the API
    gate closed first). ``epoch`` is 0 for an ordinary call and 1 for a call
    made inside a cancelled run's cleanup window; the run's cancel token
    decides admission on the GUI thread so that GUI-thread order is total.
    """

    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    CANCELLED = 'cancelled'
    SKIPPED = 'skipped'

    __slots__ = ('args', 'kwargs', 'token', 'epoch', 'state', 'result',
                 'exception', 'done', 'lock')

    def __init__(self, args, kwargs, token=None):
        self.args = args
        self.kwargs = kwargs
        self.token = token
        self.epoch = 0
        self.state = self.PENDING
        self.result = None
        self.exception = None
        self.done = threading.Event()
        self.lock = threading.Lock()

    def cancelPending(self):
        """Caller-side or stop-side: withdraw the call if not started yet."""
        with self.lock:
            if self.state != self.PENDING:
                return False
            self.state = self.CANCELLED
            self.done.set()
            return True

    def start(self):
        """GUI thread: decide whether the queued call may run."""
        with self.lock:
            if self.state != self.PENDING:
                return False
            if not apiGate.isOpen():
                self.state = self.SKIPPED
                self.done.set()
                return False
            if self.token is not None and not self.token.admit(self):
                self.state = self.CANCELLED
                self.done.set()
                return False
            self.state = self.RUNNING
            return True

    def finish(self):
        with self.lock:
            if self.state == self.RUNNING:
                self.state = self.DONE
            self.done.set()

    def waitDone(self, pollIntervalS=0.05):
        """Caller thread: block until the GUI thread is done with the call,
        honouring the run's cancel token while waiting."""
        token = self.token
        while not self.done.wait(pollIntervalS):
            if token is None:
                continue
            state = token.state
            if state == token.REQUESTED and self.epoch == 0:
                self.cancelPending()
                token.checkpoint()  # raises OperationCancelled (delivery)
            elif state == token.EXPIRED:
                self.cancelPending()
                token.checkpoint()  # raises
        if self.state == self.SKIPPED:
            raise RuntimeError('API call refused: ImSwitch is shutting down')
        if self.state == self.CANCELLED:
            if token is not None:
                token.checkpoint()
            raise OperationCancelled('API call cancelled')
        if token is not None:
            # A stop requested while the call was running is delivered now,
            # after the GUI-thread side effect has completed.
            token.checkpoint()


class _UIThreadExecWrapper(SignalInterface):
    """ Wrapper for executing the specified function on the UI thread.

    Called from the UI thread it executes the function directly. Called from
    any other thread it queues the call to the UI thread and **blocks until it
    has run**, returning its result or re-raising its exception in the
    caller's thread. A caller inside a cancellable run (a script) observes
    that run's cancel token while blocked; a stop requested before the GUI
    thread starts the call withdraws it so that nothing dispatched after
    Stop executes. """

    _sigInvoke = Signal(object)

    def __init__(self, apiFunc):
        super().__init__()
        self._apiFunc = apiFunc
        _copyMetadata(self, apiFunc)
        self._sigInvoke.connect(self._invoke)

    def __call__(self, *args, **kwargs):
        apiGate.checkOpen()
        if QtCore.QThread.currentThread() is self.thread():
            return self._apiFunc(*args, **kwargs)

        token = currentCancelToken()
        invocation = _Invocation(args, kwargs, token)
        if token is not None:
            token.openInvocation(invocation)  # may raise: nothing dispatched
        try:
            self._sigInvoke.emit(invocation)
            invocation.waitDone()
        finally:
            if token is not None:
                token.closeInvocation(invocation)
        if invocation.exception is not None:
            raise invocation.exception
        return invocation.result

    def _invoke(self, invocation):
        if not invocation.start():
            return
        try:
            invocation.result = self._apiFunc(*invocation.args, **invocation.kwargs)
        except BaseException as error:  # noqa: BLE001 - re-raised in the caller
            invocation.exception = error
        finally:
            invocation.finish()


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
