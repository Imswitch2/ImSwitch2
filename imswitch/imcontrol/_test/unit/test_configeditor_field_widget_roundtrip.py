"""FieldWidget round-trips through real Qt widgets, not just the helpers.

The helpers decide the rule; this checks the widgets actually consult them --
that a text box handed an int gives an int back, that a select handed an int
selects and returns the int entry, and that a stale string in a select lands
on the matching entry instead of growing a duplicate.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest

from imswitch.imcontrol.view.configeditor import editor


def _type(fw, text):
    """Edit a line-edit field the way an operator does, so textEdited fires."""
    fw._w.clear()
    QTest.keyClicks(fw._w, text)


def _field(key, tp, **extra):
    base = dict(key=key, label=key, type=tp, default="", req=False, grp="Basic", tip="", opts=[])
    base.update(extra)
    return base


def test_text_field_returns_the_int_it_was_given(qapp):
    fw = editor.FieldWidget(_field("analogChannel", "text"), 3)
    assert fw.get_value() == 3
    assert isinstance(fw.get_value(), int)


def test_text_field_edited_to_another_number_stays_numeric(qapp):
    fw = editor.FieldWidget(_field("analogChannel", "text"), 3)
    _type(fw, "4")
    assert fw.get_value() == 4 and isinstance(fw.get_value(), int)


def test_text_field_edited_to_a_name_becomes_a_string(qapp):
    fw = editor.FieldWidget(_field("analogChannel", "text"), 3)
    _type(fw, "Dev1/ao3")
    assert fw.get_value() == "Dev1/ao3"


def test_text_field_never_promotes_a_string(qapp):
    fw = editor.FieldWidget(_field("serial", "text"), "0042")
    assert fw.get_value() == "0042"


def test_select_with_string_options_returns_the_saved_int_kind(qapp):
    fw = editor.FieldWidget(_field("baudrate", "select", opts=["9600", "115200"]), 9600)
    assert fw.get_value() == 9600 and isinstance(fw.get_value(), int)
    fw._w.setCurrentIndex(1)
    fw._w.activated.emit(1)  # the operator picked it
    assert fw.get_value() == 115200 and isinstance(fw.get_value(), int)


def test_select_lands_a_legacy_string_on_the_matching_entry(qapp):
    """A "9600" written by the old editor shows on the 9600 entry, not a duplicate.

    Untouched, it is given back exactly as it was -- a string -- because the
    editor does not rewrite what nobody changed; validation is what should
    complain about the type. The moment the operator picks an entry, the
    entry's own kind is written.
    """
    fw = editor.FieldWidget(_field("baudrate", "select", opts=[9600, 115200]), "9600")
    assert fw._w.count() == 2
    assert fw._w.currentIndex() == 0
    assert fw.get_value() == "9600"
    fw._w.activated.emit(0)
    assert fw.get_value() == 9600 and isinstance(fw.get_value(), int)


def test_select_keeps_an_unknown_saved_value_selectable(qapp):
    fw = editor.FieldWidget(_field("baudrate", "select", opts=[9600, 115200]), 57600)
    assert fw._w.count() == 3
    assert fw.get_value() == 57600


def test_raw_line_edit_reads_back_through_its_original(qapp):
    """The raw tab stores the original on the line edit; the apply path reads it."""
    from PyQt5.QtWidgets import QLineEdit
    le = QLineEdit("5")
    le.setProperty("originalValue", "5")
    assert editor._coercion_module.raw_text_to_json(le.text(), le.property("originalValue")) == "5"
    le.setProperty("originalValue", 5)
    assert editor._coercion_module.raw_text_to_json(le.text(), le.property("originalValue")) == 5


# ── the JSON widget must round-trip every value ───────────────────────────
# It is where every property no schema can type will land, so None, "" and a
# string that happens to look like JSON must all come back as they went in.
@pytest.mark.parametrize("value", [None, "", "5", "true", "abc", "null", {"a": 1}, [1, "x"], 0, 2.5, False])
def test_json_widget_round_trips_untouched(qapp, value):
    fw = editor.FieldWidget(_field("free", "json"), value)
    assert fw.get_value() == value
    assert type(fw.get_value()) is type(value)


def test_json_widget_shows_a_string_quoted_and_none_as_null(qapp):
    assert editor.FieldWidget(_field("free", "json"), "5")._w.text() == '"5"'
    assert editor.FieldWidget(_field("free", "json"), None)._w.text() == "null"


def test_json_widget_never_mistakes_a_saved_string_for_json_text(qapp):
    """A saved "{}" is a string even when it equals the template default."""
    fw = editor.FieldWidget(dict(_field("params", "json"), default="{}"), "{}")
    assert fw._w.text() == '"{}"'
    assert fw.get_value() == "{}"


@pytest.mark.parametrize("default, expected", [("{}", {}), ("[]", []), ("", {}), ("{bad", {}),
                                               ({"a": 1}, {"a": 1}), (None, None)])
def test_a_json_template_default_is_decoded_before_it_reaches_the_widget(default, expected):
    """Templates state json defaults as text; the call site decodes them once."""
    assert editor._field_default({"key": "params", "type": "json", "default": default}) == expected


def test_a_non_json_default_is_passed_through():
    assert editor._field_default({"key": "port", "type": "text", "default": "COM3"}) == "COM3"


def test_json_widget_keeps_invalid_text_rather_than_losing_it(qapp):
    fw = editor.FieldWidget(_field("free", "json"), {"a": 1})
    _type(fw, "{not json")
    assert fw.get_value() == "{not json"


def test_section_default_none_stays_none(qapp):
    schema = {"fields": [{"key": "pinMap", "type": "json", "default": None},
                         {"key": "params", "type": "json", "default": "{}"},
                         {"key": "empty", "type": "json", "default": ""}]}
    out = editor._build_default_section(schema)
    assert out["pinMap"] is None
    assert out["params"] == {}
    assert out["empty"] == {}


# ── untouched fields give back exactly what they were handed ──────────────
# Widgets alter what they display: a spin box clamps and rounds, a check box
# has no None, a text box reads "null" as null. None of that is an edit.
@pytest.mark.parametrize("tp, value", [
    ("float", 1.23456789e-5),
    ("float", 0.1 + 0.2),
    ("int", 5_000_000_000),
    ("int", None),
    ("float", None),
    ("bool", None),
    ("text", "null"),
    ("text", ""),
    ("select", "not-an-option"),
    ("json", "null"),
])
def test_untouched_field_returns_the_original_object(qapp, tp, value):
    fw = editor.FieldWidget(_field("k", tp, opts=[1, 2]), value)
    assert fw.is_touched() is False
    assert fw.get_value() is value


def test_construction_does_not_count_as_an_edit(qapp):
    for tp, value in [("int", 3), ("float", 2.5), ("bool", True), ("text", "x"),
                      ("select", 1), ("json", {"a": 1}), ("multiselect", ["a"])]:
        fw = editor.FieldWidget(_field("k", tp, opts=[1, 2] if tp == "select" else ["a", "b"]), value)
        assert fw.is_touched() is False, tp


def _clear(fw):
    """Empty a line-edit field the way an operator does (select all, backspace)."""
    from PyQt5.QtCore import Qt
    fw._w.selectAll()
    QTest.keyClick(fw._w, Qt.Key_Backspace)


def test_an_edited_float_is_written_at_full_precision(qapp):
    fw = editor.FieldWidget(_field("conversionFactor", "float"), 1.0)
    _type(fw, "0.00001234")
    assert fw.is_touched()
    assert fw.get_value() == 0.00001234 and isinstance(fw.get_value(), float)


def test_an_edited_int_above_the_old_clamp_survives(qapp):
    fw = editor.FieldWidget(_field("serial", "int"), 1)
    _type(fw, "2000000")
    assert fw.get_value() == 2_000_000
    _type(fw, "6000000000")  # past 32 bits, which QIntValidator would have refused
    assert fw.get_value() == 6_000_000_000 and isinstance(fw.get_value(), int)


def test_a_clicked_checkbox_counts_as_an_edit(qapp):
    fw = editor.FieldWidget(_field("flag", "bool"), None)
    fw._w.click()
    assert fw.is_touched() and fw.get_value() is True


def test_a_typed_text_edit_counts(qapp):
    fw = editor.FieldWidget(_field("port", "text"), "COM3")
    _type(fw, "COM4")
    assert fw.is_touched() and fw.get_value() == "COM4"


# ── a numeric field is a plain line edit: no spin box, no validator ───────
# A spin box is a C++ int that clamps, rounds and cannot hold a string. A
# keystroke validator is no better: it drops what it refuses, so "8000.5"
# typed into an integer-kind box became 80005 (review of PR #35, BSC203's
# travelRangeUm). What is typed stays as typed.
@pytest.mark.parametrize("tp, value", [("int", "two"), ("int", 5_000_000_000), ("int", 2.5),
                                       ("float", "fast"), ("int", True)])
def test_a_value_no_spin_box_could_hold_is_shown_as_it_is_and_survives(qapp, tp, value):
    """int("two") used to crash the editor on load; 5e9 overflowed the C++ int."""
    from PyQt5.QtWidgets import QLineEdit
    fw = editor.FieldWidget(_field("channel", tp), value)
    assert isinstance(fw._w, QLineEdit)
    assert fw._w.text() == str(value)
    assert fw.get_value() is value


def test_a_string_under_a_numeric_key_is_edited_as_free_text(qapp):
    """No validator stands between the operator and fixing "two"."""
    fw = editor.FieldWidget(_field("channel", "int"), "two")
    assert fw._w.validator() is None
    _type(fw, "3")
    assert fw.get_value() == 3 and isinstance(fw.get_value(), int)


@pytest.mark.parametrize("tp", ["int", "float"])
def test_no_keystroke_is_ever_dropped(qapp, tp):
    """What was typed is what is read back -- a number when it is one."""
    fw = editor.FieldWidget(_field("channel", tp), 3)
    assert fw._w.validator() is None
    for typed in ("12abc34", "8000,5", "1_000", "nan", "inf", "0x10", "1e400"):
        _type(fw, typed)
        assert fw._w.text() == typed
        assert fw.get_value() == typed and isinstance(fw.get_value(), str), typed
    _type(fw, "-6000000000")
    assert fw.get_value() == -6_000_000_000


def test_an_integer_kind_box_takes_a_fraction(qapp):
    """The kind is how the text is read back, not what may be typed."""
    fw = editor.FieldWidget(_field("travelRangeUm", "int"), 8000)
    _type(fw, "8000.5")
    assert fw._w.text() == "8000.5"
    assert fw.get_value() == 8000.5 and isinstance(fw.get_value(), float)


def test_an_existing_fraction_under_an_integer_kind_stays_editable(qapp):
    from PyQt5.QtCore import Qt
    fw = editor.FieldWidget(_field("travelRangeUm", "int"), 8000.5)
    fw._w.setCursorPosition(len(fw._w.text()))
    QTest.keyClicks(fw._w, "5")
    assert fw._w.text() == "8000.55" and fw.get_value() == 8000.55
    QTest.keyClick(fw._w, Qt.Key_Backspace)
    QTest.keyClick(fw._w, Qt.Key_Backspace)
    QTest.keyClick(fw._w, Qt.Key_Backspace)
    assert fw.get_value() == 8000 and isinstance(fw.get_value(), int)


def test_a_float_field_takes_a_point_and_scientific_notation(qapp):
    fw = editor.FieldWidget(_field("k", "float"), 1.0)
    _type(fw, "1.5e-3")
    assert fw.get_value() == 0.0015
    _type(fw, "2")
    assert fw.get_value() == 2 and isinstance(fw.get_value(), int), "what was typed is what is written"
    _type(fw, "2.0")
    assert fw.get_value() == 2.0 and isinstance(fw.get_value(), float)


def test_clearing_a_numeric_field_writes_null(qapp):
    fw = editor.FieldWidget(_field("k", "float"), 1.0)
    _clear(fw)
    assert fw.is_touched() and fw.get_value() is None


def test_a_null_number_shows_an_empty_box(qapp):
    fw = editor.FieldWidget(_field("k", "int"), None)
    assert fw._w.text() == "" and fw._w.validator() is None
    assert fw.get_value() is None
    _type(fw, "7")
    assert fw.get_value() == 7
