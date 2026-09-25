"""Phase 3: the manager's schema is the type authority in the editor.

Three consumers must agree -- ``normalized_fields`` (what a field is),
``materialize_device_schema`` (what the form is handed) and
``build_default_device`` (what a new device carries) -- and the form must
honour the invariant the plan states: untouched stays identical, omitted
stays omitted, spelling is preserved, nothing a widget cannot hold is lost,
unknown stays verbatim. The last test runs the whole corpus (15 shipped
setups, 65 generated fixtures, the value-shape fixtures) through the form and
compares type-strictly.
"""

from __future__ import annotations

import copy

import pytest

from imswitch.imcontrol._test.unit.configeditor_testing import (
    assert_json_identical, corpus, generated_fixtures, shipped_setups, value_shape_fixtures,
)
from imswitch.imcontrol.model.configeditor import resources
from imswitch.imcontrol.model.configeditor.defaults import build_default_device
from imswitch.imcontrol.model.configeditor.schemas import (
    _infer_type_from_schema, materialize_device_schema, normalized_fields, resolve_type,
)

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QCheckBox, QComboBox, QLineEdit  # noqa: E402

from imswitch.imcontrol.view.configeditor import editor  # noqa: E402


# ── the comparison the acceptance criterion rests on ──────────────────────
class TestAssertJsonIdentical:
    @pytest.mark.parametrize("a, b", [(1, 1.0), (1, True), (0, False), ("5", 5), (None, {}), (None, []),
                                      (None, ""), ({}, []), ({"a": None}, {}), ([1], [1, 2]), ("a", "b")])
    def test_kinds_and_values_that_python_equality_confuses(self, a, b):
        with pytest.raises(AssertionError):
            assert_json_identical(a, b)
        with pytest.raises(AssertionError):
            assert_json_identical(b, a)

    def test_identical_documents_pass(self):
        doc = {"a": [1, 2.5, True, None, "x", {"b": {}}], "c": {"d": [[]]}}
        assert_json_identical(copy.deepcopy(doc), doc)

    def test_the_message_names_the_path(self):
        with pytest.raises(AssertionError, match=r"\$\.managerProperties\.hamamatsu\.speed: expected integer 3, got number 3\.0"):
            assert_json_identical({"managerProperties": {"hamamatsu": {"speed": 3.0}}},
                                  {"managerProperties": {"hamamatsu": {"speed": 3}}})
        with pytest.raises(AssertionError, match=r"\$\.managerProperties: keys differ; missing \['b'\], unexpected \['a'\]"):
            assert_json_identical({"managerProperties": {"a": 1}}, {"managerProperties": {"b": 1}})


# ── the widget a schema property asks for ─────────────────────────────────
class TestInferType:
    @pytest.mark.parametrize("prop, expected", [
        ({"x-imswitch-kind": "integer"}, "int"),
        ({"x-imswitch-kind": "number"}, "float"),
        ({"x-imswitch-kind": "boolean"}, "bool"),
        ({"x-imswitch-kind": "string"}, "text"),
        ({"x-imswitch-kind": "object"}, "json"),
        ({"x-imswitch-kind": "array"}, "json"),
        ({"x-imswitch-kind": ["integer", "string"], "type": ["integer", "string"]}, "text"),
        ({"x-imswitch-kind": ["integer", "number"]}, "float"),
        ({"x-imswitch-kind": "integer", "type": ["integer", "number", "string"]}, "int"),
        ({"type": ["string", "null"]}, "text"),
        ({"type": "null"}, "json"),
        ({"x-imswitch-nullable": True}, "json"),
        ({}, "json"),
        ({"x-imswitch-widget": "ref", "x-imswitch-kind": "string"}, "ref"),
        ({"x-imswitch-widget": "path", "x-imswitch-kind": "string"}, "path"),
        ({"enum": ["a", None], "x-imswitch-kind": "string"}, "select"),
    ])
    def test_kind_before_type_union_to_text_nothing_to_json(self, prop, expected):
        assert _infer_type_from_schema(prop) == expected


