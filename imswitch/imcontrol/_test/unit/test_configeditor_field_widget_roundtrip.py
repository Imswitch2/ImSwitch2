"""FieldWidget round-trips through real Qt widgets, not just the helpers.

The helpers decide the rule; this checks the widgets actually consult them --
that a text box handed an int gives an int back, that a select handed an int
selects and returns the int entry, and that a stale string in a select lands
on the matching entry instead of growing a duplicate.
"""

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


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
    fw._w.setText("4")
    assert fw.get_value() == 4 and isinstance(fw.get_value(), int)


def test_text_field_edited_to_a_name_becomes_a_string(qapp):
    fw = editor.FieldWidget(_field("analogChannel", "text"), 3)
    fw._w.setText("Dev1/ao3")
    assert fw.get_value() == "Dev1/ao3"


def test_text_field_never_promotes_a_string(qapp):
    fw = editor.FieldWidget(_field("serial", "text"), "0042")
    assert fw.get_value() == "0042"


def test_select_with_string_options_returns_the_saved_int_kind(qapp):
    fw = editor.FieldWidget(_field("baudrate", "select", opts=["9600", "115200"]), 9600)
    assert fw.get_value() == 9600 and isinstance(fw.get_value(), int)
    fw._w.setCurrentIndex(1)
    assert fw.get_value() == 115200 and isinstance(fw.get_value(), int)


def test_select_lands_a_legacy_string_on_the_matching_entry(qapp):
    """A "9600" written by the old editor must not become a second 9600."""
    fw = editor.FieldWidget(_field("baudrate", "select", opts=[9600, 115200]), "9600")
    assert fw._w.count() == 2
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


def test_json_widget_still_accepts_a_template_default_written_as_json_text(qapp):
    """Section templates state dict defaults as the text "{}"; that stays a dict."""
    fw = editor.FieldWidget(dict(_field("params", "json"), default="{}"), "{}")
    assert fw._w.text() == "{}"
    assert fw.get_value() == {}


def test_json_widget_keeps_invalid_text_rather_than_losing_it(qapp):
    fw = editor.FieldWidget(_field("free", "json"), {"a": 1})
    fw._w.setText("{not json")
    assert fw.get_value() == "{not json"


def test_section_default_none_stays_none(qapp):
    schema = {"fields": [{"key": "pinMap", "type": "json", "default": None},
                         {"key": "params", "type": "json", "default": "{}"},
                         {"key": "empty", "type": "json", "default": ""}]}
    out = editor._build_default_section(schema)
    assert out["pinMap"] is None
    assert out["params"] == {}
    assert out["empty"] == {}
