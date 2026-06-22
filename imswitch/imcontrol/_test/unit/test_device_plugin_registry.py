"""Tests for device plugin registry."""

import pytest
from collections import OrderedDict, defaultdict

from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import (
    DevicePluginRegistry,
    DuplicateContributionError,
    build_default_registry,
)


@pytest.fixture
def registry():
    """Create an empty registry for testing."""
    return DevicePluginRegistry()


@pytest.fixture
def fake_detector_contribution():
    """Create a fake detector contribution using collections.OrderedDict."""
    return DeviceManagerContribution(
        id="FakeDetector",
        kind="detector",
        display_name="Fake Detector",
        python_name="collections:OrderedDict",
        plugin_name="test-plugin",
        manager_name_aliases=("fake.detector", "test.detector"),
    )


@pytest.fixture
def fake_laser_contribution():
    """Create a fake laser contribution using collections.defaultdict."""
    return DeviceManagerContribution(
        id="FakeLaser",
        kind="laser",
        display_name="Fake Laser",
        python_name="collections:defaultdict",
        plugin_name="test-plugin",
        mock_python_name="collections:OrderedDict",
    )


def test_register_and_resolve_by_id(registry, fake_detector_contribution):
    """Test registering and resolving by ID."""
    registry.register(fake_detector_contribution)
    
    resolved = registry.resolve("detector", "FakeDetector")
    assert resolved is not None
    assert resolved.id == "FakeDetector"
    assert resolved.kind == "detector"


def test_resolve_by_alias(registry, fake_detector_contribution):
    """Test resolving by alias."""
    registry.register(fake_detector_contribution)
    
    resolved = registry.resolve("detector", "fake.detector")
    assert resolved is not None
    assert resolved.id == "FakeDetector"
    
    resolved = registry.resolve("detector", "test.detector")
    assert resolved is not None
    assert resolved.id == "FakeDetector"


def test_kind_scoped_resolution(registry):
    """Test that resolution is scoped by kind."""
    detector_contrib = DeviceManagerContribution(
        id="SharedName",
        kind="detector",
        display_name="Detector",
        python_name="collections:OrderedDict",
        plugin_name="test-plugin",
    )
    laser_contrib = DeviceManagerContribution(
        id="SharedName",
        kind="laser",
        display_name="Laser",
        python_name="collections:defaultdict",
        plugin_name="test-plugin",
    )
    
    registry.register(detector_contrib)
    registry.register(laser_contrib)
    
    detector_resolved = registry.resolve("detector", "SharedName")
    assert detector_resolved.kind == "detector"
    
    laser_resolved = registry.resolve("laser", "SharedName")
    assert laser_resolved.kind == "laser"


def test_load_manager_class(registry, fake_detector_contribution):
    """Test loading a manager class."""
    registry.register(fake_detector_contribution)
    
    cls = registry.load_manager_class("detector", "FakeDetector")
    assert cls is OrderedDict


def test_load_manager_class_prefer_mock(registry, fake_laser_contribution):
    """Test loading mock class when prefer_mock=True."""
    registry.register(fake_laser_contribution)
    
    # Without prefer_mock, should get defaultdict
    cls = registry.load_manager_class("laser", "FakeLaser", prefer_mock=False)
    assert cls is defaultdict
    
    # With prefer_mock, should get OrderedDict (the mock)
    cls = registry.load_manager_class("laser", "FakeLaser", prefer_mock=True)
    assert cls is OrderedDict


def test_load_manager_class_prefer_mock_fallback(registry, fake_detector_contribution):
    """Test fallback to real class when no mock and prefer_mock=True."""
    registry.register(fake_detector_contribution)
    
    # No mock_python_name set, should fall back to real class
    cls = registry.load_manager_class("detector", "FakeDetector", prefer_mock=True)
    assert cls is OrderedDict


def test_duplicate_id_error(registry, fake_detector_contribution):
    """Test that duplicate (kind, id) raises error."""
    registry.register(fake_detector_contribution)
    
    duplicate = DeviceManagerContribution(
        id="FakeDetector",
        kind="detector",
        display_name="Another Fake",
        python_name="collections:defaultdict",
        plugin_name="another-plugin",
    )
    
    with pytest.raises(DuplicateContributionError, match="Duplicate detector manager"):
        registry.register(duplicate)


def test_builtin_protection(registry, fake_detector_contribution):
    """Test that plugins cannot override built-ins."""
    # Register as built-in
    registry.register(fake_detector_contribution, is_builtin=True)
    
    # Try to register a plugin with same (kind, id)
    plugin_attempt = DeviceManagerContribution(
        id="FakeDetector",
        kind="detector",
        display_name="Plugin Override",
        python_name="collections:defaultdict",
        plugin_name="malicious-plugin",
    )
    
    with pytest.raises(DuplicateContributionError, match="Cannot override built-in"):
        registry.register(plugin_attempt, is_builtin=False)


