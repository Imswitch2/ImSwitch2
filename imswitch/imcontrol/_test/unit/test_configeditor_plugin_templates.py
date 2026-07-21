"""Tests for the config editor plugin template loader (Phase 3).

This module tests that plugin setup templates:
1. Load successfully when well-formed
2. Collect errors (not crashes) for malformed templates
3. Surface errors visibly in the editor
4. Never break the catalog or editor bootstrap
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor.catalog import (
    ManagerCatalog,
    ManagerInfo,
)
from imswitch.imcontrol.model.configeditor.templates import (
    PluginTemplate,
    PluginTemplateError,
    load_plugin_templates,
)


class TestPluginTemplateLoader:
    """Test suite for plugin template loading."""
    
    @pytest.fixture
    def temp_package(self, tmp_path):
        """Create a temporary importable package with template resources.
        
        Yields:
            tuple: (package_name, package_path) for the temporary package
        """
        # Create a unique package name
        pkg_name = f"test_plugin_pkg_{id(tmp_path)}"
        pkg_path = tmp_path / pkg_name
        pkg_path.mkdir()
        
        # Create __init__.py to make it a package
        (pkg_path / "__init__.py").write_text("")
        
        # Create templates directory
        templates_dir = pkg_path / "templates"
        templates_dir.mkdir()
        
        # Add to sys.path for imports
        sys.path.insert(0, str(tmp_path))
        
        yield pkg_name, pkg_path
        
        # Cleanup
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        if pkg_name in sys.modules:
            del sys.modules[pkg_name]
    
    def test_happy_path_single_template(self, temp_package):
        """Test that a well-formed device template loads successfully."""
        pkg_name, pkg_path = temp_package
        
        # Create a valid template
        template_data = {
            "managerName": "TestManager",
            "managerProperties": {
                "param1": "value1"
            }
        }
        templates_dir = pkg_path / "templates"
        (templates_dir / "test-device.json").write_text(
            json.dumps(template_data, indent=2)
        )
        
        # Create a catalog with one manager that declares this template
        manager_info = ManagerInfo(
            manager_name="TestManager",
            category="detectors",
            kind="detector",
            display_name="Test Detector",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/test-device.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager_info])
        
        # Load templates
        templates, errors = load_plugin_templates(catalog)
        
        # Verify results
        assert len(templates) == 1
        assert len(errors) == 0
        
        tmpl = templates[0]
        assert tmpl.name == "test-device"  # file stem
        assert tmpl.category == "detectors"
        assert tmpl.manager_name == "TestManager"
        assert tmpl.plugin_name == "test-plugin"
        assert tmpl.source_package == pkg_name
        assert tmpl.resource == "templates/test-device.json"
        assert tmpl.device == template_data
    
    def test_template_with_name_field(self, temp_package):
        """Test that template with 'name' field uses it as display name."""
        pkg_name, pkg_path = temp_package
        
        # Create a template with a "name" field
        template_data = {
            "name": "My Custom Device",
            "managerName": "TestManager",
            "managerProperties": {}
        }
        templates_dir = pkg_path / "templates"
        (templates_dir / "device-123.json").write_text(
            json.dumps(template_data, indent=2)
        )
        
        manager_info = ManagerInfo(
            manager_name="TestManager",
            category="lasers",
            kind="laser",
            display_name="Test Laser",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/device-123.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager_info])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 1
        assert len(errors) == 0
        assert templates[0].name == "My Custom Device"

    def test_template_for_different_manager_produces_error(self, temp_package):
        """A plugin cannot place another manager's device in its category."""
        pkg_name, pkg_path = temp_package
        (pkg_path / "templates" / "wrong-manager.json").write_text(json.dumps({
            "managerName": "OtherManager", "managerProperties": {}
        }))
        manager = ManagerInfo(
            manager_name="TestManager", category="detectors", kind="detector",
            display_name="Test Detector", aliases=(), plugin_name="test-plugin",
            source_package=pkg_name, docs_url=None, supported_platforms=(),
            setup_templates=("templates/wrong-manager.json",), is_builtin=False,
            from_registry=True,
        )
        other = ManagerInfo(
            manager_name="OtherManager", category="lasers", kind="laser",
            display_name="Other Laser", aliases=(), plugin_name="other-plugin",
            source_package=pkg_name, docs_url=None, supported_platforms=(),
            setup_templates=(), is_builtin=False, from_registry=True,
        )

        templates, errors = load_plugin_templates(ManagerCatalog([manager, other]))

        assert templates == []
        assert len(errors) == 1
        assert "does not resolve" in errors[0].message
    
    def test_multiple_templates_multiple_managers(self, temp_package):
        """Test loading multiple templates from multiple managers."""
        pkg_name, pkg_path = temp_package
        templates_dir = pkg_path / "templates"
        
        # Create two templates
        template1 = {"managerName": "Manager1", "managerProperties": {}}
        template2 = {"managerName": "Manager2", "managerProperties": {}}
        
        (templates_dir / "device1.json").write_text(json.dumps(template1))
        (templates_dir / "device2.json").write_text(json.dumps(template2))
        
        # Create catalog with two managers
        manager1 = ManagerInfo(
            manager_name="Manager1",
            category="detectors",
            kind="detector",
            display_name="Manager 1",
            aliases=(),
            plugin_name="plugin-a",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/device1.json",),
            is_builtin=False,
            from_registry=True,
        )
        manager2 = ManagerInfo(
            manager_name="Manager2",
            category="lasers",
            kind="laser",
            display_name="Manager 2",
            aliases=(),
            plugin_name="plugin-b",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/device2.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager1, manager2])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 2
        assert len(errors) == 0
        
        # Verify sorting (by plugin_name, category, name)
        assert templates[0].plugin_name == "plugin-a"
        assert templates[1].plugin_name == "plugin-b"
    
    def test_bad_json_produces_error(self, temp_package):
        """Test that invalid JSON produces a PluginTemplateError."""
        pkg_name, pkg_path = temp_package
        templates_dir = pkg_path / "templates"
        
        # Create an invalid JSON file
        (templates_dir / "bad.json").write_text("{ invalid json }")
        
        manager_info = ManagerInfo(
            manager_name="TestManager",
            category="detectors",
            kind="detector",
            display_name="Test Detector",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/bad.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager_info])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 0
        assert len(errors) == 1
        
        err = errors[0]
        assert err.manager_name == "TestManager"
        assert err.plugin_name == "test-plugin"
        assert err.source_package == pkg_name
        assert err.resource == "templates/bad.json"
        assert "JSON parse error" in err.message
    
    def test_not_a_device_dict_produces_error(self, temp_package):
        """Test that valid JSON that isn't a device dict produces an error."""
        pkg_name, pkg_path = temp_package
        templates_dir = pkg_path / "templates"
        
        # Create a JSON file that is an array, not a dict
        (templates_dir / "array.json").write_text('[1, 2, 3]')
        
        # Create a JSON dict without managerName
        (templates_dir / "no-manager.json").write_text('{"foo": "bar"}')
        
        manager1 = ManagerInfo(
            manager_name="TestManager1",
            category="detectors",
            kind="detector",
            display_name="Test 1",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/array.json",),
            is_builtin=False,
            from_registry=True,
        )
        manager2 = ManagerInfo(
            manager_name="TestManager2",
            category="lasers",
            kind="laser",
            display_name="Test 2",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/no-manager.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager1, manager2])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 0
        assert len(errors) == 2
        
        # Check array error
        err_array = next(e for e in errors if "array.json" in e.resource)
        assert "not a dict" in err_array.message
        
        # Check missing managerName error
        err_no_mgr = next(e for e in errors if "no-manager.json" in e.resource)
        assert "managerName" in err_no_mgr.message
    
    def test_missing_resource_produces_error(self, temp_package):
        """Test that a declared but missing resource produces an error."""
        pkg_name, pkg_path = temp_package
        
        # Don't create the template file - it's declared but missing
        manager_info = ManagerInfo(
            manager_name="TestManager",
            category="positioners",
            kind="positioner",
            display_name="Test Positioner",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/missing.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager_info])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 0
        assert len(errors) == 1
        
        err = errors[0]
        assert err.manager_name == "TestManager"
        assert err.resource == "templates/missing.json"
        assert "not found" in err.message
    
    def test_no_plugins_returns_empty(self):
        """Test that a catalog with no plugin templates returns empty lists."""
        # Create a catalog with a built-in that has no templates
        manager_info = ManagerInfo(
            manager_name="BuiltinManager",
            category="detectors",
            kind="detector",
            display_name="Built-in Detector",
            aliases=(),
            plugin_name="imswitch-core",
            source_package=None,
            docs_url=None,
            supported_platforms=(),
            setup_templates=(),
            is_builtin=True,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager_info])
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 0
        assert len(errors) == 0
    
    def test_mixed_success_and_errors(self, temp_package):
        """Test that valid templates load while invalid ones produce errors."""
        pkg_name, pkg_path = temp_package
        templates_dir = pkg_path / "templates"
        
        # Create one valid and one invalid template
        valid_template = {
            "managerName": "ValidManager",
            "managerProperties": {"param": "value"}
        }
        (templates_dir / "valid.json").write_text(json.dumps(valid_template))
        (templates_dir / "invalid.json").write_text("not json")
        
        manager1 = ManagerInfo(
            manager_name="ValidManager",
            category="detectors",
            kind="detector",
            display_name="Valid",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/valid.json",),
            is_builtin=False,
            from_registry=True,
        )
        manager2 = ManagerInfo(
            manager_name="InvalidManager",
            category="lasers",
            kind="laser",
            display_name="Invalid",
            aliases=(),
            plugin_name="test-plugin",
            source_package=pkg_name,
            docs_url=None,
            supported_platforms=(),
            setup_templates=("templates/invalid.json",),
            is_builtin=False,
            from_registry=True,
        )
        catalog = ManagerCatalog([manager1, manager2])
        
        templates, errors = load_plugin_templates(catalog)
        
        # Should have one success and one error
        assert len(templates) == 1
        assert len(errors) == 1
        
        assert templates[0].manager_name == "ValidManager"
        assert errors[0].manager_name == "InvalidManager"
    
    def test_deterministic_sorting(self, temp_package):
        """Test that templates are sorted deterministically."""
        pkg_name, pkg_path = temp_package
        templates_dir = pkg_path / "templates"
        
        # Create multiple templates with different names
        for i, name in enumerate(["zebra", "apple", "banana"]):
            template = {"managerName": f"Manager{i}", "managerProperties": {}}
            (templates_dir / f"{name}.json").write_text(json.dumps(template))
        
        managers = [
            ManagerInfo(
                manager_name=f"Manager{i}",
                category="detectors",
                kind="detector",
                display_name=f"Manager {i}",
                aliases=(),
                plugin_name="z-plugin" if i == 0 else "a-plugin",
                source_package=pkg_name,
                docs_url=None,
                supported_platforms=(),
                setup_templates=(f"templates/{name}.json",),
                is_builtin=False,
                from_registry=True,
            )
            for i, name in enumerate(["zebra", "apple", "banana"])
        ]
        catalog = ManagerCatalog(managers)
        
        templates, errors = load_plugin_templates(catalog)
        
        assert len(templates) == 3
        assert len(errors) == 0
        
        # Should be sorted by plugin_name, then category, then name
        # a-plugin comes before z-plugin
        assert templates[0].plugin_name == "a-plugin"
        assert templates[1].plugin_name == "a-plugin"
        assert templates[2].plugin_name == "z-plugin"
        
        # Within a-plugin, sorted by name
        assert templates[0].name == "apple"
        assert templates[1].name == "banana"
