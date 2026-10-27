"""Starting the ImProcess section from a shipped preset.

A preset is selected by a rule (a shipped setup that is only a ``processing``
block), not by file name, so the first test names today's ten and fails
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
    "smlm_processor",
    "bead_psf_processor",
    "restoration_processor",
    "timelapse_processor",
    "tiling_mosaic_processor",
}

#: Settings of a computer, not of a setup.
PER_COMPUTER_KEYS = ("fijiDir", "fijiPython", "imageDisplay")


def _write(folder, name, data):
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


# ── discovery (Qt-free) ─────────────────────────────────────────────────────
def test_the_rule_selects_exactly_the_ten_shipped_presets():
    found = shipped_improcess_presets()
    assert {p.stem for p in found} == EXPECTED_PRESETS
    assert [p.file_name for p in found] == sorted(p.file_name for p in found)
    assert all(p.file_name == f"{p.stem}.json" for p in found)


def test_every_preset_has_a_human_title_and_a_plain_description():
    titles = []
    for preset in shipped_improcess_presets():
        assert preset.title != preset.stem, f"{preset.stem} has no // title"
        assert "_" not in preset.title and preset.title == preset.title.strip()
        titles.append(preset.title)
        # plain sentences for someone choosing a starting point, not setup recipes
        assert "modules.json" not in preset.description, preset.stem
        assert "processing." not in preset.description, preset.stem
        assert not any(ch in preset.description for ch in "{}[]"), preset.stem
        assert preset.description.rstrip().endswith("."), preset.stem
    assert len(set(titles)) == len(titles), "titles tell the presets apart"


def test_the_title_comes_from_the_comment_key_and_falls_back_to_the_stem(tmp_path):
    block = {"reconstructors": ["view-only"]}
    _write(tmp_path, "a_named.json", {"// title": "  A name ", "processing": block})
    _write(tmp_path, "b_unnamed.json", {"processing": block})
    _write(tmp_path, "c_blank.json", {"// title": "  ", "processing": block})
    _write(tmp_path, "d_wrong_type.json", {"// title": 3, "processing": block})
    found = {p.stem: p for p in shipped_improcess_presets(tmp_path)}
    assert found["a_named"].title == "A name"
    assert [found[k].title for k in ("b_unnamed", "c_blank", "d_wrong_type")] == [
        "b_unnamed", "c_blank", "d_wrong_type"]
    assert found["a_named"].processing == block, "a title is not part of the block"


def test_every_preset_only_names_ids_the_registries_have():
    """A preset is a statement about built-ins: an id that does not exist is a typo."""
    from imswitch.improcess.reconstructors import available_reconstructor_ids
    from imswitch.improcess.processors import available_processor_specs

    reconstructors = set(available_reconstructor_ids())
    processors = {pid for pid, _, _ in available_processor_specs(origin="builtin")}
    from imswitch.improcess.model.processing_config import PROCESSING_KEYS

    for preset in shipped_improcess_presets():
        block = preset.processing
        assert set(block) <= PROCESSING_KEYS, (preset.stem, set(block) - PROCESSING_KEYS)
        assert set(block.get("reconstructors", [])) <= reconstructors, preset.stem
        assert set(block.get("processors", [])) <= processors, preset.stem


def test_a_preset_selects_ids_and_switches_only():
    """No numeric setting in a preset: a number needs a source, and a preset has none."""
    for preset in shipped_improcess_presets():
        for key, value in preset.processing.items():
            if key in ("reconstructors", "processors"):
                assert isinstance(value, list) and all(isinstance(i, str) for i in value)
            else:
                assert isinstance(value, bool), (preset.stem, key, value)


def test_every_preset_loads_through_the_real_setup_loader_without_warnings():
    import warnings

    from imswitch.imcontrol.model.SetupInfo import SetupInfo

    for path in sorted(shipped_setups_directory().glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        if path.stem not in EXPECTED_PRESETS:
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            setup = SetupInfo.from_json(raw, infer_missing=True)
        assert not [str(w.message) for w in caught], (path.stem, [str(w.message) for w in caught])
        assert setup._catchAll["processing"] == json.loads(raw)["processing"], path.stem


def test_every_preset_has_a_description_and_a_block_that_round_trips():
    for preset in shipped_improcess_presets():
        assert isinstance(preset, Preset)
        assert preset.description, preset.stem
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

from PyQt5.QtWidgets import QDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTextEdit

from imswitch.imcontrol.view.configeditor import editor

from imswitch.imcontrol._test.unit.configeditor_testing import assert_json_identical

PROCESSING = editor.SECTION_SCHEMAS["processing"]


@pytest.fixture(autouse=True)
def _fresh_dropin_session(monkeypatch):
    """Each test is a new editor session: no drop-in scan carried over."""
    monkeypatch.setattr(editor, "_DROPIN_CACHE", {})
PRESETS = {p.stem: p for p in shipped_improcess_presets()}
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
def _labels(widget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


def _picker_on(stem):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    row = [dlg._list.item(i).text() for i in range(dlg._list.count())].index(PRESETS[stem].title)
    dlg._list.setCurrentRow(row)
    return dlg


def test_the_picker_lists_titles_and_shows_title_and_description(qapp):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    titles = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert set(titles) == {p.title for p in PRESETS.values()}
    assert not set(titles) & EXPECTED_PRESETS, "titles, not file names"
    for row, title in enumerate(titles):
        dlg._list.setCurrentRow(row)
        preset = next(p for p in PRESETS.values() if p.title == title)
        assert dlg._title.text() == preset.title
        assert dlg._description.text() == preset.description
    assert dlg.chosen is None


def test_the_picker_shows_no_json(qapp):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    dlg.show()
    for row in range(dlg._list.count()):
        dlg._list.setCurrentRow(row)
        assert not dlg.findChildren(QPlainTextEdit) and not dlg.findChildren(QTextEdit)
        for text in _labels(dlg):
            assert not any(ch in text for ch in "{}[]\""), text
    dlg.close()


def test_the_picker_names_the_selected_presets_reconstructors_with_their_descriptions(qapp):
    dlg = _picker_on("monalisa_processor")
    labels = _labels(dlg)
    assert "Reconstructors" in labels
    assert "MoNaLISA" in labels and "monalisa" in labels, "the display name, the id muted beside it"
    assert "View only" in labels
    assert "Point-scanning SIM reconstruction with pattern-based signal extraction" in labels
    assert "Display raw frames without any reconstruction" in labels
    # the list's tooltip carries the names too
    row = dlg._list.currentRow()
    assert "MoNaLISA, View only" in dlg._list.item(row).toolTip()


def test_the_picker_groups_processors_by_category_with_display_names(qapp):
    dlg = _picker_on("monalisa_processor")
    labels = _labels(dlg)
    assert "Processors" in labels
    assert "Restoration" in labels and "Drift Correction" in labels
    assert "Measurement" in labels
    measurement = next(t for t in labels if "FRC Resolution" in t and "PSF / Bead Resolution" in t)
    assert "Colocalization" in measurement
    assert not any("drift-correct" == t for t in labels), "ids are not the primary text"


def test_the_picker_says_what_opens_and_what_is_hidden(qapp):
    dlg = _picker_on("fiji_processor")
    labels = _labels(dlg)
    assert "Hidden at startup" in labels and "Opens at startup" not in labels
    hidden = next(t for t in labels if "Parameter panel" in t)
    for name in ("Napari layer controls", "Reconstruction panel", "Actions panel",
                 "Directory watcher panel", "Multidata panel", "Current data panel",
                 "Results table panel"):
        assert name in hidden
    assert "Graph panel" not in hidden, "analysis panels already start hidden: nothing to say"
    assert "None at startup" in " ".join(labels), "it registers no processor"
    dlg = _picker_on("monalisa_processor")
    labels = _labels(dlg)
    assert "Opens at startup" in labels and "Hidden at startup" not in labels
    opened = next(t for t in labels if "Graph panel" in t)
    assert "ROI manager panel" in opened and "FRC panel" in opened


def test_a_part_left_at_its_default_is_not_mentioned(qapp):
    block = {"reconstructors": ["view-only"], "graphPanel": True, "frcPanel": False,
             "parameterPanel": True, "liveViewerUpdateIntervalS": 0.2}
    summary = editor._improcess_preset_summary(block, PROCESSING)
    assert summary["opens"] == ["Graph panel"]
    assert summary["hidden"] == [] and summary["other"] == []
    assert summary["processors"] is None
    assert editor._improcess_preset_summary({}, PROCESSING) == {
        "reconstructors": None, "processors": None, "opens": [], "hidden": [], "other": []}


def test_other_settings_are_said_in_words(qapp):
    block = {"liveStallTimeoutS": 0, "liveViewerUpdateIntervalS": 0.5,
             "napariEndpoints": [{"id": "x"}, {"id": "y"}], "someFutureKey": {"a": 1},
             "metadataPanel": True}
    summary = editor._improcess_preset_summary(block, PROCESSING)
    assert dict(summary["other"]) == {
        "Live stall timeout (s)": "0",
        "Live viewer update interval (s)": "0.5",
        "Napari plugin endpoints": "2 entries",
        "someFutureKey": "1 entry",
    }
    assert summary["opens"] == ["File metadata panel"]


def test_every_preset_summarises_without_json_and_with_known_names(qapp):
    for preset in shipped_improcess_presets():
        summary = editor._improcess_preset_summary(preset.processing, PROCESSING)
        assert summary["other"] == [], (preset.stem, "a preset sets only ids and switches")
        for name, ident, description in summary["reconstructors"]:
            assert name != ident, (preset.stem, ident, "has a display name")
            assert description, (preset.stem, ident)
        for category, members in summary["processors"] or []:
            assert category != "Not known here (kept as written)", preset.stem
            for name, ident in members:
                assert name != ident, (preset.stem, ident)


def test_the_smlm_preset_says_it_wants_the_storm_extra_and_what_happens_without(qapp):
    smlm = PRESETS["smlm_processor"]
    assert "storm" in smlm.description and "histogram preview" in smlm.description
    assert smlm.processing["napariStormViewer"] is True and smlm.processing["smlmRenderPanel"] is True
    dlg = _picker_on("smlm_processor")
    labels = _labels(dlg)
    opened = next(t for t in labels if "Render localizations as point clouds" in t)
    assert "SMLM render controls" in opened
    assert "SMLM localizer" in labels


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
    first = min(PRESETS.values(), key=lambda p: p.title.casefold())
    assert editor._pick_improcess_preset().stem == first.stem


def test_the_picker_lists_presets_by_title(qapp):
    dlg = editor.ImProcessPresetDialog(shipped_improcess_presets())
    titles = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert titles == sorted(titles, key=str.casefold)


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
    assert asked == [
        (f"Replace the current ImProcess settings with '{PRESETS['fiji_processor'].title}'?",
         QMessageBox.No)]
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
    assert asked == [
        (f"Replace the current ImProcess settings with '{PRESETS['monalisa_processor'].title}'?",
         QMessageBox.No)]
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


@pytest.mark.parametrize("stem", sorted(EXPECTED_PRESETS))
def test_the_editor_finds_nothing_to_flag_in_a_shipped_preset(qapp, stem):
    """The validation panel flags unknown plugin ids; a preset must not trip it."""
    assert editor._collect_xref_issues({"processing": PRESETS[stem].processing}) == []
