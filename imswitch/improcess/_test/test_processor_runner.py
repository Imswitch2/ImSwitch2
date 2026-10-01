"""A panel's processor runs on a thread of its own: it streams, it cancels, it
escalates, and it is stopped at exit (real threads, offscreen)."""

import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model import checkpoint  # noqa: E402
from imswitch.improcess.controller import processor_runner  # noqa: E402
from imswitch.improcess.controller.processor_runner import ProcessorRunner  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.processors.execution import RunOutcome  # noqa: E402


class _Logger:
    def __init__(self):
        self.exceptions = []

    def exception(self, *args):
        self.exceptions.append(args)


class _Processor:
    id = "fake"
    max_inputs = 1

    def __init__(self, apply):
        self._apply = apply

    def apply(self, result, params):
        return self._apply(result, params)


def _input(name="in"):
    return ArrayProcessingResult(name, np.zeros((2, 4, 4), np.float32), ["Z", "Y", "X"])


def _made(result, params=None):
    return ArrayProcessingResult(f"{result.name}_done", np.ones((4, 4), np.float32), ["Y", "X"])


@pytest.fixture
def runner(qtbot):
    made = ProcessorRunner(escalateAfterMs=150, reinjectAfterMs=100)
    yield made
    made.shutdown(3000)


def _start(runner, apply, *, stream=False, inputs=None):
    return runner.start(_Processor(apply), inputs or [_input()], {}, _Logger(), stream_output=stream)


def _finish(qtbot, runner, timeout=5000):
    with qtbot.waitSignal(runner.sigFinished, timeout=timeout) as blocker:
        pass
    return blocker.args[0]


def test_a_run_happens_on_another_thread_and_reports_once_on_this_one(qtbot, runner):
    where = {}

    def apply(result, params):
        where["thread"] = threading.get_ident()
        return _made(result)

    finished = []
    runner.sigFinished.connect(finished.append)
    assert _start(runner, apply) is True
    assert runner.isRunning()
    outcome = _finish(qtbot, runner)
    assert where["thread"] != threading.get_ident()
    assert isinstance(outcome, RunOutcome) and [r.name for r in outcome.results] == ["in_done"]
    assert not runner.isRunning()
    qtbot.wait(100)
    assert len(finished) == 1


def test_a_second_run_is_refused_while_one_is_running_and_allowed_after(qtbot, runner):
    release = threading.Event()

    def slow(result, params):
        release.wait(5)
        return _made(result)

    assert _start(runner, slow) is True
    assert _start(runner, slow) is False
    release.set()
    _finish(qtbot, runner)
    assert _start(runner, lambda r, p: _made(r)) is True
    _finish(qtbot, runner)


def test_a_new_run_may_be_started_from_the_finished_handler(qtbot, runner):
    outcomes = []

    def on_finished(outcome):
        outcomes.append(outcome)
        if len(outcomes) == 1:
            assert runner.isRunning() is False
            assert _start(runner, lambda r, p: _made(r)) is True

    runner.sigFinished.connect(on_finished)
    _start(runner, lambda r, p: _made(r))
    qtbot.waitUntil(lambda: len(outcomes) == 2, timeout=5000)


def test_a_failure_is_delivered_as_a_failure_and_frees_the_runner(qtbot, runner):
    def boom(result, params):
        raise RuntimeError("no good")

    _start(runner, boom)
    outcome = _finish(qtbot, runner)
    assert outcome.cancelled is False and [m for _i, m in outcome.failures] == ["no good"]
    assert not runner.isRunning()


def test_output_is_streamed_while_the_run_is_still_going(qtbot, runner):
    chunks = []
    runner.sigOutput.connect(chunks.append)
    release = threading.Event()

    def chatty(result, params):
        print("first line", flush=True)
        release.wait(5)
        print("second line")
        return _made(result)

    _start(runner, chatty, stream=True)
    qtbot.waitUntil(lambda: "".join(chunks) == "first line\n", timeout=5000)
    assert runner.isRunning()                                # seen before the run is over
    release.set()
    _finish(qtbot, runner)
    qtbot.waitUntil(lambda: "".join(chunks) == "first line\nsecond line\n", timeout=2000)


