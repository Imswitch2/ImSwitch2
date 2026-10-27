"""Edit -> Apply on any System section writes the block as the file has it.

Before the ``as_written`` policy covered every section, one Edit -> Apply of
``focusLock``, ``autofocus`` or ``tiling`` dropped every key the template did
not list and expanded a short block into every template field. These tests
drive the real dialog over every shipped setup and compare the result with
the input, kind for kind and in key order.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QCheckBox, QLineEdit

from imswitch.imcontrol.view.configeditor import editor
from imswitch.imcontrol.view.configeditor.editor import SectionEditorDialog

from imswitch.imcontrol._test.unit.configeditor_testing import SHIPPED_SETUPS, assert_json_identical

#: Every System section except ImProcess (``processing``), which has its own tests.
SECTIONS = sorted(k for k, s in editor.SECTION_SCHEMAS.items() if k != "processing" and s.get("section"))


def _shipped_blocks():
    out = []
    for path in sorted(SHIPPED_SETUPS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in SECTIONS:
            block = data.get(key)
            if isinstance(block, dict) and block:
                out.append(pytest.param(key, block, data, id=f"{path.stem}:{key}"))
    return out


SHIPPED = _shipped_blocks()


def _dialog(key, current, data=None):
    return SectionEditorDialog(key, editor.SECTION_SCHEMAS[key], current, data or {})


def _apply(key, current, data=None):
    dlg = _dialog(key, current, data)
    dlg._try_apply()
    return dlg


def test_there_are_shipped_blocks_to_check():
    assert {p.values[0] for p in SHIPPED} >= {"scan", "nidaq", "tiling", "focusLock", "teensyPulse"}
    assert len(SHIPPED) >= 20


def test_every_system_section_but_processing_is_covered():
    assert set(SECTIONS) >= {
        "focusLock", "autofocus", "tiling", "scan", "microscopeStand", "etSTED",
        "nidaq", "pulseStreamer", "teensyPulse", "pyroServerInfo",
    }


@pytest.mark.parametrize("key, block, data", SHIPPED)
def test_apply_untouched_returns_exactly_the_input(qapp, key, block, data):
    dlg = _apply(key, copy.deepcopy(block), data)
    assert dlg.result_data, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, block)
    assert list(dlg.result_data) == list(block)


@pytest.mark.parametrize("key, block, data", SHIPPED)
def test_a_key_the_template_does_not_list_survives_on_the_custom_tab(qapp, key, block, data):
    future = {**copy.deepcopy(block), "someFutureKey": {"a": [1, 2.5, None]}}
    dlg = _dialog(key, copy.deepcopy(future), data)
    assert "someFutureKey" in dlg._custom_widgets
    dlg._try_apply()
    assert_json_identical(dlg.result_data, future)
    assert list(dlg.result_data) == list(future)


@pytest.mark.parametrize("key", SECTIONS)
def test_an_empty_optional_only_block_is_not_expanded(qapp, key):
    """A section whose file block has no key at all gains none by Apply."""
    schema = editor.SECTION_SCHEMAS[key]
    if any(f.get("req") for f in schema["fields"]):
        pytest.skip("has required fields: Apply refuses an empty block")
    dlg = _apply(key, {})
    assert dlg.result_data == {}


def _bool_cases():
    cases = []
    for key in SECTIONS:
        for f in editor.SECTION_SCHEMAS[key]["fields"]:
            if f["type"] == "bool":
                cases.append(pytest.param(key, f["key"], id=f"{key}.{f['key']}"))
    return cases


def _a_valid_block(key):
    """A block that applies: the first shipped one, else the seed with its required fields filled."""
    for param in SHIPPED:
        if param.values[0] == key:
            return copy.deepcopy(param.values[1]), param.values[2]
    raise AssertionError(f"no shipped block for {key}")


@pytest.mark.parametrize("key, field", _bool_cases())
def test_touching_one_checkbox_changes_only_that_key(qapp, key, field):
    try:
        block, data = _a_valid_block(key)
    except AssertionError:
        schema = editor.SECTION_SCHEMAS[key]
        block, data = {}, {}
        if any(f.get("req") for f in schema["fields"]):
            pytest.skip("no shipped block and required fields to fill")
    dlg = _dialog(key, copy.deepcopy(block), data)
    box = dlg._field_widgets[field]._w
    assert isinstance(box, QCheckBox)
    before = box.isChecked()
    box.click()
    dlg._try_apply()
    assert dlg.result_data, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, {**block, field: not before})
    # the other keys keep the file's order, a new key goes last
    assert list(dlg.result_data)[: len(block)] == list(block)


# -------------------------------------------------------------- new sections

def _required(key):
    return [f for f in editor.SECTION_SCHEMAS[key]["fields"] if f.get("req")]


@pytest.mark.parametrize("key", SECTIONS)
def test_a_new_section_is_seeded_with_its_required_fields_only(qapp, key):
    schema = editor.SECTION_SCHEMAS[key]
    seed = editor._new_section_seed(schema)
    assert list(seed) == [f["key"] for f in _required(key)]
    for value in seed.values():
        assert value in (None, "", []), "a required field starts empty"


@pytest.mark.parametrize("key", [k for k in SECTIONS if _required(k)])
def test_apply_is_refused_until_every_required_field_is_filled(qapp, key):
    required = _required(key)
    dlg = _apply(key, editor._new_section_seed(editor.SECTION_SCHEMAS[key]))
    assert dlg.result_data == {}
    assert _missing_labels(dlg) == [f["label"] for f in required]

    # Filling them one by one shrinks the list, and the last one lets Apply through.
    filled = {}
    for n, f in enumerate(required):
        filled[f["key"]] = _fill(dlg._field_widgets[f["key"]], f)
        dlg._try_apply()
        left = required[n + 1:]
        if left:
            assert dlg.result_data == {}
            assert _missing_labels(dlg) == [g["label"] for g in left]
    assert dlg.result_data, dlg._err_lbl.text()
    assert list(dlg.result_data) == [f["key"] for f in required]
    assert_json_identical(dlg.result_data, filled)


@pytest.mark.parametrize("key", [k for k in SECTIONS if not _required(k)])
def test_a_new_section_without_required_fields_applies_to_an_empty_block(qapp, key):
    dlg = _apply(key, editor._new_section_seed(editor.SECTION_SCHEMAS[key]))
    assert dlg.result_data == {}
    assert dlg._err_lbl.text() == ""


def _missing_labels(dlg) -> list:
    """The labels Apply says are missing, in the order it names them."""
    prefix = "Missing required: "
    text = dlg._err_lbl.text()
    assert text.startswith(prefix), text
    return text[len(prefix):].split(", ")


def _fill(fw, field):
    """Fill one widget the way a person would; return the value Apply should then write."""
    w = fw._w
    tp = field["type"]
    if tp == "select":
        index = next(i for i in range(w.count()) if w.itemData(i) not in (None, ""))
        w.setCurrentIndex(index)
        w.activated.emit(index)
        return w.itemData(index)
    if tp == "ref":
        QTest.keyClicks(w.lineEdit(), "Dev")
        return "Dev"
    if tp == "int":
        QTest.keyClicks(w, "7")
        return 7
    if tp == "float":
        QTest.keyClicks(w, "1.5")
        return 1.5
    if tp == "json":
        QTest.keyClicks(w, "{}")
        return {}
    assert isinstance(w, QLineEdit), tp
    QTest.keyClicks(w, "abc")
    return "abc"


# ------------------------------------------------- the category-settings path

def test_category_settings_still_write_every_schema_field(qapp):
    """``deviceCategorySettings`` is not a section: it keeps the write-all policy."""
    schema = editor.CATEGORY_SCHEMAS["lasers"]
    assert not schema.get("section")
    assert not editor._writes_as_written(schema)
    assert editor._new_section_seed(schema) == editor._build_default_section(schema)

    dlg = SectionEditorDialog(
        "lasers", schema, {}, {}, title_prefix="Edit category settings", allow_custom_fields=True,
    )
    dlg._try_apply()
    assert set(dlg.result_data) == {f["key"] for f in schema["fields"]}
    assert dlg.result_data == {"powerMode": "coupled"}

    # unknown keys are kept only because the caller asked for it, as before
    dlg = SectionEditorDialog(
        "lasers", schema, {"powerMode": "separated", "x": 1}, {},
        title_prefix="Edit category settings", allow_custom_fields=True,
    )
    dlg._try_apply()
    assert dlg.result_data == {"powerMode": "separated", "x": 1}


def test_a_custom_schema_with_no_section_flag_writes_every_field(qapp):
    schema = {"fields": [{"key": "a", "label": "A", "type": "int", "default": 3, "req": False, "grp": "Basic"}]}
    assert not editor._writes_as_written(schema)
    dlg = SectionEditorDialog("x", schema, {}, {})
    dlg._try_apply()
    assert dlg.result_data == {"a": 3}
    assert not dlg._allow_custom


def test_a_section_can_still_opt_out_explicitly():
    schema = {"section": True, "write_policy": "write_all", "fields": []}
    assert not editor._writes_as_written(schema)
