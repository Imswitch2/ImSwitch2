"""
Test config editor widget→section helper functionality.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

# Import the config editor module by file path
_SCRIPT_PATH = Path(__file__).resolve().parents[4] / "utility_scripts" / "imswitch_config_editor.py"
spec = importlib.util.spec_from_file_location("imswitch_config_editor", _SCRIPT_PATH)
editor = importlib.util.module_from_spec(spec)
sys.modules["imswitch_config_editor"] = editor
spec.loader.exec_module(editor)


def test_widget_section_map_built_from_schemas():
    """
    Widget→section map must be derived from SECTION_SCHEMAS and contain all
    pairings including MotCorr→microscopeStand.
    """
    # Expected pairings from the task spec
    expected_pairings = {
        "FocusLock": "focusLock",
        "Autofocus": "autofocus",
        "Tiling": "tiling",
        "EtSTED": "etSTED",
        "Scan": "scan",
        "MotCorr": "microscopeStand",
    }
    
    # All expected pairings must be in the map
    for widget, section_key in expected_pairings.items():
        assert widget in editor._WIDGET_REQUIRES_SECTION, \
            f"Widget '{widget}' missing from _WIDGET_REQUIRES_SECTION"
        assert editor._WIDGET_REQUIRES_SECTION[widget] == section_key, \
            f"Widget '{widget}' maps to wrong section: " \
            f"expected '{section_key}', got '{editor._WIDGET_REQUIRES_SECTION[widget]}'"


def test_widget_section_map_matches_schemas():
    """
    Every section schema that declares requires_widget must appear in the map,
    and vice versa.
    """
    # Build the expected map from schemas
    expected_from_schemas = {}
    for section_key, schema in editor.SECTION_SCHEMAS.items():
        widget = schema.get("requires_widget")
        if widget:
            expected_from_schemas[widget] = section_key
    
    # The map should exactly match what's in the schemas
    assert editor._WIDGET_REQUIRES_SECTION == expected_from_schemas, \
        "Widget→section map doesn't match section schemas"


def test_xref_issues_emits_warning_when_widget_enabled_without_section():
    """
    _collect_xref_issues must emit a warning when a requiring widget is enabled
    but the matching section is not configured.
    """
    # Config with FocusLock widget enabled but no focusLock section
    data = {
        "availableWidgets": ["FocusLock"],
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Should contain a warning about FocusLock
    warnings = [msg for severity, msg in issues if severity == "warning"]
    focuslock_warnings = [w for w in warnings if "FocusLock" in w and "focusLock" in w]
    assert len(focuslock_warnings) > 0, \
        "Expected warning about FocusLock widget without focusLock section"


def test_xref_issues_no_warning_when_section_present():
    """
    _collect_xref_issues must NOT emit a warning when a requiring widget is
    enabled and the matching section IS configured.
    """
    # Config with FocusLock widget enabled AND focusLock section configured
    data = {
        "availableWidgets": ["FocusLock"],
        "focusLock": {
            "positioner": "SomePositioner",
            "updateFreq": 10
        },
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Should NOT contain a warning about FocusLock
    warnings = [msg for severity, msg in issues if severity == "warning"]
    focuslock_warnings = [w for w in warnings if "FocusLock" in w and "focusLock" in w]
    assert len(focuslock_warnings) == 0, \
        "Unexpected warning about FocusLock when focusLock section is configured"


def test_build_default_section_for_requiring_sections():
    """
    _build_default_section must return a dict containing the section's required
    field keys for each section that requires a widget.
    """
    # Test all sections that require a widget
    for widget, section_key in editor._WIDGET_REQUIRES_SECTION.items():
        schema = editor.SECTION_SCHEMAS.get(section_key)
        assert schema is not None, f"Schema missing for section '{section_key}'"
        
        defaults = editor._build_default_section(schema)
        
        assert isinstance(defaults, dict), \
            f"_build_default_section({section_key}) must return a dict"
        
        # Check that required fields are present
        required_fields = schema.get("required", [])
        for field in required_fields:
            assert field in defaults, \
                f"Required field '{field}' missing from defaults for section '{section_key}'"


def test_microscope_stand_section_uses_loadable_manager_name():
    """The config editor must not suggest the LeicaStand controller as a manager."""
    schema = editor.SECTION_SCHEMAS.get("microscopeStand")
    assert schema is not None

    defaults = editor._build_default_section(schema)

    assert defaults["managerName"] == "LeicaDMIStandMockManager"
    assert defaults["managerName"] in editor.CAT_MANAGERS.get("stands", [])


def test_processing_section_exposes_monalisa_reconstructor():
    """The config editor exposes the public MoNaLISA plugin id."""
    reconstructors = editor._section_option_values("processing", "reconstructors")

    assert "monalisa" in reconstructors


def test_validation_warning_includes_clickable_link():
    """
    Widget→section validation warnings must include a clickable 'Configure…' link.
    """
    # Config with Tiling widget enabled but no tiling section
    data = {
        "availableWidgets": ["Tiling"],
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Find the Tiling warning
    tiling_warnings = [
        msg for severity, msg in issues
        if severity == "warning" and "Tiling" in msg and "tiling" in msg
    ]
    assert len(tiling_warnings) > 0, "Expected warning about Tiling widget"
    
    # Check that the warning includes a clickable link
    warning_msg = tiling_warnings[0]
    assert "fixsection:tiling" in warning_msg, \
        "Warning should include 'fixsection:tiling' link"
    assert "<a href=" in warning_msg, \
        "Warning should include HTML anchor tag"
