"""Tests for MultiManager registry-first manager resolution (device plugins).

Covers the Phase 2 integration in
``imswitch.imcontrol.model.managers.MultiManager``: the device plugin registry
is consulted first, the legacy internal import path is the fallback, and an
unresolved registry-backed kind raises the actionable registry diagnostic
instead of a raw ImportError.
"""

import sys

import pytest

# NOTE: managers/__init__.py binds the name "MultiManager" to the *class*, so
# `...managers.MultiManager` resolves to the class. Fetch the real module object
# from sys.modules to monkeypatch its module-level get_default_registry.
import imswitch.imcontrol.model.managers.MultiManager  # noqa: F401
from imswitch.imcontrol.model.managers.MultiManager import MultiManager
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import (
    DevicePluginRegistry,
    UnknownDeviceManagerError,
)

multimanager_module = sys.modules[
    "imswitch.imcontrol.model.managers.MultiManager"
]

CURRENT_PACKAGE = "imswitch.imcontrol.model.managers"
THIS_MODULE = "imswitch.imcontrol._test.unit.test_multimanager_plugin_resolution"


class FakeInfo:
    """Minimal stand-in for a *Info dataclass (only managerName is read)."""

    def __init__(self, managerName):
        self.managerName = managerName


class FakePluginManager:
    """A manager class as a plugin would provide it: (info, name, **lowLevel)."""

    def __init__(self, deviceInfo, name, **lowLevelManagers):
        self.deviceInfo = deviceInfo
        self.name = name
        self.lowLevelManagers = lowLevelManagers


class _ConcreteMulti(MultiManager):
    """Concrete MultiManager so the abstract __init__ can be exercised."""

    def __init__(self, infos, package, **lowLevelManagers):
        super().__init__(infos, package, **lowLevelManagers)


def _registry_with_fake_detector():
    registry = DevicePluginRegistry()
    registry.register(
        DeviceManagerContribution(
            id="my.fakecam",
            kind="detector",
            display_name="Fake plugin camera",
            python_name=f"{THIS_MODULE}:FakePluginManager",
            plugin_name="imswitch-test-plugin",
        )
    )
    return registry


@pytest.fixture
def patch_registry(monkeypatch):
    """Patch the registry accessor used inside MultiManager."""

    def _patch(registry):
        monkeypatch.setattr(
            multimanager_module, "get_default_registry", lambda: registry
        )

    return _patch


def test_resolves_plugin_manager_from_registry(patch_registry):
    patch_registry(_registry_with_fake_detector())
    cls = MultiManager._resolveManagerClass(
        CURRENT_PACKAGE, "detectors", "detector", "my.fakecam"
    )
    assert cls is FakePluginManager


def test_resolves_plugin_manager_by_alias(patch_registry):
    registry = DevicePluginRegistry()
    registry.register(
        DeviceManagerContribution(
            id="my.fakecam",
            kind="detector",
            display_name="Fake plugin camera",
            python_name=f"{THIS_MODULE}:FakePluginManager",
            plugin_name="imswitch-test-plugin",
            manager_name_aliases=("LegacyFakeCamManager",),
        )
    )
    patch_registry(registry)
    cls = MultiManager._resolveManagerClass(
        CURRENT_PACKAGE, "detectors", "detector", "LegacyFakeCamManager"
    )
    assert cls is FakePluginManager


def test_falls_back_to_legacy_internal_import(patch_registry):
    # Empty registry -> miss -> legacy import of a real in-tree (mock) manager.
    patch_registry(DevicePluginRegistry())
    from imswitch.imcontrol.model.managers.positioners.MockPositionerManager import (
        MockPositionerManager,
    )

    cls = MultiManager._resolveManagerClass(
        CURRENT_PACKAGE, "positioners", "positioner", "MockPositionerManager"
    )
    assert cls is MockPositionerManager


def test_unknown_registry_backed_manager_raises_diagnostic(patch_registry):
    registry = _registry_with_fake_detector()
    patch_registry(registry)
    with pytest.raises(UnknownDeviceManagerError) as excinfo:
        MultiManager._resolveManagerClass(
            CURRENT_PACKAGE, "detectors", "detector", "NoSuchManager"
        )
    message = str(excinfo.value)
    assert "NoSuchManager" in message
    # The diagnostic lists installed managers for the kind.
    assert "my.fakecam" in message