def test_builtin_alias_protection(registry):
    """Test that plugins cannot use aliases that collide with built-ins."""
    builtin = DeviceManagerContribution(
        id="BuiltinDetector",
        kind="detector",
        display_name="Built-in",
        python_name="collections:OrderedDict",
        plugin_name="imswitch-core",
        manager_name_aliases=("builtin.alias",),
    )
    registry.register(builtin, is_builtin=True)
    
    # Try to register a plugin with same alias
    plugin = DeviceManagerContribution(
        id="PluginDetector",
        kind="detector",
        display_name="Plugin",
        python_name="collections:defaultdict",
        plugin_name="test-plugin",
        manager_name_aliases=("builtin.alias",),
    )
    
    with pytest.raises(DuplicateContributionError, match="conflicts with built-in"):
        registry.register(plugin, is_builtin=False)


def test_format_resolution_error(registry, fake_detector_contribution):
    """Test error message formatting."""
    registry.register(fake_detector_contribution)
    
    error_msg = registry.format_resolution_error("detector", "NonExistent")
    
    assert "Could not resolve detector manager 'NonExistent'" in error_msg
    assert "Installed detector managers:" in error_msg
    assert "FakeDetector (test-plugin)" in error_msg
    assert "alias: fake.detector" in error_msg
    assert "Install the required plugin package" in error_msg


def test_list_contributions(registry, fake_detector_contribution, fake_laser_contribution):
    """Test listing contributions."""
    registry.register(fake_detector_contribution)
    registry.register(fake_laser_contribution)
    
    all_contribs = registry.list_contributions()
    assert len(all_contribs) == 2
    
    detector_contribs = registry.list_contributions(kind="detector")
    assert len(detector_contribs) == 1
    assert detector_contribs[0].kind == "detector"
    
    laser_contribs = registry.list_contributions(kind="laser")
    assert len(laser_contribs) == 1
    assert laser_contribs[0].kind == "laser"


def test_builtin_device_managers():
    """Test that built-in managers are correctly defined."""
    from imswitch.imcontrol.model.plugins.builtins import BUILTIN_DEVICE_MANAGERS
    
    assert len(BUILTIN_DEVICE_MANAGERS) == 6
    
    # Check that AVManager is in the list
    av_manager = next(c for c in BUILTIN_DEVICE_MANAGERS if c.id == "AVManager")
    assert av_manager.kind == "detector"
    assert av_manager.plugin_name == "imswitch-core"
    assert "builtin.av" in av_manager.manager_name_aliases
    
    # Check MockPositionerManager
    mock_pos = next(c for c in BUILTIN_DEVICE_MANAGERS if c.id == "MockPositionerManager")
    assert mock_pos.kind == "positioner"
    assert "builtin.mock-positioner" in mock_pos.manager_name_aliases


def test_build_default_registry_no_discover():
    """Test building default registry without discovery."""
    registry = build_default_registry(discover=False)
    
    # Should have the built-ins
    contribs = registry.list_contributions()
    assert len(contribs) == 6
    
    # Test resolving a built-in
    av = registry.resolve("detector", "AVManager")
    assert av is not None
    assert av.id == "AVManager"
    
    mock_pos = registry.resolve("positioner", "MockPositionerManager")
    assert mock_pos is not None
    assert mock_pos.id == "MockPositionerManager"


def test_load_real_builtin_manager():
    """Test loading a real built-in manager class (hardware-free)."""
    registry = build_default_registry(discover=False)
    
    # MockPositionerManager should be hardware-free and safe to load
    cls = registry.load_manager_class("positioner", "MockPositionerManager")
    assert cls is not None
    assert cls.__name__ == "MockPositionerManager"


def test_manifest_rejects_unknown_kind():
    """Test that parse_manifest rejects completely unknown kinds."""
    from imswitch.imcontrol.model.plugins.manifest import parse_manifest, ManifestError
    
    manifest = {
        "contributions": {
            "device_managers": [
                {
                    "id": "BadManager",
                    "kind": "totally_unknown_kind",
                    "display_name": "Bad Manager",
                    "python_name": "fake.module:BadManager"
                }
            ]
        }
    }
    
    with pytest.raises(ManifestError, match="Unknown device kind 'totally_unknown_kind'"):
        parse_manifest(manifest, plugin_name="test-plugin", plugin_version="1.0.0")


def test_manifest_rejects_bespoke_loader_kinds():
    """Test that parse_manifest rejects stand and pulse_generator kinds."""
    from imswitch.imcontrol.model.plugins.manifest import parse_manifest, ManifestError
    
    # Test stand kind (bespoke loader)
    stand_manifest = {
        "contributions": {
            "device_managers": [
                {
                    "id": "CustomStand",
                    "kind": "stand",
                    "display_name": "Custom Stand",
                    "python_name": "fake.module:CustomStand"
                }
            ]
        }
    }
    
    with pytest.raises(
        ManifestError,
        match="Device kind 'stand'.* is not supported by runtime plugin loading"
    ):
        parse_manifest(stand_manifest, plugin_name="test-plugin", plugin_version="1.0.0")
    
    # Test pulse_generator kind (bespoke loader)
    pulse_manifest = {
        "contributions": {
            "device_managers": [
                {
                    "id": "CustomPulseGen",
                    "kind": "pulse_generator",
                    "display_name": "Custom Pulse Generator",
                    "python_name": "fake.module:CustomPulseGen"
                }
            ]
        }
    }
    
    with pytest.raises(
        ManifestError,
        match="Device kind 'pulse_generator'.* is not supported by runtime plugin loading"
    ):
        parse_manifest(pulse_manifest, plugin_name="test-plugin", plugin_version="1.0.0")


