"""Tests for device plugin discovery."""

import json
import pytest
from unittest.mock import Mock, patch
from pathlib import Path

from imswitch.imcontrol.model.plugins.discovery import (
    discover_contributions,
    DiscoveryError,
)
from imswitch.imcontrol.model.plugins.manifest import (
    parse_manifest,
    ManifestError,
)


class FakeEntryPoint:
    """Fake entry point for testing."""
    
    def __init__(self, name, value, dist=None):
        self.name = name
        self.value = value
        self.dist = dist


class FakeDist:
    """Fake distribution for testing."""
    
    def __init__(self, version):
        self.version = version


def test_parse_manifest_valid():
    """Test parsing a valid manifest."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestDetector",
                    "kind": "detector",
                    "display_name": "Test Detector",
                    "python_name": "test.module:TestDetector",
                    "manager_name_aliases": ["test.detector"],
                    "setup_templates": ["template.json"],
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
    contrib = contributions[0]
    assert contrib.id == "TestDetector"
    assert contrib.kind == "detector"
    assert contrib.display_name == "Test Detector"
    assert contrib.python_name == "test.module:TestDetector"
    assert contrib.plugin_name == "test-plugin"
    assert contrib.plugin_version == "1.0.0"
    assert contrib.manager_name_aliases == ("test.detector",)
    assert contrib.setup_templates == ("template.json",)


def test_parse_manifest_missing_required_field():
    """Test that missing required field raises ManifestError."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestDetector",
                    "kind": "detector",
                    # Missing display_name
                    "python_name": "test.module:TestDetector",
                }
            ]
        }
    }
    
    with pytest.raises(ManifestError, match="Missing required field 'display_name'"):
        parse_manifest(manifest_data, plugin_name="test-plugin", plugin_version=None)


def test_parse_manifest_invalid_kind():
    """Test that invalid kind raises ManifestError."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestDevice",
                    "kind": "invalid_kind",
                    "display_name": "Test Device",
                    "python_name": "test.module:TestDevice",
                }
            ]
        }
    }
    
    with pytest.raises(ManifestError, match="Unknown device kind 'invalid_kind'"):
        parse_manifest(manifest_data, plugin_name="test-plugin", plugin_version=None)


def test_parse_manifest_empty_contributions():
    """Test parsing manifest with no device_managers."""
    manifest_data = {"contributions": {}}
    
    contributions = parse_manifest(
        manifest_data,
        plugin_name="test-plugin",
        plugin_version=None,
    )
    
    assert contributions == []


def test_parse_manifest_list_field_coercion():
    """Test that list fields are coerced to tuples."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "TestDetector",
                    "kind": "detector",
                    "display_name": "Test",
                    "python_name": "test:Test",
                    "manager_name_aliases": ["alias1", "alias2"],
                    "setup_templates": ["t1", "t2"],
                    "supported_platforms": ["linux", "darwin"],
                }
            ]
        }
    }
    
    contributions = parse_manifest(
        manifest_data,
        plugin_name="test-plugin",
        plugin_version=None,
    )
    
    contrib = contributions[0]
    assert isinstance(contrib.manager_name_aliases, tuple)
    assert isinstance(contrib.setup_templates, tuple)
    assert isinstance(contrib.supported_platforms, tuple)


def test_discover_contributions_valid_plugin():
    """Test discovering contributions from a valid plugin."""
    manifest_json = json.dumps({
        "contributions": {
            "device_managers": [
                {
                    "id": "TestDetector",
                    "kind": "detector",
                    "display_name": "Test Detector",
                    "python_name": "test.module:TestDetector",
                }
            ]
        }
    })
    
    fake_resource = Mock()
    fake_resource.read_text.return_value = manifest_json
    
    fake_dist = FakeDist("1.2.3")
    entry_point = FakeEntryPoint(
        name="test-plugin",
        value="test_package:manifest.json",
        dist=fake_dist,
    )
    
    with patch("importlib.resources.files") as mock_files:
        mock_files.return_value.__truediv__.return_value = fake_resource
        
        contributions, errors = discover_contributions([entry_point])
    
    assert len(contributions) == 1
    assert len(errors) == 0
    assert contributions[0].id == "TestDetector"
    assert contributions[0].plugin_version == "1.2.3"


