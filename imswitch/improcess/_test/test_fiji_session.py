"""The Fiji session against a protocol-speaking fake host (no Java)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from imswitch.improcess.fiji.convert import PROP_RESULT_UID, import_fiji_image, plan_send
from imswitch.improcess.fiji.protocol import FijiLinkError, HostError, HostLaunchConfig
from imswitch.improcess.fiji.session import FijiDied, FijiSession, FijiStartError
from imswitch.improcess.model.array_result import ArrayProcessingResult

FAKE = "imswitch.improcess._test.fiji_fake_host"
MAX_HEAP_MB = 1234
SHORT_TIMEOUT_S = 1.5       # for scenarios that must time out; the fake otherwise connects in well under a second
SHORT_STOP_S = 0.5


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
