from __future__ import annotations

import os

import pytest

from imswitch.imcontrol.model.configeditor import slm_authoring, structured


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _definition():
    return {
        "identity": {"serial_number": "X123", "display_name": "Pupil"},
        "geometry": {"width": 1920, "height": 1152, "pixel_size_um": 9.2},
        "sections": {
            "layout": {"n_sections": 2, "axis": "x", "mode": "even", "key_prefix": "sec_"},
            "customizable": False,
        },
    }


def test_slmcore_definition_schema_is_flattened_without_host_owned_key():
    specs = slm_authoring.structured_top_level_objects()
    assert [spec.key for spec in specs] == ["definition", "startup_preferences"]

    definition = specs[0]
    fields = {field.path: field for field in structured.flatten_object_schema(definition.schema)}
    assert ("identity", "key") not in fields
    assert ("identity", "serial_number") in fields
    assert fields[("geometry", "width")].type == "int"
    assert fields[("sections", "layout", "axis")].type == "select"
    assert fields[("sections", "layout", "sizes")].type == "json"


def test_startup_dynamic_maps_remain_json_fallbacks():
    startup = slm_authoring.structured_top_level_objects()[1]
    fields = {field.path: field for field in structured.flatten_object_schema(startup.schema)}
    assert fields[("default_mappings",)].type == "json"
    assert fields[("feedback_orientations",)].type == "json"
    assert fields[("section_display_mode",)].type == "select"


def test_slmcore_semantic_validation_is_used():
    device = {
        "definition": _definition(),
        "startup_preferences": {"section_display_mode": "tabs"},
    }
    assert slm_authoring.validate_device("SLM-A", device) == ()

    bad = _definition()
    bad["sections"]["layout"].update(mode="manual", sizes=[100, 100])
    errors = slm_authoring.validate_device("SLM-A", {"definition": bad})
    assert errors
    assert errors[0].startswith("Definition:")


def test_legacy_slm_without_definition_is_not_forced_to_migrate():
    assert slm_authoring.validate_device("old", {"monitorIdx": 2}) == ()


def test_editor_schema_separates_hardware_from_legacy_compatibility():
    schema = {
        "top": [
            {"key": "monitorIdx", "grp": "Basic", "tip": "monitor"},
            {"key": "width", "grp": "Basic", "tip": "width"},
        ],
        "props": [
            {"key": "mockermode", "grp": "Basic", "tip": "mock"},
            {"key": "startConfig", "grp": "Basic", "tip": "start"},
        ],
        "nested": {},
    }

    legacy = slm_authoring.prepare_editor_schema(schema, {"monitorIdx": 2})
    assert {f["key"]: f["grp"] for f in legacy["top"]} == {
        "monitorIdx": "Hardware", "width": "Legacy compatibility"
    }
    assert {f["key"]: f["grp"] for f in legacy["props"]} == {
        "mockermode": "Hardware", "startConfig": "Legacy compatibility"
    }

    canonical = slm_authoring.prepare_editor_schema(
        schema, {"definition": _definition()}
    )
    assert [f["key"] for f in canonical["top"]] == ["monitorIdx"]
    assert [f["key"] for f in canonical["props"]] == ["mockermode"]
    assert canonical["hidden_top_keys"] == {"width"}
    assert canonical["hidden_prop_keys"] == {"startConfig"}
    assert all(f["grp"] == "Hardware" for f in canonical["top"] + canonical["props"])


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_property_editor_renders_definition_and_startup_as_structured_tabs(app):
    from imswitch.imcontrol.view.configeditor.editor import PropertyEditor

    device = {
        "managerName": "HamamatsuSLMdviManager",
        "monitorIdx": 2,
        "width": 792,  # stale compatibility value: preserve, but do not show
        "managerProperties": {"mockermode": True, "startConfig": "legacy.h5"},
        "definition": _definition(),
        "startup_preferences": {"section_display_mode": "tabs", "future_key": {"keep": True}},
    }
    editor = PropertyEditor()
    editor.load_device("slms", "SLM-A", device, {"slms": {"SLM-A": device}})

    tabs = [editor._tabs.tabText(i) for i in range(editor._tabs.count())]
    assert tabs[0] == "Definition"
    assert "Hardware" in tabs
    assert "Startup" in tabs
    assert "Legacy compatibility" not in tabs
    assert "Other" not in tabs
    assert "definition" not in editor._collect_unknown({
        "top": [], "props": [], "nested": {},
        "structured_top_keys": {"definition", "startup_preferences"},
    })[1]

    # Unknown keys inside a slmcore-owned object survive a no-op Apply.
    captured = []
    editor.sig_apply.connect(lambda _cat, _name, value: captured.append(value))
    editor._do_apply()
    assert captured
    assert captured[-1]["startup_preferences"]["future_key"] == {"keep": True}
    assert captured[-1]["width"] == 792
    assert captured[-1]["managerProperties"]["startConfig"] == "legacy.h5"


def test_invalid_structured_edit_is_not_applied(app):
    from imswitch.imcontrol.view.configeditor.editor import PropertyEditor

    device = {
        "managerName": "HamamatsuSLMdviManager",
        "monitorIdx": 2,
        "definition": _definition(),
    }
    editor = PropertyEditor()
    editor.load_device("slms", "SLM-A", device, {"slms": {"SLM-A": device}})

    definition_editor = editor._structured_widgets["definition"]
    mode = definition_editor._field_widgets[("sections", "layout", "mode")]
    sizes = definition_editor._field_widgets[("sections", "layout", "sizes")]
    mode._w.setCurrentIndex(mode._w.findData("manual"))
    mode._touched = True
    sizes._w.setText("[100, 100]")
    sizes._touched = True

    captured = []
    editor.sig_apply.connect(lambda *_args: captured.append(True))
    editor._do_apply()
    assert captured == []
    assert "Definition:" in editor._val_lbl.text()