def test_discover_contributions_broken_manifest_continues():
    """Test that a broken plugin doesn't stop discovery of others."""
    # First plugin has broken JSON
    broken_resource = Mock()
    broken_resource.read_text.return_value = "{ broken json"
    
    # Second plugin is valid
    valid_manifest = json.dumps({
        "contributions": {
            "device_managers": [
                {
                    "id": "ValidDetector",
                    "kind": "detector",
                    "display_name": "Valid Detector",
                    "python_name": "valid:ValidDetector",
                }
            ]
        }
    })
    valid_resource = Mock()
    valid_resource.read_text.return_value = valid_manifest
    
    broken_entry = FakeEntryPoint("broken-plugin", "broken:manifest.json")
    valid_entry = FakeEntryPoint("valid-plugin", "valid:manifest.json")
    
    def files_side_effect(package):
        if package == "broken":
            return Mock(__truediv__=lambda self, path: broken_resource)
        else:
            return Mock(__truediv__=lambda self, path: valid_resource)
    
    with patch("importlib.resources.files", side_effect=files_side_effect):
        contributions, errors = discover_contributions([broken_entry, valid_entry])
    
    # Valid plugin should still be discovered
    assert len(contributions) == 1
    assert contributions[0].id == "ValidDetector"
    
    # Broken plugin should be in errors
    assert len(errors) == 1
    assert errors[0].plugin_name == "broken-plugin"


def test_discover_contributions_no_import():
    """Test that discovery does not import implementation modules."""
    # Use a python_name that would raise ImportError if imported
    manifest_json = json.dumps({
        "contributions": {
            "device_managers": [
                {
                    "id": "UnimportableDetector",
                    "kind": "detector",
                    "display_name": "Unimportable",
                    "python_name": "imswitch_does_not_exist.x:Y",
                }
            ]
        }
    })
    
    fake_resource = Mock()
    fake_resource.read_text.return_value = manifest_json
    
    entry_point = FakeEntryPoint(
        name="test-plugin",
        value="test_package:manifest.json",
    )
    
    with patch("importlib.resources.files") as mock_files:
        mock_files.return_value.__truediv__.return_value = fake_resource
        
        # Should not raise ImportError because we're only parsing metadata
        contributions, errors = discover_contributions([entry_point])
    
    assert len(contributions) == 1
    assert len(errors) == 0
    assert contributions[0].python_name == "imswitch_does_not_exist.x:Y"


def test_discover_contributions_invalid_entry_point_value():
    """Test handling of invalid entry point value format."""
    entry_point = FakeEntryPoint(
        name="bad-plugin",
        value="no_colon_separator",
    )
    
    contributions, errors = discover_contributions([entry_point])
    
    assert len(contributions) == 0
    assert len(errors) == 1
    assert "package:resource_path" in errors[0].error


def test_parse_manifest_optional_fields():
    """Test that optional fields have correct defaults."""
    manifest_data = {
        "contributions": {
            "device_managers": [
                {
                    "id": "MinimalDetector",
                    "kind": "detector",
                    "display_name": "Minimal",
                    "python_name": "minimal:Minimal",
                    # All optional fields omitted
                }
            ]
        }
    }
    
    contributions = parse_manifest(
        manifest_data,
        plugin_name="test-plugin",
        plugin_version=None,
    )
    
    contrib = contributions[0]
    assert contrib.mock_python_name is None
    assert contrib.manager_name_aliases == ()
    assert contrib.manager_properties_schema is None
    assert contrib.setup_templates == ()
    assert contrib.docs_url is None
    assert contrib.supported_platforms == ()
