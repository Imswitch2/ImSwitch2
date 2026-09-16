"""The config editor should open the setup ImSwitch is actually using.

It already knows which one that is — the banner names it — so opening to an
empty pane made everyone's first action the same one.

The resolution logic is exercised directly rather than through a constructed
window: it is pure path arithmetic over an options file, and testing it that
way needs neither a Qt event loop nor this machine's real config.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

_SCRIPT = Path(__file__).resolve().parents[4] / "utility_scripts" / "imswitch_config_editor.py"
_spec = importlib.util.spec_from_file_location("imswitch_config_editor", _SCRIPT)
editor = importlib.util.module_from_spec(_spec)
sys.modules["imswitch_config_editor"] = editor
_spec.loader.exec_module(editor)


class _Resolver:
    """A MainWindow stand-in carrying only what the lookup touches."""

    def __init__(self, options_path):
        self._options_path = str(options_path) if options_path else ""

    _active_setup_name = editor.MainWindow._active_setup_name
    _active_setup_path = editor.MainWindow._active_setup_path


def _options(tmp_path, setup_name, *, valid_json=True):
    path = tmp_path / "imcontrol_options.json"
    if valid_json:
        payload = {"setupFileName": setup_name} if setup_name is not None else {}
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text("{ this is not json", encoding="utf-8")
    return path


def _setup_file(folder, name="rig.json"):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text(json.dumps({"detectors": {}}), encoding="utf-8")
    return target


def test_the_active_setup_is_found_in_the_browsed_folder(tmp_path):
    setups = tmp_path / "imcontrol_setups"
    target = _setup_file(setups, "rig.json")
    resolver = _Resolver(_options(tmp_path, "rig.json"))

    assert resolver._active_setup_path(str(setups)) == str(target)


def test_the_browsed_folder_wins_over_the_default_location(tmp_path, monkeypatch):
    """A folder passed on the command line must not be overridden."""
    chosen = tmp_path / "chosen"
    fallback = tmp_path / "fallback"
    wanted = _setup_file(chosen, "rig.json")
    _setup_file(fallback, "rig.json")
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: fallback)
    resolver = _Resolver(_options(tmp_path, "rig.json"))

    assert resolver._active_setup_path(str(chosen)) == str(wanted)


def test_the_default_location_is_used_when_no_folder_is_given(tmp_path, monkeypatch):
    fallback = tmp_path / "fallback"
    wanted = _setup_file(fallback, "rig.json")
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: fallback)
    resolver = _Resolver(_options(tmp_path, "rig.json"))

    assert resolver._active_setup_path("") == str(wanted)


@pytest.mark.parametrize(
    "options_kwargs, description",
    [
        ({"setup_name": "gone.json"}, "the named file does not exist"),
        ({"setup_name": None}, "options file names no setup"),
        ({"setup_name": "", }, "setupFileName is empty"),
        ({"setup_name": "rig.json", "valid_json": False}, "options file is corrupt"),
    ],
)
def test_nothing_is_opened_when_the_active_config_cannot_be_resolved(
    tmp_path, monkeypatch, options_kwargs, description
):
    """Opening empty is a far better outcome than refusing to start."""
    setups = tmp_path / "imcontrol_setups"
    setups.mkdir()
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setups)
    resolver = _Resolver(_options(tmp_path, **options_kwargs))

    assert resolver._active_setup_path(str(setups)) == "", description


def test_nothing_is_opened_without_an_options_file(tmp_path):
    resolver = _Resolver(None)
    assert resolver._active_setup_path(str(tmp_path)) == ""


def test_startup_loads_the_resolved_file(tmp_path, monkeypatch):
    """_open_active_config hands the resolved path to the normal load path."""
    setups = tmp_path / "imcontrol_setups"
    target = _setup_file(setups, "rig.json")
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setups)

    loaded: list[str] = []

    class _Window(_Resolver):
        _open_active_config = editor.MainWindow._open_active_config

        def _load_file(self, path):
            loaded.append(path)

    window = _Window(_options(tmp_path, "rig.json"))
    window._open_active_config(str(setups))

    assert loaded == [str(target)]


def test_a_failing_load_does_not_propagate(tmp_path, monkeypatch):
    """A broken active config must not stop the editor from opening."""
    setups = tmp_path / "imcontrol_setups"
    _setup_file(setups, "rig.json")
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setups)

    messages: list[str] = []

    class _Window(_Resolver):
        _open_active_config = editor.MainWindow._open_active_config

        def __init__(self, options_path):
            super().__init__(options_path)
            self._status = type("S", (), {"setText": lambda _s, t: messages.append(t)})()

        def _load_file(self, path):
            raise RuntimeError("unreadable")

    window = _Window(_options(tmp_path, "rig.json"))
    window._open_active_config(str(setups))  # must not raise

    assert messages and "active config" in messages[0]
