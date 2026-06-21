from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.SetupModeController import SetupModeController
from imswitch.imcontrol.controller.controllers.SetupModesController import (
    SetupModesController,
)


pytestmark = pytest.mark.nohardware


class _FakeSetupModeBackend:
    def __init__(self, workflows=None):
        self._workflows = list(workflows or [])

    def getSmartMicroscopyWorkflowNames(self):
        return list(self._workflows)


class _FakeSmartModeService:
    def __init__(self):
        self.calls = []

    def updateConfig(self, roleConfig, policyConfig=None):
        self.calls.append((roleConfig, policyConfig))


def _makeController(setupInfo=None, setupModeController=None):
    controller = SetupModesController.__new__(SetupModesController)
    controller._setupInfo = setupInfo or SimpleNamespace(
        smartMicroscopyModes=None,
        smartMicroscopyModePolicies=None,
        smartMicroscopyModeSwitchingEnabled=None,
    )
    controller._setupModeController = setupModeController
    controller._smartModeService = None
    return controller


def test_setup_mode_controller_lists_declared_smart_workflows():
    controller = SetupModeController.__new__(SetupModeController)
    controller._controllers = {
        "EtSnouty": SimpleNamespace(SMART_MODE_WORKFLOW="EtSnouty"),
        "EtSTED": SimpleNamespace(SMART_MODE_WORKFLOW="EtSTED"),
        "Other": SimpleNamespace(),
    }

    assert controller.getSmartMicroscopyWorkflowNames() == ["EtSTED", "EtSnouty"]


def test_build_workflow_names_combines_backend_and_saved_setup_info():
    setupInfo = SimpleNamespace(
        smartMicroscopyModes={"ConfigOnly": {"event": "event mode"}},
        smartMicroscopyModePolicies={"PolicyOnly": "allow"},
        smartMicroscopyModeSwitchingEnabled={"EnabledOnly": True},
    )
    controller = _makeController(
        setupInfo,
        setupModeController=_FakeSetupModeBackend(["EtSnouty", "EtSTED"]),
    )

    assert controller._buildWorkflowNames() == [
        "ConfigOnly",
        "EnabledOnly",
        "EtSTED",
        "EtSnouty",
        "PolicyOnly",
    ]


def test_build_workflow_names_falls_back_to_backend_controller_registry():
    backend = SimpleNamespace(
        _controllers={
            "snouty": SimpleNamespace(SMART_MODE_WORKFLOW="EtSnouty"),
            "plain": SimpleNamespace(),
        }
    )
    controller = _makeController(setupModeController=backend)

    assert controller._buildWorkflowNames() == ["EtSnouty"]


def test_get_current_smart_mode_config_returns_nested_copies():
    setupInfo = SimpleNamespace(
        smartMicroscopyModes={"EtSnouty": {"event": "event mode"}},
        smartMicroscopyModePolicies={"EtSnouty": "warnOnly"},
        smartMicroscopyModeSwitchingEnabled={"EtSnouty": True},
    )
    controller = _makeController(setupInfo)

    config = controller._getCurrentSmartModeConfig()
    config["modes"]["EtSnouty"]["event"] = "changed"
    config["policies"]["EtSnouty"] = "allow"
    config["enabled"]["EtSnouty"] = False

    assert setupInfo.smartMicroscopyModes["EtSnouty"]["event"] == "event mode"
    assert setupInfo.smartMicroscopyModePolicies["EtSnouty"] == "warnOnly"
    assert setupInfo.smartMicroscopyModeSwitchingEnabled["EtSnouty"] is True


def test_validate_smart_mode_config_reports_missing_nonempty_modes():
    controller = _makeController()

    problems = controller._validateSmartModeConfig(
        {
            "modes": {
                "EtSnouty": {
                    "scouting": "widefield",
                    "event": "missing",
                    "resume": "",
                    "idle": None,
                },
            },
        },
        availableModes=["widefield"],
    )

    assert len(problems) == 1
    assert "EtSnouty" in problems[0]
    assert "event" in problems[0]
    assert "missing" in problems[0]


def test_apply_smart_mode_config_normalizes_persists_and_updates_live_service(monkeypatch):
    from imswitch.imcontrol.model import configfiletools

    setupInfo = SimpleNamespace(
        smartMicroscopyModes={},
        smartMicroscopyModePolicies={},
        smartMicroscopyModeSwitchingEnabled={},
    )
    controller = _makeController(setupInfo)
    smartModeService = _FakeSmartModeService()
    controller._smartModeService = smartModeService

    saved = []
    monkeypatch.setattr(configfiletools, "loadOptions", lambda: ("test_setup",))
    monkeypatch.setattr(
        configfiletools,
        "saveSetupInfo",
        lambda option, info: saved.append((option, info)),
    )

    controller._applySmartModeConfig(
        {
            "modes": {
                "EtSnouty": {
                    "scouting": "widefield mode",
                    "event": "event mode",
                    "unknown": "ignored",
                    "idle": "",
                },
                "": {"event": "ignored"},
            },
            "policies": {
                "EtSnouty": "warnOnly",
                "EtSTED": "blockOnHazard",
                "Bad": "not-a-policy",
            },
            "enabled": {"EtSnouty": 1, "EtSTED": False},
        }
    )

    assert setupInfo.smartMicroscopyModes == {
        "EtSnouty": {
            "scouting": "widefield mode",
            "event": "event mode",
        },
    }
    assert setupInfo.smartMicroscopyModePolicies == {"EtSnouty": "warnOnly"}
    assert setupInfo.smartMicroscopyModeSwitchingEnabled == {"EtSnouty": True}
    assert smartModeService.calls == [
        (
            setupInfo.smartMicroscopyModes,
            setupInfo.smartMicroscopyModePolicies,
        )
    ]
    assert saved == [("test_setup", setupInfo)]
