"""ImProcess as a first-class entry of the config editor.

The ImProcess block used to be one row of the System card, shown only once
the file already had a ``processing`` block and otherwise hidden behind
"Add System Section...". It is now an always-visible section of the centre
canvas with its own explained dialog. These tests drive the real widgets
offscreen; the byte-for-byte guarantee is the same as the generic dialog's:
open, Apply untouched, and the block is what it was.
"""

import copy
import json
import sys
import textwrap

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton

from imswitch.imcontrol.view.configeditor import editor

from imswitch.imcontrol._test.unit.configeditor_testing import SHIPPED_SETUPS, assert_json_identical

PROCESSING = editor.SECTION_SCHEMAS["processing"]


@pytest.fixture(autouse=True)
def _fresh_dropin_session(monkeypatch):
    """Each test is a new editor session: no drop-in scan carried over."""
    monkeypatch.setattr(editor, "_DROPIN_CACHE", {})


def _setups() -> dict:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SHIPPED_SETUPS.glob("*.json"))
    }


SETUPS = _setups()
WITH_BLOCK = {name: s["processing"] for name, s in SETUPS.items() if s.get("processing")}

#: P0's extra-keys block: every kind of value the old dialog lost.
RICH_BLOCK = {
    "reconstructors": ["monalisa", "view-only"],
    "processors": ["resize", "drift-correct", "convert-type"],
    "fileWatcherPanel": True,
    "liveStallTimeoutS": 0,
    "liveViewerUpdateIntervalS": 0.5,
    "napariEndpoints": [{"id": "x", "plugin": "p"}],
    "someFutureKey": 1,
}


def _has_devices(setup: dict) -> bool:
    return any(setup.get(cat) for cat in editor.DEVICE_CATS)


def _texts(widget) -> str:
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def _buttons(widget) -> list[str]:
    return [b.text() for b in widget.findChildren(QPushButton)]


def _button(widget, text: str) -> QPushButton:
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def _dialog(current, **kw) -> "editor.ImProcessSectionDialog":
    return editor.ImProcessSectionDialog(PROCESSING, current, {}, **kw)


def _apply(current, **kw):
    dlg = _dialog(current, **kw)
    dlg._try_apply()
    return dlg


# ── the canvas section ──────────────────────────────────────────────────────
def test_an_empty_setup_shows_the_section_not_configured(qapp):
    canvas = editor.DeviceCanvas()
    canvas.load({})
    text = _texts(canvas)
    assert "ImProcess" in text
    assert "Not configured" in text
    for field in ("reconstructors", "processors"):
        for plugin_id in editor._field_default(editor._schema_fields(PROCESSING)[field]):
            assert plugin_id in text, "the defaults come from the schema"
    assert _buttons(canvas).count("Configure…") == 1
    assert "Edit…" not in _buttons(canvas) and "Remove" not in _buttons(canvas)


@pytest.mark.parametrize("name", sorted(SETUPS))
def test_every_shipped_setup_shows_the_section(qapp, name):
    canvas = editor.DeviceCanvas()
    canvas.load(copy.deepcopy(SETUPS[name]))
    assert canvas._section_order.count("improcess") == 1
    buttons = _buttons(canvas)
    if name in WITH_BLOCK:
        assert "(configured)" in _texts(canvas)
        assert "Edit…" in buttons and "Remove" in buttons
        assert "Configure…" not in buttons
        assert any(line.text().startswith("Reconstructors:") for line in canvas._improcess_lines)
    else:
        assert "(not configured)" in _texts(canvas)
        assert "Configure…" in buttons and "Edit…" not in buttons


@pytest.mark.parametrize("name", sorted(SETUPS))
def test_the_section_leads_processor_only_setups_and_follows_devices(qapp, name):
    canvas = editor.DeviceCanvas()
    canvas.load(copy.deepcopy(SETUPS[name]))
    order = canvas._section_order
    if _has_devices(SETUPS[name]):
        assert order[-1] == "improcess"
    else:
        assert order[0] == "improcess"


def test_there_are_processor_only_presets_to_check():
    assert any(not _has_devices(s) for s in SETUPS.values())
    assert any(_has_devices(s) for s in SETUPS.values())