def test_unmapped_kind_raises_raw_import_error(patch_registry):
    # kind is None for a package not backed by the registry -> original
    # behavior: a raw ImportError/AttributeError, NOT the registry diagnostic.
    patch_registry(DevicePluginRegistry())
    with pytest.raises((ImportError, AttributeError)) as excinfo:
        MultiManager._resolveManagerClass(
            CURRENT_PACKAGE, "stands", None, "DefinitelyNotAManager"
        )
    assert not isinstance(excinfo.value, UnknownDeviceManagerError)


def test_init_instantiates_resolved_plugin_manager(patch_registry):
    patch_registry(_registry_with_fake_detector())
    info = FakeInfo("my.fakecam")
    sentinel = object()
    multi = _ConcreteMulti(
        {"cam": info}, "detectors", nidaqManager=sentinel
    )
    sub = multi["cam"]
    assert isinstance(sub, FakePluginManager)
    assert sub.deviceInfo is info
    assert sub.name == "cam"
    assert sub.lowLevelManagers == {"nidaqManager": sentinel}


def test_init_with_no_devices_is_noop(patch_registry):
    patch_registry(DevicePluginRegistry())
    multi = _ConcreteMulti({}, "detectors")
    assert multi.hasDevices() is False


def test_shadowing_warning_when_both_paths_resolve(patch_registry, caplog):
    """Test that a WARNING is logged when a registry manager shadows an in-tree manager.
    
    When both the registry and legacy internal path would resolve the same
    managerName, the registry wins (deterministic precedence) and a clear
    WARNING is logged naming both sources.
    """
    import logging
    
    # Register a plugin manager with the same name as an in-tree manager
    registry = DevicePluginRegistry()
    registry.register(
        DeviceManagerContribution(
            id="MockPositionerManager",  # Same name as in-tree manager
            kind="positioner",
            display_name="Plugin Mock Positioner",
            python_name=f"{THIS_MODULE}:FakePluginManager",
            plugin_name="imswitch-test-shadowing-plugin",
        )
    )
    patch_registry(registry)
    
    # Clear any previous log records
    caplog.clear()
    
    # Resolve the manager (should get plugin version, with a warning)
    with caplog.at_level(logging.WARNING):
        cls = MultiManager._resolveManagerClass(
            CURRENT_PACKAGE, "positioners", "positioner", "MockPositionerManager"
        )
    
    # Verify registry resolution wins
    assert cls is FakePluginManager
    
    # Verify warning was logged with all key information
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    
    warning_msg = warning_records[0].message
    assert "MockPositionerManager" in warning_msg
    assert "imswitch-test-shadowing-plugin" in warning_msg
    assert "shadowing" in warning_msg.lower()
    assert "positioner" in warning_msg
    # Check that legacy path is mentioned
    assert "imswitch.imcontrol.model.managers.positioners.MockPositionerManager" in warning_msg
    # Check that precedence rule is mentioned
    assert "precedence" in warning_msg.lower() or "takes precedence" in warning_msg


def test_no_shadowing_warning_when_only_registry_resolves(patch_registry, caplog):
    """Test that no warning is logged when only the registry resolves (no shadowing)."""
    import logging
    
    patch_registry(_registry_with_fake_detector())
    
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cls = MultiManager._resolveManagerClass(
            CURRENT_PACKAGE, "detectors", "detector", "my.fakecam"
        )
    
    assert cls is FakePluginManager
    
    # No warning should be logged (no in-tree manager to shadow)
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 0


def test_no_shadowing_warning_when_only_legacy_resolves(patch_registry, caplog):
    """Test that no warning is logged when only legacy path resolves (no shadowing)."""
    import logging
    
    # Empty registry -> only legacy path will resolve
    patch_registry(DevicePluginRegistry())
    
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cls = MultiManager._resolveManagerClass(
            CURRENT_PACKAGE, "positioners", "positioner", "MockPositionerManager"
        )
    
    from imswitch.imcontrol.model.managers.positioners.MockPositionerManager import (
        MockPositionerManager,
    )
    assert cls is MockPositionerManager
    
    # No warning should be logged (registry doesn't resolve, no shadowing)
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 0
