"""Checks for Config Studio's embedded split compare workspace (patch 3A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


def _write_setup(path: Path, *, detectors=None, disabled=None) -> None:
    data = {
        "detectors": detectors or {},
        "availableWidgets": [],
    }
    if disabled:
        data[editor.DISABLED_DEVICES_KEY] = {"detectors": disabled}
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_window(tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    reference = setup_dir / "reference.json"
    _write_setup(
        active,
        detectors={
            "MainCam": {"managerName": "MainManager", "managerProperties": {"id": 1}},
        },
    )
    _write_setup(
        reference,
        detectors={
            "RefCam": {"managerName": "ReferenceManager", "managerProperties": {"id": 2}},
        },
        disabled={
            "SpareCam": {"managerName": "SpareManager", "managerProperties": {"id": 3}},
        },
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = config_dir / "imcontrol_options.json"
    options.write_text(json.dumps({"setupFileName": active.name}), encoding="utf-8")

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")

    window = editor.MainWindow(start_folder=str(setup_dir))
    return window, active, reference


def test_compare_mode_is_embedded_and_keeps_primary_config_pinned(qapp, tmp_path, monkeypatch):
    window, active, reference = _make_window(tmp_path, monkeypatch)

    window._enter_compare_mode()

    assert window._compare_mode is True
    assert window._compare_pane.isHidden() is False
    assert window._primary_pane._header.isHidden() is False
    assert editor.LeftPanel._same_path(window._path, str(active))
    assert editor.LeftPanel._same_path(window._compare_path, str(reference))
    assert window._canvas._compact is True
    assert window._compare_canvas._compact is True
    assert window._canvas.horizontalScrollBarPolicy() == editor.Qt.ScrollBarAlwaysOff
    assert window._compare_canvas.horizontalScrollBarPolicy() == editor.Qt.ScrollBarAlwaysOff

    # Compact compare cards leave room for two panes and rely on wrapping.
    assert window._canvas._cards[("detectors", "MainCam")].width() <= 170
    assert window._compare_canvas._cards[("detectors", "RefCam")].width() <= 170


def test_copy_paste_works_between_visible_config_panes_and_preserves_state(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_window(tmp_path, monkeypatch)
    window._enter_compare_mode()

    window._copy_device("detectors", "SpareCam", "compare")
    window._paste_device("primary")

    copied, enabled = editor._locate_device(window._data, "detectors", "SpareCam")
    assert copied["managerName"] == "SpareManager"
    assert enabled is False
    assert window._modified is True

    window._copy_device("detectors", "MainCam", "primary")
    window._paste_device("compare")

    copied_back, enabled_back = editor._locate_device(
        window._compare_data, "detectors", "MainCam"
    )
    assert copied_back["managerName"] == "MainManager"
    assert enabled_back is True
    assert window._compare_modified is True


def test_compare_side_saves_independently_and_is_recorded_for_restart_tracking(
    qapp, tmp_path, monkeypatch
):
    window, _active, reference = _make_window(tmp_path, monkeypatch)
    window._enter_compare_mode()

    window._copy_device("detectors", "MainCam", "primary")
    window._paste_device("compare")
    assert window._save_compare_file() is True

    saved = json.loads(reference.read_text(encoding="utf-8"))
    assert saved["detectors"]["MainCam"]["managerName"] == "MainManager"
    assert str(reference.resolve()) in window.saved_files()
    assert window._compare_modified is False


def test_leaving_compare_restores_normal_card_width(qapp, tmp_path, monkeypatch):
    window, _active, _reference = _make_window(tmp_path, monkeypatch)
    window._enter_compare_mode()
    assert window._canvas._cards[("detectors", "MainCam")].width() <= 170

    assert window._leave_compare_mode() is True

    assert window._compare_pane.isHidden() is True
    assert window._primary_pane._header.isHidden() is True
    assert window._canvas._compact is False
    assert window._canvas._cards[("detectors", "MainCam")].width() == 195


def _make_3b_window(tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups_3b"
    setup_dir.mkdir()
    active = setup_dir / "active.json"
    reference = setup_dir / "reference.json"
    _write_setup(
        active,
        detectors={
            "SameCam": {"managerName": "Camera", "managerProperties": {"id": 1}},
            "DiffCam": {"managerName": "Camera", "managerProperties": {"id": 2}},
            "LeftOnly": {"managerName": "Camera", "managerProperties": {"id": 3}},
        },
        disabled={
            "StateCam": {"managerName": "Camera", "managerProperties": {"id": 4}},
        },
    )
    _write_setup(
        reference,
        detectors={
            "SameCam": {"managerName": "Camera", "managerProperties": {"id": 1}},
            "DiffCam": {"managerName": "Camera", "managerProperties": {"id": 20}},
            "RightOnly": {"managerName": "Camera", "managerProperties": {"id": 5}},
            "StateCam": {"managerName": "Camera", "managerProperties": {"id": 4}},
        },
    )

    config_dir = tmp_path / "config_3b"
    config_dir.mkdir()
    (config_dir / "imcontrol_options.json").write_text(
        json.dumps({"setupFileName": active.name}), encoding="utf-8"
    )

    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [config_dir])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates_3b")

    window = editor.MainWindow(start_folder=str(setup_dir))
    window._enter_compare_mode()
    return window, active, reference


def test_compare_cards_show_same_different_only_and_missing(qapp, tmp_path, monkeypatch):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)

    left = window._canvas._cards
    right = window._compare_canvas._cards

    assert left[("detectors", "SameCam")].comparison_state == "same"
    assert right[("detectors", "SameCam")].comparison_state == "same"

    assert left[("detectors", "DiffCam")].comparison_state == "different"
    assert left[("detectors", "DiffCam")].copy_direction == "right"
    assert right[("detectors", "DiffCam")].comparison_state == "different"
    assert right[("detectors", "DiffCam")].copy_direction == "left"

    assert left[("detectors", "LeftOnly")].comparison_state == "only_here"
    assert right[("detectors", "LeftOnly")].comparison_state == "missing_here"
    assert right[("detectors", "LeftOnly")].missing is True

    assert left[("detectors", "RightOnly")].comparison_state == "missing_here"
    assert left[("detectors", "RightOnly")].missing is True
    assert right[("detectors", "RightOnly")].comparison_state == "only_here"

    # Equal definitions with different enabled state are still a meaningful diff.
    assert left[("detectors", "StateCam")].comparison_state == "different"
    assert right[("detectors", "StateCam")].comparison_state == "different"

    left_order = [name for cat, name in left if cat == "detectors"]
    right_order = [name for cat, name in right if cat == "detectors"]
    assert left_order == right_order


def test_compare_selection_is_synchronized_without_switching_inspector_source(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)

    window._canvas._on_card_clicked("detectors", "LeftOnly")
    assert window._canvas._selected == ("detectors", "LeftOnly")
    assert window._compare_canvas._selected == ("detectors", "LeftOnly")
    assert window._editor_pane == "primary"

    window._compare_canvas._on_card_clicked("detectors", "RightOnly")
    assert window._canvas._selected == ("detectors", "RightOnly")
    assert window._compare_canvas._selected == ("detectors", "RightOnly")
    assert window._editor_pane == "compare"


def test_copy_to_other_uses_existing_copy_paste_and_updates_comparison(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)

    window._copy_device_to_other("detectors", "LeftOnly", "primary")

    copied, enabled = editor._locate_device(
        window._compare_data, "detectors", "LeftOnly"
    )
    assert copied["managerProperties"]["id"] == 3
    assert enabled is True
    assert window._compare_modified is True
    assert window._canvas._cards[("detectors", "LeftOnly")].comparison_state == "same"
    assert window._compare_canvas._cards[("detectors", "LeftOnly")].comparison_state == "same"
    assert window._compare_canvas._cards[("detectors", "LeftOnly")].missing is False


def test_leaving_compare_removes_comparison_placeholders_and_badges(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)
    assert ("detectors", "RightOnly") in window._canvas._cards
    assert window._canvas._cards[("detectors", "RightOnly")].missing is True

    assert window._leave_compare_mode() is True

    assert ("detectors", "RightOnly") not in window._canvas._cards
    assert window._canvas._cards[("detectors", "SameCam")].comparison_state is None


def test_compare_summary_is_embedded_and_cards_remain_default(qapp, tmp_path, monkeypatch):
    window, active, reference = _make_3b_window(tmp_path, monkeypatch)

    assert window._compare_view == "cards"
    assert window._pane_splitter.isHidden() is False
    assert window._compare_summary.isHidden() is True
    assert window._compare_view_bar.isHidden() is False

    window._set_compare_view("summary")

    assert window._compare_view == "summary"
    assert window._pane_splitter.isHidden() is True
    assert window._compare_summary.isHidden() is False
    assert window._compare_summary._left_name.text() == active.name
    current = window._compare_summary._selector.currentData()
    assert editor.LeftPanel._same_path(str(current), str(reference))


def test_compare_summary_reuses_3b_states_and_can_filter_same_devices(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)
    summary = window._compare_summary
    window._set_compare_view("summary")

    left = summary._items["left"]
    right = summary._items["right"]
    assert left[("detectors", "SameCam")].text(3) == "SAME"
    assert right[("detectors", "SameCam")].text(3) == "SAME"
    assert left[("detectors", "DiffCam")].text(3) == "DIFFERENT"
    assert right[("detectors", "DiffCam")].text(3) == "DIFFERENT"
    assert left[("detectors", "LeftOnly")].text(3) == "ONLY HERE"
    assert right[("detectors", "LeftOnly")].text(3) == "MISSING HERE"
    assert left[("detectors", "RightOnly")].text(3) == "MISSING HERE"
    assert right[("detectors", "RightOnly")].text(3) == "ONLY HERE"
    assert left[("detectors", "StateCam")].text(1) == "Disabled"
    assert right[("detectors", "StateCam")].text(1) == "Enabled"

    summary._diff_only.setChecked(True)
    assert ("detectors", "SameCam") not in summary._items["left"]
    assert ("detectors", "SameCam") not in summary._items["right"]
    assert ("detectors", "DiffCam") in summary._items["left"]
    assert ("detectors", "DiffCam") in summary._items["right"]


def test_compare_summary_synchronizes_two_columns_and_uses_central_copy_buttons(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)
    window._set_compare_view("summary")
    summary = window._compare_summary

    key = ("detectors", "LeftOnly")
    summary._trees["left"].setCurrentItem(summary._items["left"][key])
    summary._items["left"][key].setSelected(True)
    summary._selection_changed("left")

    assert summary._selected_key == key
    assert summary._trees["right"].currentItem() is summary._items["right"][key]
    assert summary._selected_status_lbl.text() == "ONLY LEFT"
    assert summary._copy_right_btn.isEnabled() is True
    assert summary._copy_left_btn.isEnabled() is False

    # LeftOnly does not exist on the right, so this fast path needs no
    # overwrite confirmation and exercises 3B's normal copy/paste operation.
    summary._copy_right_btn.click()

    copied, enabled = editor._locate_device(
        window._compare_data, "detectors", "LeftOnly"
    )
    assert copied["managerProperties"]["id"] == 3
    assert enabled is True
    assert window._compare_modified is True
    assert summary._items["left"][key].text(3) == "SAME"
    assert summary._items["right"][key].text(3) == "SAME"


def test_compare_summary_side_click_drives_shared_inspector_source(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)
    window._set_compare_view("summary")
    summary = window._compare_summary

    key = ("detectors", "RightOnly")
    item = summary._items["right"][key]
    summary._item_clicked("right", item)

    assert window._editor_pane == "compare"
    assert window._canvas._selected == key
    assert window._compare_canvas._selected == key


def test_leaving_compare_from_summary_restores_normal_card_workspace(
    qapp, tmp_path, monkeypatch
):
    window, _active, _reference = _make_3b_window(tmp_path, monkeypatch)
    window._set_compare_view("summary")
    assert window._pane_splitter.isHidden() is True

    assert window._leave_compare_mode() is True

    assert window._compare_summary.isHidden() is True
    assert window._compare_view_bar.isHidden() is True
    assert window._pane_splitter.isHidden() is False
    assert window._compare_pane.isHidden() is True
    assert window._canvas._compact is False
