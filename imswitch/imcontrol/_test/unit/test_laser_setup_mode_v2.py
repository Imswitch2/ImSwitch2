from types import SimpleNamespace
from unittest.mock import Mock

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.LaserController import LaserController
from imswitch.imcontrol.model import LaserPowerContext, LaserPowerMode


class _Laser:
    def __init__(self, *, binary=False, units='mW'):
        self.isBinary = binary
        self.valueUnits = units


class _Lasers:
    def __init__(self):
        self._items = {
            '405': _Laser(),
            '488': _Laser(),
        }

    def __iter__(self):
        return iter(self._items.items())

    def __getitem__(self, name):
        return self._items[name]


class _Widget:
    def __init__(self):
        self.laserModules = {}
        self.active = {'405': False, '488': False}
        self.values = {'405': 1.0, '488': 2.0}

    def isLaserActive(self, name):
        return self.active[name]

    def getValue(self, name):
        return self.values[name]

    def setValue(self, name, value, emitSignal=False):
        self.values[name] = value

    def setLaserActive(self, name, active, emitSignal=False):
        self.active[name] = bool(active)

    def setModulationEnabled(self, *args, **kwargs):
        pass

    def setCurrentPreset(self, *args, **kwargs):
        pass


class _SetupModesBackend:
    def __init__(self, states):
        self.states = states

    def getSetupModeComponentStates(self, componentName):
        return [
            {'modeName': name, 'state': state}
            for name, state in self.states.items()
        ]

    def replaceSetupModeComponentState(self, modeName, componentName, componentState):
        self.states[modeName] = componentState


def _controller(power_mode=LaserPowerMode.SEPARATED):
    controller = LaserController.__new__(LaserController)
    controller._powerMode = power_mode
    controller._master = SimpleNamespace(lasersManager=_Lasers())
    controller._widget = _Widget()
    controller._logger = Mock()
    controller._setupInfo = SimpleNamespace(laserPresets={})
    controller._livePowerValues = {'405': 1.0, '488': 2.0}
    controller._scanPowerValues = {'488': 20.0}
    controller._currentPowerContext = LaserPowerContext.LIVE
    controller._selectedPresetByContext = {
        LaserPowerContext.LIVE: None,
        LaserPowerContext.SCAN: None,
    }
    controller._selectedCoupledPreset = None
    controller._setupModeController = None
    controller.is_scanning = False
    controller._scanLaserState = {}

    controller._refreshPresetControls = Mock()
    controller._refreshAllLaserRows = Mock()
    controller._emitLaserPowerChanged = Mock()
    controller.setSharedAttr = Mock()

    calls = []

    def set_active(name, active):
        calls.append(('active', name, bool(active)))
        controller._widget.active[name] = bool(active)

    def set_value(name, value):
        calls.append(('value', name, value))
        controller._livePowerValues[name] = value
        controller._widget.values[name] = value

    def set_scan(name, value):
        calls.append(('scan', name, value))
        controller._scanPowerValues[name] = value

    controller.setLaserActive = set_active
    controller.setLaserValue = set_value
    controller._setScanPowerValue = set_scan
    controller.toggleModulation = Mock()
    controller.frequencyChanged = Mock()
    controller.dutyCycleChanged = Mock()
    controller._testCalls = calls
    return controller


def _full_state(*, enabled=False, p405=1.0, p488=2.0):
    return {
        'lasers': {
            '405': {'enabled': enabled, 'value': p405, 'isBinary': False, 'valueUnits': 'mW'},
            '488': {'enabled': enabled, 'value': p488, 'isBinary': False, 'valueUnits': 'mW'},
        },
        'laserOrder': ['405', '488'],
        'modulation': {},
        'scanPowers': {},
    }


def _separated_state(*, live=True, scan=True, live_enabled=False, scan_powers=None):
    state = {
        'laserSetupModeStateVersion': 2,
        'capturedPowerMode': 'separated',
        'laserOrder': ['405', '488'],
        'live': {'included': live},
        'scan': {'included': scan},
    }
    if live:
        state['live'].update({
            'powerBinding': {'kind': 'snapshot', 'powers': {'405': 1.0, '488': 2.0}},
            'lasers': {
                '405': {'enabled': live_enabled, 'isBinary': False, 'valueUnits': 'mW'},
                '488': {'enabled': live_enabled, 'isBinary': False, 'valueUnits': 'mW'},
            },
            'modulation': {},
        })
    if scan:
        state['scan']['powerBinding'] = {
            'kind': 'snapshot',
            'powers': scan_powers or {'405': 10.0, '488': 20.0},
        }
    return state


