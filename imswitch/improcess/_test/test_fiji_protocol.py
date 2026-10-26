"""The Fiji host protocol and launch configuration (no sockets, no Java)."""

from __future__ import annotations

import json
import os
import platform
import sys

import numpy as np
import pytest

from imswitch.improcess.fiji import protocol as proto
from imswitch.improcess.fiji.convert import plan_send
from imswitch.improcess.fiji.protocol import (
    PROTOCOL_VERSION, BridgeJarsMissing, CloseImage, Events, Fetch, FijiNotFound, HostError,
    ListImages, Ping, PingInfo, ProtocolError, Quit, Reply, Send, check_ping, images_from_reply,
    reply_value, request_from_dict, resolve_launch_config,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult

GIB = 2 ** 30


def _send():
    result = ArrayProcessingResult("r", np.zeros((2, 4, 5), np.uint16), ["C", "Y", "X"], axis_scales=[1.0, 0.2, 0.1], scale_unit="um")
    return Send.from_plan(plan_send(result), "/tmp/x.raw")


@pytest.mark.parametrize("request_", [
    Ping(), _send(), ListImages(), Fetch(-3, "/tmp/y.raw"), Events(), Events(clear=True), CloseImage(-3), Quit(),
])
def test_every_request_round_trips_through_json(request_):
    wire = json.loads(json.dumps(request_.to_dict()))
    assert request_from_dict(wire) == request_


def test_send_from_plan_describes_the_canonical_array():
    send = _send()
    assert send.shape_tzcyx == [1, 1, 2, 4, 5] and send.dtype == "uint16"
    assert send.calibration["unit"] == "µm"


@pytest.mark.parametrize("bad", [
    {}, {"op": 3}, {"op": "explode"}, {"op": "fetch", "image_id": "x", "path": "p"},
    {"op": "fetch", "image_id": True, "path": "p"}, {"op": "fetch", "image_id": 1},
    {"op": "events", "clear": "yes"}, {"op": "close_image"}, "not a dict",
])
def test_malformed_requests_raise_protocol_error(bad):
    with pytest.raises(ProtocolError):
        request_from_dict(bad)


def test_malformed_send_is_refused():
    good = _send().to_dict()
    for change in ({"dtype": "float64"}, {"shape_tzcyx": [1, 2, 3]}, {"shape_tzcyx": [1, 1, 0, 4, 5]},
                   {"properties": {"a": 1}}, {"channel_names": [1]}, {"calibration": []}):
        with pytest.raises(ProtocolError):
            request_from_dict({**good, **change})
    broken = dict(good)
    del broken["title"]
    with pytest.raises(ProtocolError):
        request_from_dict(broken)


def test_reply_round_trip_and_error_carries_trace():
    reply = Reply(True, {"a": 1})
    assert Reply.from_dict(json.loads(json.dumps(reply.to_dict()))) == reply
    assert reply_value(reply.to_dict()) == {"a": 1}
    with pytest.raises(HostError, match="boom") as excinfo:
        reply_value(Reply(False, error="boom", trace="Traceback...").to_dict())
    assert excinfo.value.trace == "Traceback..."
    with pytest.raises(ProtocolError):
        Reply.from_dict({"ok": "yes"})


def _ping(**over):
    base = dict(protocol=PROTOCOL_VERSION, imagej="2.16", ij1="1.54", java="21", headless=False, pid=1, max_heap_mb=4096)
    base.update(over)
    return PingInfo(**base)


def test_check_ping_refuses_mismatch_and_headless():
    check_ping(_ping())
    with pytest.raises(ProtocolError, match="protocol"):
        check_ping(_ping(protocol=PROTOCOL_VERSION + 1))
    with pytest.raises(ProtocolError, match="headless"):
        check_ping(_ping(headless=True))
    assert PingInfo.from_dict(json.loads(json.dumps(_ping().to_dict()))) == _ping()


def test_images_reply_is_validated():
    with pytest.raises(ProtocolError):
        images_from_reply({"not": "a list"})
    with pytest.raises(ProtocolError):
        images_from_reply([{"image_id": 1}])


# -- launch configuration -------------------------------------------------------

def _fiji(tmp_path, *, java=("macos-arm64/zulu21/zulu-21.jdk/Contents/Home",), heap="cfg.max-heap = '75%'", exe="java"):
    root = tmp_path / "Fiji"
    (root / "jars").mkdir(parents=True)
    for home in java:
        (root / "java" / home / "bin").mkdir(parents=True)
        (root / "java" / home / "bin" / exe).write_text("")
    if heap is not None:
        (root / "config" / "jaunch").mkdir(parents=True)
        (root / "config" / "jaunch" / "fiji.toml").write_text(f"# policy\n{heap}\ncfg.other = 1\n")
    return root


@pytest.fixture
def jars(tmp_path):
    out = []
    for name in ("imglib2-imglyb-1.0.jar", "imglib2-unsafe-1.0.jar"):
        path = tmp_path / name
        path.write_text("")
        out.append(path)
    return out


def _on(monkeypatch, plat, machine):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    # The default fake Fiji bundles a macos-arm64 JDK; pin the platform so the
    # suite means the same thing on every CI runner. Tests that need another
    # platform call _on() again.
    _on(monkeypatch, "darwin", "arm64")


def test_a_single_bundled_jdk_is_used(tmp_path, jars):
    root = _fiji(tmp_path)
    config = resolve_launch_config(root, ram_bytes=8 * GIB, bridge_jars=jars)
    assert config.java_home == root / "java/macos-arm64/zulu21/zulu-21.jdk/Contents/Home"
    assert config.java_fetch == "auto" and config.fiji_dir == root


def test_a_jdk_reachable_by_a_linked_bin_counts_once(tmp_path, jars):
    # Fiji Latest's macOS layout: <zulu>/bin -> zulu-21.jdk/Contents/Home/bin.
    root = _fiji(tmp_path, java=("macos-arm64/zulu21/zulu-21.jdk/Contents/Home",))
    (root / "java/macos-arm64/zulu21/bin").symlink_to("zulu-21.jdk/Contents/Home/bin")
    config = resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars)
    assert config.java_home == (root / "java/macos-arm64/zulu21/zulu-21.jdk/Contents/Home").resolve()