def test_output_is_not_captured_unless_asked_for(qtbot, runner, capsys):
    chunks = []
    runner.sigOutput.connect(chunks.append)
    _start(runner, lambda r, p: (print("to the terminal"), _made(r))[1])
    _finish(qtbot, runner)
    assert chunks == [] and "to the terminal" in capsys.readouterr().out


def test_cancel_reaches_a_processor_that_calls_checkpoint(qtbot, runner):
    at_loop = threading.Event()

    def loop(result, params):
        at_loop.set()
        while True:
            checkpoint()
            time.sleep(0.005)

    _start(runner, loop)
    assert at_loop.wait(5)
    assert runner.cancel() is True and runner.isStopping()
    outcome = _finish(qtbot, runner, timeout=3000)
    assert outcome == RunOutcome(cancelled=True)


def test_cancel_interrupts_a_processor_that_never_checks(qtbot, runner):
    at_loop = threading.Event()

    def spin(result, params):
        at_loop.set()
        while True:
            pass

    _start(runner, spin)
    assert at_loop.wait(5)
    started = time.monotonic()
    runner.cancel()
    outcome = _finish(qtbot, runner, timeout=5000)
    assert outcome.cancelled is True and outcome.results == []
    assert time.monotonic() - started >= 0.15               # not before the grace period


def test_a_processor_that_catches_the_interruption_is_interrupted_again(qtbot, runner):
    at_loop, let_go = threading.Event(), threading.Event()
    caught = []

    def stubborn(result, params):
        at_loop.set()
        while not let_go.is_set():
            try:
                while not let_go.is_set():
                    pass
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                caught.append(type(exc).__name__)
        return _made(result)

    before = len(processor_runner._ORPHANED)
    _start(runner, stubborn)
    assert at_loop.wait(5)
    runner.cancel()
    qtbot.waitUntil(lambda: len(caught) >= 2, timeout=5000)      # interrupted, caught, interrupted again
    assert set(caught) == {"OperationCancelled"}
    # it will not stop, so exit gives up on it: parked, and no longer held against the next run
    assert runner.shutdown(200) is False
    assert runner.isRunning() is False
    assert len(processor_runner._ORPHANED) == before + 1
    let_go.set()                                                 # now it ends, and the parked pair is released
    qtbot.waitUntil(lambda: len(processor_runner._ORPHANED) == before, timeout=5000)


def test_cancel_with_nothing_running_is_a_no_op(runner):
    assert runner.cancel() is False and runner.isStopping() is False


def test_shutdown_stops_a_runaway_processor_and_returns_true(qtbot):
    made = ProcessorRunner(escalateAfterMs=100)
    at_loop = threading.Event()

    def spin(result, params):
        at_loop.set()
        while True:
            pass

    made.start(_Processor(spin), [_input()], {}, _Logger())
    assert at_loop.wait(5)
    assert made.shutdown(3000) is True
    assert made.isRunning() is False


def test_shutdown_parks_a_thread_that_will_not_stop_and_says_so(qtbot):
    made = ProcessorRunner(escalateAfterMs=50)
    release = threading.Event()
    blocked = threading.Event()

    def stuck(result, params):
        blocked.set()
        release.wait(10)                                    # a C-level wait: no bytecode to interrupt
        return _made(result)

    made.start(_Processor(stuck), [_input()], {}, _Logger())
    assert blocked.wait(5)
    before = len(processor_runner._ORPHANED)
    assert made.shutdown(300) is False
    assert len(processor_runner._ORPHANED) == before + 1
    release.set()                                           # it ends; the parked pair is released
    qtbot.waitUntil(lambda: len(processor_runner._ORPHANED) == before, timeout=5000)


def test_the_python_step_streams_and_is_cancelled_through_the_runner(qtbot, runner):
    from imswitch.improcess.processors.python_step import PythonStepProcessor

    chunks = []
    runner.sigOutput.connect(chunks.append)
    code = "import time\nprint('working', flush=True)\nwhile True:\n    time.sleep(0.005)\n"
    runner.start(PythonStepProcessor(), [_input()], {"code": code, "ports": "out"}, _Logger(), stream_output=True)
    qtbot.waitUntil(lambda: "".join(chunks) == "working\n", timeout=5000)
    runner.cancel()
    outcome = _finish(qtbot, runner, timeout=5000)
    assert outcome == RunOutcome(cancelled=True)
