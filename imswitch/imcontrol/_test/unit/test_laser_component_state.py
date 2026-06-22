"""
Unit tests for LaserController unified state persistence (Phase 2a).

Tests the StatefulComponentMixin implementation on LaserController:
- getComponentState / applyComponentState round-trip
- STARTUP_RESTORE safety (no laser enable)
- SETUP_MODE_APPLY enables lasers
- describeComponentState returns human-readable summaries
- getComponentStateHazards detects high-power lasers
- Laser appears in setup mode component discovery
- GuiLayout is excluded from setup mode discovery
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call

from imswitch.imcontrol.controller.controllers.LaserController import LaserController
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


@pytest.fixture
def mock_laser_manager():
    """Mock lasersManager with two lasers: 488nm (analog) and UV (binary)."""
    manager = MagicMock()
    
    # Create laser manager instances
    laser_488 = Mock()
    laser_488.isBinary = False
    laser_488.valueUnits = "mW"
    laser_488.valueRangeMin = 0
    laser_488.valueRangeMax = 200
    laser_488.valueDecimals = 1
    laser_488.valueRangeStep = 1.0
    laser_488.wavelength = 488
    laser_488.isModulated = True
    laser_488.freqRangeMin = 0
    laser_488.freqRangeMax = 10000
    laser_488.freqRangeInit = 1000
    laser_488.setEnabled = Mock()
    laser_488.setValue = Mock()
    laser_488.setModulationFrequency = Mock()
    laser_488.setModulationDutyCycle = Mock()
    laser_488._laserInfo = Mock()
    laser_488._laserInfo.managerProperties = {}
    
    laser_uv = Mock()
    laser_uv.isBinary = True
    laser_uv.valueUnits = ""
    laser_uv.valueRangeMin = 0
    laser_uv.valueRangeMax = 1
    laser_uv.valueDecimals = 0
    laser_uv.valueRangeStep = None
    laser_uv.wavelength = 365
    laser_uv.isModulated = False
    laser_uv.freqRangeMin = 0
    laser_uv.freqRangeMax = 0
    laser_uv.freqRangeInit = 0
    laser_uv.setEnabled = Mock()
    laser_uv.setValue = Mock()
    laser_uv._laserInfo = Mock()
    laser_uv._laserInfo.managerProperties = {}
    
    # Make manager iterable
    manager.__iter__ = Mock(return_value=iter([('488nm', laser_488), ('UV', laser_uv)]))
    manager.__getitem__ = Mock(side_effect=lambda name: laser_488 if name == '488nm' else laser_uv)
    
    return manager


@pytest.fixture
def mock_widget():
    """Mock LaserWidget."""
    widget = MagicMock()
    widget.getCurrentPreset = Mock(return_value=None)
    widget.setCurrentPreset = Mock()
    widget.isLaserActive = Mock(side_effect=lambda name: False)
    widget.getValue = Mock(side_effect=lambda name: 50.0 if name == '488nm' else 0)
    widget.setValue = Mock()
    widget.setLaserActive = Mock()
    widget.setModulationFrequency = Mock()
    widget.setModulationDutyCycle = Mock()
    widget.addLaser = Mock()
    widget.addPreset = Mock()
    widget.setLaserEditable = Mock()
    
    # Mock laser modules with modulation controls
    module_488 = Mock()
    module_488.getFrequency = Mock(return_value=2000)
    module_488.getDutyCycle = Mock(return_value=50)
    widget.laserModules = {'488nm': module_488, 'UV': None}
    
    return widget


@pytest.fixture
def mock_setupinfo():
    """Mock setupInfo."""
    setupinfo = Mock()
    setupinfo.laserPresets = {'Default': {}}
    setupinfo.lasers = {
        '488nm': Mock(getAnalogChannel=Mock(return_value='ao0')),
        'UV': Mock(getAnalogChannel=Mock(return_value=None), getDigitalLine=Mock(return_value='port0/line0'))
    }
    return setupinfo


@pytest.fixture
def mock_master(mock_laser_manager):
    """Mock master controller."""
    master = Mock()
    master.lasersManager = mock_laser_manager
    return master


@pytest.fixture
def mock_commchannel():
    """Mock communication channel."""
    commchannel = Mock()
    commchannel.sharedAttrs = Mock()
    commchannel.sharedAttrs.sigAttributeSet = Mock()
    commchannel.sharedAttrs.sigAttributeSet.connect = Mock()
    commchannel.sharedAttrs.__setitem__ = Mock()
    commchannel.sigScanStarting = Mock()
    commchannel.sigScanStarting.connect = Mock()
    commchannel.sigScanBuilt = Mock()
    commchannel.sigScanBuilt.connect = Mock()
    commchannel.sigScanEnded = Mock()
    commchannel.sigScanEnded.connect = Mock()
    return commchannel


@pytest.fixture
def laser_controller(mock_setupinfo, mock_commchannel, mock_master, mock_widget):
    """Create LaserController mock with necessary attributes and methods."""
    # Create a lightweight mock that has the methods we're testing
    controller = Mock(spec=LaserController)
    controller._setupInfo = mock_setupinfo
    controller._commChannel = mock_commchannel
    controller._master = mock_master
    controller._widget = mock_widget
    controller._logger = Mock()
    
    # Bind the actual methods from LaserController to the mock
    controller.getComponentState = lambda: LaserController.getComponentState(controller)
    controller.applyComponentState = lambda state, applyMode: LaserController.applyComponentState(
        controller, state, applyMode=applyMode
    )
    controller.describeComponentState = lambda state: LaserController.describeComponentState(controller, state)
    controller.getComponentStateHazards = lambda state, applyMode, context=None: LaserController.getComponentStateHazards(
        controller, state, applyMode=applyMode, context=context
    )
    
    # Bind helper methods
    controller._savedLaserItemsInDisplayOrder = lambda lasers, state: LaserController._savedLaserItemsInDisplayOrder(
        controller, lasers, state
    )
    controller._currentLaserDisplayOrder = lambda: LaserController._currentLaserDisplayOrder(controller)
    controller._fmt = lambda value: LaserController._fmt(controller, value)
    controller._onOff = lambda value: LaserController._onOff(controller, value)
    controller._asFloat = lambda value: LaserController._asFloat(controller, value)
    controller._isMilliwattUnit = lambda units: LaserController._isMilliwattUnit(controller, units)
    
    # Mock action methods
    controller.setLaserValue = Mock()
    controller.setLaserActive = Mock()
    controller.frequencyChanged = Mock()
    controller.dutyCycleChanged = Mock()
    
    return controller


def test_get_component_state(laser_controller, mock_widget):
    """Test getComponentState returns correct structure."""
    mock_widget.isLaserActive.side_effect = lambda name: name == '488nm'
    mock_widget.getValue.side_effect = lambda name: 100.0 if name == '488nm' else 0
    mock_widget.getCurrentPreset.return_value = 'Default'
    
    state = laser_controller.getComponentState()
    
    assert isinstance(state, dict)
    assert 'lasers' in state
    assert 'laserOrder' in state
    assert 'currentPreset' in state
    assert 'modulation' in state
    
    assert state['currentPreset'] == 'Default'
    assert state['laserOrder'] == ['488nm', 'UV']
    
    # Check 488nm laser state
    assert '488nm' in state['lasers']
    laser_488_state = state['lasers']['488nm']
    assert laser_488_state['enabled'] is True
    assert laser_488_state['value'] == 100.0
    assert laser_488_state['isBinary'] is False
    assert laser_488_state['valueUnits'] == 'mW'
    
    # Check UV laser state
    assert 'UV' in state['lasers']
    laser_uv_state = state['lasers']['UV']
    assert laser_uv_state['enabled'] is False
    assert laser_uv_state['value'] is None
    assert laser_uv_state['isBinary'] is True
    
    # Check modulation
    assert '488nm' in state['modulation']
    assert state['modulation']['488nm']['frequency'] == 2000
    assert state['modulation']['488nm']['dutyCycle'] == 50


def test_apply_component_state_startup_restore_no_enable(laser_controller, mock_widget):
    """Test STARTUP_RESTORE mode does NOT enable lasers."""
    state = {
        'lasers': {
            '488nm': {'enabled': True, 'value': 75.0, 'isBinary': False, 'valueUnits': 'mW'},
            'UV': {'enabled': True, 'value': None, 'isBinary': True, 'valueUnits': ''}
        },
        'laserOrder': ['488nm', 'UV'],
        'currentPreset': None,
        'modulation': {
            '488nm': {'frequency': 3000, 'dutyCycle': 60}
        }
    }
    
    warnings = laser_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    
    # Should set power value but NOT enable
    laser_controller.setLaserValue.assert_called()
    laser_controller.setLaserActive.assert_not_called()
    
    # Should warn that enable states were not restored
    assert any('enable states not restored' in w.lower() for w in warnings)


def test_apply_component_state_setup_mode_does_enable(laser_controller, mock_widget):
    """Test SETUP_MODE_APPLY mode DOES enable lasers."""
    state = {
        'lasers': {
            '488nm': {'enabled': True, 'value': 75.0, 'isBinary': False, 'valueUnits': 'mW'},
            'UV': {'enabled': False, 'value': None, 'isBinary': True, 'valueUnits': ''}
        },
        'laserOrder': ['488nm', 'UV'],
        'currentPreset': None,
        'modulation': {}
    }
    
    warnings = laser_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    
    # Should set both power value and enable state
    laser_controller.setLaserValue.assert_called()
    assert laser_controller.setLaserActive.call_count == 2
    laser_controller.setLaserActive.assert_any_call('488nm', True)
    laser_controller.setLaserActive.assert_any_call('UV', False)
    
    # No warnings expected for valid state
    assert warnings == []


def test_apply_component_state_missing_laser_warning(laser_controller):
    """Test warning is returned for laser not in current setup."""
    state = {
        'lasers': {
            'NonExistent': {'enabled': False, 'value': 10.0, 'isBinary': False, 'valueUnits': 'mW'}
        },
        'laserOrder': ['NonExistent'],
        'currentPreset': None,
        'modulation': {}
    }
    
    warnings = laser_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    
    assert any('NonExistent' in w and 'not present' in w for w in warnings)


def test_describe_component_state(laser_controller):
    """Test describeComponentState returns human-readable summary."""
    state = {
        'lasers': {
            '488nm': {'enabled': True, 'value': 100.0, 'isBinary': False, 'valueUnits': 'mW'},
            'UV': {'enabled': False, 'value': None, 'isBinary': True, 'valueUnits': ''}
        },
        'laserOrder': ['488nm', 'UV'],
        'currentPreset': 'Default',
        'modulation': {}
    }
    
    summary = laser_controller.describeComponentState(state)
    
    assert isinstance(summary, list)
    assert len(summary) > 0
    
    # Should include preset line
    assert any('preset: Default' in line for line in summary)
    
    # Should include states header
    assert any('states:' in line for line in summary)
    
    # Should include laser states
    assert any('488nm' in line and 'ON' in line and '100' in line and 'mW' in line for line in summary)
    assert any('UV' in line and 'OFF' in line for line in summary)


def test_get_component_state_hazards_high_power(laser_controller):
    """Test getComponentStateHazards detects high-power laser in SETUP_MODE_APPLY."""
    state = {
        'lasers': {
            '488nm': {'enabled': True, 'value': 150.0, 'isBinary': False, 'valueUnits': 'mW'},
            'UV': {'enabled': True, 'value': None, 'isBinary': True, 'valueUnits': ''}
        },
        'laserOrder': ['488nm', 'UV'],
        'currentPreset': None,
        'modulation': {}
    }
    
    context = {'laserPowerThresholdMw': 50.0}
    hazards = laser_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        context=context
    )
    
    assert len(hazards) == 1
    hazard = hazards[0]
    assert hazard['kind'] == 'high_laser_power'
    assert hazard['severity'] == 'warning'
    assert '488nm' in hazard['message']
    assert '150' in hazard['message']
    assert hazard['details']['laserName'] == '488nm'
    assert hazard['details']['value'] == 150.0
    assert hazard['details']['threshold'] == 50.0


def test_get_component_state_hazards_startup_restore_none(laser_controller):
    """Test getComponentStateHazards returns empty for STARTUP_RESTORE (no enable)."""
    state = {
        'lasers': {
            '488nm': {'enabled': True, 'value': 150.0, 'isBinary': False, 'valueUnits': 'mW'}
        },
        'laserOrder': ['488nm'],
        'currentPreset': None,
        'modulation': {}
    }
    
    context = {'laserPowerThresholdMw': 50.0}
    hazards = laser_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
        context=context
    )
    
    assert hazards == []


def test_get_component_state_hazards_disabled_laser_not_flagged(laser_controller):
    """Test disabled high-power laser is not flagged as hazard."""
    state = {
        'lasers': {
            '488nm': {'enabled': False, 'value': 150.0, 'isBinary': False, 'valueUnits': 'mW'}
        },
        'laserOrder': ['488nm'],
        'currentPreset': None,
        'modulation': {}
    }
    
    context = {'laserPowerThresholdMw': 50.0}
    hazards = laser_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        context=context
    )
    
    assert hazards == []


def test_component_registration():
    """Test LaserController has correct canonical name attribute."""
    # Test that the class has the correct componentName attribute
    assert hasattr(LaserController, 'componentName')
    assert LaserController.componentName == 'Laser'
    assert LaserController.stateSchemaVersion == 1
    assert LaserController.legacyStateNames == ()


def test_setup_mode_discovery_includes_laser():
    """Test that Laser appears in setup mode component discovery."""
    from imswitch.imcontrol.controller.SetupModeController import SetupModeController
    from imswitch.imcontrol.controller.basecontrollers import StatefulComponentMixin
    
    # Create a mock Laser controller that is an instance of StatefulComponentMixin
    laser_controller = Mock(spec=StatefulComponentMixin)
    gui_layout_controller = Mock(spec=StatefulComponentMixin)
    
    # Mock registry
    with patch('imswitch.imcontrol.controller.SetupModeController.getWidgetStatePersistence') as mock_registry:
        mock_registry_instance = Mock()
        mock_registry_instance.getRegisteredControllers = Mock(return_value=['Laser', 'GuiLayout'])
        mock_registry.return_value = mock_registry_instance
        
        setup_mode_controller = SetupModeController(
            controllers={'Laser': laser_controller, 'GuiLayout': gui_layout_controller}
        )
        
        components = setup_mode_controller.getSetupModeComponents()
        
        # Laser should be in the list
        assert 'Laser' in components
        # GuiLayout should NOT be in the list
        assert 'GuiLayout' not in components


def test_round_trip_state_persistence(laser_controller, mock_widget):
    """Test round-trip: getComponentState -> applyComponentState preserves state structure."""
    # Setup initial state
    mock_widget.isLaserActive.side_effect = lambda name: name == '488nm'
    mock_widget.getValue.side_effect = lambda name: 80.0 if name == '488nm' else 0
    mock_widget.getCurrentPreset.return_value = 'TestPreset'
    
    # Capture state
    state = laser_controller.getComponentState()
    
    # Verify the captured state has correct structure and values
    assert 'lasers' in state
    assert '488nm' in state['lasers']
    assert state['lasers']['488nm']['value'] == 80.0
    assert state['lasers']['488nm']['enabled'] is True
    assert state['currentPreset'] == 'TestPreset'
    
    # Apply state in STARTUP_RESTORE mode should succeed without errors
    warnings = laser_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    
    # Enable states not restored warning should be present
    assert any('enable states not restored' in w.lower() for w in warnings)
