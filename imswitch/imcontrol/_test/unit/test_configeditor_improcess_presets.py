"""Starting the ImProcess section from a shipped preset.

A preset is selected by a rule (a shipped setup that is only a ``processing``
block), not by file name, so the first test names today's five and fails
loudly when that set changes: a new preset is a conscious change.
"""

import copy
import json
import sys

import pytest

from imswitch.imcontrol.model.configeditor import improcess_presets as presets_module
from imswitch.imcontrol.model.configeditor.improcess_presets import (
    Preset, shipped_improcess_presets, shipped_setups_directory,
)

#: The processor-only setups that ship today. Changing this set is deliberate.
EXPECTED_PRESETS = {
    "fiji_processor",
    "monalisa_processor",
    "snouty_processor",
    "widefieldstarss_processor",
    "general_image_processing",
}

#: Settings of a computer, not of a setup.
PER_COMPUTER_KEYS = ("fijiDir", "fijiPython", "imageDisplay")


def _write(folder, name, data):
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


# ── discovery (Qt-free) ─────────────────────────────────────────────────────
def test_the_rule_selects_exactly_the_five_shipped_presets():
    found = shipped_improcess_presets()
    assert {p.title for p in found} == EXPECTED_PRESETS
    assert [p.file_name for p in found] == sorted(p.file_name for p in found)
    assert all(p.file_name == f"{p.title}.json" for p in found)


def test_every_preset_has_a_description_and_a_block_that_round_trips():
    for preset in shipped_improcess_presets():
        assert isinstance(preset, Preset)
        assert preset.description, preset.title
        assert preset.processing, preset.title
        assert json.loads(json.dumps(preset.processing)) == preset.processing


def test_no_preset_carries_per_computer_keys():
    for preset in shipped_improcess_presets():
        for key in PER_COMPUTER_KEYS:
            assert key not in preset.processing, (preset.title, key)


def test_the_presets_are_read_from_the_package_data_not_the_user_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from imswitch.imcommon.model.dirtools import DataFileDirs

    assert shipped_setups_directory() == __import__("pathlib").Path(
        DataFileDirs.UserDefaults) / "imcontrol_setups"
    assert not (tmp_path / "ImSwitchConfig").exists(), "reading presets creates no user folder"


def test_the_fallback_path_is_the_same_folder(monkeypatch):
    expected = shipped_setups_directory()
    monkeypatch.setitem(sys.modules, "imswitch.imcommon.model.dirtools", None)
    assert shipped_setups_directory().resolve() == expected.resolve()