def test_the_card_summarises_the_block(qapp):
    canvas = editor.DeviceCanvas()
    canvas.load({"processing": {
        "reconstructors": ["monalisa"],
        "processors": ["resize", "filter"],
        "graphPanel": True,
        "roiManagerPanel": True,
        "frcPanel": False,
        "liveStallTimeoutS": 0,
        "liveViewerUpdateIntervalS": 0.2,
        "someFutureKey": 1,
    }})
    lines = [line.text() for line in canvas._improcess_lines]
    assert "Reconstructors: MoNaLISA" in lines
    assert "Processors: 2" in lines
    assert "Analysis panels: graph, ROI manager" in lines
    assert any(
        line.startswith("Live mode:") and "Live stall timeout (s) = 0" in line
        and "interval" not in line   # the interval sits at its default
        for line in lines
    )
    assert "1 custom key" in lines


def test_a_compare_pane_shows_the_card_read_only(qapp):
    canvas = editor.DeviceCanvas(compact=True)
    canvas.load({"processing": {"reconstructors": ["view-only"]}})
    assert "(configured)" in _texts(canvas)
    assert not {"Configure…", "Edit…", "Remove"} & set(_buttons(canvas))


def test_the_buttons_emit_the_canvas_signals(qapp):
    canvas = editor.DeviceCanvas()
    canvas.load({"processing": {"reconstructors": ["view-only"]}})
    seen = []
    canvas.sig_improcess_edit.connect(lambda: seen.append("edit"))
    canvas.sig_improcess_remove.connect(lambda: seen.append("remove"))
    _button(canvas, "Edit…").click()
    _button(canvas, "Remove").click()
    assert seen == ["edit", "remove"]


def test_refresh_redraws_only_the_improcess_section(qapp):
    data = {"detectors": {"cam": {"managerName": "MockCameraManager", "managerProperties": {}}}}
    canvas = editor.DeviceCanvas()
    canvas.load(data)
    canvas.select_device("detectors", "cam")
    data["processing"] = {"reconstructors": ["view-only"]}
    canvas.refresh_improcess()
    assert "Edit…" in _buttons(canvas)
    assert canvas._selected == ("detectors", "cam")


# ── Apply untouched ─────────────────────────────────────────────────────────
def test_configure_on_an_empty_document_applies_to_the_seed(qapp):
    seed = editor._new_section_seed(PROCESSING)
    dlg = _apply(seed)
    assert dlg.result() == QDialog.Accepted, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, seed)


@pytest.mark.parametrize("name", sorted(WITH_BLOCK))
def test_every_shipped_block_applies_untouched_unchanged(qapp, name):
    block = WITH_BLOCK[name]
    dlg = _apply(copy.deepcopy(block))
    assert dlg.result() == QDialog.Accepted, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, block)
    assert list(dlg.result_data) == list(block)


def test_the_extra_keys_block_applies_untouched_unchanged(qapp):
    dlg = _apply(copy.deepcopy(RICH_BLOCK))
    assert dlg.result() == QDialog.Accepted, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, RICH_BLOCK)
    assert list(dlg.result_data) == list(RICH_BLOCK)


def test_edit_improcess_section_writes_the_seed_for_an_empty_document(qapp, monkeypatch):
    monkeypatch.setattr(
        editor.ImProcessSectionDialog, "exec_",
        lambda self: (self._try_apply(), self.result())[1],
    )
    data = {}
    assert editor._edit_improcess_section(data) is True
    assert_json_identical(data["processing"], editor._new_section_seed(PROCESSING))


def test_cancel_leaves_the_document_alone(qapp, monkeypatch):
    monkeypatch.setattr(editor.ImProcessSectionDialog, "exec_", lambda self: QDialog.Rejected)
    data = {"processing": {"reconstructors": ["monalisa"]}}
    assert editor._edit_improcess_section(data) is False
    assert data == {"processing": {"reconstructors": ["monalisa"]}}


def test_touching_one_option_changes_only_that_key(qapp):
    block = copy.deepcopy(RICH_BLOCK)
    dlg = _dialog(copy.deepcopy(block))
    dlg._field_widgets["graphPanel"]._w.click()
    dlg._try_apply()
    assert_json_identical(dlg.result_data, {**block, "graphPanel": True})


