"""UI checks for Config Studio's config identity and left-panel cleanup."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


def _write_setup(path):
    path.write_text(json.dumps({"detectors": {}, "availableWidgets": []}), encoding="utf-8")


def _file_list_texts(window):
    tree = window._left._file_list
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def test_identity_bar_distinguishes_editing_from_active_config(qapp, tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    other = setup_dir / "other.json"
    _write_setup(active)
    _write_setup(other)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = config_dir / "imcontrol_options.json"
    options.write_text(json.dumps({"setupFileName": active.name}), encoding="utf-8")

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")

    window = editor.MainWindow(start_folder=str(setup_dir))
    assert "Editing:" in window._editing_lbl.text()
    assert active.name in window._editing_lbl.text()
    assert "Active:" in window._active_lbl.text()
    assert active.name in window._active_lbl.text()
    assert "same as editing" in window._active_lbl.text()
    assert window._set_active_btn.isEnabled() is False
    assert window._select_active_btn.isHidden() is True
    assert window._reset_editing_btn.isHidden() is True

    window._load_file(str(other))
    assert other.name in window._editing_lbl.text()
    assert active.name in window._active_lbl.text()
    assert "same as editing" not in window._active_lbl.text()
    assert window._set_active_btn.isEnabled() is True
    assert window._select_active_btn.isHidden() is False

    texts = _file_list_texts(window)
    assert any("other.json" in text and "[EDITING]" not in text for text in texts)
    assert any("active.json" in text and "[ACTIVE]" in text for text in texts)

    window._mark_modified()
    assert "MODIFIED" in window._editing_lbl.text()
    assert window._reset_editing_btn.isHidden() is False
    assert any("other.json" in text and "[MODIFIED]" in text for text in _file_list_texts(window))


def test_select_active_shortcut_opens_active_config(qapp, tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    other = setup_dir / "other.json"
    _write_setup(active)
    _write_setup(other)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "imcontrol_options.json").write_text(
        json.dumps({"setupFileName": active.name}), encoding="utf-8"
    )

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")

    window = editor.MainWindow(start_folder=str(setup_dir))
    window._load_file(str(other))
    assert window._path == str(other)
    assert window._select_active_btn.isHidden() is False

    window._select_active_btn.click()
    assert window._path == str(active)
    assert "same as editing" in window._active_lbl.text()
    assert window._select_active_btn.isHidden() is True


def test_reset_discards_unsaved_changes_after_confirmation(qapp, tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    active.write_text(
        json.dumps({"detectors": {}, "availableWidgets": ["SavedWidget"]}),
        encoding="utf-8",
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "imcontrol_options.json").write_text(
        json.dumps({"setupFileName": active.name}), encoding="utf-8"
    )

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")

    window = editor.MainWindow(start_folder=str(setup_dir))
    window._data["availableWidgets"] = ["UnsavedWidget"]
    window._mark_modified()
    assert window._reset_editing_btn.isHidden() is False

    monkeypatch.setattr(
        editor.QMessageBox, "question", lambda *args, **kwargs: editor.QMessageBox.Yes
    )
    window._reset_editing_btn.click()

    assert window._data["availableWidgets"] == ["SavedWidget"]
    assert window._modified is False
    assert window._reset_editing_btn.isHidden() is True


def test_config_file_list_uses_available_vertical_space(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")
    panel = editor.LeftPanel()

    # The old UI capped this list at 230 px, leaving a large blank area below it.
    assert panel._file_list.maximumHeight() > 230
    assert panel.layout().stretch(panel.layout().indexOf(panel._file_list)) == 1
    assert panel.minimumWidth() == 180
    assert panel.maximumWidth() > 242


def test_available_widget_remove_action_uses_a_real_close_icon(qapp):
    panel = editor.PropertyEditor()
    chip = panel._make_chip("LaserWidget")
    buttons = chip.findChildren(editor.QPushButton)

    assert len(buttons) == 1
    remove = buttons[0]
    assert remove.text() == ""
    assert not remove.icon().isNull()
    assert "Remove widget" in remove.toolTip()
