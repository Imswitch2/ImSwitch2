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
