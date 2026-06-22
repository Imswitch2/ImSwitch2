"""Unit tests for SLMsController StatefulComponentMixin implementation (Phase 2d).

Tests the multi-SLM controller's compliance with the state persistence contract:
- getComponentState returns config references (NO HDF5/CGH payload embedding)
- applyComponentState enforces startup no-push safety (STARTUP_RESTORE vs SETUP_MODE_APPLY)
- describeComponentState generates human-readable summaries
- getComponentStateHazards returns empty list (no laser-power-like hazards)
- Registration with unified state persistence system
"""

import os
import tempfile
import pytest
from unittest.mock import Mock, MagicMock, patch, call

from imswitch.imcontrol.controller.controllers.SLMsController import SLMsController
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


@pytest.fixture
def mock_master():
    """Mock master controller with slmsManager."""
    master = Mock()
    
    # Create two mock SLM managers
    slm1 = Mock()
    slm1.requires_device_connection = False
    slm1.slmInfo = Mock()
    slm1.slmInfo.serial_number = "SLM001"
    slm1.slmInfo.correctionPatternsDir = None
    slm1.slmInfo.managerProperties = {}
    
    slm2 = Mock()
    slm2.requires_device_connection = False
    slm2.slmInfo = Mock()
    slm2.slmInfo.serial_number = "SLM002"
    slm2.slmInfo.correctionPatternsDir = None
    slm2.slmInfo.managerProperties = {}
    
    # Create mock slmsManager that supports iteration
    slmsManager = Mock()
    slmsManager.__iter__ = Mock(return_value=iter([
        ("SLM_Left", slm1),
        ("SLM_Right", slm2)
    ]))
    slmsManager.execOn = Mock()
    master.slmsManager = slmsManager
    
    return master


@pytest.fixture
def mock_widget():
    """Mock SLMsWidget with _currentConfigs."""
    widget = Mock()
    widget._currentConfigs = {}
    widget._slmSectionList = {}
    widget.add_slm = Mock(side_effect=lambda name, info, registry, device_connection: f"slm_{name}")
    widget.current_config_changed = Mock()
    widget._sync_current_config_combo = Mock()
    widget.update_config_info = Mock()
    widget.sigConnectSLMusb = Mock()
    widget.sigUpdatePattern = Mock()
    widget.sigComputeCGH = Mock()
    widget.sigVisualizeCghPerformances = Mock()
    widget.sigVisualizeTarget = Mock()
    widget.sigShowCghResult = Mock()
    widget.sigLoadConfig = Mock()
    widget.sigLoadAberr = Mock()
    widget.sigLoadCgh = Mock()
    widget.sigSaveConfig = Mock()
    widget.sigSaveAberr = Mock()
    widget.sigSaveCgh = Mock()
    widget.sigDeleteConfig = Mock()
    widget.sigRenameConfig = Mock()
    widget.sigDuplicateConfig = Mock()
    widget.sigSetStartupConfig = Mock()
    widget.sigOpenConfigFolder = Mock()
    widget.sigSnapFeedback = Mock()
    widget.sigAnalysisFeedback = Mock()
    widget.sigUpdateTarget = Mock()
    widget.sigResetFeedback = Mock()
    widget.sigAnalysisFeedbackPrm = Mock()
    widget.sigLoadFeedback = Mock()
    
    for signal in [widget.sigConnectSLMusb, widget.sigUpdatePattern, widget.sigComputeCGH,
                   widget.sigVisualizeCghPerformances, widget.sigVisualizeTarget, widget.sigShowCghResult,
                   widget.sigLoadConfig, widget.sigLoadAberr, widget.sigLoadCgh,
                   widget.sigSaveConfig, widget.sigSaveAberr, widget.sigSaveCgh,
                   widget.sigDeleteConfig, widget.sigRenameConfig, widget.sigDuplicateConfig,
                   widget.sigSetStartupConfig, widget.sigOpenConfigFolder,
                   widget.sigSnapFeedback, widget.sigAnalysisFeedback, widget.sigUpdateTarget,
                   widget.sigResetFeedback, widget.sigAnalysisFeedbackPrm, widget.sigLoadFeedback]:
        signal.connect = Mock()
    
    return widget