def test_a_lone_jdk_for_another_platform_is_refused(tmp_path, jars):
    # The layout of an older Fiji: x64 Java 8 under java/macosx.
    root = _fiji(tmp_path, java=("macosx/zulu8-macosx_x64/jre/Contents/Home",))
    with pytest.raises(FijiNotFound, match="no Java for this platform"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars)


def test_the_jdk_is_chosen_by_platform(tmp_path, jars, monkeypatch):
    root = _fiji(tmp_path, java=("macos-arm64/z/Contents/Home", "win64/z"), exe="java")
    (root / "java/win64/z/bin/java").unlink()
    (root / "java/win64/z/bin/java.exe").write_text("")
    _on(monkeypatch, "win32", "AMD64")
    assert resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars).java_home == root / "java/win64/z"
    _on(monkeypatch, "darwin", "arm64")
    assert "macos-arm64" in str(resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars).java_home)


def test_an_ambiguous_jdk_is_listed(tmp_path, jars, monkeypatch):
    root = _fiji(tmp_path, java=("linux64/a", "linux64/b"))
    _on(monkeypatch, "linux", "x86_64")
    with pytest.raises(FijiNotFound, match="linux64/a"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars)


def test_fiji_app_is_redirected_to_its_parent(tmp_path, jars):
    root = _fiji(tmp_path)
    (root / "Fiji.app").mkdir()
    config = resolve_launch_config(root / "Fiji.app", ram_bytes=GIB, bridge_jars=jars)
    assert config.fiji_dir == root and any("launcher bundle" in n for n in config.notes)


def test_a_folder_without_jars_or_java_is_refused(tmp_path, jars):
    with pytest.raises(FijiNotFound, match="jars"):
        resolve_launch_config(tmp_path, ram_bytes=GIB, bridge_jars=jars)
    root = _fiji(tmp_path, java=())
    with pytest.raises(FijiNotFound, match="bundled Java"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars)


@pytest.mark.parametrize("line,expected", [
    ("cfg.max-heap = '75%'", 6144), ("cfg.max-heap = '4g'", 4096), ("cfg.max-heap = '512m'", 512),
    ('cfg.max-heap="50%"', 4096),
])
def test_heap_follows_fijis_own_policy(tmp_path, jars, line, expected):
    config = resolve_launch_config(_fiji(tmp_path, heap=line), ram_bytes=8 * GIB, bridge_jars=jars)
    assert config.max_heap_mb == expected
    assert config.java_options() == [f"-Xmx{expected}m"]


@pytest.mark.parametrize("heap", [None, "cfg.other = 1", "cfg.max-heap = 'lots'", "cfg.max-heap = '4096'"])
def test_unknown_heap_is_reported_not_invented(tmp_path, jars, heap):
    config = resolve_launch_config(_fiji(tmp_path, heap=heap), ram_bytes=8 * GIB, bridge_jars=jars)
    assert config.max_heap_mb is None and config.java_options() == []
    assert any("heap policy not found" in n for n in config.notes)


def test_both_bridge_jars_are_required(tmp_path, jars):
    root = _fiji(tmp_path)
    with pytest.raises(BridgeJarsMissing, match="imglib2-unsafe"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars[:1])
    with pytest.raises(BridgeJarsMissing, match="imglib2-imglyb"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars[1:])
    jars[0].unlink()
    with pytest.raises(BridgeJarsMissing, match="imglib2-imglyb"):
        resolve_launch_config(root, ram_bytes=GIB, bridge_jars=jars)


def test_env_names_what_the_host_needs(tmp_path, jars):
    config = resolve_launch_config(_fiji(tmp_path), ram_bytes=8 * GIB, bridge_jars=jars)
    env = config.env()
    assert env["FIJI_DIR"] == str(config.fiji_dir) and env["JAVA_HOME"] == str(config.java_home)
    assert env["IMSWITCH_FIJI_MAX_HEAP_MB"] == "6144"
    assert env["IMSWITCH_FIJI_CLASSPATH"].split(os.pathsep) == [str(j) for j in jars]
    assert all(k.startswith("IMSWITCH_FIJI_") or k in ("FIJI_DIR", "JAVA_HOME") for k in env)