# ── pages and explanations ──────────────────────────────────────────────────
def test_the_pages_are_the_schema_groups_then_the_fixed_ones(qapp):
    groups = []
    for field in PROCESSING["fields"]:
        if field["grp"] not in groups:
            groups.append(field["grp"])
    dlg = _dialog({})
    assert groups[:2] == ["Reconstructors", "Processors"], "the main components lead"
    # drop-in plugins are about reconstructors and processors: right after them
    assert dlg._page_titles == (
        groups[:2] + ["Drop-in plugins"] + groups[2:] + ["Custom", "Set on this computer"]
    )
    assert dlg._page_titles == [
        "Reconstructors", "Processors", "Drop-in plugins", "Startup layout",
        "Analysis panels", "Localization viewer", "Live mode", "napari plugins",
        "Custom", "Set on this computer",
    ]
    assert dlg._page_list.currentRow() == 0 and dlg._page_titles[0] == "Reconstructors"
    assert [dlg._page_list.item(i).text() for i in range(dlg._page_list.count())] == dlg._page_titles
    assert dlg._stack.count() == len(dlg._page_titles)


def test_every_option_shows_its_explanation_not_only_a_tooltip(qapp):
    dlg = _dialog({})
    shown = {label.text() for label in dlg.findChildren(QLabel)}
    for field in PROCESSING["fields"]:
        assert field["tip"] in shown, field["key"]


def test_the_header_is_the_schema_summary(qapp):
    assert PROCESSING["summary"] in _texts(_dialog({}))


def test_the_computer_page_names_each_setting_and_where_it_is_set(qapp):
    dlg = _dialog({})
    text = _texts(dlg._stack.widget(dlg._page_titles.index("Set on this computer")))
    assert "improcess_options.json" in text
    assert "Plugins → Fiji → Fiji location" in text
    assert "Preferences → Image display" in text
    assert "Preferences → Default folders" in text


def test_the_menu_paths_named_exist_in_improcess():
    from pathlib import Path

    root = Path(editor.__file__).resolve().parents[3] / "improcess"
    view = (root / "view" / "ImProcessMainView.py").read_text(encoding="utf-8")
    fiji = (root / "controller" / "FijiController.py").read_text(encoding="utf-8")
    assert "addMenu('&Plugins')" in view and "addMenu('Fiji')" in view
    assert '"Fiji location…"' in fiji
    assert "addMenu('&Preferences')" in view and "addMenu('Image display')" in view
    assert "'Default folders…'" in view


def test_per_computer_settings_are_not_fields_of_the_dialog(qapp):
    dlg = _dialog({})
    for key in ("fijiDir", "fijiPython", "imageDisplay", "folders"):
        assert key not in dlg._field_widgets


def test_unknown_keys_are_on_the_custom_page_and_kept(qapp):
    dlg = _dialog(copy.deepcopy(RICH_BLOCK))
    assert set(dlg._custom_widgets) == {"someFutureKey"}
    assert dlg.add_custom_field("another", 2) == ""
    dlg._try_apply()
    assert dlg.result_data["another"] == 2
    assert dlg.result_data["someFutureKey"] == 1


def test_a_required_field_left_empty_is_refused(qapp):
    dlg = _dialog(editor._new_section_seed(PROCESSING))
    box = dlg._field_widgets["reconstructors"]._w
    for option_id in box.checked_ids():
        box.checkbox(option_id).click()
    dlg._try_apply()
    assert dlg.result() != QDialog.Accepted
    assert "Reconstructors" in dlg._err_lbl.text()


# ── napari endpoints ────────────────────────────────────────────────────────
#: The documented dock adapter (docs/improcess-napari-plugins.rst).
VALID_ENDPOINT = {
    "id": "napari-skimage:threshold",
    "label": "napari-skimage: Automated threshold",
    "lane": "dock",
    "plugin": "napari-skimage",
    "widget": "Automated Threshold",
    "kinds": ["image"],
}


def _endpoints_text(dlg, text: str) -> None:
    dlg._endpoints._w.setPlainText(text)


def test_the_endpoints_show_as_indented_json_in_a_multi_line_box(qapp):
    block = {"reconstructors": ["view-only"], "napariEndpoints": [{"id": "a", "plugin_name": "p"}]}
    dlg = _dialog(block)
    assert dlg._endpoints._w.toPlainText() == json.dumps(block["napariEndpoints"], indent=2)
    assert dlg._endpoints._w.toPlainText().count("\n") > 1