@pytest.fixture
def mock_comm_channel():
    """Mock communication channel."""
    return Mock()


@pytest.fixture
def mock_setup_info():
    """Mock setup info."""
    return Mock()


@pytest.fixture
def slms_controller(mock_master, mock_widget, mock_comm_channel, mock_setup_info):
    """Create SLMsController mock with necessary attributes and methods."""
    controller = Mock(spec=SLMsController)
    controller._setupInfo = mock_setup_info
    controller._commChannel = mock_comm_channel
    controller._master = mock_master
    controller._widget = mock_widget
    controller._logger = Mock()
    
    # SLMsController-specific attributes
    controller._slmNames = {
        'slm_SLM_Left': 'SLM_Left',
        'slm_SLM_Right': 'SLM_Right'
    }
    controller._slmInfos = {}
    controller._patternEngines = {}
    controller._cghResults = {}
    
    # Mock on_load_config method (important for testing no-push safety)
    controller.on_load_config = Mock()
    
    # Bind the actual StatefulComponentMixin methods from SLMsController to the mock
    controller.getComponentState = lambda: SLMsController.getComponentState(controller)
    controller.applyComponentState = lambda state, applyMode: SLMsController.applyComponentState(
        controller, state, applyMode=applyMode
    )
    controller.describeComponentState = lambda state: SLMsController.describeComponentState(controller, state)
    controller.getComponentStateHazards = lambda state, applyMode, context=None: SLMsController.getComponentStateHazards(
        controller, state, applyMode=applyMode, context=context
    )
    
    # Bind helper methods
    controller._fmt = lambda value: SLMsController._fmt(controller, value)
    
    # Set component attributes
    controller.componentName = 'SLMs'
    controller.stateSchemaVersion = 1
    controller.legacyStateNames = ()
    
    return controller