def test_the_rule_ignores_comment_and_reserved_keys_and_other_content(tmp_path):
    block = {"reconstructors": ["view-only"]}
    _write(tmp_path, "a_preset.json", {"// description": ["one", "two"], "__x": 1, "processing": block})
    _write(tmp_path, "b_plain.json", {"// description": "text", "processing": block})
    _write(tmp_path, "c_with_device.json", {"detectors": {}, "processing": block})
    _write(tmp_path, "d_no_block.json", {"// description": "x"})
    _write(tmp_path, "e_not_object.json", {"processing": [1]})
    (tmp_path / "f_broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "g_list.json").write_text("[]", encoding="utf-8")
    found = shipped_improcess_presets(tmp_path)
    assert [p.title for p in found] == ["a_preset", "b_plain"]
    assert found[0].description == "one two"
    assert found[1].description == "text"


def test_a_missing_folder_gives_no_presets(tmp_path):
    assert shipped_improcess_presets(tmp_path / "nothing") == []


# ── the editor ──────────────────────────────────────────────────────────────
pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QDialog, QMessageBox, QPlainTextEdit, QPushButton

from imswitch.imcontrol.view.configeditor import editor

from imswitch.imcontrol._test.unit.configeditor_testing import assert_json_identical

PROCESSING = editor.SECTION_SCHEMAS["processing"]
PRESETS = {p.title: p for p in shipped_improcess_presets()}
BUTTON = editor.IMPROCESS_PRESET_BUTTON
OLD_BLOCK = {"reconstructors": ["view-only"], "processors": ["resize"], "someFutureKey": 1}


def _buttons(widget) -> list[str]:
    return [b.text() for b in widget.findChildren(QPushButton)]


def _button(widget, text: str) -> QPushButton:
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def _choose(monkeypatch, title):
    """Make the picker answer with ``title`` (None = cancelled)."""
    monkeypatch.setattr(
        editor, "_pick_improcess_preset",
        lambda parent=None: PRESETS[title] if title else None,
    )


def _answer(monkeypatch, button):
    """Answer the replace question and record whether and how it was asked."""
    asked = []

    def question(parent, title, text, buttons=None, default=None):
        asked.append((text, default))
        return button

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")
    win = editor.MainWindow(start_folder=str(setup_dir))
    win._data.pop("processing", None)
    win._refresh_canvas()
    win._modified = False
    yield win
    win._modified = False
    win._compare_modified = False
    win.close()


# ── the picker ──────────────────────────────────────────────────────────────
def test_the_picker_lists_titles_with_description_and_a_json_preview(qapp):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    titles = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert set(titles) == EXPECTED_PRESETS
    for row, title in enumerate(titles):
        dlg._list.setCurrentRow(row)
        assert dlg._description.text() == PRESETS[title].description
        preview = dlg._preview
        assert preview.isReadOnly()
        assert json.loads(preview.toPlainText()) == PRESETS[title].processing
    assert dlg.chosen is None


def test_use_preset_answers_the_selected_one_and_cancel_none(qapp):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    dlg._list.setCurrentRow(2)
    dlg._use.click()
    assert dlg.result() == QDialog.Accepted
    assert dlg.chosen.title == dlg._list.item(2).text()

    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    dlg.reject()
    assert dlg.chosen is None


def test_an_empty_picker_cannot_choose(qapp):
    dlg = editor.ImProcessPresetDialog([])
    assert not dlg._use.isEnabled()
    assert "No shipped presets" in dlg._description.text()


def test_pick_says_so_when_no_preset_is_shipped(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(editor, "_shipped_improcess_presets", lambda: [])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert editor._pick_improcess_preset() is None
    assert shown and "No shipped" in shown[0]


def test_pick_runs_the_real_picker(qapp, monkeypatch):
    monkeypatch.setattr(
        editor.ImProcessPresetDialog, "exec_",
        lambda self: (self._list.setCurrentRow(0), self._use.click(), self.result())[2],
    )
    assert editor._pick_improcess_preset().title == sorted(PRESETS)[0]


# ── the canvas ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("configured", [False, True])
def test_the_card_has_the_button_and_it_emits(qapp, configured):
    canvas = editor.DeviceCanvas()
    canvas.load({"processing": dict(OLD_BLOCK)} if configured else {})
    assert _buttons(canvas).count(BUTTON) == 1
    emitted = []
    canvas.sig_improcess_preset.connect(lambda: emitted.append(1))
    _button(canvas, BUTTON).click()
    assert emitted == [1]


def test_the_compare_pane_has_no_preset_button(qapp):
    canvas = editor.DeviceCanvas(compact=True)
    canvas.load({})
    assert BUTTON not in _buttons(canvas)


@pytest.mark.parametrize("title", sorted(EXPECTED_PRESETS))
def test_each_preset_into_an_empty_document_is_exactly_its_block(window, monkeypatch, title):
    asked = _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, title)
    _button(window._canvas, BUTTON).click()
    assert asked == [], "nothing to replace, nothing to ask"
    assert_json_identical(window._data["processing"], PRESETS[title].processing)
    assert window._modified is True
    assert "Edit…" in _buttons(window._canvas) and "Remove" in _buttons(window._canvas)


def test_the_document_gets_a_copy_not_the_presets_own_block(window, monkeypatch):
    _choose(monkeypatch, "general_image_processing")
    window._on_improcess_preset()
    window._data["processing"]["reconstructors"].append("changed")
    assert "changed" not in PRESETS["general_image_processing"].processing["reconstructors"]
    assert "changed" not in shipped_improcess_presets()[0].processing.get("reconstructors", [])


def test_a_cancelled_picker_changes_nothing(window, monkeypatch):
    window._data["processing"] = copy.deepcopy(OLD_BLOCK)
    asked = _answer(monkeypatch, QMessageBox.Yes)
    _choose(monkeypatch, None)
    window._on_improcess_preset()
    assert asked == [] and window._data["processing"] == OLD_BLOCK
    assert window._modified is False


def test_replacing_asks_with_default_no_and_no_keeps_the_old_block(window, monkeypatch):
    window._data["processing"] = copy.deepcopy(OLD_BLOCK)
    window._refresh_canvas()
    asked = _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, "fiji_processor")
    _button(window._canvas, BUTTON).click()
    assert asked == [("Replace the current ImProcess settings with 'fiji_processor'?", QMessageBox.No)]
    assert_json_identical(window._data["processing"], OLD_BLOCK)
    assert window._modified is False


def test_replacing_yes_writes_the_preset_and_revalidates(window, monkeypatch):
    window._data["processing"] = copy.deepcopy(OLD_BLOCK)
    window._refresh_canvas()
    _answer(monkeypatch, QMessageBox.Yes)
    _choose(monkeypatch, "fiji_processor")
    validated = []
    monkeypatch.setattr(window, "_validate_pane", lambda pane: validated.append(pane))
    _button(window._canvas, BUTTON).click()
    assert_json_identical(window._data["processing"], PRESETS["fiji_processor"].processing)
    assert window._modified is True
    assert validated == ["primary"]
    assert "(configured)" in "\n".join(l.text() for l in window._canvas.findChildren(editor.QLabel))


