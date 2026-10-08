"""Manual/Scan laser power-context and preset contracts."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.LaserController import LaserController
from imswitch.imcontrol.model import LaserPowerContext
from imswitch.imcontrol.view.guitools import LaserPresetInfo, ViewSetupInfo


class _Laser:
    def __init__(self, *, binary=False):
        self.isBinary = binary
        self.isModulated = False
        self.valueUnits = "mW" if not binary else ""
        self.calls = []

    def setValue(self, value):
        self.calls.append(("value", value))

    def setEnabled(self, enabled):
        self.calls.append(("enabled", enabled))


class _LasersManager:
    def __init__(self):
        self._lasers = {
            "488": _Laser(),
            "binary": _Laser(binary=True),
        }

    def __iter__(self):
        return iter(self._lasers.items())

    def __getitem__(self, name):
        return self._lasers[name]


class _Widget:
    def __init__(self, manual=5.0):
        self.values = {"488": manual, "binary": 0}
        self.active = {"488": False, "binary": False}
        self.currentPreset = None
        self.laserModules = {"488": None, "binary": None}
        self.presets = []

    def getValue(self, name):
        return self.values[name]

    def setValue(self, name, value, emitSignal=False):
        self.values[name] = value

    def isLaserActive(self, name):
        return self.active[name]

    def setLaserActive(self, name, active, emitSignal=False):
        self.active[name] = active

    def getCurrentPreset(self):
        return self.currentPreset

    def setCurrentPreset(self, name):
        self.currentPreset = name

    def addPreset(self, name):
        if name not in self.presets:
            self.presets.append(name)

    def removePreset(self, name):
        if name in self.presets:
            self.presets.remove(name)


class _Logger:
    def warning(self, *args, **kwargs):
        pass


def _controller(*, manual=5.0, setup=None):
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = SimpleNamespace(lasersManager=_LasersManager())
    ctrl._widget = _Widget(manual=manual)
    ctrl._setupInfo = setup or ViewSetupInfo()
    ctrl._commChannel = SimpleNamespace(sharedAttrs={})
    ctrl._logger = _Logger()
    ctrl.settingAttr = False
    ctrl._scanPowerValues = {}
    ctrl._selectedPresetByContext = {
        LaserPowerContext.MANUAL: None,
        LaserPowerContext.SCAN: None,
    }
    return ctrl


def test_manual_power_write_uses_existing_hardware_path():
    ctrl = _controller(manual=3.0)

    ctrl.setLaserPower("488", 12.5, LaserPowerContext.MANUAL)

    assert ("value", 12.5) in ctrl._master.lasersManager["488"].calls
    assert ctrl._widget.getValue("488") == 12.5


def test_scan_power_write_is_software_only():
    ctrl = _controller(manual=3.0)

    ctrl.setLaserPower("488", 21.0, LaserPowerContext.SCAN)

    assert ctrl._scanPowerValues == {"488": 21.0}
    assert ctrl._master.lasersManager["488"].calls == []
    assert ctrl._widget.getValue("488") == 3.0


def test_unset_scan_power_follows_manual_until_scan_becomes_explicit():
    ctrl = _controller(manual=4.0)

    assert ctrl.getLaserPower("488", "scan") == 4.0
    ctrl.setLaserPower("488", 6.0, "manual")
    assert ctrl.getLaserPower("488", "scan") == 6.0

    ctrl.setLaserPower("488", 25.0, "scan")
    ctrl.setLaserPower("488", 7.0, "manual")
    assert ctrl.getLaserPower("488", "manual") == 7.0
    assert ctrl.getLaserPower("488", "scan") == 25.0


def test_manual_and_scan_preset_namespaces_are_independent():
    setup = ViewSetupInfo(
        laserPresets={"Screening": {"488": LaserPresetInfo(value=2.0)}},
        laserScanPresets={"pRESOLFT": {"488": LaserPresetInfo(value=30.0)}},
    )
    ctrl = _controller(setup=setup)

    assert ctrl.getLaserPresetNames("manual") == ["Screening"]
    assert ctrl.getLaserPresetNames("scan") == ["pRESOLFT"]


def test_get_laser_preset_is_side_effect_free():
    setup = ViewSetupInfo(
        laserScanPresets={"pRESOLFT": {"488": LaserPresetInfo(value=30.0)}}
    )
    ctrl = _controller(manual=2.0, setup=setup)

    preset = ctrl.getLaserPreset("pRESOLFT", "scan")

    assert preset == {"488": LaserPresetInfo(value=30.0)}
    assert preset is not setup.laserScanPresets["pRESOLFT"]
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] is None
    assert ctrl._master.lasersManager["488"].calls == []

    preset["488"] = LaserPresetInfo(value=99.0)
    assert setup.laserScanPresets["pRESOLFT"]["488"].value == 30.0
    assert ctrl.getLaserPreset("missing", "scan") is None


def test_loading_scan_preset_never_writes_hardware():
    setup = ViewSetupInfo(
        laserScanPresets={"pRESOLFT": {"488": LaserPresetInfo(value=35.0)}}
    )
    ctrl = _controller(manual=0.5, setup=setup)

    assert ctrl.loadLaserPreset("pRESOLFT", "scan") is True

    assert ctrl.getLaserPower("488", "scan") == 35.0
    assert ctrl.getLaserPower("488", "manual") == 0.5
    assert ctrl._master.lasersManager["488"].calls == []
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "pRESOLFT"


def test_saving_scan_preset_does_not_modify_manual_namespace():
    setup = ViewSetupInfo(
        laserPresets={"Screening": {"488": LaserPresetInfo(value=2.0)}}
    )
    ctrl = _controller(manual=2.0, setup=setup)
    ctrl.setLaserPower("488", 40.0, "scan")

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveLaserPreset("Acquisition", "scan")

    assert list(setup.laserPresets) == ["Screening"]
    assert setup.laserScanPresets["Acquisition"]["488"].value == 40.0
    assert ctrl._master.lasersManager["488"].calls == []


def test_saving_preset_does_not_select_unless_requested():
    setup = ViewSetupInfo(
        laserScanPresets={"Current": {"488": LaserPresetInfo(value=20.0)}}
    )
    ctrl = _controller(manual=2.0, setup=setup)
    ctrl._setSelectedPresetName(LaserPowerContext.SCAN, "Current")
    ctrl.setLaserPower("488", 40.0, "scan")

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveLaserPreset("Other", "scan")
        assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "Current"

        ctrl.saveLaserPreset("Selected", "scan", select=True)
        assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "Selected"

    assert setup.laserScanPresets["Other"]["488"].value == 40.0
    assert setup.laserScanPresets["Selected"]["488"].value == 40.0
    assert ctrl._master.lasersManager["488"].calls == []


def test_bulk_power_api_respects_context_semantics():
    ctrl = _controller(manual=3.0)

    ctrl.setLaserPowers({"488": 8.0}, "manual")
    assert ctrl.getLaserPowers("manual") == {"488": 8.0}
    assert ("value", 8.0) in ctrl._master.lasersManager["488"].calls

    ctrl._master.lasersManager["488"].calls.clear()
    ctrl.setLaserPowers({"488": 32.0}, "scan")
    assert ctrl.getLaserPowers("scan") == {"488": 32.0}
    assert ctrl._master.lasersManager["488"].calls == []


def test_zero_scan_power_is_explicit_and_persists():
    ctrl = _controller(manual=5.0)

    ctrl.setLaserPower("488", 0, "scan")
    ctrl.setLaserPower("488", 9.0, "manual")

    assert ctrl.getLaserPower("488", "scan") == 0
    state = ctrl.getComponentState()
    assert state["scanPowers"] == {"488": 0}

    restored = _controller(manual=1.0)
    restored.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert restored._scanPowerValues == {"488": 0}
    assert restored.getLaserPower("488", "scan") == 0


def test_legacy_setup_json_without_scan_presets_still_loads():
    legacy = json.dumps({
        "laserPresets": {
            "Default": {"488": {"value": 1.25}}
        }
    })

    setup = ViewSetupInfo.from_json(legacy, infer_missing=True)

    assert setup.laserPresets["Default"]["488"].value == 1.25
    assert setup.laserScanPresets == {}


def test_component_state_persists_only_explicit_scan_overrides():
    ctrl = _controller(manual=5.0)
    ctrl.setLaserPower("488", 28.0, "scan")

    state = ctrl.getComponentState()

    assert state["scanPowers"] == {"488": 28.0}
    assert state["manualPreset"] is None
    assert state["scanPreset"] is None

    restored = _controller(manual=1.0)
    restored._master.lasersManager["488"].calls.clear()
    warnings = restored.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )

    assert warnings == []
    assert restored._scanPowerValues == {"488": 28.0}
    # One hardware write is the existing Manual-state restore. Scan restore is inert.
    assert restored._master.lasersManager["488"].calls == [("value", 5.0)]


def test_old_component_state_without_scan_keys_resets_to_manual_fallback():
    ctrl = _controller(manual=3.0)
    ctrl._scanPowerValues = {"488": 99.0}
    oldState = {
        "lasers": {
            "488": {
                "enabled": False,
                "value": 8.0,
                "isBinary": False,
                "valueUnits": "mW",
            }
        },
        "laserOrder": ["488"],
        "currentPreset": None,
        "modulation": {},
    }

    ctrl.applyComponentState(
        oldState, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )

    assert ctrl._scanPowerValues == {}
    assert ctrl.getLaserPower("488", "scan") == 8.0
