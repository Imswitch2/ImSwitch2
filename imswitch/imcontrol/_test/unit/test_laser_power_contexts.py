"""Live/Scan laser power-context and preset contracts."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.LaserController import LaserController
from imswitch.imcontrol.model import (
    LaserPowerContext,
    LaserPowerMode,
    LaserScanPowerControl,
    LaserScanState,
    normalizeLaserPowerMode,
)
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
    def __init__(self, live=5.0):
        self.values = {"488": live, "binary": 0}
        self.active = {"488": False, "binary": False}
        self.currentPreset = None
        self.laserModules = {"488": None, "binary": None}
        self.presets = []
        self.context = "live"
        self.presetNames = []
        self.aoOwned = {}
        self.powerEditable = {}
        self.scanActive = {}
        self.secondary = {}

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

    def setPowerContext(self, context, emitSignal=False):
        self.context = context

    def setPresetNames(self, names, selected=None):
        self.presetNames = list(names)
        self.currentPreset = selected

    def setLaserAOOwned(self, name, owned, tooltip=''):
        self.aoOwned[name] = (owned, tooltip)

    def setLaserPowerEditable(self, name, editable):
        self.powerEditable[name] = editable

    def setLaserScanActive(self, name, active):
        self.scanActive[name] = active

    def setLaserSecondaryStatus(self, name, text, **kwargs):
        self.secondary[name] = (text, kwargs)


class _Logger:
    def warning(self, *args, **kwargs):
        pass


def _controller(*, live=5.0, setup=None, mode='separated'):
    ctrl = LaserController.__new__(LaserController)
    ctrl._master = SimpleNamespace(lasersManager=_LasersManager())
    ctrl._widget = _Widget(live=live)
    ctrl._setupInfo = setup or ViewSetupInfo(laserPowerMode=mode)
    ctrl._setupInfo.laserPowerMode = mode
    ctrl._commChannel = SimpleNamespace(sharedAttrs={})
    ctrl._logger = _Logger()
    ctrl.settingAttr = False
    ctrl._livePowerValues = {"488": live}
    ctrl._scanPowerValues = {}
    ctrl._powerMode = LaserPowerMode(mode)
    ctrl._currentPowerContext = LaserPowerContext.LIVE
    ctrl._scanLaserState = {}
    ctrl._laserDisplayMeta = {"488": ("mW", 1)}
    ctrl._selectedPresetByContext = {
        LaserPowerContext.LIVE: None,
        LaserPowerContext.SCAN: None,
    }
    ctrl._selectedCoupledPreset = None
    return ctrl




def test_laser_power_mode_defaults_to_coupled_and_rejects_unknown_values():
    assert normalizeLaserPowerMode(None) is LaserPowerMode.COUPLED
    assert normalizeLaserPowerMode("coupled") is LaserPowerMode.COUPLED
    assert normalizeLaserPowerMode("separated") is LaserPowerMode.SEPARATED

    import pytest
    with pytest.raises(ValueError, match="coupled.*separated"):
        normalizeLaserPowerMode("other")


def test_view_setup_info_defaults_to_coupled_power_mode():
    assert ViewSetupInfo().laserPowerMode == "coupled"


def test_context_api_uses_explicit_setting_names():
    # get/setLaserPower were introduced only by the Live/Scan work and are
    # intentionally not retained as ambiguous public aliases.
    assert not hasattr(LaserController, 'getLaserPower')
    assert not hasattr(LaserController, 'setLaserPower')
    assert hasattr(LaserController, 'getLaserPowerSetting')
    assert hasattr(LaserController, 'setLaserPowerSetting')
    assert hasattr(LaserController, 'getLaserAcquisitionState')

def test_live_power_write_uses_existing_hardware_path():
    ctrl = _controller(live=3.0)

    ctrl.setLaserPowerSetting("488", 12.5, LaserPowerContext.LIVE)

    assert ("value", 12.5) in ctrl._master.lasersManager["488"].calls
    assert ctrl._widget.getValue("488") == 12.5


def test_scan_power_write_is_software_only():
    ctrl = _controller(live=3.0)

    ctrl.setLaserPowerSetting("488", 21.0, LaserPowerContext.SCAN)

    assert ctrl._scanPowerValues == {"488": 21.0}
    assert ctrl._master.lasersManager["488"].calls == []
    assert ctrl._widget.getValue("488") == 3.0


def test_unset_scan_power_follows_live_until_scan_becomes_explicit():
    ctrl = _controller(live=4.0)

    assert ctrl.getLaserPowerSetting("488", "scan") == 4.0
    ctrl.setLaserPowerSetting("488", 6.0, "live")
    assert ctrl.getLaserPowerSetting("488", "scan") == 6.0

    ctrl.setLaserPowerSetting("488", 25.0, "scan")
    ctrl.setLaserPowerSetting("488", 7.0, "live")
    assert ctrl.getLaserPowerSetting("488", "live") == 7.0
    assert ctrl.getLaserPowerSetting("488", "scan") == 25.0


def test_coupled_acquisition_state_uses_live_setting_not_dormant_scan_setting():
    ctrl = _controller(live=4.0, mode='coupled')
    ctrl._scanLaserState = {
        '488': LaserScanState(
            active_for_scan=True,
            power_control=LaserScanPowerControl.DIRECT,
        )
    }
    ctrl.setLaserPowerSetting('488', 30.0, 'scan')

    state = ctrl.getLaserAcquisitionState('488')

    assert state == {
        'active_for_scan': True,
        'power_mode': 'coupled',
        'power_control': 'direct',
        'power_device': '488',
        'value': 4.0,
        'value_context': 'live',
        'uses_fallback': False,
    }
    assert ctrl.getLaserPowerSetting('488', 'scan') == 30.0


def test_separated_acquisition_state_distinguishes_scan_value_and_live_fallback():
    ctrl = _controller(live=4.0, mode='separated')
    ctrl._scanLaserState = {
        '488': LaserScanState(
            active_for_scan=True,
            power_control=LaserScanPowerControl.DIRECT,
        )
    }

    fallback = ctrl.getLaserAcquisitionState('488')
    assert fallback['value'] == 4.0
    assert fallback['value_context'] == 'live'
    assert fallback['uses_fallback'] is True

    ctrl.setLaserPowerSetting('488', 30.0, 'scan')
    explicit = ctrl.getLaserAcquisitionState('488')
    assert explicit['value'] == 30.0
    assert explicit['value_context'] == 'scan'
    assert explicit['uses_fallback'] is False


def test_acquisition_state_has_no_scalar_for_ao_or_inactive_paths():
    ctrl = _controller(live=4.0, mode='separated')
    ctrl._scanLaserState = {
        '488': LaserScanState(
            active_for_scan=True,
            power_control=LaserScanPowerControl.AO,
        )
    }
    ctrl.setLaserPowerSetting('488', 30.0, 'scan')

    ao = ctrl.getLaserAcquisitionState('488')
    assert ao['power_control'] == 'ao'
    assert ao['value'] is None
    assert ao['value_context'] is None
    assert ao['uses_fallback'] is False

    ctrl._scanLaserState['488'] = LaserScanState(
        active_for_scan=False,
        power_control=LaserScanPowerControl.DIRECT,
    )
    inactive = ctrl.getLaserAcquisitionState('488')
    assert inactive['active_for_scan'] is False
    assert inactive['value'] is None
    assert inactive['value_context'] is None


def test_acquisition_state_resolves_gate_to_actual_power_device():
    ctrl = _controller(live=4.0, mode='separated')
    ctrl._master.lasersManager._lasers['561'] = _Laser(binary=True)
    ctrl._master.lasersManager._lasers['561AOTF'] = _Laser()
    ctrl._widget.values.update({'561': 0, '561AOTF': 7.0})
    ctrl._widget.active.update({'561': False, '561AOTF': False})
    ctrl._widget.laserModules.update({'561': None, '561AOTF': None})
    ctrl._livePowerValues['561AOTF'] = 7.0
    ctrl._scanPowerValues['561AOTF'] = 22.0
    ctrl._setupInfo.lasers = {
        '561': SimpleNamespace(powerDevice='561AOTF'),
        '561AOTF': SimpleNamespace(powerDevice=None),
    }
    ctrl._scanLaserState = {
        '561': LaserScanState(True, LaserScanPowerControl.DIRECT),
        '561AOTF': LaserScanState(True, LaserScanPowerControl.DIRECT),
    }

    state = ctrl.getLaserAcquisitionState('561')

    assert state['power_device'] == '561AOTF'
    assert state['power_control'] == 'direct'
    assert state['value'] == 22.0
    assert state['value_context'] == 'scan'


def test_scan_view_refreshes_live_fallback_but_preserves_explicit_scan_value():
    ctrl = _controller(live=4.0)
    ctrl._currentPowerContext = LaserPowerContext.SCAN
    ctrl._widget.setPowerContext('scan')

    ctrl.setLaserPowerSetting('488', 6.0, LaserPowerContext.LIVE)
    assert ctrl._widget.getValue('488') == 6.0

    ctrl.setLaserPowerSetting('488', 25.0, LaserPowerContext.SCAN)
    assert ctrl._widget.getValue('488') == 25.0

    ctrl.setLaserPowerSetting('488', 7.0, LaserPowerContext.LIVE)
    assert ctrl.getLaserPowerSetting('488', LaserPowerContext.SCAN) == 25.0
    assert ctrl._widget.getValue('488') == 25.0


def _contextual(context, value):
    return {"context": context, "powers": {"488": {"value": value}}}


def test_separated_preset_visibility_uses_context_and_unspecified_scan_fallback():
    setup = ViewSetupInfo(laserPresets={
        "Legacy": {"488": {"value": 10.0}},
        "Screening": _contextual("live", 2.0),
        "pRESOLFT": _contextual("scan", 30.0),
    })
    ctrl = _controller(setup=setup)

    assert ctrl.getLaserPresetNames("live") == ["Screening"]
    assert ctrl.getLaserPresetNames("scan") == ["Legacy", "pRESOLFT"]


def test_coupled_preset_visibility_shows_every_context():
    setup = ViewSetupInfo(laserPowerMode="coupled", laserPresets={
        "Legacy": {"488": {"value": 10.0}},
        "Screening": _contextual("live", 2.0),
        "pRESOLFT": _contextual("scan", 30.0),
    })
    ctrl = _controller(setup=setup, mode='coupled')

    assert ctrl.getLaserPresetNames("live") == ["Legacy", "Screening", "pRESOLFT"]
    assert ctrl.getLaserPresetNames("scan") == ["Legacy", "Screening", "pRESOLFT"]


def test_get_laser_preset_is_side_effect_free():
    setup = ViewSetupInfo(laserPresets={
        "pRESOLFT": _contextual("scan", 30.0),
    })
    ctrl = _controller(live=2.0, setup=setup)

    preset = ctrl.getLaserPreset("pRESOLFT", "scan")

    assert preset == {"488": LaserPresetInfo(value=30.0)}
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] is None
    assert ctrl._master.lasersManager["488"].calls == []

    preset["488"] = LaserPresetInfo(value=99.0)
    assert ctrl.getLaserPreset("pRESOLFT", "scan")["488"].value == 30.0
    assert ctrl.getLaserPreset("missing", "scan") is None


def test_loading_scan_preset_never_writes_hardware_in_separated():
    setup = ViewSetupInfo(laserPresets={
        "pRESOLFT": _contextual("scan", 35.0),
    })
    ctrl = _controller(live=0.5, setup=setup)

    assert ctrl.loadLaserPreset("pRESOLFT", "scan") is True

    assert ctrl.getLaserPowerSetting("488", "scan") == 35.0
    assert ctrl.getLaserPowerSetting("488", "live") == 0.5
    assert ctrl._master.lasersManager["488"].calls == []
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "pRESOLFT"


def test_loading_any_preset_in_coupled_applies_single_hardware_power():
    setup = ViewSetupInfo(laserPowerMode="coupled", laserPresets={
        "pRESOLFT": _contextual("scan", 35.0),
    })
    ctrl = _controller(live=0.5, setup=setup, mode='coupled')

    assert ctrl.loadLaserPreset("pRESOLFT", "scan") is True
    assert ctrl.getLaserPowerSetting("488", "live") == 35.0
    assert ("value", 35.0) in ctrl._master.lasersManager["488"].calls
    assert ctrl._selectedCoupledPreset == "pRESOLFT"


def test_new_scan_preset_is_contextual_and_does_not_modify_live_hardware():
    setup = ViewSetupInfo(laserPresets={
        "Screening": _contextual("live", 2.0),
    })
    ctrl = _controller(live=2.0, setup=setup)
    ctrl.setLaserPowerSetting("488", 40.0, "scan")

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveLaserPreset("Acquisition", "scan")

    assert setup.laserPresets["Acquisition"] == {
        "context": "scan", "powers": {"488": {"value": 40.0}}
    }
    assert ctrl._master.lasersManager["488"].calls == []


def test_coupled_new_preset_is_unspecified_but_update_preserves_existing_context():
    setup = ViewSetupInfo(laserPowerMode="coupled", laserPresets={
        "Scan preset": _contextual("scan", 20.0),
    })
    ctrl = _controller(live=7.0, setup=setup, mode='coupled')

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveLaserPreset("New coupled", "live")
        ctrl.saveLaserPreset("Scan preset", "live")

    assert setup.laserPresets["New coupled"] == {"488": {"value": 7.0}}
    assert setup.laserPresets["Scan preset"] == {
        "context": "scan", "powers": {"488": {"value": 7.0}}
    }


def test_new_live_preset_in_separated_is_contextual():
    setup = ViewSetupInfo(laserPresets={})
    ctrl = _controller(live=3.0, setup=setup, mode='separated')

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveLaserPreset("Screening", "live")

    assert setup.laserPresets["Screening"] == {
        "context": "live", "powers": {"488": {"value": 3.0}}
    }


def test_rename_preserves_contextual_preset_payload():
    setup = ViewSetupInfo(laserPresets={
        "Old": _contextual("scan", 25.0),
    })
    ctrl = _controller(setup=setup, mode='separated')

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        assert ctrl.renameLaserPreset("Old", "New", "scan") is True

    assert "Old" not in setup.laserPresets
    assert setup.laserPresets["New"] == _contextual("scan", 25.0)


def test_saving_preset_does_not_select_unless_requested():
    setup = ViewSetupInfo(laserPresets={
        "Current": _contextual("scan", 20.0),
    })
    ctrl = _controller(live=2.0, setup=setup)
    ctrl._setSelectedPresetName(LaserPowerContext.SCAN, "Current")
    ctrl.setLaserPowerSetting("488", 40.0, "scan")

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

    assert setup.laserPresets["Other"] == {
        "context": "scan", "powers": {"488": {"value": 40.0}}
    }
    assert setup.laserPresets["Selected"] == {
        "context": "scan", "powers": {"488": {"value": 40.0}}
    }
    assert ctrl._master.lasersManager["488"].calls == []


def test_bulk_power_api_respects_context_semantics():
    ctrl = _controller(live=3.0)

    ctrl.setLaserPowerSettings({"488": 8.0}, "live")
    assert ctrl.getLaserPowerSettings("live") == {"488": 8.0}
    assert ("value", 8.0) in ctrl._master.lasersManager["488"].calls

    ctrl._master.lasersManager["488"].calls.clear()
    ctrl.setLaserPowerSettings({"488": 32.0}, "scan")
    assert ctrl.getLaserPowerSettings("scan") == {"488": 32.0}
    assert ctrl._master.lasersManager["488"].calls == []


def test_zero_scan_power_is_explicit_and_persists():
    ctrl = _controller(live=5.0)

    ctrl.setLaserPowerSetting("488", 0, "scan")
    ctrl.setLaserPowerSetting("488", 9.0, "live")

    assert ctrl.getLaserPowerSetting("488", "scan") == 0
    state = ctrl.getComponentState()
    assert state["scanPowers"] == {"488": 0}

    restored = _controller(live=1.0)
    restored.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert restored._scanPowerValues == {"488": 0}
    assert restored.getLaserPowerSetting("488", "scan") == 0


def test_legacy_setup_json_without_scan_presets_still_loads():
    legacy = json.dumps({
        "laserPresets": {
            "Default": {"488": {"value": 1.25}}
        }
    })

    setup = ViewSetupInfo.from_json(legacy, infer_missing=True)

    assert setup.laserPresets["Default"]["488"]["value"] == 1.25
    assert setup.laserPowerMode == "coupled"


def test_component_state_persists_only_explicit_scan_overrides():
    ctrl = _controller(live=5.0)
    ctrl.setLaserPowerSetting("488", 28.0, "scan")

    state = ctrl.getComponentState()

    assert state["scanPowers"] == {"488": 28.0}
    assert state["livePreset"] is None
    assert state["scanPreset"] is None

    restored = _controller(live=1.0)
    restored._master.lasersManager["488"].calls.clear()
    warnings = restored.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )

    assert warnings == []
    assert restored._scanPowerValues == {"488": 28.0}
    # One hardware write is the existing Live-state restore. Scan restore is inert.
    assert restored._master.lasersManager["488"].calls == [("value", 5.0)]


def test_old_component_state_without_scan_keys_resets_to_live_fallback():
    ctrl = _controller(live=3.0)
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
    assert ctrl.getLaserPowerSetting("488", "scan") == 8.0


def test_context_switch_changes_preset_namespace_without_loading_or_hardware_write():
    setup = ViewSetupInfo(laserPresets={
        "Screening": _contextual("live", 2.0),
        "Acquisition": _contextual("scan", 30.0),
    })
    ctrl = _controller(live=2.0, setup=setup)
    ctrl._setSelectedPresetName(LaserPowerContext.LIVE, "Screening")
    ctrl._setSelectedPresetName(LaserPowerContext.SCAN, "Acquisition")

    ctrl.powerContextChanged("scan")

    assert ctrl._widget.context == "scan"
    assert ctrl._widget.presetNames == ["Acquisition"]
    assert ctrl._widget.currentPreset == "Acquisition"
    assert ctrl.getLaserPowerSetting("488", "scan") == 2.0  # still fallback; not loaded
    assert ctrl._master.lasersManager["488"].calls == []


def test_combo_selection_immediately_loads_current_scan_context_only():
    setup = ViewSetupInfo(laserPresets={
        "Acquisition": _contextual("scan", 30.0),
    })
    ctrl = _controller(live=2.0, setup=setup)
    ctrl.powerContextChanged("scan")

    ctrl.presetSelected("Acquisition")

    assert ctrl.getLaserPowerSetting("488", "scan") == 30.0
    assert ctrl.getLaserPowerSetting("488", "live") == 2.0
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "Acquisition"
    assert ctrl._master.lasersManager["488"].calls == []


def test_save_current_values_to_does_not_load_or_change_selected_preset():
    setup = ViewSetupInfo(laserPresets={
        "Current": _contextual("scan", 20.0),
        "Other": _contextual("scan", 10.0),
    })
    ctrl = _controller(live=2.0, setup=setup)
    ctrl.powerContextChanged("scan")
    ctrl.loadLaserPreset("Current", "scan")
    ctrl.setLaserPowerSetting("488", 42.0, "scan")

    with patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.loadOptions",
        return_value=(SimpleNamespace(), False),
    ), patch(
        "imswitch.imcontrol.controller.controllers.LaserController.configfiletools.saveSetupInfo"
    ):
        ctrl.saveCurrentValuesTo("Other")

    assert setup.laserPresets["Other"] == {
        "context": "scan", "powers": {"488": {"value": 42.0}}
    }
    assert ctrl._selectedPresetByContext[LaserPowerContext.SCAN] == "Current"
    assert ctrl.getLaserPowerSetting("488", "scan") == 42.0
    assert ctrl._master.lasersManager["488"].calls == []


def test_ao_status_disables_scan_power_but_never_writes_hardware():
    from imswitch.imcontrol.model import LaserScanState, LaserScanPowerControl

    ctrl = _controller(live=2.0)
    ctrl.setLaserPowerSetting("488", 35.0, "scan")
    ctrl.powerContextChanged("scan")
    ctrl._master.lasersManager["488"].calls.clear()

    ctrl.scanLaserStateChanged({
        "488": LaserScanState(True, LaserScanPowerControl.AO)
    })

    assert ctrl._widget.aoOwned["488"][0] is True
    assert ctrl._widget.aoOwned["488"][1] == LaserController._AO_TOOLTIP
    assert ctrl._widget.powerEditable["488"] is False
    assert ctrl._widget.scanActive["488"] is True
    assert ctrl._master.lasersManager["488"].calls == []


def test_same_preset_store_switches_modes_without_rewriting_contexts():
    setup = ViewSetupInfo(laserPresets={
        "Legacy": {"488": {"value": 10.0}},
        "Screening": _contextual("live", 2.0),
        "pRESOLFT": _contextual("scan", 30.0),
    })
    original = json.loads(json.dumps(setup.laserPresets))

    coupled = _controller(setup=setup, mode='coupled')
    assert coupled.getLaserPresetNames('live') == ["Legacy", "Screening", "pRESOLFT"]

    separated = _controller(setup=setup, mode='separated')
    assert separated.getLaserPresetNames('live') == ["Screening"]
    assert separated.getLaserPresetNames('scan') == ["Legacy", "pRESOLFT"]

    coupled_again = _controller(setup=setup, mode='coupled')
    assert coupled_again.getLaserPresetNames('scan') == ["Legacy", "Screening", "pRESOLFT"]
    assert setup.laserPresets == original


def test_contextual_setup_json_roundtrip_shape_is_self_contained():
    raw = json.dumps({
        "laserPowerMode": "separated",
        "laserPresets": {
            "Legacy": {"488": {"value": 1.0}},
            "Screening": {
                "context": "live",
                "powers": {"488": {"value": 0.5}},
            },
            "pRESOLFT": {
                "context": "scan",
                "powers": {"488": {"value": 35.0}},
            },
        },
    })
    setup = ViewSetupInfo.from_json(raw, infer_missing=True)
    data = setup.to_dict()

    assert data["laserPowerMode"] == "separated"
    assert data["laserPresets"]["Legacy"]["488"]["value"] == 1.0
    assert data["laserPresets"]["Screening"]["context"] == "live"
    assert data["laserPresets"]["pRESOLFT"]["context"] == "scan"