def test_separated_snapshot_freezes_effective_scan_values():
    controller = _controller(LaserPowerMode.SEPARATED)
    # 405 has no explicit Scan override and therefore currently follows Live.
    options = {
        'powerMode': 'separated',
        'sections': [
            {'id': 'live', 'included': False, 'bindingKind': 'snapshot'},
            {'id': 'scan', 'included': True, 'bindingKind': 'snapshot'},
        ],
    }

    state = controller.prepareSetupModeState(_full_state(), captureOptions=options)

    assert state['capturedPowerMode'] == 'separated'
    assert state['live']['included'] is False
    assert state['scan']['powerBinding']['powers'] == {'405': 1.0, '488': 20.0}


def test_separated_scan_only_apply_never_touches_live():
    controller = _controller(LaserPowerMode.SEPARATED)
    controller._widget.active['405'] = True
    controller._widget.active['488'] = True

    warnings = controller.applyComponentState(
        _separated_state(live=False, scan=True),
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    assert controller._testCalls == [
        ('scan', '405', 10.0),
        ('scan', '488', 20.0),
    ]
    assert controller._widget.active == {'405': True, '488': True}


def test_coupled_saved_to_separated_updates_scan_only():
    controller = _controller(LaserPowerMode.SEPARATED)
    state = {
        'laserSetupModeStateVersion': 2,
        'capturedPowerMode': 'coupled',
        'laserOrder': ['405', '488'],
        'coupled': {
            'powerBinding': {'kind': 'snapshot', 'powers': {'405': 12.0, '488': 34.0}},
            'lasers': {
                '405': {'enabled': True, 'isBinary': False, 'valueUnits': 'mW'},
                '488': {'enabled': True, 'isBinary': False, 'valueUnits': 'mW'},
            },
            'modulation': {},
        },
    }

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert controller._testCalls == [
        ('scan', '405', 12.0),
        ('scan', '488', 34.0),
    ]
    assert any('Scan only' in warning for warning in warnings)


def test_separated_scan_to_coupled_forces_off_before_each_setpoint_and_stays_off():
    controller = _controller(LaserPowerMode.COUPLED)
    controller._widget.active = {'405': True, '488': True}

    warnings = controller.applyComponentState(
        _separated_state(live=False, scan=True),
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert controller._testCalls == [
        ('active', '405', False),
        ('value', '405', 10.0),
        ('active', '488', False),
        ('value', '488', 20.0),
    ]
    assert controller._widget.active == {'405': False, '488': False}
    assert any('kept OFF' in warning for warning in warnings)


def test_separated_live_only_to_coupled_does_nothing():
    controller = _controller(LaserPowerMode.COUPLED)

    warnings = controller.applyComponentState(
        _separated_state(live=True, scan=False, live_enabled=True),
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert controller._testCalls == []
    assert any('only separated Live' in warning for warning in warnings)


def test_follow_preset_uses_current_values_and_missing_entries_fallback():
    controller = _controller()
    controller._setupInfo.laserPresets = {
        '3D': {'context': 'scan', 'powers': {'488': {'value': 77.0}}}
    }
    binding = {
        'kind': 'preset',
        'presetName': '3D',
        'presetContextAtCapture': 'scan',
        'fallbackPowers': {'405': 10.0, '488': 20.0},
    }

    powers, warnings = controller._resolveSetupModePowerBinding(binding)

    assert powers == {'405': 10.0, '488': 77.0}
    assert any('405' in warning for warning in warnings)


def test_missing_followed_preset_uses_saved_fallback():
    controller = _controller()
    binding = {
        'kind': 'preset',
        'presetName': 'Deleted',
        'fallbackPowers': {'405': 10.0, '488': 20.0},
    }

    powers, warnings = controller._resolveSetupModePowerBinding(binding)

    assert powers == {'405': 10.0, '488': 20.0}
    assert any('fallback' in warning for warning in warnings)


def test_high_power_hazard_resolves_current_followed_live_preset():
    controller = _controller(LaserPowerMode.SEPARATED)
    controller._setupInfo.laserPresets = {
        'Screening': {
            'context': 'live',
            'powers': {'405': {'value': 80.0}, '488': {'value': 2.0}},
        }
    }
    state = _separated_state(live=True, scan=False, live_enabled=True)
    state['live']['powerBinding'] = {
        'kind': 'preset',
        'presetName': 'Screening',
        'fallbackPowers': {'405': 1.0, '488': 2.0},
    }

    hazards = controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        context={'laserPowerThresholdMw': 50.0},
    )

    assert [hazard['details']['laserName'] for hazard in hazards] == ['405']
    assert hazards[0]['details']['value'] == 80.0


def test_preset_reference_rewrite_preserves_fallback():
    controller = _controller()
    state = _separated_state(live=False, scan=True)
    state['scan']['powerBinding'] = {
        'kind': 'preset',
        'presetName': '3D',
        'fallbackPowers': {'405': 10.0, '488': 20.0},
    }
    backend = _SetupModesBackend({'RESOLFT': state})
    controller._setupModeController = backend

    assert controller._setupModePresetReferences('3D') == ['RESOLFT']
    controller._rewriteSetupModePresetReferences('3D', '3D tuned')

    binding = backend.states['RESOLFT']['scan']['powerBinding']
    assert binding['presetName'] == '3D tuned'
    assert binding['fallbackPowers'] == {'405': 10.0, '488': 20.0}


def test_coupled_capture_records_coupled_mode_without_nested_live_scan():
    controller = _controller(LaserPowerMode.COUPLED)
    options = {
        'powerMode': 'coupled',
        'sections': [
            {'id': 'coupled', 'included': True, 'bindingKind': 'snapshot'},
        ],
    }

    state = controller.prepareSetupModeState(
        _full_state(enabled=True, p405=3.0, p488=4.0), captureOptions=options
    )

    assert state['capturedPowerMode'] == 'coupled'
    assert 'coupled' in state
    assert 'live' not in state
    assert 'scan' not in state
    assert state['coupled']['powerBinding']['powers'] == {'405': 3.0, '488': 4.0}


def test_separated_live_only_capture_marks_scan_not_included():
    controller = _controller(LaserPowerMode.SEPARATED)
    options = {
        'powerMode': 'separated',
        'sections': [
            {'id': 'live', 'included': True, 'bindingKind': 'snapshot'},
            {'id': 'scan', 'included': False, 'bindingKind': 'snapshot'},
        ],
    }

    state = controller.prepareSetupModeState(_full_state(), captureOptions=options)

    assert state['capturedPowerMode'] == 'separated'
    assert state['live']['included'] is True
    assert state['scan'] == {'included': False}


def test_old_scan_power_state_normalizes_as_separated():
    controller = _controller(LaserPowerMode.SEPARATED)
    old = _full_state(enabled=False, p405=1.0, p488=2.0)
    old['scanPowers'] = {'488': 25.0}

    normalized, warnings = controller._normalizeSetupModeState(old)

    assert warnings == []
    assert normalized['capturedPowerMode'] == 'separated'
    assert normalized['scan']['powerBinding']['powers'] == {'405': 1.0, '488': 25.0}


def test_native_coupled_apply_restores_saved_on_state_after_power():
    controller = _controller(LaserPowerMode.COUPLED)
    state = {
        'laserSetupModeStateVersion': 2,
        'capturedPowerMode': 'coupled',
        'laserOrder': ['405'],
        'coupled': {
            'powerBinding': {'kind': 'snapshot', 'powers': {'405': 12.0}},
            'lasers': {
                '405': {'enabled': True, 'isBinary': False, 'valueUnits': 'mW'},
            },
            'modulation': {},
        },
    }

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert warnings == []
    assert controller._testCalls == [
        ('active', '405', False),
        ('value', '405', 12.0),
        ('active', '405', True),
    ]


def test_separated_live_only_apply_leaves_existing_scan_state_unchanged():
    controller = _controller(LaserPowerMode.SEPARATED)
    before = dict(controller._scanPowerValues)

    controller.applyComponentState(
        _separated_state(live=True, scan=False, live_enabled=False),
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert controller._scanPowerValues == before
    assert not any(call[0] == 'scan' for call in controller._testCalls)


def test_separated_live_and_scan_to_coupled_uses_scan_and_ignores_live():
    controller = _controller(LaserPowerMode.COUPLED)
    state = _separated_state(
        live=True, scan=True, live_enabled=True,
        scan_powers={'405': 30.0, '488': 40.0},
    )

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert controller._testCalls == [
        ('active', '405', False),
        ('value', '405', 30.0),
        ('active', '488', False),
        ('value', '488', 40.0),
    ]
    assert any('saved Live state was not applied' in warning for warning in warnings)


def test_explicit_zero_scan_power_survives_cross_mode_conversion():
    controller = _controller(LaserPowerMode.COUPLED)
    state = _separated_state(
        live=False, scan=True, scan_powers={'405': 0.0, '488': 20.0}
    )

    controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert ('value', '405', 0.0) in controller._testCalls


def test_scan_only_cross_mode_application_has_no_high_power_hazard():
    controller = _controller(LaserPowerMode.COUPLED)
    state = _separated_state(
        live=False, scan=True, scan_powers={'405': 100.0, '488': 120.0}
    )

    hazards = controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        context={'laserPowerThresholdMw': 50.0},
    )

    assert hazards == []
