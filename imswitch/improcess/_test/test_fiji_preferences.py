"""Fiji preferences: the options-file round trip and the availability reasons."""

from __future__ import annotations

import json
import platform
import sys

import pytest

from imswitch.improcess.fiji import preferences as prefs_module
from imswitch.improcess.fiji.preferences import (
    BRIDGE_JARS_DIR,
    INSTALL_HINT,
    LOCATION_HINT,
    FijiPreferences,
    availability,
    launch_config,
    load_fiji_preferences,
    save_fiji_preferences,
)

GIB = 2 ** 30


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")


def _fiji(tmp_path):
    root = tmp_path / "Fiji"
    home = root / "java" / "macos-arm64" / "zulu21" / "zulu-21.jdk" / "Contents" / "Home"
    (root / "jars").mkdir(parents=True)
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    (root / "config" / "jaunch").mkdir(parents=True)
    (root / "config" / "jaunch" / "fiji.toml").write_text("cfg.max-heap = '75%'\n")
    return root


def _has(name):
    return object()


def _hasnt(name):
    return None


def test_round_trip_keeps_foreign_keys(tmp_path):
    path = tmp_path / "improcess_options.json"
    path.write_text(json.dumps({"dataFolder": "/data", "future": {"a": 1}}))
    save_fiji_preferences(FijiPreferences(fijiDir="/opt/Fiji", fijiPython="/py"), path)
    assert load_fiji_preferences(path) == FijiPreferences("/opt/Fiji", "/py")
    content = json.loads(path.read_text())
    assert content["dataFolder"] == "/data" and content["future"] == {"a": 1}


def test_missing_file_and_bad_file_give_defaults(tmp_path):
    assert load_fiji_preferences(tmp_path / "none.json") == FijiPreferences()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_fiji_preferences(bad) == FijiPreferences()
    bad.write_text("[1, 2]")
    assert load_fiji_preferences(bad) == FijiPreferences()
    save_fiji_preferences(FijiPreferences(fijiDir="/x"), bad)        # a bad file is replaced
    assert load_fiji_preferences(bad).fijiDir == "/x"


def test_non_string_values_mean_unset(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"fijiDir": 5, "fijiPython": None}))
    assert load_fiji_preferences(path) == FijiPreferences()


def test_availability_needs_pyimagej_unless_a_host_python_is_set(tmp_path):
    root = _fiji(tmp_path)
    ok, reason = availability(FijiPreferences(fijiDir=str(root)), find_spec=_hasnt)
    assert not ok and reason == INSTALL_HINT and 'imswitch2[fiji]' in reason
    ok, reason = availability(FijiPreferences(str(root), "/other/python"), find_spec=_hasnt)
    assert ok and reason == ""


def test_availability_needs_a_location(tmp_path):
    ok, reason = availability(FijiPreferences(), find_spec=_has)
    assert not ok and reason == LOCATION_HINT and "Fiji location" in reason


def test_availability_reports_an_unusable_folder(tmp_path):
    ok, reason = availability(FijiPreferences(fijiDir=str(tmp_path)), find_spec=_has)
    assert not ok and "jars" in reason


def test_availability_ok_for_a_good_fiji(tmp_path):
    assert availability(FijiPreferences(fijiDir=str(_fiji(tmp_path))), find_spec=_has) == (True, "")


def test_launch_config_uses_the_vendored_jars(tmp_path):
    config = launch_config(FijiPreferences(fijiDir=str(_fiji(tmp_path))), ram_bytes=8 * GIB)
    assert {p.parent for p in config.classpath_jars} == {BRIDGE_JARS_DIR}
    assert [p.name.split("-1")[0] for p in config.classpath_jars] == ["imglib2-imglyb", "imglib2-unsafe"]
    assert config.max_heap_mb == 6 * 1024


def test_unknown_ram_means_no_heap_limit_and_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs_module, "physical_ram_bytes", lambda: None)
    config = launch_config(FijiPreferences(fijiDir=str(_fiji(tmp_path))))
    assert config.max_heap_mb is None and config.java_options() == []
    assert any("physical memory unknown" in n for n in config.notes)


def test_physical_ram_is_positive_here_or_unknown():
    ram = prefs_module.physical_ram_bytes()
    assert ram is None or ram > 0


def test_an_installer_build_says_the_link_needs_a_pip_install(tmp_path):
    from imswitch.improcess.fiji.preferences import FROZEN_HINT

    prefs = FijiPreferences(str(_fiji(tmp_path)), "/other/python")
    assert availability(prefs, find_spec=_has, frozen=True) == (False, FROZEN_HINT)
    assert "pip" in FROZEN_HINT
