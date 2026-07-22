import importlib
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.StandManager import StandManager
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution
from imswitch.imcontrol.model.plugins.registry import (
    DevicePluginRegistry,
    build_default_registry,
)


stand_manager_module = importlib.import_module(
    "imswitch.imcontrol.model.managers.StandManager"
)
THIS_MODULE = "imswitch.imcontrol._test.unit.test_stand_manager_resolution"


class FakeStandManager:
    def __init__(self, deviceInfo, **lowLevelManagers):
        self.deviceInfo = deviceInfo
        self.lowLevelManagers = lowLevelManagers


@pytest.fixture
def stand_info():
    def _make(managerName):
        return SimpleNamespace(
            managerName=managerName,
            rs232device="mock-rs232",
            managerProperties={},
        )

    return _make


def test_stand_manager_resolves_registry_contribution(monkeypatch, stand_info):
    registry = DevicePluginRegistry()
    registry.register(
        DeviceManagerContribution(
            id="test.olympus-stand",
            kind="stand",
            display_name="Test Olympus Stand",
            python_name=f"{THIS_MODULE}:FakeStandManager",
            plugin_name="imswitch-test-plugin",
            manager_name_aliases=("OlympusStandManager",),
        )
    )
    monkeypatch.setattr(
        stand_manager_module, "get_default_registry", lambda: registry
    )

    sentinel = object()
    manager = StandManager(
        stand_info("OlympusStandManager"),
        testLowLevelManager=sentinel,
    )

    assert isinstance(manager._subManager, FakeStandManager)
    assert manager._subManager.lowLevelManagers == {
        "testLowLevelManager": sentinel,
    }
    assert manager.mocker is False


def test_stand_manager_resolves_canonical_mock_name(monkeypatch, stand_info):
    registry = build_default_registry(discover=False)
    monkeypatch.setattr(
        stand_manager_module, "get_default_registry", lambda: registry
    )

    manager = StandManager(stand_info("LeicaDMIStandMockManager"))

    assert manager._subManager.__class__.__name__ == "MockLeicaDMIStandManager"
    assert manager.mocker is True


def test_stand_manager_legacy_leica_name_falls_back_to_mock(
    monkeypatch, caplog, stand_info
):
    registry = build_default_registry(discover=False)
    monkeypatch.setattr(
        stand_manager_module, "get_default_registry", lambda: registry
    )

    with caplog.at_level("WARNING", logger="imswitch"):
        manager = StandManager(stand_info("LeicaDMIManager"))

    assert manager._subManager.__class__.__name__ == "MockLeicaDMIStandManager"
    assert manager.mocker is True
    assert "Stand manager 'LeicaDMIManager' is unavailable" in caplog.text
    assert "Loading mock manager 'LeicaDMIManager_mock'" in caplog.text


def test_unknown_namespaced_stand_id_gives_the_actionable_diagnostic():
    """A hyphenated plugin id is not a legal module path, so joinModulePath
    rejects it with ValueError. Letting that escape replaced the "install this
    package" diagnostic — which is keyed by exactly these namespaced ids in
    external.py — with an opaque "invalid characters" message.

    Same defect as MultiManager had; StandManager is the other loader that
    falls back to a legacy in-tree import.
    """
    import logging

    with pytest.raises(ImportError) as excinfo:
        StandManager._resolveStandManagerClass(
            "imswitch.imcontrol.model.managers",
            "vendor.stand-x",
            logging.getLogger("test"),
        )

    assert not isinstance(excinfo.value, ValueError)
    assert "vendor.stand-x" in str(excinfo.value)
