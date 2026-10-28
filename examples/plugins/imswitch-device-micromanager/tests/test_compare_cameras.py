"""``tools/compare_cameras.py``: measure two cameras the same way, diff the reports.

The rig session (plan ``micromanager-bridge-rig-test.md``) runs this tool against a Hamamatsu camera
driven natively and through the bridge. Here it runs against the two cameras that need no hardware:
the demo setup's ``DemoCamera`` through the bridge, and the native ``HamamatsuManager`` on its mock
(the template's native entry with ``cameraListIndex`` ``mock512``: a configured mock with a 512 x 512
sensor, which is the demo camera's size, so the two reports are comparable). Each ``measure`` boots the
real app in its own interpreter (the three run side by side to stay under a minute), so the
command line, exit codes and report files are what is tested.

The pure ``diff`` logic is tested on small synthetic reports in this process.
"""
import importlib.util
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "compare_cameras.py"
SMALL = ["--exposures", "10", "--frames", "20", "--seconds", "1", "--round-frames", "10",
         "--roi", "64,32,128,96", "--binning", "2"]
JOB_TIMEOUT_S = 240


def _load_tool():
    spec = importlib.util.spec_from_file_location("compare_cameras_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _native_mock_setup(path: Path) -> Path:
    """The template's native entry, on a configured mock with the demo camera's 512 x 512 sensor."""
    data = json.loads((resources.files("imswitch_device_micromanager")
                       / "setup_templates/hamamatsu_native_vs_bridge.json").read_text(encoding="utf-8"))
    props = data["detectors"]["HamamatsuNative"]["managerProperties"]
    props["cameraListIndex"] = "mock512"
    props["hamamatsu"].update(subarray_hsize=512, subarray_vsize=512, image_width=512, image_height=512)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(root: Path, name: str, *args: str, home: Path = None) -> dict:
    """Run ``measure`` to the end; ``home`` is its HOME (a fresh one by default)."""
    if home is None:
        home = root / f"home-{name}"
        home.mkdir()
    env = dict(os.environ, HOME=str(home), QT_QPA_PLATFORM="offscreen", MPLBACKEND="Agg")
    env.pop("PYTEST_CURRENT_TEST", None)
    try:
        done = subprocess.run([sys.executable, str(TOOL), "measure", *args], env=env, capture_output=True,
                              text=True, timeout=JOB_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"measure ({name}) timed out\n{exc.stdout}\n{exc.stderr}")
    return {"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _user_files(home: Path) -> dict:
    """``{relative path: sha256}`` of every file under the app's user-files folder."""
    import hashlib

    root = home / "ImSwitchConfig"
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Side by side: bridged demo camera, native mock, and the native mock refused twice (the user-files check)."""
    pytest.importorskip("mm_test_adapters")
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path_factory.mktemp("compare-cameras")
    native_setup = _native_mock_setup(root / "native_mock.json")
    demo = Path(str(resources.files("imswitch_device_micromanager") / "setup_templates/demo_scope.json"))
    reports = {"bridge": root / "bridge.json", "native": root / "native.json", "refused": root / "refused.json"}
    refused_args = (str(native_setup), "--detector", "HamamatsuNative", "--out", str(reports["refused"]),
                    "--exposures", "10", "--frames", "5")

    def refused_twice():
        """The first start populates the (scratch) ImSwitchConfig as any ImSwitch start does; the second must change nothing."""
        home = root / "home-refused"
        home.mkdir()
        first = _run(root, "refused", *refused_args, home=home)
        before = _user_files(home)
        second = _run(root, "refused", *refused_args, home=home)
        return {**second, "first_rc": first["rc"], "files_before": before, "files_after": _user_files(home)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "bridge": pool.submit(_run, root, "bridge", str(demo), "--detector", "Camera",
                                  "--out", str(reports["bridge"]), *SMALL),
            "native": pool.submit(_run, root, "native", str(native_setup), "--detector", "HamamatsuNative",
                                  "--allow-mock", "--out", str(reports["native"]), *SMALL),
            "refused": pool.submit(refused_twice),
        }
        results = {name: future.result() for name, future in futures.items()}
    for name, result in results.items():
        result["report"] = reports[name]
    return results


def _table(text: str) -> dict:
    """``{quantity: verdict}`` of the diff's main table (the one under the ``Quantity`` header)."""
    rows = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("| Quantity"):
            in_table = True
        elif in_table and not line.startswith("|"):
            break
        elif in_table and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[cells[0]] = cells[4]
    return rows


@pytest.fixture(scope="module")
def diff_text(runs):
    for name in ("bridge", "native"):
        assert runs[name]["rc"] == 0, f"{name}: {runs[name]['stdout'][-1500:]}\n{runs[name]['stderr'][-2500:]}"
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert tool.main(["diff", str(runs["native"]["report"]), str(runs["bridge"]["report"])]) == 0
    return buffer.getvalue()


# ----------------------------------------------------------------------- measure
def test_measure_writes_a_complete_report_for_the_bridged_demo_camera(runs):
    result = runs["bridge"]
    assert result["rc"] == 0, result["stdout"][-1500:] + result["stderr"][-2500:]
    report = json.loads(result["report"].read_text(encoding="utf-8"))

    assert report["tool_version"] == tool.TOOL_VERSION and report["created"].endswith("+00:00")
    assert report["detector"] == "Camera" and report["manager"]["bridge"] is True
    assert report["manager"]["class"].endswith("MicroManagerCameraManager")
    assert len(report["setup"]["sha256"]) == 64 and report["setup"]["path"].endswith("demo_scope.json")
    env = report["environment"]
    assert env["python"].startswith("3.") and env["platform"] and env["packages"]["pymmcore-plus"]
    assert env["packages"]["imswitch2"] is not None or env["packages"]["imswitch-device-micromanager"] is not None

    identity = report["identity"]
    assert identity["model"] == "DemoCamera/DCam" and identity["full_shape"] == [512, 512]
    assert identity["dtype"] == "uint16" and identity["bit_depth"] == 16
    assert identity["supported_binnings"] == [1, 2, 4, 8]
    assert identity["exposure_parameter"] == {"set": "Exposure", "read": "Exposure", "unit": "ms"}
    assert "Exposure" in identity["parameter_names"] and "Binning" not in identity["parameter_names"]
    assert identity["runtime_mode"] == "real" and identity["connection_state"] == "connected"
    assert report["hub"]["adapter_paths"]

    [exposure] = report["exposures"]
    assert exposure["requested_ms"] == 10 and exposure["readback_ms"] == pytest.approx(10)
    live = exposure["live"]
    assert live["new_frames_seen"] > 5 and live["fps"] > 0 and live["error"] is None
    assert live["last_frame"]["shape"] == [512, 512] and live["bridge"]["frames_received_in_window"] > 0
    recording = exposure["recording"]
    assert recording["frames_written"] == recording["frames_requested"] == 20
    assert recording["shape"] == [20, 512, 512] and recording["dtype"] == "uint16"
    assert recording["stream_complete"] == 1 and recording["wall_s"] > 0 and recording["fps"] > 0
    assert recording["mean_of_means"] > 0 and recording["min_mean"] <= recording["mean_of_means"] <= recording["max_mean"]
    assert recording["bridge"]["frames_lost"] == 0 and recording["bridge"]["fault"] is None

    assert report["roi"]["applied_frame_start"] == [64, 32] and report["roi"]["applied_shape"] == [128, 96]
    assert report["roi"]["recording"]["shape"] == [10, 96, 128]
    assert report["roi"]["restored_shape"] == [512, 512]
    assert report["binning"]["applied_binning"] == 2 and report["binning"]["recording"]["shape"] == [10, 256, 256]
    assert report["binning"]["restored_binning"] == 1
    # The ROI at a binning: the Settings panel multiplies its fields by the binning before it calls crop().
    # What a driver does with that is the finding of the rig row G3, so the test pins only that the round ran,
    # recorded and restored; it does not bless the geometry.
    combined = report["roi_binned"]
    assert combined["requested_roi"] == [64, 32, 128, 96] and combined["requested_binning"] == 2
    assert combined["applied_binning"] == 2 and combined["recording"]["frames_written"] == 10
    assert combined["restored_binning"] == 1 and combined["restored_shape"] == [512, 512]
    assert report["errors"] == []


def test_measure_on_the_native_mock_reports_the_native_parameters_in_milliseconds(runs):
    result = runs["native"]
    assert result["rc"] == 0, result["stdout"][-1500:] + result["stderr"][-2500:]
    report = json.loads(result["report"].read_text(encoding="utf-8"))

    assert report["manager"]["class"].endswith("HamamatsuManager") and report["manager"]["bridge"] is False
    assert report["identity"]["exposure_parameter"] == {"set": "Set exposure time", "read": "Real exposure time", "unit": "s"}
    [exposure] = report["exposures"]
    assert exposure["readback_ms"] == pytest.approx(10)                     # 0.01 s in the parameter, 10 in the report
    assert exposure["recording"]["frames_written"] == 20
    assert exposure["recording"]["bridge"] is None                         # a native camera has no stream counters
    assert report["setup"]["disabled_detectors"] == ["HamamatsuMM"]
    assert report["identity"]["runtime_mode"] != "real" or report["identity"]["connection_state"] != "connected"


def test_measure_refuses_a_camera_that_is_not_confirmed_connected_unless_told(runs):
    result = runs["refused"]
    assert result["rc"] == tool.EXIT_MOCK
    assert "refused" in result["stderr"] and "--allow-mock" in result["stderr"]
    assert not result["report"].exists()


def test_measure_leaves_the_users_config_folder_as_it_found_it(runs):
    """The user files are not moved: a second start on a populated ImSwitchConfig changes none of its files
    (no widget state saved, no setup written, no defaults re-synced)."""
    result = runs["refused"]
    assert result["first_rc"] == tool.EXIT_MOCK
    assert result["files_before"], "the first start should have populated the scratch ImSwitchConfig"
    assert result["files_after"] == result["files_before"]


def test_measure_deletes_its_temporary_recordings(runs):
    """The recordings went to a temporary folder (the line names it); it is gone after the run."""
    lines = [line for line in runs["bridge"]["stdout"].splitlines() if line.startswith("each recording is written to")]
    assert len(lines) == 1 and "deleted afterwards" in lines[0]
    assert not Path(lines[0].split()[5]).exists()


# --------------------------------------------------------------------------- diff
def test_diff_of_the_two_mocks_has_the_expected_rows_and_verdicts(diff_text):
    rows = _table(diff_text)
    for quantity in ("manager class", "model", "runs on the real backend", "full shape (w x h)", "dtype",
                     "supported binnings", "10 ms: exposure read-back (ms)", "10 ms: live fps",
                     "10 ms: frames written (requested 20)", "10 ms: recorded shape (n, h, w)",
                     "10 ms: recorded dtype", "10 ms: lost frames (bridge counter; native has none)",
                     "ROI: applied frame start (x, y)", "ROI: recorded shape (n, h, w)",
                     "binning: applied", "binning: frames written (requested 10)",
                     "ROI at binning: applied binning", "ROI at binning: recorded shape (n, h, w)"):
        assert quantity in rows, f"{quantity!r} missing from\n{diff_text}"

    # Count, shape and dtype agree between the two drivers; the bridge lost nothing.
    for quantity in ("full shape (w x h)", "shape at start (w x h)", "dtype", "10 ms: exposure read-back (ms)",
                     "10 ms: frames written (requested 20)", "10 ms: recorded shape (n, h, w)", "10 ms: recorded dtype",
                     "10 ms: lost frames (bridge counter; native has none)", "ROI: applied shape (manager, w x h)",
                     "ROI: frames written (requested 10)", "ROI: recorded shape (n, h, w)",
                     "binning: frames written (requested 10)"):
        assert rows[quantity] == "PASS", f"{quantity}: {rows[quantity]}\n{diff_text}"
    # The mock is not the camera, and the diff says so.
    assert rows["runs on the real backend"] == "CHECK"
    # Binning differs between the managers (the native one keeps the full shape): a finding, not hidden.
    assert rows["binning: recorded shape (n, h, w)"] == "CHECK"


def test_diff_prints_the_mean_intensities_without_a_verdict(diff_text):
    section = diff_text.split("## Mean intensity", 1)[1].split("##", 1)[0]
    assert "| 10 ms |" in section and "PASS" not in section and "CHECK" not in section


def test_diff_lists_property_names_present_on_both_sides(diff_text):
    section = diff_text.split("## Property names that exist in both", 1)[1]
    assert "Camera pixel size" in section and "names only in A" in section


# ------------------------------------------------------------- diff logic (synthetic)
def _report(*, fps=100.0, live_fps=100.0, readback=10.0, frames=200, shape=(200, 64, 64), lost=None,
            full=(64, 64), dtype="uint16", mean=1000.0, cls="x.HamamatsuManager"):
    recording = {"frames_requested": 200, "frames_written": frames, "shape": list(shape), "dtype": dtype, "fps": fps,
                 "mean_of_means": mean, "std_of_means": 1.0,
                 "bridge": None if lost is None else {"frames_lost": lost, "fault": None}}
    return {
        "tool_version": "1", "created": "t", "setup": {"path": "s.json"}, "detector": "Cam", "manager": {"class": cls},
        "identity": {"model": "m", "full_shape": list(full), "shape": list(full), "binning": 1, "dtype": dtype,
                     "pixel_size_um": [1, 1, 1], "supported_binnings": [1, 2], "runtime_mode": "real",
                     "connection_state": "connected", "parameters": {}, "advanced_properties": []},
        "exposures": [{"requested_ms": 10.0, "readback_ms": readback, "live": {"fps": live_fps}, "recording": recording}],
        "roi": None, "binning": None, "warnings": [], "errors": [],
    }


def _diff(a, b, **kw):
    return _table(tool.build_diff(a, b, kw.get("fps", 0.10), kw.get("exposure", 0.01)))


def test_equal_reports_pass_everywhere_a_verdict_is_given():
    rows = _diff(_report(lost=0), _report(lost=0))
    assert set(rows.values()) <= {"PASS", "-"} and "CHECK" not in rows.values()


def test_fps_tolerance_is_relative_and_configurable():
    a, b = _report(fps=100.0, live_fps=100.0), _report(fps=108.0, live_fps=108.0)
    assert _diff(a, b)["10 ms: live fps"] == "PASS"
    assert _diff(a, _report(fps=112.0, live_fps=112.0))["10 ms: live fps"] == "CHECK"
    assert _diff(a, _report(fps=112.0, live_fps=112.0), fps=0.15)["10 ms: live fps"] == "PASS"
    rows = _diff(a, _report(fps=60.0, live_fps=60.0))
    assert rows["10 ms: recording fps (includes start and stop latency)"] == "CHECK"


def test_exposure_readback_tolerance_is_one_percent_by_default():
    assert _diff(_report(readback=10.0), _report(readback=10.09))["10 ms: exposure read-back (ms)"] == "PASS"
    assert _diff(_report(readback=10.0), _report(readback=10.2))["10 ms: exposure read-back (ms)"] == "CHECK"


def test_frame_count_shape_dtype_must_match_exactly():
    base = _report()
    assert _diff(base, _report(frames=199))["10 ms: frames written (requested 200)"] == "CHECK"
    assert _diff(base, _report(shape=(200, 64, 65)))["10 ms: recorded shape (n, h, w)"] == "CHECK"
    assert _diff(base, _report(dtype="uint8"))["10 ms: recorded dtype"] == "CHECK"
    assert _diff(base, _report(full=(128, 64)))["full shape (w x h)"] == "CHECK"


def test_lost_frames_fail_the_row_and_a_missing_counter_is_not_a_pass():
    row = "10 ms: lost frames (bridge counter; native has none)"
    assert _diff(_report(), _report(lost=0))[row] == "PASS"
    assert _diff(_report(), _report(lost=3))[row] == "CHECK"
    assert _diff(_report(), _report())[row] == "n/a"


def test_an_exposure_measured_in_one_report_only_is_a_check():
    b = _report()
    b["exposures"][0]["requested_ms"] = 20.0
    rows = _diff(_report(), b)
    assert rows["10 ms: measured in both reports"] == "CHECK" and rows["20 ms: measured in both reports"] == "CHECK"


def test_a_failed_phase_is_a_check_not_a_crash():
    a, b = _report(), _report()
    b["exposures"][0]["recording"] = {"error": "boom"}
    assert _diff(a, b)["10 ms: recording ran in both"] == "CHECK"


def test_fail_on_check_sets_the_exit_code(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_report()), encoding="utf-8")
    b.write_text(json.dumps(_report(frames=1)), encoding="utf-8")
    assert tool.main(["diff", str(a), str(b)]) == 0
    assert tool.main(["diff", str(a), str(b), "--fail-on-check"]) == 1
    capsys.readouterr()


def test_a_file_that_is_not_a_report_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a compare_cameras report"):
        tool.load_report(str(path))


# ------------------------------------------------------------------------- helpers
def test_frame_signature_tells_noisy_frames_apart_and_equal_frames_together():
    rng = np.random.default_rng(1)
    first = rng.integers(0, 4096, (256, 256), dtype=np.uint16)
    third = rng.integers(0, 4096, (256, 256), dtype=np.uint16)
    assert tool.frame_signature(first) == tool.frame_signature(first.copy())
    assert tool.frame_signature(first) != tool.frame_signature(third)
    assert tool.frame_signature(np.zeros((512, 512), np.uint16)) != tool.frame_signature(third)


@pytest.mark.parametrize("text, expected", [("10", [10.0]), ("10, 20,50", [10.0, 20.0, 50.0])])
def test_exposures_parse(text, expected):
    assert tool.parse_exposures(text) == expected


@pytest.mark.parametrize("text", ["", "0", "-5", "abc", "10,nan"])
def test_bad_exposures_are_refused(text):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        tool.parse_exposures(text)


def test_roi_parse_and_refusal():
    import argparse

    assert tool.parse_roi("1,2,3,4") == (1, 2, 3, 4)
    for bad in ("1,2,3", "a,b,c,d", "-1,0,5,5", "0,0,0,5"):
        with pytest.raises(argparse.ArgumentTypeError):
            tool.parse_roi(bad)


def test_help_justifies_the_defaults(capsys):
    for command, words in (("measure", ["--exposures", "--frames", "--seconds", "--allow-mock"]),
                           ("diff", ["--tolerance-fps", "--tolerance-exposure"])):
        with pytest.raises(SystemExit) as exit_info:
            tool.main([command, "--help"])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert all(word in out for word in words)
    assert "line time" in out and "start latency" in out         # the diff defaults carry their reasons

