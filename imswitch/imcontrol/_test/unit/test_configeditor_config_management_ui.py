"""UI and file-operation checks for Config Studio config management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


def _write_setup(path: Path, marker: str = "") -> None:
    path.write_text(
        json.dumps({"detectors": {}, "availableWidgets": [], "marker": marker}),
        encoding="utf-8",
    )


def _make_window(tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    other = setup_dir / "other.json"
    _write_setup(active, "active")
    _write_setup(other, "other")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = config_dir / "imcontrol_options.json"
    options.write_text(json.dumps({"setupFileName": active.name}), encoding="utf-8")

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")

    window = editor.MainWindow(start_folder=str(setup_dir))
    return window, setup_dir, active, other, options


def test_config_list_exposes_management_actions_and_preserves_selection(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")
    setup_dir = tmp_path / "setups"
    setup_dir.mkdir()
    one = setup_dir / "one.json"
    two = setup_dir / "two.json"
    _write_setup(one)
    _write_setup(two)

    panel = editor.LeftPanel()
    panel.set_folder(str(setup_dir))
    assert panel._duplicate_config_btn.isEnabled() is False
    assert panel._rename_config_btn.isEnabled() is False
    assert panel._delete_config_btn.isEnabled() is False
    assert panel._show_tmpl_btn.text() == "Device Templates…"

    assert panel.select_config_path(str(two)) is True
    assert panel._duplicate_config_btn.isEnabled() is True
    assert panel._rename_config_btn.isEnabled() is True
    assert panel._delete_config_btn.isEnabled() is True

    panel.reload_folder()
    assert editor.LeftPanel._same_path(panel.selected_config_path(), str(two))

    shortcuts = {action.shortcut().toString() for action in panel._file_list.actions()}
    assert {"Ctrl+D", "F2", "Del"}.issubset(shortcuts) or {"Ctrl+D", "F2", "Delete"}.issubset(shortcuts)


def test_duplicate_config_creates_a_separate_file_without_switching_editor(qapp, tmp_path, monkeypatch):
    window, _setup_dir, active, other, _options = _make_window(tmp_path, monkeypatch)
    monkeypatch.setattr(
        editor.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("other_copy.json", True),
    )

    window._duplicate_config_file(str(other))

    duplicate = other.with_name("other_copy.json")
    assert duplicate.exists()
    assert json.loads(duplicate.read_text(encoding="utf-8"))["marker"] == "other"
    assert editor.LeftPanel._same_path(window._path, str(active))
    assert editor.LeftPanel._same_path(window._left.selected_config_path(), str(duplicate))


def test_renaming_active_config_keeps_options_and_editor_in_sync(qapp, tmp_path, monkeypatch):
    window, setup_dir, active, _other, options = _make_window(tmp_path, monkeypatch)
    monkeypatch.setattr(
        editor.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("renamed_active.json", True),
    )

    window._rename_config_file(str(active))

    renamed = setup_dir / "renamed_active.json"
    assert renamed.exists()
    assert not active.exists()
    assert json.loads(options.read_text(encoding="utf-8"))["setupFileName"] == renamed.name
    assert editor.LeftPanel._same_path(window._path, str(renamed))
    assert window.active_config_changed is True
    assert renamed.name in window._active_lbl.text()


def test_active_config_must_be_replaced_before_delete(qapp, tmp_path, monkeypatch):
    window, _setup_dir, active, _other, _options = _make_window(tmp_path, monkeypatch)
    shown = []
    monkeypatch.setattr(editor.QMessageBox, "information", lambda *args: shown.append(args))

    window._delete_config_file(str(active))

    assert active.exists()
    assert shown
    assert "active config cannot be deleted" in shown[0][2].lower()


def test_config_can_be_set_active_directly_from_list_without_opening_it(qapp, tmp_path, monkeypatch):
    window, _setup_dir, active, other, options = _make_window(tmp_path, monkeypatch)

    window._set_path_as_active_config(str(other))

    assert json.loads(options.read_text(encoding="utf-8"))["setupFileName"] == other.name
    assert editor.LeftPanel._same_path(window._path, str(active))
    assert other.name in window._active_lbl.text()
    assert window.active_config_changed is True


def test_opening_from_config_list_respects_unsaved_changes_guard(qapp, tmp_path, monkeypatch):
    window, _setup_dir, active, other, _options = _make_window(tmp_path, monkeypatch)
    window._mark_modified()
    monkeypatch.setattr(window, "_confirm_discard", lambda: False)

    window._open_file_from_list(str(other))

    assert editor.LeftPanel._same_path(window._path, str(active))
    assert window._modified is True