class TestResolveType:
    """A template's type survives only as a compatible refinement."""

    @pytest.mark.parametrize("template_type, prop, expected", [
        ("select", {"x-imswitch-kind": "string"}, "select"),
        ("select", {"x-imswitch-kind": "integer"}, "select"),
        ("select", {"x-imswitch-kind": ["integer", "string"]}, "select"),
        ("select", {"enum": ["a"], "x-imswitch-kind": "string"}, "select"),
        ("multiselect", {"x-imswitch-kind": "array"}, "multiselect"),
        ("path", {"x-imswitch-kind": "string"}, "path"),
        ("ref", {"x-imswitch-kind": "string"}, "ref"),
        ("text", {"x-imswitch-widget": "ref", "x-imswitch-kind": "string"}, "ref"),
        ("select", {"x-imswitch-widget": "ref", "x-imswitch-kind": "string"}, "select"),
        ("text", {"x-imswitch-widget": "path", "x-imswitch-kind": "string"}, "path"),
        ("text", {"x-imswitch-kind": "string"}, "text"),
        ("int", {"x-imswitch-kind": "integer"}, "int"),
        ("json", {"x-imswitch-kind": "object", "type": "object"}, "json"),
        ("json", {"x-imswitch-kind": "array"}, "json"),
        ("text", {"x-imswitch-kind": "boolean", "x-imswitch-source": ["code:default:boolean"]}, "bool"),
        ("int", {"x-imswitch-kind": ["integer", "string"], "type": ["integer", "string"]}, "text"),
        ("json", {"x-imswitch-kind": "string", "x-imswitch-source": ["code:Path()"]}, "text"),
        ("text", {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:int()", "code:required"]}, "int"),
        ("path", {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:default:integer"]}, "int"),
        ("text", {}, "text"),
        ("select", {"x-imswitch-nullable": True, "x-imswitch-source": ["code:optional(get)"]}, "select"),
        (None, {"x-imswitch-kind": "number"}, "float"),
        (None, {}, "text"),
        ("", {"x-imswitch-kind": "boolean"}, "bool"),
    ])
    def test_refinements_equivalents_and_schema_wins(self, template_type, prop, expected):
        assert resolve_type(template_type, prop) == expected

    def test_a_numeric_kind_from_an_example_alone_does_not_overrule_a_numeric_template(self):
        """An example setup writing -10 does not prove the manager wants an int."""
        by_example = {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:required", "example:integer"]}
        assert resolve_type("float", by_example) == "float"
        by_docs = {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:optional(get)", "docs:integer"]}
        assert resolve_type("float", by_docs) == "float"
        by_code = {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:default:integer", "code:optional(get)"]}
        assert resolve_type("float", by_code) == "int"
        assert resolve_type("int", {"x-imswitch-kind": "number", "x-imswitch-source": ["code:float()"]}) == "float"


# ── normalized_fields: what the two sources make of one property ──────────
SCHEMA = {
    "type": "object",
    "required": ["port", "level"],
    "properties": {
        "port": {"x-imswitch-kind": "string", "x-imswitch-widget": "ref", "x-imswitch-ref-category": "rs232devices"},
        "level": {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:int()", "code:required"]},
        "gain": {"x-imswitch-kind": "number", "x-imswitch-nullable": True, "x-imswitch-source": ["code:optional(get)"]},
        "mode": {"enum": ["a", "b"], "x-imswitch-kind": "string", "description": "which mode"},
        "seed": {"x-imswitch-aliases": ["old_seed"], "x-imswitch-nullable": True},
        "old_seed": {"x-imswitch-alias-of": "seed", "x-imswitch-nullable": True},
        "flag": {"x-imswitch-kind": "boolean", "default": True},
        "table": {"x-imswitch-kind": "object", "type": "object"},
    },
}
TEMPLATE = {
    "top": [{"key": "wavelength", "label": "Wavelength", "type": "int", "default": 488, "req": True, "grp": "Basic", "tip": "", "opts": []}],
    "props": [
        {"key": "port", "label": "Serial port", "type": "text", "default": "COM3", "req": False, "grp": "Basic", "tip": "t", "opts": []},
        {"key": "level", "label": "Level", "default": "5", "req": False, "grp": "Basic", "tip": "", "opts": []},
        {"key": "extra", "label": "Template only", "type": "select", "default": "x", "req": False, "grp": "Advanced", "tip": "", "opts": ["x", "y"]},
    ],
    "nested": {"table": [{"key": "speed", "label": "Speed", "type": "int", "default": 1, "req": False, "grp": "Basic", "tip": "", "opts": []}]},
}


class TestNormalizedFields:
    def test_schema_only_properties_are_typed_labelled_and_grouped(self):
        by_key = {f.key: f for f in normalized_fields(template=None, json_schema=SCHEMA)}
        assert set(by_key) == {"port", "level", "gain", "mode", "seed", "flag", "table"}, "the alias copy is not a field"
        assert by_key["port"].type == "ref" and by_key["port"].options == ("rs232devices",)
        assert by_key["port"].required and by_key["port"].required_by_schema and by_key["port"].default == ""
        assert by_key["level"].type == "int" and by_key["level"].default == 0, "required: seeded with the kind's empty value"
        assert by_key["gain"].type == "float" and by_key["gain"].nullable and by_key["gain"].default is None
        assert by_key["mode"].type == "select" and by_key["mode"].options == ("a", "b") and by_key["mode"].tooltip == "which mode"
        assert by_key["seed"].type == "json" and by_key["seed"].aliases == ("old_seed",)
        assert by_key["flag"].default is True and by_key["flag"].required is False
        assert by_key["table"].type == "json"
        assert all(f.group == "Properties" and f.location == "prop" for f in by_key.values())

    def test_a_template_backed_property_is_refined_not_replaced(self):
        by_key = {(f.location, f.key): f for f in normalized_fields(template=TEMPLATE, json_schema=SCHEMA)}
        port = by_key[("prop", "port")]
        assert port.label == "Serial port" and port.tooltip == "t" and port.group == "Basic", "presentation stays the template's"
        assert port.type == "ref" and port.options == ("rs232devices",), "the schema's widget wins over plain text"
        assert port.required is True and port.required_by_schema is True, "the schema strengthens requiredness"
        assert port.default == "COM3"
        level = by_key[("prop", "level")]
        assert level.type == "int", "a template field with no type takes the schema's kind"
        assert by_key[("prop", "extra")].type == "select", "a property the schema does not know keeps its template type"
        assert by_key[("top", "wavelength")].type == "int"
        assert ("nested", "speed") in by_key and ("prop", "table") not in by_key, "a nested container the template lays out is not also a json field"

    def test_a_template_without_type_and_no_schema_is_text(self):
        [level] = [f for f in normalized_fields(template={"props": [{"key": "level", "default": "5"}]}, json_schema=None)]
        assert level.type == "" or level.type == "text"
        materialized = materialize_device_schema(template={"props": [{"key": "level", "default": "5"}]}, json_schema=None)
        assert materialized["props"][0]["type"] == "text"


class TestMaterialize:
    def test_template_backed_fields_receive_the_schema_facts(self):
        form = materialize_device_schema(template=TEMPLATE, json_schema=SCHEMA)
        props = {f["key"]: f for f in form["props"]}
        assert props["port"]["type"] == "ref" and props["port"]["opts"] == ["rs232devices"]
        assert props["port"]["req"] is True and props["port"]["schema_req"] is True
        assert props["level"]["type"] == "int" and props["level"]["schema_req"] is True
        assert "schema_req" not in props["extra"]
        assert props["gain"]["nullable"] is True and props["gain"]["default"] is None and props["gain"]["req"] is False
        assert props["seed"]["aliases"] == ["old_seed"] and "old_seed" not in props
        assert props["mode"]["tip"] == "which mode"
        assert list(props) == ["port", "level", "extra", "gain", "mode", "seed", "flag"], "template order, then the schema's"

    def test_the_inputs_are_not_mutated(self):
        template, schema = copy.deepcopy(TEMPLATE), copy.deepcopy(SCHEMA)
        materialize_device_schema(template=template, json_schema=schema)
        assert template == TEMPLATE and schema == SCHEMA

    def test_the_real_aaaotf_form(self):
        form = editor._schema_for_manager("AAAOTFLaserManager")
        props = {f["key"]: f for f in form["props"]}
        assert props["rs232device"]["type"] == "ref" and props["rs232device"]["opts"] == ["rs232devices"]
        assert props["channel"]["type"] == "int" and props["channel"]["schema_req"] is True
        assert props["protocolProfile"]["type"] == "select"
        assert props["protocolProfile"]["opts"] == ["aa.compatibility", "aa.frequency-startup"], "the template's list is the presentation"
        assert props["frequencyMHz"]["type"] == "float" and props["frequencyMHz"]["nullable"] is True
        assert props["calibCsvPath"]["type"] == "path"
        assert props["ttlToggling"]["type"] == "bool" and props["ttlToggling"]["grp"] == "Properties"
        assert props["ttlToggling"]["default"] is None and "schema_req" not in props["ttlToggling"]
        assert props["toggleTrueExternal"]["type"] == "bool"

    def test_the_real_thorcam_form_is_typed_though_it_has_no_template(self):
        form = editor._schema_for_manager("ThorCamTSIManager")
        props = {f["key"]: f for f in form["props"]}
        assert {k: f["type"] for k, f in props.items()} == {
            "cameraPixelSizeUm": "json", "cameraSerial": "text", "defaults": "json",
            "dllLocation": "text", "flushFrameLimit": "int",
            # The SDK ring depth, declared when the audit made it settable.
            "frameBufferDepth": "int",
        }
        assert props["cameraSerial"]["nullable"] is True
        assert props["cameraSerial"]["tip"].startswith("Camera serial number")
        assert {f["key"] for f in form["top"]} == {"analogChannel", "digitalLine", "forAcquisition", "forFocusLock"}

    def test_a_union_kind_gets_the_text_box_even_over_a_template_int(self):
        form = editor._schema_for_manager("HamamatsuManager")
        [field] = [f for f in form["props"] if f["key"] == "cameraListIndex"]
        assert field["type"] == "text"

    def test_apd_alias_spellings_are_one_field(self):
        form = editor._schema_for_manager("APDManager")
        props = {f["key"]: f for f in form["props"]}
        assert props["mockPhotonCountMean"]["type"] == "float"
        assert props["mockPhotonCountMean"]["aliases"] == ["mock_photon_count_mean"]
        assert "mock_photon_count_mean" not in props


# ── build_default_device: what a new device carries ───────────────────────
class TestDefaults:
    def test_seeding_rules(self):
        device = build_default_device("X", template=TEMPLATE, json_schema=SCHEMA)
        assert device["wavelength"] == 488
        props = device["managerProperties"]
        assert props["port"] == "COM3", "a template default, verbatim"
        assert props["level"] == 5, "a text default coerced by the resolved kind when the template states no type"
        assert props["extra"] == "x"
        assert props["table"] == {"speed": 1}
        assert props["flag"] is True, "a schema default"
        assert "gain" not in props and "seed" not in props and "mode" not in props, "optional, no default: not seeded"
        assert "old_seed" not in props

    def test_a_required_schema_only_property_is_seeded_empty(self):
        device = build_default_device("X", template=None, json_schema=SCHEMA)
        assert device["managerProperties"] == {"port": "", "level": 0, "flag": True}

    def test_the_real_aaaotf_new_device(self):
        device = editor._build_default_device("AAAOTFLaserManager")
        assert device["managerProperties"] == {
            "rs232device": "aaaotf", "channel": 1, "protocolProfile": "aa.compatibility",
            "frequencyMHz": 0.0, "calibCsvPath": "",
        }
        assert device["wavelength"] == 488 and device["analogChannel"] is None

    def test_the_real_thorcam_new_device_seeds_nothing_optional(self):
        device = editor._build_default_device("ThorCamTSIManager")
        assert device["managerProperties"] == {}
        # The blank template no longer seeds top-level keys: an omitted
        # forAcquisition *means* the dataclass default (False), and the form
        # shows exactly that.
        assert device == {"managerName": "ThorCamTSIManager", "managerProperties": {}}


# ── the form ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def property_editor(qapp):
    return editor.PropertyEditor()


def _round_trip(pe, section, name, device):
    """Load, apply without touching anything, return what the editor emitted."""
    received = []

    def catch(_cat, _name, emitted):
        received.append(emitted)

    pe.sig_apply.connect(catch)
    try:
        pe.load_device(section, name, copy.deepcopy(device))
        pe._do_apply()
    finally:
        pe.sig_apply.disconnect(catch)
    assert len(received) == 1
    return received[0]


def _apply(pe):
    received = []

    def catch(_cat, _name, emitted):
        received.append(emitted)

    pe.sig_apply.connect(catch)
    try:
        pe._do_apply()
    finally:
        pe.sig_apply.disconnect(catch)
    return received[0]


def _type(fw, text):
    fw._w.clear()
    QTest.keyClicks(fw._w, text)


def test_the_gate_is_lifted():
    info = editor._MANAGER_CATALOG.get("ThorCamTSIManager")
    assert info is not None and info.properties_schema is not None
    assert editor._MANAGER_CATALOG.get("AAAOTFLaserManager").properties_schema["title"] == "AAAOTFLaserManager managerProperties"


def test_every_untemplated_manager_shows_typed_fields_not_a_raw_tab(property_editor):
    """The Phase 3 exit criterion, over every manager without a template."""
    fixtures = generated_fixtures()
    checked = 0
    for info in editor._MANAGER_CATALOG.managers():
        name = info.manager_name
        if name in editor.SCHEMAS or name not in fixtures or not info.properties_schema:
            continue
        form = editor._schema_for_manager(name)
        canonical = {k for k, p in info.properties_schema["properties"].items() if "x-imswitch-alias-of" not in p}
        assert {f["key"] for f in form["props"]} == canonical, name
        assert all(f["type"] != "" for f in form["props"]), name
        [(section, device_name, device)] = [(s, n, d) for s, entries in fixtures[name].items() for n, d in entries.items()]
        property_editor.load_device(section, device_name, copy.deepcopy(device))
        raw = [key for (sec, key) in property_editor._field_widgets if sec == "raw_prop"]
        assert raw == [], f"{name}: {raw} landed in the raw tab"
        checked += 1
    assert checked >= 40, checked


class TestOmittedStaysOmitted:
    def test_an_absent_optional_key_is_not_written_until_edited(self, property_editor):
        device = shipped_setups()["example_sted.json"]["lasers"]["561AOTF"]
        assert "calibCsvPath" not in device["managerProperties"]
        applied = _round_trip(property_editor, "lasers", "561AOTF", device)
        assert_json_identical(applied, device)
        for key in ("calibCsvPath", "ttlToggling", "toggleTrueExternal", "protocolProfile", "frequencyMHz"):
            assert key not in applied["managerProperties"], key
        # The form shows the fields; editing one writes exactly that one.
        property_editor.load_device("lasers", "561AOTF", copy.deepcopy(device))
        _type(property_editor._field_widgets[("props", "calibCsvPath")], "/data/calib.csv")
        property_editor._field_widgets[("props", "ttlToggling")]._w.click()
        applied = _apply(property_editor)
        assert applied["managerProperties"]["calibCsvPath"] == "/data/calib.csv"
        assert applied["managerProperties"]["ttlToggling"] is True
        assert "toggleTrueExternal" not in applied["managerProperties"]
        assert "protocolProfile" not in applied["managerProperties"], "a template default is shown, not saved"

    def test_a_template_required_top_field_the_file_lacks_is_not_invented(self, property_editor):
        device = {"managerName": "APDManager", "managerProperties": {"ctrInputLine": "Dev1/ctr0", "terminal": "PFI0"}}
        applied = _round_trip(property_editor, "detectors", "apd", device)
        assert_json_identical(applied, device)
        assert "forAcquisition" not in applied

    def test_a_schema_required_key_the_file_lacks_is_written(self, property_editor):
        """The manager reads it unguarded; the file is broken without it, and validation says so."""
        device = {"managerName": "AAAOTFLaserManager", "managerProperties": {"rs232device": "aaaotf"}}
        applied = _round_trip(property_editor, "lasers", "aotf", device)
        assert applied["managerProperties"]["channel"] == 1
        warnings = property_editor._val_lbl.text()
        assert "AOTF Channel" not in warnings, "written, so nothing to warn about"
        assert "Wavelength" in warnings, "a template-required top field the file lacks is warned about, not invented"
        assert "wavelength" not in applied


class TestSpellingIsPreserved:
    def _device(self, **props):
        return {"managerName": "APDManager", "managerProperties": {"ctrInputLine": "Dev1/ctr0", "terminal": "PFI0", **props}}

    def test_the_alias_spelling_is_loaded_and_written_back(self, property_editor):
        device = self._device(mock_photon_count_mean=400)
        assert_json_identical(_round_trip(property_editor, "detectors", "apd", device), device)
        property_editor.load_device("detectors", "apd", copy.deepcopy(device))
        fw = property_editor._field_widgets[("props", "mockPhotonCountMean")]
        assert fw._w.text() == "400", "shown under the canonical field"
        _type(fw, "800")
        props = _apply(property_editor)["managerProperties"]
        assert props["mock_photon_count_mean"] == 800 and "mockPhotonCountMean" not in props

    def test_the_canonical_spelling_is_used_for_a_key_the_file_never_had(self, property_editor):
        property_editor.load_device("detectors", "apd", self._device())
        _type(property_editor._field_widgets[("props", "mockPhotonCountMean")], "800")
        props = _apply(property_editor)["managerProperties"]
        assert props["mockPhotonCountMean"] == 800 and "mock_photon_count_mean" not in props

    def test_both_spellings_survive_and_neither_is_shown_as_unknown(self, property_editor):
        device = self._device(mockPhotonCountMean=400.0, mock_photon_count_mean=800.0)
        assert_json_identical(_round_trip(property_editor, "detectors", "apd", device), device)
        assert ("raw_prop", "mock_photon_count_mean") not in property_editor._field_widgets


class TestRawTabOnlyForUnknownKeys:
    def test_a_schema_only_key_is_a_typed_field_and_a_vendor_key_is_raw(self, property_editor):
        device = {"managerName": "AAAOTFLaserManager",
                  "managerProperties": {"rs232device": "aaaotf", "channel": 1, "ttlToggling": True, "vendorExtra": 1}}
        property_editor.load_device("lasers", "aotf", copy.deepcopy(device))
        widgets = property_editor._field_widgets
        assert isinstance(widgets[("props", "ttlToggling")]._w, QCheckBox)
        assert ("raw_prop", "ttlToggling") not in widgets
        assert isinstance(widgets[("raw_prop", "vendorExtra")], QLineEdit)
        assert isinstance(widgets[("props", "rs232device")]._w, QComboBox), "the ref widget"
        assert_json_identical(_apply(property_editor), device)


class TestNothingAWidgetCannotHoldIsLost:
    def test_a_union_key_keeps_each_of_its_kinds(self, property_editor):
        for value in (0, "mock"):
            device = {"managerName": "HamamatsuManager", "managerProperties": {"cameraListIndex": value, "hamamatsu": {}}}
            assert_json_identical(_round_trip(property_editor, "detectors", "cam", device), device)
            fw = property_editor._field_widgets[("props", "cameraListIndex")]
            assert isinstance(fw._w, QLineEdit) and fw._w.validator() is None
        _type(property_editor._field_widgets[("props", "cameraListIndex")], "mock2")
        assert _apply(property_editor)["managerProperties"]["cameraListIndex"] == "mock2"

    def test_nothing_is_clamped_or_rounded(self, property_editor):
        device = {"managerName": "ThorCamTSIManager", "managerProperties": {"flushFrameLimit": 5_000_000_000}}
        assert_json_identical(_round_trip(property_editor, "detectors", "cam", device), device)
        device = {"managerName": "AAAOTFLaserManager",
                  "managerProperties": {"rs232device": "a", "channel": 1, "frequencyMHz": 87.123456789}}
        assert_json_identical(_round_trip(property_editor, "lasers", "l", device), device)
        _type(property_editor._field_widgets[("props", "frequencyMHz")], "87.987654321")
        assert _apply(property_editor)["managerProperties"]["frequencyMHz"] == 87.987654321

    def test_a_stale_select_string_stays_a_string_until_picked(self, property_editor):
        device = {"managerName": "RS232Manager", "managerProperties": {"port": "COM3", "baudrate": "9600", "recv_termination": "\n"}}
        assert_json_identical(_round_trip(property_editor, "rs232devices", "r", device), device)
        fw = property_editor._field_widgets[("props", "baudrate")]
        fw._w.setCurrentIndex(1)
        fw._w.activated.emit(1)
        assert _apply(property_editor)["managerProperties"]["baudrate"] == 19200

    def test_a_nested_container_with_nothing_the_form_knows_comes_back_as_it_was(self, property_editor):
        for container in ({}, None, {"vendor_only": 1}, "odd"):
            device = {"managerName": "TISManager", "managerProperties": {"cameraListIndex": 0, "tis": container}}
            assert_json_identical(_round_trip(property_editor, "detectors", "cam", device), device)


class TestSchemaToFormToApplyToNewDevice:
    def test_aaaotf(self, property_editor):
        device = editor._build_default_device("AAAOTFLaserManager")
        assert_json_identical(_round_trip(property_editor, "lasers", "new", device), device)
        property_editor.load_device("lasers", "new", copy.deepcopy(device))
        _type(property_editor._field_widgets[("props", "channel")], "3")
        combo = property_editor._field_widgets[("props", "rs232device")]._w
        combo.lineEdit().clear()
        QTest.keyClicks(combo.lineEdit(), "aotf2")
        applied = _apply(property_editor)
        assert applied["managerProperties"]["channel"] == 3
        assert applied["managerProperties"]["rs232device"] == "aotf2"
        assert applied["managerProperties"]["protocolProfile"] == "aa.compatibility", "seeded by the template, kept"
        assert "ttlToggling" not in applied["managerProperties"]

    def test_thorcam(self, property_editor):
        device = editor._build_default_device("ThorCamTSIManager")
        assert_json_identical(_round_trip(property_editor, "detectors", "new", device), device)
        property_editor.load_device("detectors", "new", copy.deepcopy(device))
        _type(property_editor._field_widgets[("props", "cameraSerial")], "26925")
        _type(property_editor._field_widgets[("props", "flushFrameLimit")], "10")
        applied = _apply(property_editor)
        assert applied["managerProperties"] == {"cameraSerial": "26925", "flushFrameLimit": 10}
        assert isinstance(applied["managerProperties"]["cameraSerial"], str)
        assert isinstance(applied["managerProperties"]["flushFrameLimit"], int)


# ── the corpus ────────────────────────────────────────────────────────────
_CORPUS = corpus()


def test_the_corpus_is_what_the_plan_says():
    sources = {item[0].split(":", 1)[0] for item in _CORPUS}
    assert sources == {"shipped", "generated", "value-shape"}
    # 16: galvo_apd_mock_scan_setup.json joined the shipped setups with the
    # single-axis scan work. 62 fixtures: the four camera managers whose drivers were never in the tree (Basler, ESP32Cam, GXPIPY, JetsonCam) were removed by the magic-number audit.
    assert len(shipped_setups()) == 16
    assert len(generated_fixtures()) == 62
    assert len(value_shape_fixtures()) == 13
    assert sum(1 for item in _CORPUS if item[0].startswith("shipped")) >= 78


@pytest.mark.parametrize("source, section, name, device", _CORPUS,
                         ids=[f"{s}/{sec}/{n}" for s, sec, n, _ in _CORPUS])
def test_round_trip_is_type_strictly_identical(property_editor, source, section, name, device):
    applied = _round_trip(property_editor, section, name, device)
    assert_json_identical(applied, device)


def test_the_validator_sees_no_new_complaint_after_a_round_trip(property_editor):
    """What the editor writes for a shipped setup validates as cleanly as the file did."""
    pytest.importorskip("jsonschema")
    from imswitch.imcontrol.model.plugins.registry import build_default_registry
    from imswitch.imcontrol.model.plugins.validation import validate_setup_data

    registry = build_default_registry(discover=False)
    for file_name, setup in shipped_setups().items():
        rewritten = copy.deepcopy(setup)
        for section, name, device in list(_iter_devices(setup)):
            rewritten[section][name] = _round_trip(property_editor, section, name, device)
        before = _schema_codes(validate_setup_data(setup, registry))
        after = _schema_codes(validate_setup_data(rewritten, registry))
        assert after == before, file_name


def _iter_devices(setup):
    from imswitch.imcontrol._test.unit.configeditor_testing import devices_in
    return devices_in(setup)


def _schema_codes(report):
    return sorted((d.code, tuple(d.path), d.message) for d in report.diagnostics
                  if d.code in ("manager.schema", "manager.alias-conflict"))


def test_generated_schemas_are_what_the_editor_reads():
    """The editor's catalog and the validator read the same file for a manager."""
    assert editor._MANAGER_CATALOG.get("APDManager").properties_schema == resources.generated_schema_for("APDManager")


# ── review of PR #35: a widget preference is not a constraint ────────────
class TestNumericEditsAreKeptAsTyped:
    """BSC203's travelRangeUm is integer-*kind* (its default is 8000) but the
    manager accepts a fraction; the keystroke validator turned a typed
    "8000.5" into 80005 and Apply saved it."""

    DEVICE = {"managerName": "BSC203StageManager", "axes": ["X"],
              "managerProperties": {"port": "COM9", "travelRangeUm": 8000}}

    def test_a_fraction_typed_into_an_integer_kind_field_is_saved(self, property_editor):
        form = editor._schema_for_manager("BSC203StageManager")
        assert {f["key"]: f["type"] for f in form["props"]}["travelRangeUm"] == "int"
        property_editor.load_device("positioners", "stage", copy.deepcopy(self.DEVICE))
        _type(property_editor._field_widgets[("props", "travelRangeUm")], "8000.5")
        applied = _apply(property_editor)
        assert applied["managerProperties"]["travelRangeUm"] == 8000.5
        assert property_editor._val_lbl.text() == ""

    def test_text_that_is_not_a_number_is_saved_as_typed_and_named(self, property_editor):
        property_editor.load_device("positioners", "stage", copy.deepcopy(self.DEVICE))
        _type(property_editor._field_widgets[("props", "travelRangeUm")], "8000,5")
        applied = _apply(property_editor)
        assert applied["managerProperties"]["travelRangeUm"] == "8000,5"
        assert "'8000,5' is not a number" in property_editor._val_lbl.text()

    def test_an_untouched_string_under_a_numeric_key_is_not_reported(self, property_editor):
        """Not an edit: validation's to report, not Apply's."""
        device = copy.deepcopy(self.DEVICE)
        device["managerProperties"]["travelRangeUm"] = "wide"
        assert_json_identical(_round_trip(property_editor, "positioners", "stage", device), device)
        assert property_editor._val_lbl.text() == ""


# ── review of PR #35: a dict the template lays out is still the schema's ──
NESTED_TEMPLATE = {
    "top": [],
    "props": [{"key": "port", "label": "Port", "default": "COM1", "req": True, "grp": "Basic", "tip": "", "opts": []}],
    "nested": {"table": [
        {"key": "speed", "label": "Speed", "type": "text", "default": "1", "req": False, "grp": "Table", "tip": "", "opts": []},
        {"key": "mode", "label": "Mode", "type": "select", "default": "a", "req": False, "grp": "Table", "tip": "", "opts": ["a", "b"]},
    ]},
}


def _nested_schema(*, container_required: bool) -> dict:
    return {
        "type": "object",
        "required": ["port"] + (["table"] if container_required else []),
        "properties": {
            "port": {"x-imswitch-kind": "string"},
            "table": {
                "type": "object", "x-imswitch-kind": "object",
                "required": ["count"],
                "properties": {
                    "speed": {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:int()"]},
                    "mode": {"x-imswitch-kind": "string"},
                    "count": {"x-imswitch-kind": "integer", "x-imswitch-source": ["code:required"]},
                },
            },
        },
    }


class TestTemplateNestedContainers:
    """The template lays a dict out field by field; the schema still types it.

    Loading Hamamatsu without its required ``hamamatsu`` dict and pressing
    Apply left the dict absent, with no warning: normalisation skipped the
    schema of any dict the template laid out, so neither the dict's
    requiredness nor its children's types reached the form.
    """

    def test_children_are_typed_and_the_container_carries_its_requiredness(self):
        fields = normalized_fields(template=NESTED_TEMPLATE, json_schema=_nested_schema(container_required=True))
        by_key = {(f.location, f.nested_key, f.key): f for f in fields}
        assert by_key[("nested", "table", "speed")].type == "int", "the sub-schema's kind wins over plain text"
        assert by_key[("nested", "table", "mode")].type == "select", "a select stays a refinement"
        count = by_key[("nested", "table", "count")]
        assert count.type == "int" and count.required and count.required_by_schema and count.group == "Table"
        container = by_key[("container", None, "table")]
        assert container.required_by_schema is True
        form = materialize_device_schema(template=NESTED_TEMPLATE, json_schema=_nested_schema(container_required=True))
        assert form["nested_meta"] == {"table": {"schema_req": True}}
        children = {f["key"]: f for f in form["nested"]["table"]}
        assert children["count"]["schema_req"] is True and children["speed"]["type"] == "int"
        assert "schema_req" not in children["speed"]
        optional = materialize_device_schema(template=NESTED_TEMPLATE, json_schema=_nested_schema(container_required=False))
        assert optional["nested_meta"] == {"table": {"schema_req": False}}

    @pytest.fixture
    def with_schema(self, monkeypatch):
        def install(container_required: bool):
            form = materialize_device_schema(template=NESTED_TEMPLATE,
                                             json_schema=_nested_schema(container_required=container_required))
            monkeypatch.setattr(editor, "_schema_for_manager", lambda _name: copy.deepcopy(form))
        return install

    def test_a_required_container_the_file_lacks_is_written_with_only_what_it_requires(self, property_editor, with_schema):
        with_schema(True)
        applied = _round_trip(property_editor, "detectors", "d", {"managerName": "X", "managerProperties": {"port": "COM3"}})
        assert applied["managerProperties"] == {"port": "COM3", "table": {"count": 0}}

    def test_an_optional_container_the_file_lacks_stays_absent(self, property_editor, with_schema):
        with_schema(False)
        device = {"managerName": "X", "managerProperties": {"port": "COM3"}}
        assert_json_identical(_round_trip(property_editor, "detectors", "d", device), device)

    def test_a_required_sub_key_is_written_into_a_dict_the_file_has(self, property_editor, with_schema):
        with_schema(False)
        applied = _round_trip(property_editor, "detectors", "d",
                              {"managerName": "X", "managerProperties": {"port": "COM3", "table": {"speed": 5}}})
        assert applied["managerProperties"]["table"] == {"speed": 5, "count": 0}

    def test_something_that_is_not_a_dict_is_kept_as_it_was(self, property_editor, with_schema):
        with_schema(True)
        device = {"managerName": "X", "managerProperties": {"port": "COM3", "table": None}}
        assert_json_identical(_round_trip(property_editor, "detectors", "d", device), device)

    def test_an_edit_that_creates_an_optional_dict_writes_what_the_dict_requires(self, property_editor, with_schema):
        """Review of PR #37: editing ``speed`` saved ``{"table": {"speed": 7}}``,
        which the schema rejects, and the next untouched Apply added ``count``."""
        jsonschema = pytest.importorskip("jsonschema")
        with_schema(False)
        property_editor.load_device("detectors", "d", {"managerName": "X", "managerProperties": {"port": "COM3"}})
        _type(property_editor._field_widgets[("nested:table", "speed")], "7")
        first = _apply(property_editor)
        assert first["managerProperties"]["table"] == {"speed": 7, "count": 0}
        assert jsonschema.Draft202012Validator(_nested_schema(container_required=False)).is_valid(first["managerProperties"])
        assert_json_identical(_round_trip(property_editor, "detectors", "d", first), first), "stable on the next Apply"

    def test_an_edit_turns_a_non_dict_into_a_complete_dict(self, property_editor, with_schema):
        with_schema(False)
        property_editor.load_device("detectors", "d",
                                    {"managerName": "X", "managerProperties": {"port": "COM3", "table": None}})
        _type(property_editor._field_widgets[("nested:table", "speed")], "7")
        assert _apply(property_editor)["managerProperties"]["table"] == {"speed": 7, "count": 0}

    def test_a_required_field_left_empty_in_a_dict_is_warned_about(self, property_editor, monkeypatch):
        template = copy.deepcopy(NESTED_TEMPLATE)
        template["nested"]["table"][1]["req"] = True  # "mode", a template hint
        form = materialize_device_schema(template=template, json_schema=_nested_schema(container_required=True))
        monkeypatch.setattr(editor, "_schema_for_manager", lambda _name: copy.deepcopy(form))
        _round_trip(property_editor, "detectors", "d",
                    {"managerName": "X", "managerProperties": {"port": "COM3", "table": {"count": 2}}})
        assert "Required: Mode (table)" in property_editor._val_lbl.text()
        _round_trip(property_editor, "detectors", "d",
                    {"managerName": "X", "managerProperties": {"port": "COM3", "table": {"count": 2, "mode": "b"}}})
        assert "(table)" not in property_editor._val_lbl.text()

    def test_a_new_device_seeds_the_required_sub_key_the_template_does_not_list(self):
        device = build_default_device("X", template=NESTED_TEMPLATE, json_schema=_nested_schema(container_required=True))
        assert device["managerProperties"]["table"] == {"speed": 1, "mode": "a", "count": 0}

    def test_the_real_hamamatsu_without_its_dict(self, property_editor):
        """The reviewer's case: the dict is written, empty -- the manager iterates it."""
        applied = _round_trip(property_editor, "detectors", "cam",
                              {"managerName": "HamamatsuManager", "managerProperties": {"cameraListIndex": 0}})
        assert applied["managerProperties"] == {"cameraListIndex": 0, "hamamatsu": {}}
        # The template marks two of its fields as the ones to fill.
        assert "(hamamatsu)" in property_editor._val_lbl.text()
        applied = _round_trip(property_editor, "detectors", "cam",
                              {"managerName": "TISManager", "managerProperties": {"cameraListIndex": 0}})
        assert applied["managerProperties"] == {"cameraListIndex": 0, "tis": {}}
