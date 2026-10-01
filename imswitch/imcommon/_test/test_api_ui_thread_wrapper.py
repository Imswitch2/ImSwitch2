"""The generated API's UI-thread wrapper: synchronous, cancellable, gated.

Pins plan D-05 (return values and exceptions used to be discarded), R-01/R-08
(no call dispatched after Stop may execute; the decision is taken on the GUI
thread), and R-10 (the gate covers every export, UI-thread or not).
"""
import inspect
import threading

import pytest
from qtpy import QtCore

from imswitch.imcommon.model import (
    APIExport, CancelToken, OperationCancelled, apiGate, generateAPI,
    clearCurrentCancelToken, setCurrentCancelToken,
)


class Target(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.calls = []

    @APIExport(runOnUIThread=True)
    def add(self, a: int, b: int) -> int:
        """Adds."""
        self.calls.append(('add', threading.get_ident()))
        return a + b

    @APIExport(runOnUIThread=True)
    def boom(self) -> None:
        raise ValueError('boom')

    @APIExport(runOnUIThread=True)
    def stopMyself(self, token) -> int:
        # A re-entrant Stop while the call runs (e.g. a dialog pumping events).
        self.calls.append(('stopMyself', threading.get_ident()))
        token.requestStop()
        return 42

    @APIExport()
    def raw(self, x: int) -> int:
        """Doubles."""
        self.calls.append(('raw', threading.get_ident()))
        return 2 * x


@pytest.fixture
def target(qtbot):
    apiGate.reopen()
    t = Target()
    yield t
    apiGate.reopen()
    clearCurrentCancelToken()


@pytest.fixture
def api(target):
    return generateAPI([target])


def _wait_without_pumping(predicate, timeout=3.0):
    """Poll without processing Qt events, so a queued invocation stays queued."""
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not met in time')
        time.sleep(0.005)


class _Worker:
    """Runs a callable on a plain thread, optionally inside a cancel token."""

    def __init__(self, fn, token=None):
        self.fn = fn
        self.token = token
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        if self.token is not None:
            setCurrentCancelToken(self.token)
        try:
            self.result = self.fn()
        except BaseException as error:  # noqa: BLE001 - inspected by the test
            self.error = error
        finally:
            clearCurrentCancelToken()

    def start(self):
        self.thread.start()
        return self

    def join(self, qtbot, timeout=3000):
        qtbot.waitUntil(lambda: not self.thread.is_alive(), timeout=timeout)
        return self


def test_return_value_and_gui_thread_execution_from_a_worker(qtbot, target, api):
    gui = threading.get_ident()
    w = _Worker(lambda: api.add(2, 3)).start().join(qtbot)
    assert w.error is None
    assert w.result == 5
    assert target.calls == [('add', gui)]


def test_exceptions_are_reraised_in_the_caller(qtbot, target, api):
    w = _Worker(lambda: api.boom()).start().join(qtbot)
    assert isinstance(w.error, ValueError)
    assert str(w.error) == 'boom'


def test_direct_call_when_already_on_the_gui_thread(qtbot, target, api):
    assert api.add(1, 1) == 2
    with pytest.raises(ValueError):
        api.boom()
    assert target.calls[0][1] == threading.get_ident()


def test_calls_from_one_thread_execute_in_order(qtbot, target, api):
    seen = []

    def sequence():
        for i in range(5):
            seen.append(api.add(i, 0))
        return seen

    w = _Worker(sequence).start().join(qtbot)
    assert w.error is None
    assert w.result == [0, 1, 2, 3, 4]


def test_a_call_queued_before_stop_never_runs_when_stop_is_handled_first(qtbot, target, api):
    # R-08: the GUI thread handles Stop, then dispatches the queued call,
    # all before the worker's poll notices anything.
    token = CancelToken()
    w = _Worker(lambda: api.add(1, 2), token).start()
    _wait_without_pumping(lambda: token._inflight is not None)  # emitted, blocked in waitDone
    token.requestStop()                 # GUI thread, synchronously cancels the pending call
    qtbot.wait(150)                     # now the queued invocation is delivered → skipped
    w.join(qtbot)
    assert target.calls == []           # the function never executed
    assert isinstance(w.error, OperationCancelled)
    assert token.state == CancelToken.DELIVERED


def test_a_call_made_after_stop_is_refused_before_dispatch(qtbot, target, api):
    token = CancelToken()
    token.requestStop()
    w = _Worker(lambda: api.add(1, 2), token).start().join(qtbot)
    assert target.calls == []
    assert isinstance(w.error, OperationCancelled)


def test_cleanup_calls_run_inside_the_cleanup_window(qtbot, target, api):
    token = CancelToken()
    token.requestStop()

    def cleanup():
        try:
            token.checkpoint()          # first delivery
        except OperationCancelled:
            pass
        return api.add(20, 22)          # epoch-1 call: admitted

    w = _Worker(cleanup, token).start().join(qtbot)
    assert w.error is None
    assert w.result == 42


def test_calls_are_refused_once_the_cleanup_budget_expired(qtbot, target, api):
    token = CancelToken(cleanupBudgetS=0.0)
    token.requestStop()
    try:
        token.checkpoint()
    except OperationCancelled:
        pass
    w = _Worker(lambda: api.add(1, 2), token).start().join(qtbot)
    assert target.calls == []
    assert isinstance(w.error, OperationCancelled)


def test_a_stop_during_a_running_call_lets_it_finish_then_cancels_the_caller(qtbot, target, api):
    token = CancelToken()
    w = _Worker(lambda: api.stopMyself(token), token).start().join(qtbot)
    assert [c[0] for c in target.calls] == ['stopMyself']  # ran to completion
    assert isinstance(w.error, OperationCancelled)          # result discarded, caller cancelled
    assert w.result is None


def test_closed_gate_refuses_new_calls_and_skips_pending_ones(qtbot, target, api):
    token = CancelToken()
    w = _Worker(lambda: api.add(1, 2), token).start()
    _wait_without_pumping(lambda: token._inflight is not None)  # queued, not yet delivered
    apiGate.close()
    qtbot.wait(150)                     # delivered → skipped, never executed
    w.join(qtbot)
    assert target.calls == []
    assert isinstance(w.error, RuntimeError)
    with pytest.raises(RuntimeError):
        api.add(1, 2)                   # direct path
    with pytest.raises(RuntimeError):
        api.raw(3)                      # non-UI export is gated too
    apiGate.reopen()
    assert api.raw(3) == 6


def test_non_ui_exports_run_on_the_caller_thread(qtbot, target, api):
    w = _Worker(lambda: api.raw(4)).start().join(qtbot)
    assert w.result == 8
    assert target.calls == [('raw', w.thread.ident)]


def test_wrappers_keep_metadata_for_docs_and_servers(target, api):
    assert api.add.__name__ == 'add'
    assert api.add.__doc__ == 'Adds.'
    assert str(inspect.signature(api.add)) == '(a: int, b: int) -> int'
    assert api.add.module == 'test_api_ui_thread_wrapper'
    assert api.raw.__name__ == 'raw'
    assert str(inspect.signature(api.raw)) == '(x: int) -> int'
    assert api.raw.module == 'test_api_ui_thread_wrapper'
    assert getattr(api.add, '_APIRunOnUIThread') is True
    assert getattr(api.raw, '_APIRunOnUIThread') is False
