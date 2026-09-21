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
