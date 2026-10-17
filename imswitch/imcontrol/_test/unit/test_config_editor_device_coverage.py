"""
Test config editor device coverage: category registry, blanks, and auto-discovery.
"""
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor

_MANAGERS_ROOT = Path(editor.__file__).resolve().parents[2] / "model" / "managers"


def test_authoritative_category_registry_exists():
    """All 9 categories from the registry must be present in DEVICE_CATS."""
    expected = {
        "detectors", "lasers", "positioners", "rotators",
        "rs232devices", "slms", "flipMirrors", "pulsegen", "stands"
    }
    assert expected <= set(editor.DEVICE_CATS), \
        f"Missing categories: {expected - set(editor.DEVICE_CATS)}"


def test_missing_categories_present():
    """flipMirrors, pulsegen, stands must be in DEVICE_CATS."""
    assert "flipMirrors" in editor.DEVICE_CATS
    assert "pulsegen" in editor.DEVICE_CATS
    assert "stands" in editor.DEVICE_CATS


def test_category_labels_and_colors():
    """All registry categories must have labels and colors."""
    for cat in editor._CATEGORY_REGISTRY:
        assert cat in editor.CAT_LABEL, f"Missing label for {cat}"
        assert cat in editor.CAT_COLOR, f"Missing color for {cat}"


def test_category_settings_schema_exists_for_every_device_category():
    """Every visible category exposes category settings, even with no fields."""
    assert set(editor.DEVICE_CATS) <= set(editor.CATEGORY_SCHEMAS)

    laser_schema = editor.CATEGORY_SCHEMAS["lasers"]
    assert any(field.get("key") == "powerMode" for field in laser_schema["fields"])

    for cat in editor.DEVICE_CATS:
        schema = editor.CATEGORY_SCHEMAS[cat]
        assert schema["category"] == cat
        assert isinstance(schema.get("fields"), list)


def test_empty_category_settings_schema_is_truthy():
    """Empty categories still satisfy DeviceCanvas' Settings-button check."""
    empty_categories = [
        cat for cat in editor.DEVICE_CATS
        if not editor.CATEGORY_SCHEMAS[cat].get("fields")
    ]
    assert empty_categories
    for cat in empty_categories:
        assert editor.CATEGORY_SCHEMAS[cat]


def test_blank_schemas_exist():
    """Every category in the registry must have a non-None blank schema.

    A blank no longer carries a ``top`` list: the top-level keys of a device
    entry are typed from the kind's SetupInfo dataclass (``schemas/kinds/``),
    so the form for an untemplated manager still shows them.
    """
    for cat in editor._CATEGORY_REGISTRY:
        assert cat in editor.BLANK_SCHEMAS, f"Missing blank schema for {cat}"
        blank = editor.BLANK_SCHEMAS[cat]
        assert blank is not None
        assert blank.get("category") == cat
        assert "top" not in blank
        assert "props" in blank
    # An untemplated detector (BaslerManager until the four camera managers whose drivers were never in the tree (Basler, ESP32Cam, GXPIPY, JetsonCam) were removed by the magic-number audit).
    form = editor._schema_for_manager("PhotometricsManager")
    top = {f["key"]: f for f in form["top"]}
    assert top["forAcquisition"]["type"] == "bool" and top["forAcquisition"]["grp"] == "Device"
    assert top["analogChannel"]["type"] == "text"


def test_auto_discovery_finds_templated_managers():
    """Auto-discovery must find at least the managers that have templates."""
    # Check a few known templated managers
    if "TISManager" in editor.SCHEMAS:
        assert "TISManager" in editor.CAT_MANAGERS.get("detectors", [])
    if "NidaqLaserManager" in editor.SCHEMAS:
        assert "NidaqLaserManager" in editor.CAT_MANAGERS.get("lasers", [])


def test_auto_discovery_finds_non_templated_managers():
    """Auto-discovery must find managers that have no template."""
    # BaslerManager exists in the codebase but may not have a template
    managers_root = _MANAGERS_ROOT
    if not managers_root.is_dir():
        pytest.skip("Managers tree not found")
    
    basler_file = managers_root / "detectors" / "BaslerManager.py"
    if basler_file.exists():
        # BaslerManager should be discovered
        assert "BaslerManager" in editor.CAT_MANAGERS.get("detectors", [])


def test_discovered_managers_without_templates_have_display_fallbacks():
    """Template and combo rendering must not assume every manager has a schema."""
    assert "AVManager" in editor.CAT_MANAGERS.get("detectors", [])
    assert "AVManager" not in editor.SCHEMAS
    assert editor._manager_display_name("AVManager") == "AVManager"
    assert "AVManager" in editor._all_known_managers()

    for managers in editor.CAT_MANAGERS.values():
        for manager_name in managers:
            assert editor._manager_display_name(manager_name)


