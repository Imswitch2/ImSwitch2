"""Tests for device plugin diagnostics CLI and validation."""

import json
import tempfile
import pytest

from imswitch.imcontrol.model.plugins.manifest import parse_manifest
from imswitch.imcontrol.model.plugins.registry import build_default_registry
from imswitch.imcontrol.model.plugins.validation import (
    legacy_manager_exists,
    validate_manager_properties,
    validate_setup_file,
    validate_setup_data,
)
from imswitch.imcontrol.model.plugins.__main__ import main


def test_list_command(capsys):
    """Test the 'list' command returns 0 and shows built-in IDs."""
    exit_code = main(["list"])
    captured = capsys.readouterr()
    
    assert exit_code == 0
    assert "AVManager" in captured.out
    assert "[detector]" in captured.out


def test_list_command_with_kind_filter(capsys):
    """Test 'list --kind laser' shows only laser managers."""
    exit_code = main(["list", "--kind", "laser"])
    captured = capsys.readouterr()
    
    assert exit_code == 0
    assert "[laser]" in captured.out
    # Should not show detector managers
    assert "AVManager" not in captured.out


def test_inspect_command(capsys):
    """Test 'inspect AVManager' returns 0 and prints python_name."""
    exit_code = main(["inspect", "AVManager"])
    captured = capsys.readouterr()
    
    assert exit_code == 0
    assert "Python Name:" in captured.out
    assert "AVManager:AVManager" in captured.out


def test_inspect_command_not_found(capsys):
    """Test 'inspect' with bogus ID returns 1."""
    exit_code = main(["inspect", "does.not.exist"])
    captured = capsys.readouterr()
    
    assert exit_code == 1
    assert "not found" in captured.err


def test_validate_setup_valid_and_unresolved(capsys):
    """Test validate-setup with one valid and one unresolved manager."""
    registry = build_default_registry(discover=False)
    
    # Setup device sections are JSON objects keyed by device name (matching the
    # real ImSwitch setup format, Dict[str, DetectorInfo]), NOT lists.
    setup_data = {
        "detectors": {
            "TestCamera": {
                "managerName": "AVManager",
                "managerProperties": {},
            },
            "BogusCamera": {
                "managerName": "NoSuchCam",
                "managerProperties": {},
            },
        }
    }
    
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(setup_data, f)
        temp_path = f.name
    
    try:
        exit_code = main(["validate-setup", temp_path])
        captured = capsys.readouterr()
        
        assert exit_code == 1  # Should fail due to unresolved manager
        # Check for manager names in output
        assert "NoSuchCam" in captured.out or "manager.unresolved" in captured.out
        # Should report validation failed
        assert "FAILED" in captured.out
    finally:
        import os
        os.unlink(temp_path)


def test_validate_setup_resolves_legacy_stand_mock_fallback():
    """Test that stand managers resolve via registry-mock fallback."""
    registry = build_default_registry(discover=False)
    setup_data = {
        "microscopeStand": {
            "managerName": "LeicaDMIManager",
            "rs232device": "mock-rs232",
            "managerProperties": {},
        },
        "rs232devices": {
            "mock-rs232": {
                "managerName": "RS232Manager",
                "managerProperties": {"port": "mock"},
            }
        }
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(setup_data, f)
        temp_path = f.name

    try:
        report = validate_setup_file(temp_path, registry)
        # Stand should resolve via registry-mock, no errors but may have notes
        assert report.has_errors is False
        # No manager.unresolved errors
        unresolved = [d for d in report.diagnostics if d.code == "manager.unresolved"]
        assert len(unresolved) == 0
    finally:
        import os
        os.unlink(temp_path)


def test_legacy_manager_exists_real():
    """Test legacy_manager_exists returns True for real in-tree module."""
    # MockPositionerManager exists in the managers/positioners directory
    assert legacy_manager_exists("positioner", "MockPositionerManager") is True


def test_legacy_manager_exists_bogus():
    """Test legacy_manager_exists returns False for non-existent manager."""
    assert legacy_manager_exists("detector", "TotallyBogusManager") is False


def test_validate_manager_properties():
    """Test validate_manager_properties with jsonschema."""
    jsonschema = pytest.importorskip("jsonschema")
    
    schema = {
        "type": "object",
        "properties": {
            "port": {"type": "integer"},
            "name": {"type": "string"},
        },
        "required": ["port"],
    }
    
    # Valid properties
    valid_props = {"port": 8080, "name": "test"}
    errors = validate_manager_properties(schema, valid_props)
    assert errors == []
    
    # Invalid properties (missing required field)
    invalid_props = {"name": "test"}
    errors = validate_manager_properties(schema, invalid_props)
    assert len(errors) > 0
    assert any("port" in e for e in errors)
    
    # Invalid properties (wrong type)
    invalid_props2 = {"port": "not-a-number", "name": "test"}
    errors = validate_manager_properties(schema, invalid_props2)
    assert len(errors) > 0


def test_source_package_field():
    """Test that parse_manifest sets source_package field."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestManager",
                    "kind": "detector",
                    "display_name": "Test Manager",
                    "python_name": "test.module:TestManager",
                }
            ]
        }
    }
    
    contributions = parse_manifest(
        manifest_data,
        plugin_name="test-plugin",
        plugin_version="1.0.0",
        source_package="test.package",
    )
    
    assert len(contributions) == 1
    assert contributions[0].source_package == "test.package"


def test_source_package_field_defaults_to_none():
    """Test that source_package defaults to None when not provided."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestManager",
                    "kind": "detector",
                    "display_name": "Test Manager",
                    "python_name": "test.module:TestManager",
                }
            ]
        }
    }
    
    contributions = parse_manifest(
        manifest_data,
        plugin_name="test-plugin",
        plugin_version="1.0.0",
    )
    
    assert len(contributions) == 1
    assert contributions[0].source_package is None


def test_validate_setup_file_with_legacy():
    """validate_setup_file resolves an in-tree manager absent from the builtins
    table via the legacy import path (dict-keyed section, real setup shape)."""
    registry = build_default_registry(discover=False)

    # GRBLStageManager is a real in-tree positioner manager that is NOT in the
    # built-in registry table, so it must resolve via the legacy path.
    setup_data = {
        "positioners": {
            "TestStage": {
                "managerName": "GRBLStageManager",
                "managerProperties": {},
            },
        }
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(setup_data, f)
        temp_path = f.name

    try:
        report = validate_setup_file(temp_path, registry)

        # Should resolve via legacy, no errors but should have a legacy-fallback note
        assert report.has_errors is False
        
        # Check for legacy-fallback note
        legacy_notes = [d for d in report.diagnostics if d.code == "manager.legacy-fallback"]
        assert len(legacy_notes) == 1
        assert "GRBLStageManager" in legacy_notes[0].message
        assert legacy_notes[0].path == ("positioners", "TestStage", "managerName")
    finally:
        import os
        os.unlink(temp_path)
