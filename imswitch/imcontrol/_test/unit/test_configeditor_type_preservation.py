"""The config editor must give back the kind of value it was handed.

A setup file that said ``"analogChannel": 3`` came back from the editor
saying ``"3"``: every value without a dedicated widget is rendered as a line
of text and was read back as a string. ``SetupInfo.getAnalogChannel()`` only
builds ``Dev1/ao3`` for an int, so the line silently stopped resolving. The
raw tab had the mirror-image bug -- a port ``"5"`` came back as ``5`` -- and a
select whose options were strings wrote ``"9600"`` over a baud rate that every
shipped setup stores as a number.
"""

import pytest

from imswitch.imcontrol.model.configeditor.coercion import (
    options_like, raw_text_to_json, text_to_json,
)
from imswitch.imcontrol.model.configeditor.defaults import build_default_device


# ── text fields ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("original", [3, 0, -7, 2.5, 1e-6, True, False, [1, 2], ["a", "b"]])
def test_unedited_text_gives_back_the_original_untouched(original):
    display = ", ".join(str(v) for v in original) if isinstance(original, list) else str(original)
    assert text_to_json(display, original) is original or text_to_json(display, original) == original
    assert type(text_to_json(display, original)) is type(original)


def test_edited_int_stays_an_int():
    assert text_to_json("4", 3) == 4
    assert isinstance(text_to_json("4", 3), int)


def test_edited_float_stays_a_float():
    assert text_to_json("0.05", 0.01) == 0.05


def test_edited_int_that_no_longer_parses_becomes_text():
    """Typing a channel *name* over a channel number is a legitimate edit."""
    assert text_to_json("Dev1/ao3", 3) == "Dev1/ao3"


def test_bool_accepts_the_spellings_people_type():
    assert text_to_json("false", True) is False
    assert text_to_json("Yes", False) is True


def test_a_list_keeps_its_element_type():
    assert text_to_json("1, 2, 3", [1, 2]) == [1, 2, 3]
    assert text_to_json("X, Y", ["X"]) == ["X", "Y"]


def test_a_string_is_never_promoted():
    """A serial number that happens to be digits must stay a string."""
    assert text_to_json("0042", "0041") == "0042"
    assert text_to_json("5", "COM3") == "5"


def test_null_is_null_whatever_it_was():
    assert text_to_json("null", 3) is None
    assert text_to_json("NULL", "x") is None


def test_typed_fields_keep_their_own_coercion():
    assert text_to_json("12", "anything", "int") == 12
    assert text_to_json("abc", 3, "int") == "abc"


# ── raw (untemplated) properties ──────────────────────────────────────────
def test_raw_string_stays_a_string_even_when_it_looks_like_json():
    assert raw_text_to_json("5", "5") == "5"
    assert raw_text_to_json("true", "true") == "true"
    assert raw_text_to_json("[1,2]", "[1,2]") == "[1,2]"


def test_raw_number_is_parsed():
    assert raw_text_to_json("6", 5) == 6
    assert raw_text_to_json("[1, 2, 3]", [1]) == [1, 2, 3]


def test_raw_text_that_is_not_json_is_kept_rather_than_lost():
    assert raw_text_to_json("Dev1/ao3", 3) == "Dev1/ao3"


def test_raw_null():
    assert raw_text_to_json("null", 3) is None


# ── select options ────────────────────────────────────────────────────────
def test_string_options_follow_a_numeric_saved_value():
    assert options_like(["9600", "115200"], 9600) == [9600, 115200]
    assert options_like(["0", "90"], 90.0) == [0.0, 90.0]


def test_options_stay_strings_for_a_string_value():
    assert options_like(["9600", "115200"], "9600") == ["9600", "115200"]


def test_options_stay_strings_when_one_does_not_parse():
    """Half-retyped options would make two entries compare unequal."""
    assert options_like(["9600", "auto"], 9600) == ["9600", "auto"]


def test_bool_is_not_treated_as_a_number():
    assert options_like(["0", "1"], True) == ["0", "1"]


# ── new-device defaults ───────────────────────────────────────────────────
def test_a_numeric_select_default_reaches_a_new_device_as_a_number():
    template = {
        "top": [],
        "props": [
            {"key": "baudrate", "type": "select", "default": 9600, "opts": [9600, 115200]},
            {"key": "port", "type": "text", "default": "COM3"},
            {"key": "timeout", "type": "float", "default": "1.5"},
        ],
        "nested": {},
    }
    device = build_default_device("RS232Manager", template=template, json_schema=None)
    props = device["managerProperties"]
    assert props["baudrate"] == 9600 and isinstance(props["baudrate"], int)
    assert props["port"] == "COM3"
    assert props["timeout"] == 1.5, "text defaults still go through the typed coercion"
