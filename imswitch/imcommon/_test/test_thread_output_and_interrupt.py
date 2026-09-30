"""Per-thread output routing and asynchronous thread interruption (no Qt)."""
import io
import sys
import threading
import time

import pytest

from imswitch.imcommon.model import (
    OperationCancelled, ThreadRoutingStream, interruptThread, routeThisThreadsOutputTo,
)


@pytest.fixture
def restore_streams():
    out, err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = out, err


def test_a_thread_routes_its_own_output_and_nobody_elses(restore_streams):
    foreign = io.StringIO()
    sys.stdout = foreign
    mine = io.StringIO()
    other_sink = io.StringIO()
    started, release = threading.Event(), threading.Event()

    def other():
        with routeThisThreadsOutputTo(other_sink):
            print("from the other thread")
            started.set()
            release.wait(5)

    thread = threading.Thread(target=other)
    with routeThisThreadsOutputTo(mine):
        thread.start()
        started.wait(5)
        print("from this thread")
        sys.stderr.write("an error line\n")
        release.set()
        thread.join(5)
    print("after the block")
    assert mine.getvalue() == "from this thread\nan error line\n"
    assert other_sink.getvalue() == "from the other thread\n"
    assert foreign.getvalue() == "after the block\n"      # only what nobody routed reaches the real stream


def test_the_router_wraps_a_foreign_stream_once_and_delegates_to_it(restore_streams):
    foreign = io.StringIO()
    sys.stdout = foreign
    with routeThisThreadsOutputTo(io.StringIO()):
        first = sys.stdout
    assert isinstance(first, ThreadRoutingStream)
    with routeThisThreadsOutputTo(io.StringIO()):
        assert sys.stdout is first                          # not wrapped again
    first.write("unrouted")
    first.writelines(["a", "b"])
    first.flush()
    assert foreign.getvalue() == "unroutedab"
    assert first.getvalue() == "unroutedab"                 # attributes come from the wrapped stream


def test_a_route_ends_with_its_block_even_when_the_block_raises(restore_streams):
    sink = io.StringIO()
    with pytest.raises(RuntimeError):
        with routeThisThreadsOutputTo(sink):
            print("kept")
            raise RuntimeError("boom")
    print("not kept")
    assert sink.getvalue() == "kept\n"


def test_interrupting_a_thread_stuck_in_a_python_loop_raises_in_that_thread_only():
    outcome = {}
    ready = threading.Event()

    def spin():
        ready.set()
        try:
            while True:
                pass
        except OperationCancelled:
            outcome["caught"] = True

    bystander_done = threading.Event()
    bystander = threading.Thread(target=lambda: (bystander_done.wait(5), outcome.setdefault("bystander", "untouched")))
    thread = threading.Thread(target=spin)
    bystander.start()
    thread.start()
    ready.wait(5)
    assert interruptThread(thread.ident) is True
    thread.join(5)
    bystander_done.set()
    bystander.join(5)
    assert outcome == {"caught": True, "bystander": "untouched"} and not thread.is_alive()


def test_interrupting_a_thread_that_does_not_exist_is_refused():
    assert interruptThread(2**40) is False


def test_the_exception_to_inject_can_be_chosen():
    outcome = {}
    ready = threading.Event()

    def spin():
        ready.set()
        try:
            time.sleep(0.5)
            while True:
                pass
        except KeyboardInterrupt:
            outcome["kind"] = "KeyboardInterrupt"
        except BaseException as exc:  # noqa: BLE001
            outcome["kind"] = type(exc).__name__

    thread = threading.Thread(target=spin)
    thread.start()
    ready.wait(5)
    time.sleep(0.05)
    # the injection lands when the thread next runs bytecode; a sleeping thread takes it on waking
    assert interruptThread(thread.ident, KeyboardInterrupt) is True
    thread.join(10)
    assert outcome.get("kind") == "KeyboardInterrupt"


def test_routes_nest_and_the_outer_one_is_put_back(restore_streams):
    from imswitch.imcommon.model import currentRoute

    outer, inner = io.StringIO(), io.StringIO()
    assert currentRoute() is None
    with routeThisThreadsOutputTo(outer):
        print("one")
        assert currentRoute() is outer
        with routeThisThreadsOutputTo(inner):
            print("two")
            assert currentRoute() is inner
        print("three")
        assert currentRoute() is outer
    assert currentRoute() is None
    assert outer.getvalue() == "one\nthree\n" and inner.getvalue() == "two\n"


def test_an_inner_route_can_pass_what_it_captures_to_the_outer_one(restore_streams):
    from imswitch.imcommon.model import currentRoute

    class Tee:
        def __init__(self, *sinks):
            self.sinks = [s for s in sinks if s is not None]

        def write(self, text):
            for sink in self.sinks:
                sink.write(text)
            return len(text)

    outer, captured = io.StringIO(), io.StringIO()
    with routeThisThreadsOutputTo(outer):
        with routeThisThreadsOutputTo(Tee(captured, currentRoute())):
            print("seen by both")
    assert outer.getvalue() == captured.getvalue() == "seen by both\n"