@pytest.mark.parametrize("text, expect", [
    ("[{", "not valid JSON"),
    ('{"id": "a"}', "must be a list"),
    ('[{"label": "no id here"}]', "no id"),
    ('["not an object"]', "not an object"),
])
def test_a_bad_endpoint_edit_is_refused_with_a_message(qapp, text, expect):
    dlg = _dialog({"reconstructors": ["view-only"]})
    _endpoints_text(dlg, text)
    dlg._try_apply()
    assert dlg.result() != QDialog.Accepted
    assert expect in dlg._err_lbl.text()
    assert dlg.result_data == {}


def test_a_valid_endpoint_edit_is_written(qapp):
    dlg = _dialog({"reconstructors": ["view-only"]})
    _endpoints_text(dlg, json.dumps([VALID_ENDPOINT]))
    dlg._try_apply()
    assert dlg.result() == QDialog.Accepted, dlg._err_lbl.text()
    assert dlg.result_data["napariEndpoints"] == [VALID_ENDPOINT]
    assert list(dlg.result_data) == ["reconstructors", "napariEndpoints"]


def test_untouched_endpoints_are_not_written_when_the_file_had_none(qapp):
    dlg = _apply({"reconstructors": ["view-only"]})
    assert "napariEndpoints" not in dlg.result_data


# ── drop-in plugins ─────────────────────────────────────────────────────────
PLUGIN_SOURCE = textwrap.dedent('''
    from imswitch.improcess.processors.base import Processor

    class TinyProcessor(Processor):
        name = "Tiny (test)"
        id = "user.tiny-test"
        category = "User"
        kinds = ("image",)
''')