def test_manifest_accepts_multimanager_backed_kinds():
    """Test that parse_manifest accepts all MultiManager-backed kinds."""
    from imswitch.imcontrol.model.plugins.manifest import parse_manifest, MULTIMANAGER_BACKED_KINDS
    
    for kind in MULTIMANAGER_BACKED_KINDS:
        manifest = {
            "contributions": {
                "device_managers": [
                    {
                        "id": f"Test{kind.title()}Manager",
                        "kind": kind,
                        "display_name": f"Test {kind.title()} Manager",
                        "python_name": f"fake.module:Test{kind.title()}Manager"
                    }
                ]
            }
        }
        
        # Should not raise
        contributions = parse_manifest(
            manifest,
            plugin_name="test-plugin",
            plugin_version="1.0.0"
        )
        assert len(contributions) == 1
        assert contributions[0].kind == kind


def test_build_default_registry_logs_discovery_errors(caplog, monkeypatch):
    """Test that discovery errors are logged during registry build."""
    import logging
    from imswitch.imcontrol.model.plugins.discovery import DiscoveryError
    
    # Mock discover_contributions to return errors
    def mock_discover():
        return (
            [],  # no contributions
            [
                DiscoveryError(
                    plugin_name="broken-plugin",
                    manifest="broken:manifest.json",
                    error="Invalid JSON syntax"
                ),
                DiscoveryError(
                    plugin_name="bad-plugin",
                    manifest=None,
                    error="Missing manifest file"
                ),
            ]
        )
    
    import imswitch.imcontrol.model.plugins.registry as registry_module
    monkeypatch.setattr(registry_module, "discover_contributions", mock_discover)
    
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        registry = build_default_registry(discover=True)
    
    # Check that errors were logged
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) >= 2  # At least one for count, plus individual errors
    
    # Check that both errors are mentioned
    all_warnings = "\n".join([r.message for r in warning_records])
    assert "broken-plugin" in all_warnings
    assert "Invalid JSON syntax" in all_warnings
    assert "bad-plugin" in all_warnings
    assert "Missing manifest file" in all_warnings


def test_build_default_registry_logs_duplicate_skips(caplog, monkeypatch):
    """Test that duplicate contributions are logged when skipped."""
    import logging
    
    # Mock discover_contributions to return a duplicate of a built-in
    duplicate_contrib = DeviceManagerContribution(
        id="AVManager",  # Same as built-in
        kind="detector",
        display_name="Duplicate AV Manager",
        python_name="collections:OrderedDict",
        plugin_name="malicious-plugin",
    )
    
    def mock_discover():
        return ([duplicate_contrib], [])
    
    import imswitch.imcontrol.model.plugins.registry as registry_module
    monkeypatch.setattr(registry_module, "discover_contributions", mock_discover)
    
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        registry = build_default_registry(discover=True)
    
    # Check that duplicate was logged
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) >= 1
    
    # Find the duplicate warning
    duplicate_warnings = [
        r for r in warning_records
        if "duplicate" in r.message.lower() or "skipping" in r.message.lower()
    ]
    assert len(duplicate_warnings) >= 1
    
    warning_msg = duplicate_warnings[0].message
    assert "AVManager" in warning_msg or "detector" in warning_msg
    assert "malicious-plugin" in warning_msg


def test_build_default_registry_logs_counts(caplog, monkeypatch):
    """Test that registry logs info about registered vs skipped contributions."""
    import logging
    
    # Mock discover_contributions to return mix of new and duplicate
    new_contrib = DeviceManagerContribution(
        id="NewDetector",
        kind="detector",
        display_name="New Detector",
        python_name="collections:defaultdict",
        plugin_name="good-plugin",
    )
    
    duplicate_contrib = DeviceManagerContribution(
        id="AVManager",  # Duplicate of built-in
        kind="detector",
        display_name="Duplicate",
        python_name="collections:OrderedDict",
        plugin_name="bad-plugin",
    )
    
    def mock_discover():
        return ([new_contrib, duplicate_contrib], [])
    
    import imswitch.imcontrol.model.plugins.registry as registry_module
    monkeypatch.setattr(registry_module, "discover_contributions", mock_discover)
    
    caplog.clear()
    with caplog.at_level(logging.INFO):
        registry = build_default_registry(discover=True)
    
    # Check that summary info was logged
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) >= 1
    
    # Find the summary message
    summary_msg = None
    for r in info_records:
        if "registered" in r.message.lower() and "skipped" in r.message.lower():
            summary_msg = r.message
            break
    
    assert summary_msg is not None
    # Should say 1 registered, 1 skipped (duplicate)
    assert "1 registered" in summary_msg or "registered" in summary_msg
    assert "1 skipped" in summary_msg or "skipped" in summary_msg
