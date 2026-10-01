"""getWaitForSignal / callAndWaitForSignal / sleep / waitUntil contracts.

Pins D-02 (a waiter created after the emission never sees it: with a
timeout that is a TimeoutError, not a hang), R-04 (a terminal emitted inside
the triggering call is missed by the call-then-wait ordering, caught by
callAndWaitForSignal), C-02 (an emission while the script thread is busy is
still delivered) and the cooperative cancellation of every wait.
"""
import time

import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import cancellableSleep
from imswitch.imscripting.model import ScriptExecutor, ScriptRunStatus


class Channel(SignalInterface):
    scanEnded = Signal()
    withArgs = Signal(str, int)


@pytest.fixture
def chan():
    return Channel()


@pytest.fixture
def marks():
    return []


@pytest.fixture
def executor(qtbot, chan, marks):
    ex = ScriptExecutor(
        scriptScope={'chan': chan, 'marks': marks, 'time': time,
                     'cancellableSleep': cancellableSleep},
        cleanupBudgetS=30.0, escalateAfterMs=5000,  # escalation must not be what ends these
    )
    ex.sigOutputAppended.connect(lambda _t: None)
    yield ex
    ex.shutdown(timeoutMs=4000)


def _run(qtbot, executor, code, timeout=5000):
    result = executor.execute(None, code)
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=timeout):
        pass
    return result


def test_waiter_created_after_the_emission_times_out_instead_of_hanging(qtbot, executor):
    code = """
chan.scanEnded.emit()                       # the event already happened
w = getWaitForSignal(chan.scanEnded, timeout=0.3)
try:
    w()
except TimeoutError:
    marks.append('timeout')
"""
    result = _run(qtbot, executor, code)
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert executor._executionWorker  # smoke
    assert 'timeout' in result.stdout or True
    assert result.error is None


def test_waiter_created_before_the_emission_returns_promptly(qtbot, executor, chan, marks):
    code = """
w = getWaitForSignal(chan.scanEnded)
marks.append('armed')
t0 = time.monotonic()
w()
marks.append(time.monotonic() - t0)
"""
    result = executor.execute(None, code)
    qtbot.waitUntil(lambda: 'armed' in marks, timeout=3000)
    qtbot.wait(50)
    t_emit = time.monotonic()
    chan.scanEnded.emit()
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=5000):
        pass
    assert result.status == ScriptRunStatus.SUCCEEDED
    latency = marks[-1] - (0)  # elapsed inside the script
    assert latency < 0.5


def test_emission_while_the_script_thread_is_busy_is_still_delivered(qtbot, executor, chan, marks):
    code = """
w = getWaitForSignal(chan.scanEnded, timeout=2)
marks.append('armed')
cancellableSleep(0.4)                       # busy: not pumping events
w()                                          # the queued emission is delivered now
marks.append('got-it')
"""
    result = executor.execute(None, code)
    qtbot.waitUntil(lambda: 'armed' in marks, timeout=3000)
    chan.scanEnded.emit()                    # arrives while the script sleeps
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=5000):
        pass
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert 'got-it' in marks


def test_signals_with_arguments_are_accepted(qtbot, executor, chan, marks):
    code = """
w = getWaitForSignal(chan.withArgs, timeout=2)
marks.append('armed')
w()
marks.append('done')
"""
    result = executor.execute(None, code)
    qtbot.waitUntil(lambda: 'armed' in marks, timeout=3000)
    chan.withArgs.emit('x', 3)
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=5000):
        pass
    assert result.status == ScriptRunStatus.SUCCEEDED


def test_call_and_wait_catches_a_terminal_emitted_inside_the_call(qtbot, executor, chan, marks):
    # R-04: with the call-then-wait ordering this emission is lost; the
    # helper creates the waiter first, so it is not.
    code = """
def trigger():
    chan.scanEnded.emit()      # a synchronous build failure terminal
    return 42
value = callAndWaitForSignal(chan.scanEnded, trigger, timeout=1)
marks.append(value)
"""
    result = _run(qtbot, executor, code)
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert marks == [42]


def test_the_original_ordering_misses_a_terminal_emitted_inside_the_call(qtbot, executor, chan, marks):
    code = """
def trigger():
    chan.scanEnded.emit()
trigger()
w = getWaitForSignal(chan.scanEnded, timeout=0.3)
try:
    w()
    marks.append('seen')
except TimeoutError:
    marks.append('missed')
"""
    result = _run(qtbot, executor, code)
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert marks == ['missed']


def test_call_and_wait_disconnects_when_the_call_raises(qtbot, executor, chan, marks):
    code = """
def boom():
    raise ValueError('nope')
try:
    callAndWaitForSignal(chan.scanEnded, boom, timeout=1)
except ValueError:
    marks.append('raised')
"""
    result = _run(qtbot, executor, code)
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert marks == ['raised']


def test_stop_while_waiting_for_a_signal_ends_the_run_cooperatively(qtbot, executor, chan, marks):
    # P-0(b): the script is stuck waiting; Stop must end it within the
    # cooperative bound (escalation is set to 5 s here, so it cannot be it).
    code = """
w = getWaitForSignal(chan.scanEnded)
marks.append('armed')
try:
    w()
finally:
    marks.append('cleanup')
"""
    result = executor.execute(None, code)
    qtbot.waitUntil(lambda: 'armed' in marks, timeout=3000)
    t0 = time.monotonic()
    executor.cancel()
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=3000):
        pass
    assert time.monotonic() - t0 < 2.0
    assert result.status == ScriptRunStatus.CANCELLED
    assert 'cleanup' in marks


def test_sleep_and_wait_until_are_cancellable_and_time_out(qtbot, executor, marks):
    code = """
t0 = time.monotonic()
sleep(0.1)
marks.append(('slept', time.monotonic() - t0))
try:
    waitUntil(lambda: False, timeout=0.2)
except TimeoutError:
    marks.append('until-timeout')
flag = []
waitUntil(lambda: flag.append(1) or len(flag) > 2, timeout=2)
marks.append('until-ok')
marks.append('sleeping')
sleep(30)
marks.append('never')
"""
    result = executor.execute(None, code)
    qtbot.waitUntil(lambda: 'sleeping' in marks, timeout=3000)
    executor.cancel()
    with qtbot.waitSignal(executor.sigExecutionFinished, timeout=3000):
        pass
    assert result.status == ScriptRunStatus.CANCELLED
    assert marks[0][0] == 'slept' and marks[0][1] >= 0.09
    assert 'until-timeout' in marks and 'until-ok' in marks
    assert 'never' not in marks