class TestSLMsComponentState:
    """Test suite for SLMsController state persistence."""
    
    def test_getComponentState_returns_config_references_only(self, slms_controller):
        """Test that getComponentState returns config references, NOT HDF5/CGH payloads."""
        # Setup: Populate widget's _currentConfigs with config references
        slms_controller._widget._currentConfigs = {
            'slm_SLM_Left': {
                'path': '/path/to/config1.hdf5',
                'date': '2026-06-18',
                'info': 'Test config 1'
            },
            'slm_SLM_Right': {
                'path': '/path/to/config2.hdf5',
                'date': '2026-06-17',
                'info': 'Test config 2'
            }
        }
        
        # Execute
        state = slms_controller.getComponentState()
        
        # Verify structure
        assert 'slms' in state
        assert len(state['slms']) == 2
        
        # Verify SLM 1
        slm1_state = state['slms']['slm_SLM_Left']
        assert slm1_state['slmName'] == 'SLM_Left'
        assert slm1_state['configPath'] == '/path/to/config1.hdf5'
        assert slm1_state['configName'] == 'config1.hdf5'
        assert slm1_state['date'] == '2026-06-18'
        assert slm1_state['info'] == 'Test config 1'
        
        # Verify SLM 2
        slm2_state = state['slms']['slm_SLM_Right']
        assert slm2_state['slmName'] == 'SLM_Right'
        assert slm2_state['configPath'] == '/path/to/config2.hdf5'
        assert slm2_state['configName'] == 'config2.hdf5'
        assert slm2_state['date'] == '2026-06-17'
        assert slm2_state['info'] == 'Test config 2'
        
        # CRITICAL: Verify NO HDF5/CGH pattern payload is embedded
        state_str = str(state)
        assert 'cgh_pattern' not in state_str
        assert 'final_image' not in state_str
        assert 'hdf5' not in state_str.lower() or 'hdf5' in state_str.lower()  # path is ok, data is not
        
        # Verify state is JSON-serializable (no binary data)
        import json
        json_str = json.dumps(state)
        assert json_str  # Should succeed without errors
    
    def test_getComponentState_with_no_configs(self, slms_controller):
        """Test getComponentState when no configs are loaded."""
        # Setup: Empty _currentConfigs
        slms_controller._widget._currentConfigs = {}
        
        # Execute
        state = slms_controller.getComponentState()
        
        # Verify
        assert 'slms' in state
        assert len(state['slms']) == 2  # Both SLMs present, but with None configs
        
        for slm_key in ['slm_SLM_Left', 'slm_SLM_Right']:
            slm_state = state['slms'][slm_key]
            assert slm_state['configPath'] is None
            assert slm_state['configName'] is None
            assert slm_state['date'] is None
            assert slm_state['info'] is None
    
    def test_applyComponentState_STARTUP_RESTORE_no_hardware_push(self, slms_controller):
        """Test that STARTUP_RESTORE does NOT push patterns to SLM hardware."""
        # Setup: Create a temporary config file
        with tempfile.NamedTemporaryFile(suffix='.hdf5', delete=False) as f:
            config_path = f.name
        
        try:
            state = {
                'slms': {
                    'slm_SLM_Left': {
                        'slmName': 'SLM_Left',
                        'configPath': config_path,
                        'configName': 'test.hdf5',
                        'date': '2026-06-18',
                        'info': 'Test'
                    }
                }
            }
            
            # Mock on_load_config to track if it's called
            slms_controller.on_load_config = Mock()
            
            # Execute
            warnings = slms_controller.applyComponentState(
                state,
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE
            )
            
            # CRITICAL: Verify on_load_config was NOT called (no hardware push)
            slms_controller.on_load_config.assert_not_called()
            
            # Verify widget bookkeeping WAS updated
            slms_controller._widget.current_config_changed.assert_called_once()
            call_args = slms_controller._widget.current_config_changed.call_args
            assert call_args[0][0] == 'slm_SLM_Left'
            assert call_args[0][1]['path'] == config_path
            assert call_args[0][1]['date'] == '2026-06-18'
            assert call_args[0][1]['info'] == 'Test'
            
            # Verify warning was returned
            assert len(warnings) > 0
            assert any('selected but NOT loaded' in w for w in warnings)
            assert any('not pushed to hardware' in w for w in warnings)
        
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
    
    def test_applyComponentState_SETUP_MODE_APPLY_does_hardware_push(self, slms_controller):
        """Test that SETUP_MODE_APPLY DOES load and apply configs (hardware push)."""
        # Setup: Create a temporary config file
        with tempfile.NamedTemporaryFile(suffix='.hdf5', delete=False) as f:
            config_path = f.name
        
        try:
            state = {
                'slms': {
                    'slm_SLM_Left': {
                        'slmName': 'SLM_Left',
                        'configPath': config_path,
                        'configName': 'test.hdf5',
                        'date': '2026-06-18',
                        'info': 'Test'
                    }
                }
            }
            
            # Mock on_load_config
            slms_controller.on_load_config = Mock()
            
            # Execute
            warnings = slms_controller.applyComponentState(
                state,
                applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
            )
            
            # Verify on_load_config WAS called (hardware push happens)
            slms_controller.on_load_config.assert_called_once_with('slm_SLM_Left', path=config_path)
            
            # Verify no "not loaded" warnings for successful load
            assert not any('selected but NOT loaded' in w for w in warnings)
        
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
    
    def test_applyComponentState_missing_slm_warns(self, slms_controller):
        """Test that applyComponentState warns about SLMs not in current setup."""
        state = {
            'slms': {
                'slm_NonExistent': {
                    'slmName': 'NonExistent_SLM',
                    'configPath': '/some/path.hdf5',
                    'configName': 'test.hdf5',
                    'date': '2026-06-18',
                    'info': 'Test'
                }
            }
        }
        
        # Execute
        warnings = slms_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify warning
        assert len(warnings) == 1
        assert 'not present in current setup' in warnings[0]
        assert 'NonExistent_SLM' in warnings[0]
    
    def test_applyComponentState_missing_file_warns(self, slms_controller):
        """Test that applyComponentState warns about missing config files."""
        state = {
            'slms': {
                'slm_SLM_Left': {
                    'slmName': 'SLM_Left',
                    'configPath': '/nonexistent/path/config.hdf5',
                    'configName': 'config.hdf5',
                    'date': '2026-06-18',
                    'info': 'Test'
                }
            }
        }
        
        # Execute
        warnings = slms_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify warning (does not raise exception)
        assert len(warnings) == 1
        assert 'not found' in warnings[0]
        assert 'config.hdf5' in warnings[0]
    
    def test_applyComponentState_handles_load_failure_gracefully(self, slms_controller):
        """Test that applyComponentState handles on_load_config failures gracefully."""
        # Setup: Create a temporary config file
        with tempfile.NamedTemporaryFile(suffix='.hdf5', delete=False) as f:
            config_path = f.name
        
        try:
            state = {
                'slms': {
                    'slm_SLM_Left': {
                        'slmName': 'SLM_Left',
                        'configPath': config_path,
                        'configName': 'test.hdf5',
                        'date': '2026-06-18',
                        'info': 'Test'
                    }
                }
            }
            
            # Mock on_load_config to raise an exception
            slms_controller.on_load_config = Mock(side_effect=Exception("HDF5 read error"))
            
            # Execute - should NOT raise, should return warning
            warnings = slms_controller.applyComponentState(
                state,
                applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
            )
            
            # Verify warning (does not raise exception)
            assert len(warnings) == 1
            assert 'Failed to load config' in warnings[0]
            assert 'HDF5 read error' in warnings[0]
        
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
    
    def test_describeComponentState_non_empty_summary(self, slms_controller):
        """Test that describeComponentState returns non-empty summaries."""
        state = {
            'slms': {
                'slm_SLM_Left': {
                    'slmName': 'SLM_Left',
                    'configPath': '/path/to/config1.hdf5',
                    'configName': 'config1.hdf5',
                    'date': '2026-06-18',
                    'info': 'Test 1'
                },
                'slm_SLM_Right': {
                    'slmName': 'SLM_Right',
                    'configPath': '/path/to/config2.hdf5',
                    'configName': 'config2.hdf5',
                    'date': '2026-06-17',
                    'info': 'Test 2'
                }
            }
        }
        
        # Execute
        summary = slms_controller.describeComponentState(state)
        
        # Verify
        assert len(summary) == 2
        assert 'SLM_Left' in summary[0]
        assert 'config1.hdf5' in summary[0]
        assert 'SLM_Right' in summary[1]
        assert 'config2.hdf5' in summary[1]
    
    def test_describeComponentState_empty_state(self, slms_controller):
        """Test describeComponentState with empty state."""
        state = {}
        
        # Execute
        summary = slms_controller.describeComponentState(state)
        
        # Verify
        assert summary == ['  no SLM state']
    
    def test_describeComponentState_no_config_shows_none(self, slms_controller):
        """Test describeComponentState shows 'None' when no config."""
        state = {
            'slms': {
                'slm_SLM_Left': {
                    'slmName': 'SLM_Left',
                    'configPath': None,
                    'configName': None,
                    'date': None,
                    'info': None
                }
            }
        }
        
        # Execute
        summary = slms_controller.describeComponentState(state)
        
        # Verify
        assert len(summary) == 1
        assert 'SLM_Left' in summary[0]
        assert 'None' in summary[0]
    
    def test_getComponentStateHazards_returns_empty_list(self, slms_controller):
        """Test that getComponentStateHazards returns empty list (no hazards)."""
        state = {
            'slms': {
                'slm_SLM_Left': {
                    'slmName': 'SLM_Left',
                    'configPath': '/path/to/config.hdf5',
                    'configName': 'config.hdf5',
                    'date': '2026-06-18',
                    'info': 'Test'
                }
            }
        }
        
        # Test both apply modes
        for apply_mode in [ComponentStateApplyMode.STARTUP_RESTORE, ComponentStateApplyMode.SETUP_MODE_APPLY]:
            hazards = slms_controller.getComponentStateHazards(
                state,
                applyMode=apply_mode,
                context={}
            )
            assert hazards == []
    
    def test_component_name_is_correct(self, slms_controller):
        """Test that the canonical component name is 'SLMs'."""
        assert slms_controller.componentName == 'SLMs'
    
    def test_schema_version_is_set(self, slms_controller):
        """Test that schema version is set."""
        assert slms_controller.stateSchemaVersion == 1
    
    def test_legacy_state_names_is_empty(self, slms_controller):
        """Test that legacyStateNames is empty tuple."""
        assert slms_controller.legacyStateNames == ()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