@pytest.fixture
def plugins_dir(tmp_path, monkeypatch):
    folder = tmp_path / "improcess_plugins"
    folder.mkdir()
    (folder / "tiny.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (folder / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    yield folder
    for name in ("improcess_user_plugin_tiny", "improcess_user_plugin_broken"):
        sys.modules.pop(name, None)


def _dropin_rows(dlg):
    tree = dlg._dropin_tree
    return [
        [tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
        for i in range(tree.topLevelItemCount())
    ]


def test_dropins_are_listed_read_only_and_the_registries_are_untouched(qapp, plugins_dir):
    from imswitch.improcess.processors import available_processor_ids
    from imswitch.improcess.reconstructors import available_reconstructor_ids

    processors, reconstructors = available_processor_ids(), available_reconstructor_ids()
    dlg = _dialog({"reconstructors": ["view-only"]}, dropin_directory=str(plugins_dir))

    rows = _dropin_rows(dlg)
    assert [r[:4] for r in rows] == [["user.tiny-test", "processor", "Tiny (test)", "tiny.py"]]
    assert dlg._dropin_tree.editTriggers() == dlg._dropin_tree.NoEditTriggers
    assert str(plugins_dir) in _texts(dlg)
    assert "boom" in _texts(dlg), "a file that failed to load is reported"
    assert "not listed in the setup file" in _texts(dlg)
    # nothing was installed
    assert available_processor_ids() == processors
    assert available_reconstructor_ids() == reconstructors
    assert "user.tiny-test" not in available_processor_ids()
    # and the list is not an input: Apply writes no drop-in id
    dlg._try_apply()
    assert_json_identical(dlg.result_data, {"reconstructors": ["view-only"]})


def test_a_dropin_already_in_the_setup_is_marked(qapp, plugins_dir):
    block = {"reconstructors": ["view-only"], "processors": ["user.tiny-test", "resize"]}
    dlg = _dialog(copy.deepcopy(block), dropin_directory=str(plugins_dir))
    note = _dropin_rows(dlg)[0][4]
    assert "listed in this setup" in note and "not portable" in note
    dlg._try_apply()
    assert_json_identical(dlg.result_data, block)    # the id stays as it was


def test_an_empty_plugins_folder_says_so(qapp, tmp_path):
    dlg = _dialog({}, dropin_directory=str(tmp_path / "nothing-here"))
    assert dlg._dropin_tree.topLevelItemCount() == 0
    assert "No drop-in plugin files found" in _texts(dlg)


def test_the_dialog_works_when_improcess_cannot_be_imported(qapp, monkeypatch):
    monkeypatch.setitem(sys.modules, "imswitch.improcess.plugins.user_plugins", None)
    dlg = _apply(copy.deepcopy(RICH_BLOCK))
    assert "Drop-in discovery is unavailable" in _texts(dlg)
    assert_json_identical(dlg.result_data, RICH_BLOCK)


def test_discovery_does_not_run_at_import_or_for_the_canvas(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(editor, "_discover_dropins", lambda *a, **k: calls.append(1) or editor._DropIns())
    editor.DeviceCanvas().load({"processing": {"reconstructors": ["view-only"]}})
    assert calls == []
    _dialog({})
    assert calls == [1]


# ── System card and picker ──────────────────────────────────────────────────
def test_the_system_card_no_longer_offers_processing(qapp):
    panel = editor.PropertyEditor()
    panel.load_config_extras({"processing": {"reconstructors": ["view-only"]}, "scan": {}})
    rows = _texts(panel._sys_sections_inner)
    assert "ImProcess" not in rows and "processing" not in rows
    add = _button(panel._sys_sections_inner, "＋ Add System Section…")
    assert "processing" not in add.toolTip()
    assert "processing" not in _buttons(panel._sections_inner), "and it is not an Extras entry either"


def test_the_system_card_does_not_offer_processing_on_an_empty_document(qapp):
    panel = editor.PropertyEditor()
    panel.load_config_extras({})
    assert "processing" not in _button(panel._sys_sections_inner, "＋ Add System Section…").toolTip()
    hdr = next(l for l in panel.findChildren(QLabel) if l.text().startswith("<b>System</b>"))
    assert "processing" not in hdr.text()


def test_the_picker_does_not_list_processing(qapp, monkeypatch):
    seen = []

    class Picker:
        chosen_key = None

        def __init__(self, candidates, parent=None):
            seen.extend(key for key, _ in candidates)

        def exec_(self):
            return QDialog.Rejected

    monkeypatch.setattr(editor, "SectionPickerDialog", Picker)
    panel = editor.PropertyEditor()
    panel.load_config_extras({})
    panel._open_section_picker()
    assert seen and "processing" not in seen


def test_the_system_row_route_opens_the_improcess_dialog(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        editor.ImProcessSectionDialog, "exec_",
        lambda self: opened.append(type(self).__name__) or QDialog.Rejected,
    )
    panel = editor.PropertyEditor()
    panel.load_config_extras({"processing": {"reconstructors": ["view-only"]}})
    panel._open_section_schema("processing")
    assert opened == ["ImProcessSectionDialog"]


def test_the_widget_auto_offer_opens_the_improcess_dialog(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(editor, "_WIDGET_REQUIRES_SECTION", {"SomeWidget": "processing"})
    monkeypatch.setattr(
        editor.WidgetPickerDialog, "exec_", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        editor.WidgetPickerDialog, "selected_widgets", lambda self: ["SomeWidget"])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(
        editor.ImProcessSectionDialog, "exec_",
        lambda self: (opened.append(1), self._try_apply(), self.result())[2],
    )
    panel = editor.PropertyEditor()
    data = {}
    panel.load_config_extras(data)
    panel._open_widget_picker()
    assert opened == [1]
    assert_json_identical(data["processing"], editor._new_section_seed(PROCESSING))


# ── the main window ─────────────────────────────────────────────────────────
@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    setup_dir = tmp_path / "imcontrol_setups"
    setup_dir.mkdir()
    monkeypatch.setattr(editor.MainWindow, "_OPTIONS_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(editor, "_default_setup_dir", lambda: setup_dir)
    monkeypatch.setattr(editor, "_user_template_dir", lambda: tmp_path / "templates")
    win = editor.MainWindow(start_folder=str(setup_dir))
    yield win
    # closing a modified window would ask what to do with the changes
    win._modified = False
    win._compare_modified = False
    win.close()


def _accept_untouched(monkeypatch):
    monkeypatch.setattr(
        editor.ImProcessSectionDialog, "exec_",
        lambda self: (self._try_apply(), self.result())[1],
    )


def test_configure_edit_and_remove_from_the_canvas(window, monkeypatch):
    _accept_untouched(monkeypatch)
    window._data.pop("processing", None)
    window._refresh_canvas()
    assert window._modified is False

    _button(window._canvas, "Configure…").click()
    assert_json_identical(window._data["processing"], editor._new_section_seed(PROCESSING))
    assert window._modified is True
    assert "Edit…" in _buttons(window._canvas) and "Remove" in _buttons(window._canvas)

    before = copy.deepcopy(window._data["processing"])
    _button(window._canvas, "Edit…").click()
    assert_json_identical(window._data["processing"], before)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    _button(window._canvas, "Remove").click()
    assert "processing" in window._data

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    _button(window._canvas, "Remove").click()
    assert "processing" not in window._data
    assert "Configure…" in _buttons(window._canvas)


def test_the_validation_link_opens_the_improcess_dialog(window, monkeypatch):
    _accept_untouched(monkeypatch)
    window._data.pop("processing", None)
    window._refresh_canvas()
    window._on_fix_section_from_validation("processing")
    assert_json_identical(window._data["processing"], editor._new_section_seed(PROCESSING))
    assert "Edit…" in _buttons(window._canvas)


def test_a_change_from_the_system_card_route_redraws_the_canvas_card(window):
    window._refresh_canvas()
    window._data["processing"] = {"reconstructors": ["view-only"]}
    window._on_extras_modified()
    assert "Edit…" in _buttons(window._canvas)


# ── option labels ───────────────────────────────────────────────────────────
def _field(key):
    return editor._schema_fields(PROCESSING)[key]


def test_the_options_carry_display_names_and_categories():
    processors, reconstructors = _field("processors"), _field("reconstructors")
    assert reconstructors["option_labels"]["monalisa"] == "MoNaLISA"
    assert processors["option_labels"]["resize"] == "Scale/Resize"
    assert processors["option_groups"]["resize"] == "Transform"
    assert "option_groups" not in reconstructors
    assert set(processors["option_labels"]) <= set(processors["opts"])


def test_without_the_registries_the_options_are_bare_ids(monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("imswitch.improcess"):
            raise ImportError("improcess not installed")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(editor, "PLUGIN_DISCOVERY_ERROR", None)
    schema = {"fields": [
        {"key": "reconstructors", "opts": ["__improcess_reconstructors__"]},
        {"key": "processors", "opts": ["__improcess_processors__"]},
    ]}
    editor._resolve_section_dynamic_options(schema)
    monkeypatch.setattr(editor, "PLUGIN_DISCOVERY_ERROR", None)
    for field in schema["fields"]:
        assert "option_labels" not in field and "option_groups" not in field
        assert field["opts"]


def _rows(fw) -> dict:
    """``{id: (name, muted id text or None, description or None)}`` of a rich list."""
    box = fw._w
    out = {}
    for option_id in box.option_ids():
        check = box.checkbox(option_id)
        row = check.parentWidget()
        labels = [l.text() for l in row.findChildren(QLabel)]
        ident = option_id if option_id in labels else None
        description = next((t for t in labels if t != option_id), None)
        out[option_id] = (check.text(), ident, description)
    return out


def test_a_multiselect_shows_name_id_and_description_but_returns_ids(qapp):
    fw = editor.FieldWidget(_field("reconstructors"), ["view-only"])
    rows = _rows(fw)
    name, ident, description = rows["monalisa"]
    assert name == "MoNaLISA" and ident == "monalisa"
    assert description == "Point-scanning SIM reconstruction with pattern-based signal extraction"
    # untouched: the file's value, as it was
    original = ["view-only"]
    fw = editor.FieldWidget(_field("reconstructors"), original)
    assert fw.get_value() is original and not fw.is_touched()
    fw._w.checkbox("monalisa").click()
    assert fw.is_touched()
    assert fw.get_value() == ["monalisa", "view-only"], "ids, in the schema's option order"


def test_every_reconstructor_row_has_a_description(qapp):
    """All of them: the ``description`` attribute, else the docstring's first line."""
    rows = _rows(editor.FieldWidget(_field("reconstructors"), []))
    assert len(rows) >= 11
    assert all(description for _, _, description in rows.values())
    # monalisa-legacy has no ``description``: its docstring's first line stands in
    assert rows["monalisa-legacy"][2].startswith("Classic MoNaLISA")


def test_the_descriptions_come_from_the_registry_attribute_or_the_docstring():
    class WithAttr:
        """Docstring line.\n\nMore."""
        description = "  The attribute.  "

    class WithDoc:
        """First line of the docstring.

        Second paragraph."""

    class Neither:
        pass

    assert editor._plugin_description(WithAttr) == "The attribute."
    assert editor._plugin_description(WithDoc) == "First line of the docstring."
    assert editor._plugin_description(Neither) == ""


def test_the_processor_descriptions_are_the_first_docstring_line():
    field = _field("processors")
    assert field["option_descriptions"]["drift-correct"] == "Drift correction using phase cross-correlation."
    assert set(field["option_descriptions"]) <= set(field["opts"])


def test_a_multiselect_without_registry_descriptions_stays_a_plain_list(qapp):
    field = {k: v for k, v in _field("reconstructors").items() if k != "option_descriptions"}
    fw = editor.FieldWidget(field, ["view-only"])
    assert isinstance(fw._w, editor.QListWidget)
    texts = [fw._w.item(i).text() for i in range(fw._w.count())]
    assert "MoNaLISA  (monalisa)" in texts
    fw._w.item(texts.index("MoNaLISA  (monalisa)")).setCheckState(Qt.Checked)
    assert fw.get_value() == ["monalisa", "view-only"]


def test_a_multiselect_without_labels_reads_as_before(qapp):
    fw = editor.FieldWidget(
        {"key": "k", "label": "K", "type": "multiselect", "opts": ["a", "b"]}, ["b"]
    )
    assert [fw._w.item(i).text() for i in range(fw._w.count())] == ["a", "b"]
    fw._w.item(0).setCheckState(Qt.Checked)
    assert fw.get_value() == ["a", "b"]


def test_grouped_processors_have_headers_and_keep_unknown_ids(qapp):
    original = ["resize", "user.tiny-test"]
    fw = editor.FieldWidget(_field("processors"), original)
    box = fw._w
    headers = box.header_texts()
    assert "Transform" in headers and "Restoration" in headers
    assert headers[-1] == "Not known here (kept as written)"
    assert headers[:-1] == sorted(headers[:-1], key=str.lower), "categories read alphabetically"
    assert fw.get_value() is original, "untouched: the very object the file had"
    # the id the registry does not offer is listed, checked, and keeps its name
    assert box.checkbox("user.tiny-test").isChecked()
    assert box.checkbox("user.tiny-test").text() == "user.tiny-test"
    assert _rows(fw)["user.tiny-test"][2] is None
    # touch something unrelated: it survives
    box.checkbox("filter").click()
    value = fw.get_value()
    assert "user.tiny-test" in value and "resize" in value and "filter" in value
    ids = box.option_ids()
    assert value == [i for i in ids if i in value], "stable: option order, extras last"
    assert ids[-1] == "user.tiny-test"


def test_processors_sit_under_their_category_header(qapp):
    fw = editor.FieldWidget(_field("processors"), [])
    box = fw._w
    layout = box.layout()
    seen, current = {}, None
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if widget is None:
            continue
        if isinstance(widget, QLabel):
            current = widget.text()
        else:
            check = widget.findChild(editor.QCheckBox)
            seen[next(k for k, v in box._boxes.items() if v is check)] = current
    assert seen["resize"] == "Transform" and seen["drift-correct"] == "Restoration"
    assert len(box._boxes) == len(box.option_ids()) == len(_field("processors")["opts"]), \
        "one check box per option; the headers are labels, not choices"


def test_a_plain_untouched_list_keeps_the_original_object_for_the_two_pages(qapp):
    block = {"reconstructors": ["view-only"], "processors": ["resize", "mystery"]}
    dlg = _dialog(copy.deepcopy(block))
    for key in ("reconstructors", "processors"):
        assert dlg._field_widgets[key].get_value() == block[key]
        assert not dlg._field_widgets[key].is_touched()


def test_the_tip_of_the_two_lists_is_said_once_at_the_top_of_the_page(qapp):
    dlg = _dialog({})
    for title, key in (("Reconstructors", "reconstructors"), ("Processors", "processors")):
        page = dlg._stack.widget(dlg._page_titles.index(title))
        tip = _field(key)["tip"]
        assert [l.text() for l in page.findChildren(QLabel)].count(tip) == 1
        layout = page.widget().layout()
        assert layout.itemAt(0).widget().text() == tip, "the first thing on the page"
        assert layout.itemAt(1).widget() is dlg._field_widgets[key], "then the list, nothing under it"
