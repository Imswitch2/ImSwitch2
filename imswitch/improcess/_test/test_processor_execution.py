"""A processor run that can be cancelled and can stream its output (Qt-free)."""

import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model import CancelToken, OperationCancelled, checkpoint, currentCancelToken  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.processors.execution import RunOutcome, execute_run  # noqa: E402


def _input(name="in"):
    return ArrayProcessingResult(name, np.zeros((2, 4, 4), np.float32), ["Z", "Y", "X"])


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


def _made(result, params):
    return ArrayProcessingResult(f"{result.name}_done", np.ones((4, 4), np.float32), ["Y", "X"])


def test_a_finished_run_returns_what_run_processor_returns():
    logger = _Logger()
    outcome = execute_run(_Processor(_made), [_input("a"), _input("b")], {}, logger)
    assert isinstance(outcome, RunOutcome) and outcome.cancelled is False and outcome.failures == []
    assert [r.name for r in outcome.results] == ["a_done", "b_done"]


def test_a_failure_is_reported_as_a_failure_not_a_cancellation():
    def boom(result, params):
        raise RuntimeError("no good")

    logger = _Logger()
    outcome = execute_run(_Processor(boom), [_input()], {}, logger)
    assert outcome.cancelled is False and outcome.results == []
    assert [message for _input_, message in outcome.failures] == ["no good"] and logger.exceptions


def test_the_token_is_published_to_the_run_and_withdrawn_afterwards():
    seen = []
    token = CancelToken()

    def look(result, params):
        seen.append(currentCancelToken())
        return _made(result, params)

    execute_run(_Processor(look), [_input()], {}, _Logger(), token=token)
    assert seen == [token] and currentCancelToken() is None

    def fail(result, params):
        raise ValueError("x")

    execute_run(_Processor(fail), [_input()], {}, _Logger(), token=token)
    assert currentCancelToken() is None                      # even when the run failed


def test_a_processor_that_calls_checkpoint_stops_when_asked_and_nothing_is_kept():
    token = CancelToken()
    at_loop = threading.Event()
    done = []

    def loop(result, params):
        at_loop.set()
        for _ in range(1000):
            checkpoint()
            time.sleep(0.01)
        return _made(result, params)

    def run():
        done.append(execute_run(_Processor(loop), [_input()], {}, _Logger(), token=token))

    thread = threading.Thread(target=run)
    thread.start()
    at_loop.wait(5)
    token.requestStop()
    thread.join(5)
    assert not thread.is_alive()
    assert done == [RunOutcome(results=[], failures=[], cancelled=True)]


def test_one_cancelled_input_discards_the_whole_batch_not_just_its_own_result():
    calls = []

    def second_one_is_cancelled(result, params):
        calls.append(result.name)
        if result.name == "second":
            raise OperationCancelled("stop")
        return _made(result, params)

    outcome = execute_run(_Processor(second_one_is_cancelled), [_input("first"), _input("second"), _input("third")], {}, _Logger())
    assert calls == ["first", "second"]                      # the third never ran
    assert outcome == RunOutcome(cancelled=True)


def test_a_run_that_finished_after_being_asked_to_stop_is_still_reported_cancelled():
    token = CancelToken()

    def finishes_anyway(result, params):
        token.requestStop()
        return _made(result, params)

    outcome = execute_run(_Processor(finishes_anyway), [_input()], {}, _Logger(), token=token)
    assert outcome.cancelled is True and outcome.results == []


def test_output_is_streamed_from_the_run_thread_only_and_only_when_asked(capsys):
    chunks = []

    def chatty(result, params):
        print("hello", end="")
        print(" world")
        return _made(result, params)

    outcome = execute_run(_Processor(chatty), [_input()], {}, _Logger(), on_output=chunks.append)
    assert "".join(chunks) == "hello world\n" and outcome.results
    assert capsys.readouterr().out == ""                     # captured, not printed

    execute_run(_Processor(chatty), [_input()], {}, _Logger())      # no callback: it goes where it always did
    assert capsys.readouterr().out == "hello world\n"

    other = []

    def quiet(result, params):
        t = threading.Thread(target=lambda: print("from elsewhere"))
        t.start()
        t.join()
        return _made(result, params)

    execute_run(_Processor(quiet), [_input()], {}, _Logger(), on_output=other.append)
    assert other == [] and "from elsewhere" in capsys.readouterr().out


def test_a_multi_input_processor_gets_every_input_in_params():
    seen = {}

    def gather(result, params):
        seen["names"] = [r.name for r in params["results"]]
        return _made(result, params)

    processor = _Processor(gather)
    processor.max_inputs = None
    outcome = execute_run(processor, [_input("a"), _input("b")], {}, _Logger())
    assert seen["names"] == ["a", "b"] and len(outcome.results) == 1


def test_the_python_step_streams_while_it_runs_and_is_cancelled_by_its_token():
    from imswitch.improcess.processors.python_step import PythonStepProcessor

    token = CancelToken()
    chunks = []
    started = threading.Event()

    def on_output(text):
        chunks.append(text)
        started.set()

    code = "import time\nprint('started', flush=True)\nfor i in range(10000):\n    time.sleep(0.01)\nout = data\n"
    holder = []
    thread = threading.Thread(target=lambda: holder.append(
        execute_run(PythonStepProcessor(), [_input()], {"code": code, "ports": "out"}, _Logger(),
                    token=token, on_output=on_output)))
    thread.start()
    assert started.wait(5) and "".join(chunks) == "started\n"      # seen before the script is done
    token.requestStop()
    from imswitch.imcommon.model import interruptThread

    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        interruptThread(thread.ident)                                # the script never reaches a checkpoint
        thread.join(0.1)
    assert not thread.is_alive()
    assert holder == [RunOutcome(cancelled=True)]


def test_the_frameworks_own_failure_log_is_not_captured_into_the_streamed_output(capsys):
    import sys

    class LoudLogger:
        def exception(self, message, *args):
            print("LOG:", message % args if args else message, file=sys.stderr)

    def boom(result, params):
        print("the processor's own line")
        raise RuntimeError("no good")

    chunks = []
    outcome = execute_run(_Processor(boom), [_input()], {}, LoudLogger(), on_output=chunks.append)
    assert "".join(chunks) == "the processor's own line\n"         # the log line is not in it
    assert "LOG:" in capsys.readouterr().err                       # it went where logs go
    assert [m for _i, m in outcome.failures] == ["no good"]
