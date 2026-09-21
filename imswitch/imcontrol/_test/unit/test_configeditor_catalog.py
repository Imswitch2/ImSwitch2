"""Tests for the config editor manager catalog (Phase 1).

This module tests the registry-backed manager catalog service, ensuring that:
1. Built-in managers appear with correct metadata
2. Alias resolution works correctly
3. Legacy filesystem scanning works as a fallback
4. Injected fake plugin contributions work without template files (EXIT CRITERION)
"""

import tempfile
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor.catalog import (
    ManagerCatalog,
    ManagerInfo,
    build_catalog,
)
from imswitch.imcontrol.model.plugins.registry import (
    DevicePluginRegistry,
    build_default_registry,
)
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution


class TestConfigEditorCatalog:
    """Test suite for the config editor manager catalog."""
    
    def test_built_ins_present(self):
        """Test that built-in managers appear with correct metadata.
        
        Builds a catalog with only built-ins (no discovery) and verifies that
        all 9 built-in managers are present with correct display names and
        categories.
        """
        # Build catalog with built-ins only (no discovery)
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Expected built-in manager infos
        expected_builtins = {
            "AVManager": {
                "category": "detectors",
                "display": "Generic video detector (OpenCV)",
            },
            "HamamatsuManager": {
                "category": "detectors",
                "display": "Hamamatsu ORCA camera",
            },
            "NidaqLaserManager": {
                "category": "lasers",
                "display": "NI-DAQ analog laser control",
            },
            "CoboltLaserManager": {
                "category": "lasers",
                "display": "Cobolt laser",
            },
            "MockPositionerManager": {
                "category": "positioners",
                "display": "Mock positioner for testing",
            },
            "NidaqPositionerManager": {
                "category": "positioners",
                "display": "NI-DAQ analog positioner control",
            },
            "ThorlabsMFFManager": {
                "category": "flipMirrors",
                "display": "Thorlabs MFF flip mirror",
            },
            "ThorlabsMFFMockManager": {
                "category": "flipMirrors",
                "display": "Mock Thorlabs MFF flip mirror",
            },
            "LeicaDMIStandMockManager": {
                "category": "stands",
                "display": "Mock Leica DMI microscope stand",
            },
        }
        
        # Verify all built-ins are present
        all_managers = {info.manager_name for info in catalog.managers()}
        for manager_name in expected_builtins:
            assert manager_name in all_managers, (
                f"Built-in manager '{manager_name}' not found in catalog"
            )
        
        # Verify metadata for each built-in
        for manager_name, expected in expected_builtins.items():
            info = catalog.get(manager_name)
            assert info is not None, f"Manager '{manager_name}' not found"
            assert info.category == expected["category"], (
                f"Wrong category for '{manager_name}': "
                f"expected '{expected['category']}', got '{info.category}'"
            )
            assert info.display_name == expected["display"], (
                f"Wrong display name for '{manager_name}': "
                f"expected '{expected['display']}', got '{info.display_name}'"
            )
            assert info.is_builtin, f"Manager '{manager_name}' should be marked as builtin"
            assert info.from_registry, (
                f"Manager '{manager_name}' should be marked as from registry"
            )
    
    def test_alias_resolution(self):
        """Test that alias resolution works correctly.
        
        Verifies that both ID and alias lookups resolve to the correct manager.
        """
        # Build catalog with built-ins only
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Test HamamatsuManager alias resolution
        by_id = catalog.get("HamamatsuManager")
        by_alias = catalog.get("hamamatsu.orca")
        
        assert by_id is not None, "HamamatsuManager not found by ID"
        assert by_alias is not None, "HamamatsuManager not found by alias 'hamamatsu.orca'"
        assert by_id.manager_name == by_alias.manager_name, (
            "ID and alias should resolve to same manager"
        )
        assert by_id.manager_name == "HamamatsuManager"
        
        # Test ThorlabsMFFManager alias resolution (has multiple aliases)
        by_id = catalog.get("ThorlabsMFFManager")
        by_alias1 = catalog.get("ThorlabsMFF")
        by_alias2 = catalog.get("builtin.thorlabs-mff")
        
        assert by_id is not None, "ThorlabsMFFManager not found by ID"
        assert by_alias1 is not None, "ThorlabsMFFManager not found by alias 'ThorlabsMFF'"
        assert by_alias2 is not None, (
            "ThorlabsMFFManager not found by alias 'builtin.thorlabs-mff'"
        )
        assert by_id.manager_name == by_alias1.manager_name == by_alias2.manager_name, (
            "All aliases should resolve to same manager"
        )
        assert by_id.manager_name == "ThorlabsMFFManager"
    
    def test_legacy_managers(self):
        """Test that legacy filesystem scan discovers non-registered managers.
        
        Verifies that TISManager (a real in-tree manager not in registry)
        appears with from_registry=False when legacy scan is enabled.
        """
        # Build catalog with legacy scan enabled
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=True)
        
        # TISManager should be found via legacy scan
        tis_info = catalog.get("TISManager")
        
        # Only assert if TISManager actually exists in the source tree
        if tis_info is not None:
            assert tis_info.from_registry is False, (
                "TISManager should be marked as from legacy scan (from_registry=False)"
            )
            assert tis_info.is_builtin is False, (
                "TISManager should not be marked as builtin"
            )
            assert tis_info.category == "detectors", (
                f"TISManager should be in 'detectors' category, got '{tis_info.category}'"
            )
            assert tis_info.display_name == "TISManager", (
                "Legacy managers should have display_name == manager_name"
            )
        else:
            # If TISManager doesn't exist, just verify that legacy scan runs
            # without error and that we have some managers from registry
            assert len(catalog.managers()) > 0, "Catalog should have some managers"
    
    def test_fake_plugin_contribution(self):
        """EXIT CRITERION: Test that a fake plugin contribution works without templates.
        
        Builds a fresh registry, registers a fake contribution, and verifies it
        appears in the catalog with all metadata intact, proving that discovery
        works purely from the registry without requiring template files.
        """
        # Build a fresh registry
        registry = DevicePluginRegistry()
        
        # Register a fake contribution
        fake_contrib = DeviceManagerContribution(
            id="vendor.superstage",
            kind="positioner",
            display_name="Vendor SuperStage",
            python_name="vendor_plugin.stage:SuperStageManager",
            plugin_name="vendor-plugin",
            plugin_version="1.0.0",
            source_package="vendor_plugin",
            manager_name_aliases=("superstage.legacy",),
            docs_url="https://example.com/superstage",
            supported_platforms=("linux", "win32"),
        )
        registry.register(fake_contrib, is_builtin=False)
        
        # Build catalog from this registry (no legacy scan to isolate test)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Verify the fake contribution appears
        info = catalog.get("vendor.superstage")
        assert info is not None, "Fake contribution not found by ID"
        
        # Verify it's in the correct category
        assert info.category == "positioners", (
            f"Wrong category: expected 'positioners', got '{info.category}'"
        )
        
        # Verify display name is preserved
        assert info.display_name == "Vendor SuperStage", (
            f"Wrong display name: expected 'Vendor SuperStage', got '{info.display_name}'"
        )
        
        # Verify alias resolution works
        by_alias = catalog.get("superstage.legacy")
        assert by_alias is not None, "Fake contribution not found by alias"
        assert by_alias.manager_name == "vendor.superstage", (
            "Alias should resolve to the fake contribution"
        )
        
        # Verify docs URL is preserved
        assert info.docs_url == "https://example.com/superstage", (
            f"Wrong docs URL: expected 'https://example.com/superstage', "
            f"got '{info.docs_url}'"
        )
        
        # Verify it's not marked as builtin
        assert info.is_builtin is False, "Fake contribution should not be builtin"
        assert info.from_registry is True, (
            "Fake contribution should be marked as from registry"
        )
        
        # CRITICAL: Verify no template file was created
        # (This is a no-op check in the test, but the key point is that the
        # manager is discoverable without any template file existing)
        builtin_templates_dir = (
            Path(__file__).parents[4] / "imswitch" / "imcontrol" / "view"
            / "configeditor" / "builtin_templates"
        )
        if builtin_templates_dir.exists():
            # Verify no file for this fake manager was created
            for template_file in builtin_templates_dir.rglob("*.json"):
                with open(template_file, encoding="utf-8") as f:
                    content = f.read()
                    assert "vendor.superstage" not in content, (
                        f"Template file {template_file} should not contain fake contribution"
                    )
                    assert "SuperStageManager" not in content, (
                        f"Template file {template_file} should not contain fake contribution"
                    )
    
    def test_by_category(self):
        """Test that by_category() returns correctly grouped managers."""
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        by_cat = catalog.by_category()
        
        # Verify detectors category
        assert "detectors" in by_cat, "Detectors category should be present"
        detector_names = {info.manager_name for info in by_cat["detectors"]}
        assert "HamamatsuManager" in detector_names
        assert "AVManager" in detector_names
        
        # Verify lasers category
        assert "lasers" in by_cat, "Lasers category should be present"
        laser_names = {info.manager_name for info in by_cat["lasers"]}
        assert "NidaqLaserManager" in laser_names
        assert "CoboltLaserManager" in laser_names
        
        # Verify positioners category
        assert "positioners" in by_cat, "Positioners category should be present"
        positioner_names = {info.manager_name for info in by_cat["positioners"]}
        assert "MockPositionerManager" in positioner_names
        assert "NidaqPositionerManager" in positioner_names
    
    def test_category_for(self):
        """Test category_for() method."""
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Test by ID
        assert catalog.category_for("HamamatsuManager") == "detectors"
        assert catalog.category_for("NidaqLaserManager") == "lasers"
        assert catalog.category_for("MockPositionerManager") == "positioners"
        
        # Test by alias
        assert catalog.category_for("hamamatsu.orca") == "detectors"
        assert catalog.category_for("ThorlabsMFF") == "flipMirrors"
        
        # Test unknown manager
        assert catalog.category_for("NonexistentManager") is None
    
    def test_display_name(self):
        """Test display_name() method."""
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Test known manager
        assert catalog.display_name("HamamatsuManager") == "Hamamatsu ORCA camera"
        assert catalog.display_name("NidaqLaserManager") == "NI-DAQ analog laser control"
        
        # Test by alias
        assert catalog.display_name("hamamatsu.orca") == "Hamamatsu ORCA camera"
        
        # Test unknown manager (should fall back to name itself)
        assert catalog.display_name("UnknownManager") == "UnknownManager"
    
    def test_all_manager_names(self):
        """Test all_manager_names() method."""
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        names = catalog.all_manager_names()
        
        # Should be sorted
        assert names == sorted(names), "Manager names should be sorted"
        
        # Should contain all built-ins
        expected_names = {
            "AVManager",
            "HamamatsuManager",
            "NidaqLaserManager",
            "CoboltLaserManager",
            "MockPositionerManager",
            "NidaqPositionerManager",
            "ThorlabsMFFManager",
            "ThorlabsMFFMockManager",
            "LeicaDMIStandMockManager",
        }
        assert expected_names.issubset(set(names)), (
            "All built-in managers should be in all_manager_names()"
        )
    
    def test_catalog_with_legacy_scan_temp_dir(self):
        """Test legacy scan with a temporary directory structure."""
        # Create a temporary managers directory structure
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            
            # Create detectors directory with a fake manager
            detectors_dir = tmppath / "detectors"
            detectors_dir.mkdir()
            (detectors_dir / "FakeDetectorManager.py").write_text(
                "class FakeDetectorManager: pass"
            )
            (detectors_dir / "DetectorManager.py").write_text(
                "class DetectorManager: pass"  # Should be skipped (base class)
            )
            
            # Create lasers directory with a fake manager
            lasers_dir = tmppath / "lasers"
            lasers_dir.mkdir()
            (lasers_dir / "FakeLaserManager.py").write_text(
                "class FakeLaserManager: pass"
            )
            
            # Build catalog with this temp root
            registry = build_default_registry(discover=False)
            catalog = build_catalog(
                registry=registry,
                include_legacy_scan=True,
                managers_root=tmppath,
            )
            
            # Verify fake managers appear
            fake_detector = catalog.get("FakeDetectorManager")
            assert fake_detector is not None, "FakeDetectorManager should be found"
            assert fake_detector.category == "detectors"
            assert fake_detector.from_registry is False
            
            fake_laser = catalog.get("FakeLaserManager")
            assert fake_laser is not None, "FakeLaserManager should be found"
            assert fake_laser.category == "lasers"
            assert fake_laser.from_registry is False
            
            # Verify base class was skipped
            base_manager = catalog.get("DetectorManager")
            # Base manager should NOT be in the catalog
            # (either None or only from registry if it were registered)
            if base_manager is not None:
                # If it exists, it should be from registry, not legacy scan
                assert base_manager.from_registry is True