def test_auto_discovery_degrades_gracefully():
    """Discovery must not raise even if managers tree is absent."""
    # The module already loaded, so if it didn't crash, we're good
    assert editor.DISCOVERED_MANAGERS is not None


def test_build_default_device_with_template():
    """Building a device for a templated manager yields correct structure."""
    if "TISManager" not in editor.SCHEMAS:
        pytest.skip("TISManager template not found")
    
    device = editor._build_default_device("TISManager")
    assert device["managerName"] == "TISManager"
    assert "managerProperties" in device
    # TIS detector should have top-level fields
    assert "analogChannel" in device or "forAcquisition" in device


def test_build_default_device_without_template():
    """Building a device for a non-templated manager uses category blank."""
    # Use a known non-templated manager if discovered, or a fake one
    managers_root = _MANAGERS_ROOT
    if not managers_root.is_dir():
        pytest.skip("Managers tree not found")
    
    basler_file = managers_root / "detectors" / "BaslerManager.py"
    if basler_file.exists() and "BaslerManager" not in editor.SCHEMAS:
        device = editor._build_default_device("BaslerManager")
        assert device["managerName"] == "BaslerManager"
        assert "managerProperties" in device
        # No top-level key the dataclass gives a default for is seeded: an
        # omitted forAcquisition *means* False, and the form shows exactly
        # that. Only what the manager's own schema requires is carried.
        assert "analogChannel" not in device
        assert "forAcquisition" not in device
        assert "cameraListIndex" in device["managerProperties"]


def test_category_for_manager_lookup():
    """_get_category_for_manager must resolve both templated and discovered."""
    # Templated
    if "TISManager" in editor.SCHEMAS:
        assert editor._get_category_for_manager("TISManager") == "detectors"
    
    # Discovered non-templated
    managers_root = _MANAGERS_ROOT
    if not managers_root.is_dir():
        pytest.skip("Managers tree not found")
    
    basler_file = managers_root / "detectors" / "BaslerManager.py"
    if basler_file.exists():
        assert editor._get_category_for_manager("BaslerManager") == "detectors"
    
    # Unknown
    assert editor._get_category_for_manager("NonexistentManager") is None


@pytest.fixture
def _qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _category_dialog(cat, current):
    return editor.SectionEditorDialog(
        cat, editor.CATEGORY_SCHEMAS[cat], current, {},
        title_prefix="Edit category settings", allow_custom_fields=True,
    )


def test_category_settings_keep_existing_custom_keys(_qapp):
    """Apply writes back keys the schema does not define, with their types."""
    current = {"powerMode": "separated", "serial": "0042", "maxMw": 50, "flags": [1, 2]}
    dlg = _category_dialog("lasers", current)
    assert set(dlg._custom_widgets) == {"serial", "maxMw", "flags"}
    dlg._try_apply()
    assert dlg.result_data == current


def test_category_settings_add_and_remove_custom_property(_qapp):
    dlg = _category_dialog("positioners", {})
    assert dlg.add_custom_field("homeOnStart", True) == ""
    assert dlg.add_custom_field("  speed ", 2.5) == ""
    assert dlg.add_custom_field("note", "text") == ""
    assert dlg.add_custom_field("speed", 3) != ""     # duplicate key
    assert dlg.add_custom_field("   ", 1) != ""       # empty key
    row = dlg._custom_widgets["note"].parentWidget()
    dlg._remove_custom_row("note", row)
    dlg._try_apply()
    assert dlg.result_data == {"homeOnStart": True, "speed": 2.5}


def test_category_settings_custom_key_cannot_shadow_schema_field(_qapp):
    dlg = _category_dialog("lasers", {})
    assert dlg.add_custom_field("powerMode", "separated") != ""
    dlg._try_apply()
    assert dlg.result_data == {"powerMode": "coupled"}


def test_section_dialog_without_custom_fields_is_unchanged(_qapp):
    dlg = editor.SectionEditorDialog("lasers", editor.CATEGORY_SCHEMAS["lasers"], {"x": 1}, {})
    assert dlg.add_custom_field("y", 2) != ""
    dlg._try_apply()
    # No custom properties are offered; a key the form does not show is
    # kept as it was, as every section form keeps it.
    assert dlg.result_data == {"powerMode": "coupled", "x": 1}


def test_parse_freeform_value():
    assert editor._parse_freeform_value("42") == 42
    assert editor._parse_freeform_value("true") is True
    assert editor._parse_freeform_value("[1, 2]") == [1, 2]
    assert editor._parse_freeform_value("(1, 2)") == (1, 2)
    assert editor._parse_freeform_value("hello") == "hello"
    assert editor._parse_freeform_value("  ") is None
