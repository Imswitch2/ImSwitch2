"""The Fiji session against a protocol-speaking fake host (no Java)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.fiji.convert import PROP_RESULT_UID, import_fiji_image, plan_send
from imswitch.improcess.fiji.protocol import FijiLinkError, HostError, HostLaunchConfig
from imswitch.improcess.fiji.session import (
    KILL_WAIT_S, STOP_HANDOVER_S, FijiDied, FijiSession, FijiStartError,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult

FAKE = "imswitch.improcess._test.fiji_fake_host"
MAX_HEAP_MB = 1234
SHORT_TIMEOUT_S = 1.5       # for scenarios that must time out; the fake otherwise connects in well under a second
SHORT_STOP_S = 0.5
#: How long the fake host lives before it exits by itself ("the user closed Fiji").
SELF_EXIT_DELAY_S = 0.3
#: Generous wait for the watcher to react to that exit.
WATCH_WAIT_S = 30.0
#: How long a fake host that connects late sleeps first: long enough for the
#: test to land a stop (or a stray connection) before it connects.
LATE_CONNECT_S = 3.0
#: What a stop that has to kill a stuck host may take in these tests: the
#: polite wait plus the session's own handover bound.
STOP_BOUND_S = SHORT_STOP_S + KILL_WAIT_S + STOP_HANDOVER_S
#: A stop that kills a stuck host is in practice done in well under this; the
#: documented bound above is only the worst case.
PROMPT_STOP_S = SHORT_STOP_S + 5.0


@pytest.fixture
def config(tmp_path):
    return HostLaunchConfig(fiji_dir=tmp_path, java_home=tmp_path, max_heap_mb=MAX_HEAP_MB, classpath_jars=())


@pytest.fixture
def make(config, tmp_path):
    made = []

    def build(scenario: str = "", **kwargs) -> FijiSession:
        kwargs.setdefault("startup_timeout_s", 60.0)
        session = FijiSession(
            config, host_module=FAKE, temp_root=tmp_path,
            extra_env={"IMSWITCH_FIJI_FAKE": scenario}, **kwargs,
        )
        made.append(session)
        return session

    yield build
    for session in made:
        session.stop(timeout_s=SHORT_STOP_S)
        # whatever a test did, no child may outlive it
        assert session.process is None or session.process.poll() is not None


def _result(shape=(3, 4, 5), labels="ZYX", scales=(0.5, 0.2, 0.1), dtype=np.uint16):
    data = (np.arange(int(np.prod(shape))) % 60000).astype(dtype).reshape(shape)
    return ArrayProcessingResult("src", data, list(labels), axis_scales=list(scales), scale_unit="um")


def test_happy_path_and_clean_stop(make, tmp_path):
    session = make()
    assert session.state == "stopped"
    session.start()
    assert session.state == "ready" and session.error == ""
    assert session.ping_info.max_heap_mb == MAX_HEAP_MB and not session.ping_info.headless
    temp_dir = session.temp_dir
    assert temp_dir.exists()

    plan = plan_send(_result())
    image_id = session.send(plan)
    assert image_id < 0
    assert list(temp_dir.glob("*.npy")) == []            # the send file is gone
    listed = session.list_images()
    assert [d.image_id for d in listed] == [image_id] and listed[0].dims_xyczt == (5, 4, 1, 3, 1)
    data, desc = session.fetch(image_id)
    assert np.array_equal(data, plan.data) and desc.image_id == image_id
    assert list(temp_dir.glob("*.npy")) == []

    dup = session.run_command(image_id, "Duplicate...", "title=dup")
    assert dup == image_id - 1
    kinds = [(e["kind"], e.get("command") or e.get("id")) for e in session.events(clear=True)["events"]]
    assert ("command", "Duplicate...") in kinds and ("opened", dup) in kinds
    assert session.events()["events"] == []
    session.close_image(dup)
    assert [d.image_id for d in session.list_images()] == [image_id]

    proc = session.process
    session.stop()
    assert session.state == "stopped" and proc.poll() == 0 and not temp_dir.exists()


def test_context_manager_starts_and_stops(make):
    with make() as session:
        assert session.state == "ready"
    assert session.state == "stopped"


def test_the_exact_round_trip_through_convert(make):
    source = _result()
    before = source.data.copy()
    plan = plan_send(source)
    session = make()
    session.start()
    image_id = session.send(plan)
    data, desc = session.fetch(image_id)
    imported = import_fiji_image(desc, data, source, sent=session.sent[image_id])
    result = imported.result
    assert np.array_equal(result.data, before) and result.data.dtype == np.uint16
    assert list(result.axis_labels) == ["Z", "Y", "X"]
    assert np.allclose(result.axis_scales, source.axis_scales) and result.scale_unit == "um"
    assert np.array_equal(source.data, before)           # the source result is untouched


def test_the_sent_record_is_stored(make):
    source = _result((2, 4, 5), "CYX", scales=(1.0, 0.2, 0.1))
    plan = plan_send(source)
    session = make()
    session.start()
    image_id = session.send(plan)
    record = session.sent[image_id]
    assert record.image_id == image_id and record.result_uid == plan.properties[PROP_RESULT_UID]
    assert record.source_axes == ("C", "Y", "X") and record.shape_tzcyx == (1, 1, 2, 4, 5)
    assert plan.notes and record.notes == tuple(plan.notes)          # what the send could not carry is kept


def test_start_while_ready_is_a_noop(make):
    session = make()
    session.start()
    pid = session.ping_info.pid
    session.start()
    assert session.ping_info.pid == pid


def test_a_host_error_keeps_the_session_ready(make):
    session = make()
    session.start()
    with pytest.raises(HostError, match="no open image with id -99"):
        session.fetch(-99)
    assert session.state == "ready"
    assert list(session.temp_dir.glob("*.npy")) == []


def test_exit_at_start_fails_fast_with_the_log_tail(make):
    session = make("exit_at_start")
    started = time.monotonic()
    with pytest.raises(FijiStartError, match="exited with code 3") as excinfo:
        session.start()
    assert "fake host: refusing to start" in str(excinfo.value)
    assert time.monotonic() - started < 30
    assert session.state == "failed" and "refusing to start" in session.error
    assert session.process.poll() == 3


def test_a_host_that_never_connects_is_killed_on_timeout(make):
    session = make("never_connect", startup_timeout_s=SHORT_TIMEOUT_S)
    with pytest.raises(FijiStartError, match="did not connect"):
        session.start()
    assert session.state == "failed" and session.process.poll() is not None


def test_a_wrong_protocol_is_refused(make):
    session = make("wrong_protocol")
    with pytest.raises(FijiStartError, match="protocol"):
        session.start()
    assert session.state == "failed" and session.process.poll() is not None


def test_a_headless_host_is_refused(make):
    session = make("headless")
    with pytest.raises(FijiStartError, match="headless"):
        session.start()
    assert session.state == "failed" and session.process.poll() is not None


def test_an_unlaunchable_interpreter_is_a_start_error(make):
    session = make(python="/no/such/python")
    with pytest.raises(FijiStartError, match="could not be launched"):
        session.start()
    assert session.state == "failed"


def test_death_on_a_request_marks_the_session_dead(make):
    session = make("die_on_request=3")           # ping is request 1
    session.start()
    plan = plan_send(_result())
    session.send(plan)                           # request 2
    with pytest.raises(FijiDied, match="exit code 7"):
        session.list_images()                    # request 3: the host dies
    assert session.state == "dead" and "exit code 7" in session.error
    assert session.process.poll() == 7
    with pytest.raises(FijiDied):
        session.list_images()
    with pytest.raises(FijiDied):
        session.send(plan)


def test_a_host_that_exits_by_itself_is_noticed_without_any_request(make):
    codes = []
    session = make(f"exit_after={SELF_EXIT_DELAY_S}", on_exit=codes.append)
    session.start()
    deadline = time.monotonic() + WATCH_WAIT_S
    while session.state != "dead" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert session.state == "dead" and "exit code 7" in session.error
    deadline = time.monotonic() + WATCH_WAIT_S
    while not codes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert codes == [7]
    with pytest.raises(FijiDied, match="exit code 7"):
        session.list_images()
    time.sleep(0.2)
    assert codes == [7]                         # once, not again for the failed request
    # and a send to a dead session is refused, a start launches a new one
    session._extra_env["IMSWITCH_FIJI_FAKE"] = ""
    session.start()
    assert session.state == "ready"


def test_stop_never_fires_on_exit(make):
    codes = []
    session = make(on_exit=codes.append)
    session.start()
    session.stop()
    time.sleep(0.3)
    assert codes == [] and session.state == "stopped"
    session = make("slow_quit", on_exit=codes.append)       # killed rather than quitting
    session.start()
    session.stop(timeout_s=SHORT_STOP_S)
    time.sleep(0.3)
    assert codes == []


def test_a_request_in_flight_when_the_host_dies_still_raises_died_and_notifies_once(make):
    codes = []
    session = make("die_on_request=2", on_exit=codes.append)
    session.start()
    with pytest.raises(FijiDied):
        session.list_images()
    deadline = time.monotonic() + WATCH_WAIT_S
    while not codes and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)
    assert codes == [7] and session.state == "dead"


def test_restart_after_dead_is_a_new_process(make):
    session = make("die_on_request=2")
    session.start()
    old_pid = session.ping_info.pid
    with pytest.raises(FijiDied):
        session.list_images()
    session._extra_env["IMSWITCH_FIJI_FAKE"] = ""
    session.start()
    assert session.state == "ready" and session.ping_info.pid != old_pid
    assert session.sent == {} and session.list_images() == []


def test_restart_after_failed_works(make):
    session = make("exit_at_start")
    with pytest.raises(FijiStartError):
        session.start()
    session._extra_env["IMSWITCH_FIJI_FAKE"] = ""
    session.start()
    assert session.state == "ready"


def test_requests_need_a_ready_session(make):
    session = make()
    with pytest.raises(FijiLinkError, match="stopped"):
        session.list_images()


def test_stop_is_idempotent(make):
    session = make()
    session.stop()
    session.start()
    session.stop()
    session.stop()
    assert session.state == "stopped"


def test_stop_after_death_cleans_up(make):
    session = make("die_on_request=2")
    session.start()
    temp_dir = session.temp_dir
    with pytest.raises(FijiDied):
        session.list_images()
    session.stop()
    assert session.state == "stopped" and not temp_dir.exists()


def test_a_host_that_ignores_quit_is_killed(make):
    session = make("slow_quit")
    session.start()
    proc = session.process
    started = time.monotonic()
    session.stop(timeout_s=SHORT_STOP_S)
    assert time.monotonic() - started >= SHORT_STOP_S
    assert proc.poll() is not None and proc.returncode != 0
    assert session.state == "stopped"


def test_the_host_path_starts_with_this_imswitch():
    import os

    import imswitch
    from imswitch.improcess.fiji.session import _host_pythonpath

    root = str(Path(imswitch.__file__).resolve().parent.parent)
    path = _host_pythonpath(os.pathsep.join(["/elsewhere", root]))
    assert path.split(os.pathsep) == [root, "/elsewhere"]
    assert _host_pythonpath("") == root


# -- stopping what is in flight ---------------------------------------------------

def _run_in_thread(fn):
    """Run ``fn`` on a thread; ``box`` gets ``value`` or ``error`` and ``done`` is set at the end."""
    box = {"done": threading.Event()}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            box["done"].set()

    threading.Thread(target=target, daemon=True).start()
    return box


def _wait_for(condition, what, timeout_s=WATCH_WAIT_S):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def test_stop_during_a_hanging_request_kills_the_host_and_frees_the_caller(make):
    session = make("hang_on_request=2")          # the ping is request 1
    session.start()
    proc, temp_dir = session.process, session.temp_dir
    request = _run_in_thread(session.list_images)
    time.sleep(0.2)                               # the request is now waiting for a reply that never comes
    assert not request["done"].is_set()
    started = time.monotonic()
    session.stop(timeout_s=SHORT_STOP_S)
    elapsed = time.monotonic() - started
    assert SHORT_STOP_S <= elapsed < STOP_BOUND_S and elapsed < PROMPT_STOP_S
    assert session.state == "stopped" and proc.poll() is not None and not temp_dir.exists()
    assert request["done"].wait(WATCH_WAIT_S)
    assert isinstance(request.get("error"), FijiDied)


def test_stop_during_a_slow_start_ends_it_promptly_and_a_restart_works(make):
    session = make(f"connect_after={LATE_CONNECT_S}", startup_timeout_s=60.0)
    start = _run_in_thread(session.start)
    _wait_for(lambda: session.process is not None, "the host to be launched")
    proc, temp_dir = session.process, session.temp_dir
    started = time.monotonic()
    session.stop(timeout_s=SHORT_STOP_S)
    elapsed = time.monotonic() - started
    assert elapsed < PROMPT_STOP_S                # nowhere near the 60 s start-up timeout
    assert start["done"].wait(WATCH_WAIT_S)
    assert isinstance(start.get("error"), FijiStartError) and "stopped while starting" in str(start["error"])
    assert session.state == "stopped" and proc.poll() is not None and not temp_dir.exists()
    session._extra_env["IMSWITCH_FIJI_FAKE"] = ""
    session.start()
    assert session.state == "ready"


def _stray_connection_before_the_host(make, misbehave):
    session = make(f"connect_after={LATE_CONNECT_S}")
    start = _run_in_thread(session.start)
    _wait_for(lambda: session._listener is not None, "the listener")
    with socket.create_connection(session._listener.address, timeout=5.0) as stray:
        misbehave(stray)
    assert start["done"].wait(WATCH_WAIT_S)
    assert "error" not in start, start.get("error")
    assert session.state == "ready" and session.ping_info is not None
    assert session._listener is None              # closed once the host was in
    return session


def test_a_stray_connection_that_closes_at_once_does_not_fail_the_start(make):
    _stray_connection_before_the_host(make, lambda stray: None)


def test_a_stray_connection_with_the_wrong_key_does_not_fail_the_start(make):
    garbage = bytes(range(256)) + bytes(44)      # 300 bytes that are not an authentication answer

    def misbehave(stray):
        stray.sendall(garbage)

    _stray_connection_before_the_host(make, misbehave)


def test_stray_attempts_are_named_in_the_error_of_a_start_that_fails(make):
    session = make("never_connect", startup_timeout_s=SHORT_TIMEOUT_S)
    start = _run_in_thread(session.start)
    _wait_for(lambda: session._listener is not None, "the listener")
    socket.create_connection(session._listener.address, timeout=5.0).close()
    assert start["done"].wait(WATCH_WAIT_S)
    assert isinstance(start.get("error"), FijiStartError)
    assert "did not connect" in str(start["error"]) and "connection attempt(s)" in str(start["error"])
