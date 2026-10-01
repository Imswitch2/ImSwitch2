"""Stop semantics of ScriptExecutor: non-blocking cancel, one-shot delivery,
cleanup window, budget expiry, escalation, deferred re-run, bounded shutdown.

All scripts here use ``cancellableSleep`` (the same cooperative primitive the
scripting ``sleep``/``getWaitForSignal`` actions use) or deliberately
non-cooperative sleeps/loops to exercise escalation.
"""
import time

import pytest

from imswitch.imcommon.model import cancellableSleep
from imswitch.imscripting.model import ScriptExecutor, ScriptRunStatus


@pytest.fixture
def marks():
    return []


@pytest.fixture
def executor(qtbot, marks):
    ex = ScriptExecutor(
        scriptScope={'cancellableSleep': cancellableSleep, 'marks': marks},
        cleanupBudgetS=30.0,
        escalateAfterMs=200,
    )
    ex.sigOutputAppended.connect(lambda _t: None)
    yield ex
    ex.shutdown(timeoutMs=4000)


def _wait_finished(qtbot, executor, timeout=5000):
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=timeout) as blocker:
        pass
    return blocker.args[0]


def _wait_until(qtbot, predicate, timeout=5000):
    qtbot.waitUntil(predicate, timeout=timeout)


def test_cancel_returns_immediately_and_the_script_ends_cancelled(qtbot, executor, marks):
    result = executor.execute(None, "marks.append('start')\ncancellableSleep(30)\nmarks.append('never')")
    _wait_until(qtbot, lambda: 'start' in marks)
    t0 = time.monotonic()
    assert executor.cancel() is True
    assert time.monotonic() - t0 < 0.1  # never blocks the caller
    assert executor.isStopping()
    finished = _wait_finished(qtbot, executor)
    assert finished is result
    assert result.status == ScriptRunStatus.CANCELLED
    assert 'never' not in marks
    assert result.cleanup_timed_out is False
    assert not executor.isExecuting()


def test_cancellation_is_delivered_once_and_cleanup_runs_to_completion(qtbot, executor, marks):
    code = """
try:
    marks.append('start')
    cancellableSleep(30)
finally:
    marks.append('cleanup-start')
    cancellableSleep(0.2)          # must NOT raise inside the cleanup window
    marks.append('cleanup-done')
"""
    result = executor.execute(None, code)
    _wait_until(qtbot, lambda: 'start' in marks)
    executor.cancel()
    _wait_finished(qtbot, executor)
    assert marks == ['start', 'cleanup-start', 'cleanup-done']
    assert result.status == ScriptRunStatus.CANCELLED
    assert result.cleanup_timed_out is False


def test_cleanup_budget_expiry_cuts_cleanup_off_and_is_reported(qtbot, marks):
    ex = ScriptExecutor(
        scriptScope={'cancellableSleep': cancellableSleep, 'marks': marks},
        cleanupBudgetS=0.3, escalateAfterMs=200,
    )
    ex.sigOutputAppended.connect(lambda _t: None)
    try:
        code = """
try:
    marks.append('start')
    cancellableSleep(30)
finally:
    marks.append('cleanup-start')
    cancellableSleep(30)           # exceeds the budget: raises again
    marks.append('cleanup-done')
"""
        result = ex.execute(None, code)
        _wait_until(qtbot, lambda: 'start' in marks)
        ex.cancel()
        _wait_finished(qtbot, ex)
        assert marks == ['start', 'cleanup-start']
        assert result.status == ScriptRunStatus.CANCELLED
        assert result.cleanup_timed_out is True
    finally:
        ex.shutdown(timeoutMs=4000)


def test_a_busy_loop_is_stopped_by_escalation(qtbot, executor, marks):
    result = executor.execute(None, "marks.append('start')\nwhile True:\n    pass")
    _wait_until(qtbot, lambda: 'start' in marks)
    executor.cancel()
    _wait_finished(qtbot, executor, timeout=6000)
    assert result.status == ScriptRunStatus.CANCELLED


def test_a_non_cooperative_sleep_ends_when_it_returns(qtbot, executor, marks):
    result = executor.execute(None, "import time\nmarks.append('start')\ntime.sleep(0.6)\nmarks.append('after')")
    _wait_until(qtbot, lambda: 'start' in marks)
    executor.cancel()
    _wait_finished(qtbot, executor, timeout=6000)
    # The injected exception lands when time.sleep returns, before the next line.
    assert result.status == ScriptRunStatus.CANCELLED
    assert 'after' not in marks


def test_run_while_running_defers_the_new_script_until_the_old_one_ended(qtbot, executor, marks):
    results = []
    executor.sigExecutionFinished.connect(results.append)
    first = executor.execute(None, "marks.append('A')\ncancellableSleep(30)")
    _wait_until(qtbot, lambda: 'A' in marks)
    second = executor.execute(None, "marks.append('B')")
    assert second.status == ScriptRunStatus.RUNNING
    _wait_until(qtbot, lambda: len(results) == 2, timeout=6000)
    assert results == [first, second]
    assert first.status == ScriptRunStatus.CANCELLED
    assert second.status == ScriptRunStatus.SUCCEEDED
    assert marks == ['A', 'B']


def test_shutdown_is_bounded_when_the_script_ignores_everything(qtbot, marks):
    ex = ScriptExecutor(
        scriptScope={'cancellableSleep': cancellableSleep, 'marks': marks},
        cleanupBudgetS=30.0, escalateAfterMs=100,
    )
    ex.sigOutputAppended.connect(lambda _t: None)
    # A C-level sleep cannot be interrupted: shutdown must give up in time.
    ex.execute(None, "import time\nmarks.append('start')\ntime.sleep(2.0)")
    _wait_until(qtbot, lambda: 'start' in marks)
    t0 = time.monotonic()
    finished = ex.shutdown(timeoutMs=300)
    elapsed = time.monotonic() - t0
    assert finished is False
    assert elapsed < 1.5
    # The thread ends on its own once the sleep returns.
    _wait_until(qtbot, lambda: not ex.isExecuting(), timeout=6000)
    ex.shutdown(timeoutMs=2000)


def test_shutdown_returns_true_for_a_cooperative_script(qtbot, executor, marks):
    executor.execute(None, "marks.append('start')\ncancellableSleep(30)")
    _wait_until(qtbot, lambda: 'start' in marks)
    assert executor.shutdown(timeoutMs=3000) is True
    assert not executor.isExecuting()


def test_cancel_without_a_running_script_is_a_noop(executor):
    assert executor.cancel() is False
    assert not executor.isStopping()
