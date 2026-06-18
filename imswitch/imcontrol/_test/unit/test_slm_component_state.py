"""Unit tests for SLMController unified state persistence (Phase 2c).

Tests the StatefulComponentMixin implementation for SLM component state:
- Round-trip state capture and restore
- Apply-mode safety (STARTUP_RESTORE vs SETUP_MODE_APPLY)
- Human-readable state descriptions
- Hazard detection (none for SLM)
- Setup-mode discovery and registration
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call
from imswitch.imcontrol.controller.controllers.SLMController import SLMController
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


class MockSLMManager:
    """Mock SLMManager for testing."""
    
    def __init__(self):
        self.maskCombined = Mock()
        self.maskCombined.image.return_value = [[0] * 792 for _ in range(600)]
        self._general = {
            'radius': 100.0,
            'sigma': 50.0,
            'rotationAngle': 0.0,
            'tiltAngle': 0.0
        }
        self._centers = {
            'left': (200, 300),
            'right': (600, 300)
        }
        self._aber = {
            'left': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': 0.5,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            },
            'right': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': -0.3,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            }
        }
    
    def getCenters(self):
        return self._centers
    
    def setCenters(self, centers):
        # Convert dict format to tuple format if needed
        if isinstance(centers, dict):
            for mask in ['left', 'right']:
                if mask in centers and isinstance(centers[mask], dict):
                    self._centers[mask] = (centers[mask]['xcenter'], centers[mask]['ycenter'])
        else:
            self._centers = centers
    
    def setGeneral(self, general):
        self._general = general
    
    def setAberrationFactors(self, aber):
        self._aber = aber
    
    def saveState(self, state_general=None, state_pos=None, state_aber=None):
        pass
    
    def update(self, maskChange=False, tiltChange=False, aberChange=False):
        import numpy as np
        return np.zeros((600, 792), dtype=np.uint8)
    
    def moveMask(self, mask, direction, amount):
        pass
    
    def setMask(self, mask, mode):
        pass
    
    def setAberrations(self, info_dict, mask=None):
        pass


class MockParameterTree:
    """Mock parameter tree for SLM params."""
    
    def __init__(self, is_general=False):
        self.is_general = is_general
        self.p = self
        self._params = {}
        
        if is_general:
            self._params['general'] = {
                'radius': 100.0,
                'sigma': 50.0,
                'rotationAngle': 0.0,
                'tiltAngle': 0.0
            }
        else:
            self._params = {
                'left': {
                    'tilt': 0.0, 'tip': 0.0, 'defocus': 0.5,
                    'spherical': 0.0, 'verticalComa': 0.0,
                    'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                    'obliqueAstigmatism': 0.0
                },
                'right': {
                    'tilt': 0.0, 'tip': 0.0, 'defocus': -0.3,
                    'spherical': 0.0, 'verticalComa': 0.0,
                    'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                    'obliqueAstigmatism': 0.0
                }
            }
    
    def param(self, name):
        mock_param = Mock()
        if self.is_general:
            if name == 'general':
                parent = Mock()
                parent.value = lambda pname=None: (
                    lambda: self._params['general'].get(pname, 0.0)
                )
                parent.param = lambda pname: self._make_value_mock(self._params['general'].get(pname, 0.0))
                parent.setValue = lambda val, pname=name: self._params['general'].__setitem__(pname, val)
                return parent
        else:
            if name in ('left', 'right'):
                parent = Mock()
                parent.param = lambda pname: self._make_value_mock(self._params[name].get(pname, 0.0))
                parent.setValue = lambda val: None
                return parent
        return mock_param
    
    def _make_value_mock(self, value):
        mock = Mock()
        mock.value = Mock(return_value=value)
        mock.setValue = Mock()
        return mock


class MockWidget:
    """Mock SLMWidget."""
    
    def __init__(self):
        self.slmParameterTree = MockParameterTree(is_general=True)
        self.aberParameterTree = MockParameterTree(is_general=False)
        self.controlPanel = Mock()
        self.controlPanel.objlensComboBox = Mock()
        self.controlPanel.objlensComboBox.currentText.return_value = 'Oil'
        self.controlPanel.objlensComboBox.findText = Mock(return_value=0)
        self.controlPanel.objlensComboBox.setCurrentIndex = Mock()
        self.img = Mock()
        self.img.setImage = Mock()
        
    def replaceWithError(self, msg):
        pass
    
    def initSLMDisplay(self, monitorIdx):
        pass
    
    def setSLMDisplayVisible(self, enabled):
        pass
    
    def setSLMDisplayMonitor(self, monitor):
        pass
    
    def updateSLMDisplay(self, img):
        pass


@pytest.fixture
def mock_setup_info():
    """Setup info with SLM configured."""
    setup = Mock()
    setup.slm = Mock()
    setup.slm.monitorIdx = 1
    return setup


@pytest.fixture
def mock_setup_info_no_slm():
    """Setup info with NO SLM configured."""
    setup = Mock()
    setup.slm = None
    return setup


@pytest.fixture
def mock_master():
    """Mock master controller."""
    master = Mock()
    master.slmManager = MockSLMManager()
    return master


@pytest.fixture
def mock_comm_channel():
    """Mock communication channel."""
    channel = Mock()
    channel.sigSLMMaskUpdated = Mock()
    channel.sigSLMMaskUpdated.connect = Mock()
    return channel


@pytest.fixture
def mock_widget_registry():
    """Mock widget state persistence registry."""
    registry = Mock()
    registry.register = Mock()
    return registry


@pytest.fixture
def slm_controller(mock_setup_info, mock_master, mock_comm_channel, mock_widget_registry):
    """Create SLMController mock with necessary attributes and methods."""
    widget = MockWidget()
    
    # Create a lightweight mock that has the methods we're testing
    controller = Mock(spec=SLMController)
    controller._setupInfo = mock_setup_info
    controller._commChannel = mock_comm_channel
    controller._master = mock_master
    controller._widget = widget
    controller._logger = Mock()
    controller.componentName = 'SLM'
    controller.stateSchemaVersion = 1
    controller.legacyStateNames = ()
    
    # Bind the actual methods from SLMController to the mock
    controller.getComponentState = lambda: SLMController.getComponentState(controller)
    controller.applyComponentState = lambda state, applyMode: SLMController.applyComponentState(
        controller, state, applyMode=applyMode
    )
    controller.describeComponentState = lambda state: SLMController.describeComponentState(controller, state)
    controller.getComponentStateHazards = lambda state, applyMode, context=None: SLMController.getComponentStateHazards(
        controller, state, applyMode=applyMode, context=context
    )
    
    # Bind helper methods
    controller.getInfoDict = lambda generalParams=None, aberParams=None, centers=None: SLMController.getInfoDict(
        controller, generalParams=generalParams, aberParams=aberParams, centers=centers
    )
    controller.setParamTree = lambda state_general, state_aber: SLMController.setParamTree(
        controller, state_general=state_general, state_aber=state_aber
    )
    controller.updateDisplayImage = Mock()
    
    return controller


def test_slm_controller_implements_stateful_mixin(slm_controller):
    """Verify SLMController implements StatefulComponentMixin."""
    from imswitch.imcontrol.controller.basecontrollers import StatefulComponentMixin
    assert isinstance(slm_controller, StatefulComponentMixin)
    assert slm_controller.componentName == 'SLM'
    assert slm_controller.stateSchemaVersion == 1
    assert slm_controller.legacyStateNames == ()


def test_slm_controller_has_correct_class_attributes():
    """SLMController has correct StatefulComponentMixin class attributes."""
    assert SLMController.componentName == 'SLM'
    assert SLMController.stateSchemaVersion == 1
    assert SLMController.legacyStateNames == ()
    
    # Verify it implements the required methods
    assert hasattr(SLMController, 'getComponentState')
    assert hasattr(SLMController, 'applyComponentState')
    assert hasattr(SLMController, 'describeComponentState')
    assert hasattr(SLMController, 'getComponentStateHazards')


def test_slm_registration_code_path():
    """Verify that SLMController registration code exists and is in correct place."""
    import inspect
    source = inspect.getsource(SLMController.__init__)
    
    # Verify registration happens ONLY after SLM is configured (not on error path)
    assert "getWidgetStatePersistence().register('SLM', self)" in source
    
    # Verify it's after the early return for unconfigured SLM
    lines = source.split('\n')
    register_line = None
    error_return_line = None
    
    for i, line in enumerate(lines):
        if 'replaceWithError' in line and 'return' in lines[i+1] if i+1 < len(lines) else False:
            error_return_line = i
        if "getWidgetStatePersistence().register('SLM', self)" in line:
            register_line = i
    
    # Registration should come after the error path return
    if register_line is not None and error_return_line is not None:
        assert register_line > error_return_line, "Registration should be after early return for unconfigured SLM"


def test_get_component_state_captures_full_slm_config(slm_controller):
    """getComponentState captures general/position/aber params and objective."""
    state = slm_controller.getComponentState()
    
    assert isinstance(state, dict)
    
    # Check general params
    assert 'general' in state
    general = state['general']
    assert 'radius' in general
    assert 'sigma' in general
    assert 'rotationAngle' in general
    assert 'tiltAngle' in general
    
    # Check position centers
    assert 'position' in state
    position = state['position']
    assert 'left' in position
    assert 'right' in position
    assert 'xcenter' in position['left']
    assert 'ycenter' in position['left']
    
    # Check aberration coefficients
    assert 'aber' in state
    aber = state['aber']
    assert 'left' in aber
    assert 'right' in aber
    assert 'defocus' in aber['left']
    
    # Check objective
    assert 'objective' in state
    assert state['objective'] == 'Oil'


def test_get_component_state_json_serializable(slm_controller):
    """getComponentState returns JSON-serializable dict."""
    import json
    state = slm_controller.getComponentState()
    
    # Should not raise
    json_str = json.dumps(state)
    reconstructed = json.loads(json_str)
    
    assert reconstructed['general']['radius'] == state['general']['radius']


def test_apply_component_state_restores_params_both_modes(slm_controller):
    """applyComponentState restores params in BOTH STARTUP_RESTORE and SETUP_MODE_APPLY."""
    # Capture initial state
    initial_state = slm_controller.getComponentState()
    
    # Modify state
    modified_state = {
        'general': {
            'radius': 150.0,
            'sigma': 75.0,
            'rotationAngle': 45.0,
            'tiltAngle': 10.0
        },
        'position': {
            'left': {'xcenter': 250, 'ycenter': 350},
            'right': {'xcenter': 650, 'ycenter': 350}
        },
        'aber': {
            'left': {
                'tilt': 0.1, 'tip': 0.2, 'defocus': 0.8,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            },
            'right': {
                'tilt': -0.1, 'tip': -0.2, 'defocus': -0.5,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            }
        },
        'objective': 'Oil'
    }
    
    # Test STARTUP_RESTORE mode
    warnings_startup = slm_controller.applyComponentState(
        modified_state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert isinstance(warnings_startup, list)
    
    # Verify manager methods were called
    assert slm_controller._master.slmManager._general['radius'] == 150.0
    assert slm_controller._master.slmManager._centers['left'] == (250, 350)
    
    # Test SETUP_MODE_APPLY mode (should work identically for SLM)
    warnings_setup = slm_controller.applyComponentState(
        initial_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    assert isinstance(warnings_setup, list)
    
    # Should restore initial values
    assert slm_controller._master.slmManager._general['radius'] == 100.0


def test_apply_component_state_warns_on_missing_sections(slm_controller):
    """applyComponentState warns when all sections are missing."""
    empty_state = {}
    
    warnings = slm_controller.applyComponentState(
        empty_state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    
    assert len(warnings) > 0
    assert any('No SLM state to restore' in w for w in warnings)


def test_apply_component_state_warns_on_missing_objective(slm_controller):
    """applyComponentState warns when objective not found in dropdown."""
    state = slm_controller.getComponentState()
    state['objective'] = 'NonExistentObjective'
    
    # Mock findText to return -1 (not found)
    slm_controller._widget.controlPanel.objlensComboBox.findText.return_value = -1
    
    warnings = slm_controller.applyComponentState(
        state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    
    assert any('not found in dropdown' in w for w in warnings)


def test_describe_component_state_returns_human_readable_summary(slm_controller):
    """describeComponentState returns human-readable summary lines."""
    state = {
        'general': {
            'radius': 120.0,
            'sigma': 60.0,
            'rotationAngle': 30.0,
            'tiltAngle': 5.0
        },
        'position': {
            'left': {'xcenter': 200, 'ycenter': 300},
            'right': {'xcenter': 600, 'ycenter': 300}
        },
        'aber': {
            'left': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': 0.5,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            },
            'right': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': 0.0,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            }
        },
        'objective': 'Oil'
    }
    
    summary = slm_controller.describeComponentState(state)
    
    assert isinstance(summary, list)
    assert len(summary) > 0
    
    # Check for expected content
    summary_text = '\n'.join(summary)
    assert 'Oil' in summary_text
    assert 'radius' in summary_text or '120' in summary_text
    assert 'left' in summary_text
    assert 'right' in summary_text
    assert 'defocus' in summary_text


def test_describe_component_state_handles_empty_state(slm_controller):
    """describeComponentState handles empty state gracefully."""
    summary = slm_controller.describeComponentState({})
    
    assert isinstance(summary, list)
    assert len(summary) > 0
    assert any('no SLM state' in s for s in summary)


def test_describe_component_state_shows_only_nonzero_aberrations(slm_controller):
    """describeComponentState only shows non-zero aberration terms."""
    state = {
        'aber': {
            'left': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': 0.5,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            },
            'right': {
                'tilt': 0.0, 'tip': 0.0, 'defocus': 0.0,
                'spherical': 0.0, 'verticalComa': 0.0,
                'horizontalComa': 0.0, 'verticalAstigmatism': 0.0,
                'obliqueAstigmatism': 0.0
            }
        }
    }
    
    summary = slm_controller.describeComponentState(state)
    summary_text = '\n'.join(summary)
    
    # Left should show defocus
    assert 'defocus=0.5' in summary_text
    # Right should show "all zero"
    assert 'all zero' in summary_text


def test_get_component_state_hazards_returns_empty_list(slm_controller):
    """getComponentStateHazards returns empty list for both modes (no hazards)."""
    state = slm_controller.getComponentState()
    
    # STARTUP_RESTORE
    hazards_startup = slm_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert isinstance(hazards_startup, list)
    assert len(hazards_startup) == 0
    
    # SETUP_MODE_APPLY
    hazards_setup = slm_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    assert isinstance(hazards_setup, list)
    assert len(hazards_setup) == 0


def test_round_trip_state_preservation(slm_controller):
    """Full round-trip: getComponentState -> applyComponentState -> getComponentState."""
    # Get initial state
    state1 = slm_controller.getComponentState()
    initial_radius = state1['general']['radius']
    initial_left_center = state1['position']['left']
    
    # Create a modified state
    modified_state = state1.copy()
    modified_state['general'] = state1['general'].copy()
    modified_state['general']['radius'] = 200.0
    modified_state['position'] = state1['position'].copy()
    modified_state['position']['left'] = {'xcenter': 100, 'ycenter': 100}
    
    # Apply modified state
    warnings = slm_controller.applyComponentState(
        modified_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    assert len(warnings) == 0
    
    # Verify manager state changed
    assert slm_controller._master.slmManager._general['radius'] == 200.0
    assert slm_controller._master.slmManager._centers['left'] == (100, 100)
    
    # Apply original state back
    warnings = slm_controller.applyComponentState(
        state1,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    assert len(warnings) == 0
    
    # Verify manager state restored
    assert slm_controller._master.slmManager._general['radius'] == initial_radius
    assert slm_controller._master.slmManager._centers['left'] == (
        initial_left_center['xcenter'], initial_left_center['ycenter']
    )


def test_slm_appears_in_setup_mode_discovery(slm_controller):
    """SLM component appears in setup mode discovery when configured."""
    # This test verifies that a SLMController would be discovered by SetupModeController
    # by checking it has the required attributes and methods
    
    from imswitch.imcontrol.controller.basecontrollers import StatefulComponentMixin
    
    # Verify SLMController is a StatefulComponentMixin
    assert issubclass(SLMController, StatefulComponentMixin)
    
    # Verify the controller has the required attributes
    assert slm_controller.componentName == 'SLM'
    assert hasattr(slm_controller, 'getComponentState')
    assert hasattr(slm_controller, 'applyComponentState')
    assert hasattr(slm_controller, 'describeComponentState')
    assert hasattr(slm_controller, 'getComponentStateHazards')


def test_existing_save_load_params_still_works(slm_controller):
    """Existing saveParams/loadParams UI feature continues to work."""
    # This test verifies that the legacy save/load feature is not broken
    # and shares code with the new unified methods
    
    # The getInfoDict method should still work as before
    info_dict = slm_controller.getInfoDict(
        generalParams=slm_controller._widget.slmParameterTree.p,
        aberParams=slm_controller._widget.aberParameterTree.p,
        centers=slm_controller._master.slmManager.getCenters()
    )
    
    assert 'general' in info_dict
    assert 'position' in info_dict
    assert 'aber' in info_dict
    
    # The structure should be compatible with the unified state
    state = slm_controller.getComponentState()
    assert state['general'] == info_dict['general']
    assert state['position'] == info_dict['position']
    assert state['aber'] == info_dict['aber']
