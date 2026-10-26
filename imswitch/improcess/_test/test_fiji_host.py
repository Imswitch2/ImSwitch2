"""The pure parts of the Fiji host process (no Java, no imagej import)."""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from imswitch.improcess.fiji import host
from imswitch.improcess.fiji.protocol import HostLaunchConfig

ENDPOINT = "net.imglib2:imglib2-imglyb:1.1.0"
JARS = ["/x/imglib2-imglyb-1.1.0.jar", "/x/imglib2-unsafe-1.0.0.jar"]


def _environ(**over):
    base = {
        "FIJI_DIR": "/f", "JAVA_HOME": "/j", "IMSWITCH_FIJI_JAVA_FETCH": "auto",
        "IMSWITCH_FIJI_CLASSPATH": os.pathsep.join(JARS), "IMSWITCH_FIJI_MAX_HEAP_MB": "6144",
        "IMSWITCH_FIJI_ADDR": "127.0.0.1:5555",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_env_parses_what_launch_config_produces(tmp_path):
    config = HostLaunchConfig(
        fiji_dir=tmp_path, java_home=tmp_path / "j", max_heap_mb=2048, classpath_jars=tuple(JARS),
    )
    parsed = host.parse_env({**config.env(), "IMSWITCH_FIJI_ADDR": "127.0.0.1:41"})
    assert parsed.fiji_dir == str(tmp_path) and parsed.java_fetch == "auto"
    assert parsed.classpath == tuple(JARS) and parsed.max_heap_mb == 2048
    assert parsed.address == ("127.0.0.1", 41)


def test_unknown_heap_stays_unknown():
    assert host.parse_env(_environ(IMSWITCH_FIJI_MAX_HEAP_MB=None)).max_heap_mb is None


@pytest.mark.parametrize("over", [
    {"FIJI_DIR": None}, {"IMSWITCH_FIJI_ADDR": None}, {"IMSWITCH_FIJI_ADDR": "nocolon"},
    {"IMSWITCH_FIJI_ADDR": "h:port"}, {"IMSWITCH_FIJI_MAX_HEAP_MB": "lots"},
])
def test_a_broken_environment_is_refused(over):
    with pytest.raises(host.HostConfigError):
        host.parse_env(_environ(**over))


def test_authkey_is_one_hex_line_from_stdin():
    stream = io.StringIO(bytes(range(32)).hex() + "\nleftover")
    assert host.read_authkey(stream) == bytes(range(32))
    assert stream.read() == "leftover"


@pytest.mark.parametrize("text", ["", "\n", "zz\n"])
def test_a_missing_or_bad_authkey_is_refused(text):
    with pytest.raises(host.HostConfigError):
        host.read_authkey(io.StringIO(text))


def test_requested_and_vendored_versions_are_read():
    assert host.requested_imglyb_version(["org.x:y:1", ENDPOINT]) == "1.1.0"
    assert host.requested_imglyb_version(["org.x:y:1"]) is None
    assert host.vendored_imglyb_version(JARS) == "1.1.0"
    assert host.vendored_imglyb_version(["/x/imglib2-unsafe-1.0.0.jar"]) is None


def test_matching_versions_pass_and_nothing_requested_passes():
    assert host.bridge_version_problem([ENDPOINT], JARS) is None
    assert host.bridge_version_problem([], JARS) is None


def test_a_version_mismatch_is_refused_not_mixed():
    problem = host.bridge_version_problem(["net.imglib2:imglib2-imglyb:2.0.0"], JARS)
    assert "2.0.0" in problem and "1.1.0" in problem


def test_a_requested_jar_with_none_vendored_is_refused():
    assert "no imglib2-imglyb jar" in host.bridge_version_problem([ENDPOINT], ["/x/imglib2-unsafe-1.0.0.jar"])


def test_plane_order_is_stack_order_and_matches_the_index():
    n_t, n_z, n_c = 2, 3, 4
    planes = list(host.plane_order(n_t, n_z, n_c))
    assert len(planes) == n_t * n_z * n_c
    # ImageJ's 1-based stack index counts 1..N in exactly this order.
    assert [host.stack_index(c, z, t, n_c, n_z) for t, z, c in planes] == list(range(1, len(planes) + 1))
    assert planes[:5] == [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 0, 3), (0, 1, 0)]


def test_event_log_stamps_ms_since_origin_and_clears_on_request():
    now = iter([10.12345, 11.0])
    log = host.EventLog(clock=lambda: next(now), origin=10.0)
    log.record("opened", id=-3)
    log.record("closed", id=-3)
    assert log.snapshot()[0]["time"] == 0.123 and log.snapshot()[0]["kind"] == "opened"
    assert len(log.snapshot(clear=True)) == 2 and log.snapshot() == []


def test_event_log_numbers_events_and_stamps_command_ends():
    now = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    log = host.EventLog(clock=lambda: next(now), origin=0.0)
    first = log.begin_command("Gaussian Blur...", current_id=-2)
    log.record("updated", id=-2)
    second = log.begin_command("Crop", current_id=-2)
    assert first < second
    before = log.snapshot()
    assert [e["seq"] for e in before] == sorted(e["seq"] for e in before)
    assert before[0]["end"] is None and before[2]["end"] is None

    log.end_command(first)
    assert log.snapshot()[0]["end"] == 3.0 and log.snapshot()[0]["end"] >= log.snapshot()[0]["time"]
    assert log.snapshot()[2]["end"] is None
    # An end is stamped once, and an unknown seq is ignored.
    log.end_command(first)
    log.end_command(99)
    assert log.snapshot()[0]["end"] == 3.0


def test_event_log_snapshot_is_a_copy_and_clearing_drops_pending_ends():
    log = host.EventLog()
    seq = log.begin_command("Crop")
    taken = log.snapshot()
    taken[0]["command"] = "tampered"
    taken[0]["end"] = 123.0
    assert log.snapshot()[0]["command"] == "Crop" and log.snapshot()[0]["end"] is None
    log.snapshot(clear=True)
    log.end_command(seq)              # the command's event is gone; nothing to stamp
    assert log.snapshot() == []


def test_command_ends_can_carry_fingerprints():
    log = host.EventLog()
    seq = log.begin_command("Invert", current_id=-2)
    assert log.input_of(seq) == -2
    log.end_command(seq, end_fingerprints={"-2": "ab"})
    assert log.snapshot()[0]["end_fingerprints"] == {"-2": "ab"}


def test_the_fingerprint_fold_is_order_sensitive_and_fixed_width():
    one, two = host.fold_fingerprint([1, 2, 3]), host.fold_fingerprint([3, 2, 1])
    assert one != two and len(one) == len(two) == 16
    assert host.fold_fingerprint([-1]) == host.fold_fingerprint([2 ** 64 - 1])


def test_importing_the_host_loads_no_java_bridge_and_no_model():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    code = ("import sys; import imswitch.improcess.fiji.host; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('imagej', 'scyjava', 'jpype', 'h5py') "
            "or m.startswith('imswitch.improcess.model') or m == 'imswitch.improcess.fiji.convert']; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root,
                         env={**os.environ, "PYTHONPATH": root}, check=True).stdout
    assert out.strip() == "[]"