def test_an_empty_object_counts_as_nothing_to_replace(window, monkeypatch):
    window._data["processing"] = {}
    asked = _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, "snouty_processor")
    window._on_improcess_preset()
    assert asked == []
    assert_json_identical(window._data["processing"], PRESETS["snouty_processor"].processing)


# ── inside the dialog ───────────────────────────────────────────────────────
def _dialog(current, data=None):
    return editor.ImProcessSectionDialog(PROCESSING, current, {} if data is None else data)


def test_the_dialog_header_has_the_button(qapp):
    assert _buttons(_dialog({})).count(BUTTON) == 1


@pytest.mark.parametrize("title", sorted(EXPECTED_PRESETS))
def test_a_preset_chosen_in_the_dialog_then_applied_untouched_is_the_preset(qapp, monkeypatch, title):
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    asked = _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, title)
    _button(dlg, BUTTON).click()
    assert asked == [], "an untouched new section has nothing to lose"
    dlg._try_apply()
    assert dlg.result() == QDialog.Accepted
    assert_json_identical(dlg.result_data, PRESETS[title].processing)


def test_a_preset_in_the_dialog_replaces_a_block_after_asking(qapp, monkeypatch):
    dlg = _dialog(copy.deepcopy(OLD_BLOCK), {"processing": copy.deepcopy(OLD_BLOCK)})
    asked = _answer(monkeypatch, QMessageBox.Yes)
    _choose(monkeypatch, "monalisa_processor")
    _button(dlg, BUTTON).click()
    assert asked == [("Replace the current ImProcess settings with 'monalisa_processor'?", QMessageBox.No)]
    dlg._try_apply()
    assert_json_identical(dlg.result_data, PRESETS["monalisa_processor"].processing)
    assert "someFutureKey" not in dlg.result_data


def test_no_in_the_dialog_keeps_what_was_there(qapp, monkeypatch):
    dlg = _dialog(copy.deepcopy(OLD_BLOCK), {"processing": copy.deepcopy(OLD_BLOCK)})
    _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, "monalisa_processor")
    _button(dlg, BUTTON).click()
    dlg._try_apply()
    assert_json_identical(dlg.result_data, OLD_BLOCK)


def test_edits_not_yet_applied_are_asked_about_too(qapp, monkeypatch):
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    dlg._field_widgets["graphPanel"]._w.click()
    asked = _answer(monkeypatch, QMessageBox.No)
    _choose(monkeypatch, "snouty_processor")
    _button(dlg, BUTTON).click()
    assert len(asked) == 1
    dlg._try_apply()
    assert dlg.result_data["graphPanel"] is True, "answering No kept the edit"


def test_a_cancelled_picker_in_the_dialog_changes_nothing(qapp, monkeypatch):
    dlg = _dialog(copy.deepcopy(OLD_BLOCK), {"processing": copy.deepcopy(OLD_BLOCK)})
    asked = _answer(monkeypatch, QMessageBox.Yes)
    _choose(monkeypatch, None)
    _button(dlg, BUTTON).click()
    assert asked == []
    dlg._try_apply()
    assert_json_identical(dlg.result_data, OLD_BLOCK)


def test_after_a_preset_the_dialog_keeps_its_pages_and_the_current_one(qapp, monkeypatch):
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    titles = list(dlg._page_titles)
    dlg._page_list.setCurrentRow(2)
    _choose(monkeypatch, "general_image_processing")
    _button(dlg, BUTTON).click()
    assert dlg._page_titles == titles
    assert dlg._page_list.count() == len(titles)
    assert dlg._stack.count() == len(titles)
    assert dlg._page_list.currentRow() == 2


def test_editing_after_a_preset_changes_only_that_key(qapp, monkeypatch):
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    _choose(monkeypatch, "general_image_processing")
    _button(dlg, BUTTON).click()
    expected = copy.deepcopy(PRESETS["general_image_processing"].processing)
    assert "fileWatcherPanel" not in expected, "the test needs a key the preset leaves out"
    dlg._field_widgets["fileWatcherPanel"]._w.click()    # its default is on
    dlg._try_apply()
    expected["fileWatcherPanel"] = False
    assert_json_identical(dlg.result_data, expected)


def test_a_preset_does_not_rescan_the_dropin_folder(qapp, monkeypatch):
    calls = []
    real = editor._discover_dropins
    monkeypatch.setattr(editor, "_discover_dropins", lambda *a, **k: calls.append(1) or real(*a, **k))
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    _choose(monkeypatch, "fiji_processor")
    _button(dlg, BUTTON).click()
    assert calls == [1]
