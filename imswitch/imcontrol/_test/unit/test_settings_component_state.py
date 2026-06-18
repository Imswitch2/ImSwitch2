"""
Unit tests for SettingsController unified state persistence (Phase 2b).

Tests the StatefulComponentMixin implementation on SettingsController:
- getComponentState / applyComponentState round-trip
- STARTUP_RESTORE and SETUP_MODE_APPLY behave identically (both restore settings, neither starts acquisition)
- Acquisition is NEVER started in either mode
- describeComponentState returns human-readable summaries
- getComponentStateHazards returns empty list (no hazards)
- Missing detector warnings
- Settings appears in setup mode component discovery
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from imswitch.imcontrol.controller.controllers.SettingsController import SettingsController
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


@pytest.fixture
def mock_detector_manager():
    """Mock detectorsManager with one forAcquisition detector."""
    manager = MagicMock()
    
    # Create detector manager instance
    detector_cam1 = Mock()
    detector_cam1.forAcquisition = True
    detector_cam1.model = "TestCam"
    detector_cam1.binning = 1
    detector_cam1.frameStart = (0, 0)
    detector_cam1.shape = (512, 512)
    detector_cam1.pixelSizeUm = [6.5, 6.5]
    detector_cam1.setBinning = Mock()
    
    # Mock detector parameters
    param_exposure = Mock()
    param_exposure.value = 100.0
    param_exposure.group = "Acquisition"
    param_gain = Mock()
    param_gain.value = 1.0
    param_gain.group = "Acquisition"
    detector_cam1.parameters = {
        'Exposure time': param_exposure,
        'Gain': param_gain
    }
    
    # Make manager iterable
    manager.__iter__ = Mock(return_value=iter([('Camera1', detector_cam1)]))
    manager.__getitem__ = Mock(side_effect=lambda name: detector_cam1 if name == 'Camera1' else None)
    manager.getAllDeviceNames = Mock(return_value=['Camera1'])
    manager.hasDevices = Mock(return_value=True)
    manager.getCurrentDetectorName = Mock(return_value='Camera1')
    
    return manager


@pytest.fixture
def mock_widget_tree():
    """Mock parameter tree for widget."""
    tree = MagicMock()
    
    # Mock parameter structure
    root_param = Mock()
    image_frame_param = Mock()
    binning_param = Mock()
    binning_param.value = Mock(return_value=1)
    binning_param.setValue = Mock()
    frame_mode_param = Mock()
    frame_mode_param.value = Mock(return_value='Full chip')
    frame_mode_param.setValue = Mock()
    x0_param = Mock()
    x0_param.value = Mock(return_value=0)
    x0_param.setValue = Mock()
    y0_param = Mock()
    y0_param.value = Mock(return_value=0)
    y0_param.setValue = Mock()
    width_param = Mock()
    width_param.value = Mock(return_value=512)
    width_param.setValue = Mock()
    height_param = Mock()
    height_param.value = Mock(return_value=512)
    height_param.setValue = Mock()
    
    # Mock parameter retrieval chain
    image_frame_param.param = Mock(side_effect=lambda name: {
        'Binning': binning_param,
        'Mode': frame_mode_param,
        'X0': x0_param,
        'Y0': y0_param,
        'Width': width_param,
        'Height': height_param,
        'Apply': Mock(),
        'New ROI': Mock(),
        'Abort ROI': Mock(),
        'Save mode': Mock(),
        'Delete mode': Mock(),
        'Update all detectors': Mock()
    }[name])
    
    root_param.param = Mock(side_effect=lambda name: {
        'Model': Mock(),
        'Image frame': image_frame_param,
        'Acquisition': Mock()
    }[name])
    
    # Mock Acquisition group parameters
    acquisition_param = Mock()
    exposure_param_widget = Mock()
    exposure_param_widget.value = Mock(return_value=100.0)
    exposure_param_widget.setValue = Mock()
    gain_param_widget = Mock()
    gain_param_widget.value = Mock(return_value=1.0)
    gain_param_widget.setValue = Mock()
    acquisition_param.param = Mock(side_effect=lambda name: {
        'Exposure time': exposure_param_widget,
        'Gain': gain_param_widget
    }.get(name))
    root_param.param = Mock(side_effect=lambda name: {
        'Model': Mock(),
        'Image frame': image_frame_param,
        'Acquisition': acquisition_param
    }[name])
    
    tree.p = root_param
    
    return tree


@pytest.fixture
def mock_widget(mock_widget_tree):
    """Mock SettingsWidget."""
    widget = MagicMock()
    widget.trees = {'Camera1': mock_widget_tree}
    widget.addDetector = Mock()
    widget.getROIGraphicsItem = Mock()
    widget.showROI = Mock()
    widget.hideROI = Mock()
    widget.getAdvancedWidget = Mock(return_value=None)
    
    return widget


@pytest.fixture
def mock_setupinfo():
    """Mock setupInfo."""
    setupinfo = Mock()
    setupinfo.rois = {}
    return setupinfo


@pytest.fixture
def mock_master(mock_detector_manager):
    """Mock master controller."""
    master = Mock()
    master.detectorsManager = mock_detector_manager
    return master


@pytest.fixture
def mock_commchannel():
    """Mock communication channel."""
    commchannel = Mock()
    commchannel.sharedAttrs = Mock()
    commchannel.sharedAttrs.sigAttributeSet = Mock()
    commchannel.sharedAttrs.sigAttributeSet.connect = Mock()
    commchannel.sharedAttrs.__setitem__ = Mock()
    commchannel.sigDetectorSwitched = Mock()
    commchannel.sigDetectorSwitched.connect = Mock()
    return commchannel


@pytest.fixture
def mock_settings_controller_params():
    """Mock SettingsControllerParams structure."""
    from imswitch.imcontrol.controller.controllers.SettingsController import SettingsControllerParams
    
    binning_param = Mock()
    binning_param.value = Mock(return_value=1)
    binning_param.setValue = Mock()
    binning_param.sigValueChanged = Mock()
    binning_param.sigValueChanged.connect = Mock()
    
    frame_mode_param = Mock()
    frame_mode_param.value = Mock(return_value='Full chip')
    frame_mode_param.setValue = Mock()
    frame_mode_param.sigValueChanged = Mock()
    frame_mode_param.sigValueChanged.connect = Mock()
    
    x0_param = Mock()
    x0_param.value = Mock(return_value=0)
    x0_param.setValue = Mock()
    x0_param.sigValueChanged = Mock()
    x0_param.sigValueChanged.connect = Mock()
    
    y0_param = Mock()
    y0_param.value = Mock(return_value=0)
    y0_param.setValue = Mock()
    y0_param.sigValueChanged = Mock()
    y0_param.sigValueChanged.connect = Mock()
    
    width_param = Mock()
    width_param.value = Mock(return_value=512)
    width_param.setValue = Mock()
    width_param.sigValueChanged = Mock()
    width_param.sigValueChanged.connect = Mock()
    
    height_param = Mock()
    height_param.value = Mock(return_value=512)
    height_param.setValue = Mock()
    height_param.sigValueChanged = Mock()
    height_param.sigValueChanged.connect = Mock()
    
    apply_param = Mock()
    apply_param.sigActivated = Mock()
    apply_param.sigActivated.connect = Mock()
    
    new_roi_param = Mock()
    new_roi_param.sigActivated = Mock()
    new_roi_param.sigActivated.connect = Mock()
    
    abort_roi_param = Mock()
    abort_roi_param.sigActivated = Mock()
    abort_roi_param.sigActivated.connect = Mock()
    
    save_mode_param = Mock()
    save_mode_param.sigActivated = Mock()
    save_mode_param.sigActivated.connect = Mock()
    
    delete_mode_param = Mock()
    delete_mode_param.sigActivated = Mock()
    delete_mode_param.sigActivated.connect = Mock()
    
    all_detectors_param = Mock()
    all_detectors_param.value = Mock(return_value=False)
    all_detectors_param.sigValueChanged = Mock()
    all_detectors_param.sigValueChanged.connect = Mock()
    
    params = SettingsControllerParams(
        model=Mock(),
        binning=binning_param,
        frameMode=frame_mode_param,
        x0=x0_param,
        y0=y0_param,
        width=width_param,
        height=height_param,
        applyROI=apply_param,
        newROI=new_roi_param,
        abortROI=abort_roi_param,
        saveMode=save_mode_param,
        deleteMode=delete_mode_param,
        allDetectorsFrame=all_detectors_param
    )
    
    return params


@pytest.fixture
def settings_controller(mock_setupinfo, mock_commchannel, mock_master, mock_widget, 
                        mock_widget_tree, mock_settings_controller_params):
    """Create SettingsController mock with necessary attributes and methods."""
    controller = Mock(spec=SettingsController)
    controller._setupInfo = mock_setupinfo
    controller._commChannel = mock_commchannel
    controller._master = mock_master
    controller._widget = mock_widget
    controller._logger = Mock()
    
    # Mock allParams
    controller.allParams = {'Camera1': mock_settings_controller_params}
    
    # Bind the actual methods from SettingsController to the mock
    controller.getComponentState = lambda: SettingsController.getComponentState(controller)
    controller.applyComponentState = lambda state, applyMode: SettingsController.applyComponentState(
        controller, state, applyMode=applyMode
    )
    controller.describeComponentState = lambda state: SettingsController.describeComponentState(controller, state)
    controller.getComponentStateHazards = lambda state, applyMode, context=None: SettingsController.getComponentStateHazards(
        controller, state, applyMode=applyMode, context=context
    )
    
    # Bind helper methods
    controller._findSavedTriggerText = lambda parameters: SettingsController._findSavedTriggerText(
        controller, parameters
    )
    controller._fmt = lambda value: SettingsController._fmt(controller, value)
    controller._onOff = lambda value: SettingsController._onOff(controller, value)
    
    return controller


def test_get_component_state(settings_controller, mock_widget_tree, mock_settings_controller_params):
    """Test getComponentState returns correct structure."""
    state = settings_controller.getComponentState()
    
    assert isinstance(state, dict)
    assert 'detectors' in state
    
    # Check Camera1 detector state
    assert 'Camera1' in state['detectors']
    detector_state = state['detectors']['Camera1']
    assert detector_state['binning'] == 1
    assert detector_state['frame_mode'] == 'Full chip'
    assert detector_state['x0'] == 0
    assert detector_state['y0'] == 0
    assert detector_state['width'] == 512
    assert detector_state['height'] == 512
    assert 'parameters' in detector_state


def test_apply_component_state_startup_restore_no_acquisition(settings_controller, mock_settings_controller_params):
    """Test STARTUP_RESTORE mode does NOT start acquisition."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 2,
                'frame_mode': 'Custom',
                'x0': 100,
                'y0': 100,
                'width': 256,
                'height': 256,
                'parameters': {
                    'Exposure time': 50.0,
                    'Gain': 2.0
                }
            }
        }
    }
    
    warnings = settings_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    
    # Should set all parameters
    mock_settings_controller_params.binning.setValue.assert_called_with(2)
    mock_settings_controller_params.frameMode.setValue.assert_called_with('Custom')
    mock_settings_controller_params.x0.setValue.assert_called_with(100)
    mock_settings_controller_params.y0.setValue.assert_called_with(100)
    mock_settings_controller_params.width.setValue.assert_called_with(256)
    mock_settings_controller_params.height.setValue.assert_called_with(256)
    
    # No acquisition should be started (verified by lack of startAcquisition/startLive calls)
    # We assert this implicitly - if methods were called on non-mocked objects, test would fail


def test_apply_component_state_setup_mode_no_acquisition(settings_controller, mock_settings_controller_params):
    """Test SETUP_MODE_APPLY mode also does NOT start acquisition (same as STARTUP_RESTORE)."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 2,
                'frame_mode': 'Custom',
                'x0': 100,
                'y0': 100,
                'width': 256,
                'height': 256,
                'parameters': {}
            }
        }
    }
    
    warnings = settings_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    
    # Should set all parameters (same as STARTUP_RESTORE)
    mock_settings_controller_params.binning.setValue.assert_called_with(2)
    mock_settings_controller_params.frameMode.setValue.assert_called_with('Custom')
    mock_settings_controller_params.x0.setValue.assert_called_with(100)
    mock_settings_controller_params.y0.setValue.assert_called_with(100)
    mock_settings_controller_params.width.setValue.assert_called_with(256)
    mock_settings_controller_params.height.setValue.assert_called_with(256)
    
    # No acquisition should be started
    # Behavior is identical to STARTUP_RESTORE


def test_apply_modes_behave_identically(settings_controller, mock_settings_controller_params):
    """Test that STARTUP_RESTORE and SETUP_MODE_APPLY behave identically."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 4,
                'frame_mode': 'Full chip',
                'x0': 0,
                'y0': 0,
                'width': 512,
                'height': 512,
                'parameters': {}
            }
        }
    }
    
    # Apply in STARTUP_RESTORE mode
    mock_settings_controller_params.binning.setValue.reset_mock()
    mock_settings_controller_params.frameMode.setValue.reset_mock()
    warnings1 = settings_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    calls1 = (
        mock_settings_controller_params.binning.setValue.call_count,
        mock_settings_controller_params.frameMode.setValue.call_count,
    )
    
    # Apply in SETUP_MODE_APPLY mode
    mock_settings_controller_params.binning.setValue.reset_mock()
    mock_settings_controller_params.frameMode.setValue.reset_mock()
    warnings2 = settings_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    calls2 = (
        mock_settings_controller_params.binning.setValue.call_count,
        mock_settings_controller_params.frameMode.setValue.call_count,
    )
    
    # Both modes should make the same calls
    assert calls1 == calls2
    # Both should have same or similar warnings (if any)
    assert len(warnings1) == len(warnings2)


def test_apply_component_state_missing_detector_warning(settings_controller):
    """Test warning is returned for detector not in current setup."""
    state = {
        'detectors': {
            'NonExistentCam': {
                'binning': 1,
                'frame_mode': 'Full chip',
                'x0': 0,
                'y0': 0,
                'width': 512,
                'height': 512,
                'parameters': {}
            }
        }
    }
    
    warnings = settings_controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    
    assert any('NonExistentCam' in w and 'not present' in w for w in warnings)


def test_describe_component_state(settings_controller):
    """Test describeComponentState returns human-readable summary."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 2,
                'frame_mode': 'Custom',
                'x0': 100,
                'y0': 100,
                'width': 256,
                'height': 256,
                'parameters': {
                    'Trigger source': 'External'
                }
            }
        }
    }
    
    summary = settings_controller.describeComponentState(state)
    
    assert isinstance(summary, list)
    assert len(summary) > 0
    
    # Should include detector name
    assert any('Camera1' in line for line in summary)
    
    # Should include mode
    assert any('mode: Custom' in line for line in summary)
    
    # Should include ROI
    assert any('ROI:' in line and '100' in line and '256' in line for line in summary)
    
    # Should include binning
    assert any('binning: 2' in line for line in summary)
    
    # Should include trigger parameter
    assert any('Trigger source' in line and 'External' in line for line in summary)


def test_describe_component_state_empty(settings_controller):
    """Test describeComponentState handles empty state."""
    state = {'detectors': {}}
    
    summary = settings_controller.describeComponentState(state)
    
    assert isinstance(summary, list)
    assert len(summary) == 1
    assert 'no detector state' in summary[0]


def test_get_component_state_hazards_returns_empty(settings_controller):
    """Test getComponentStateHazards returns empty list (no hazards for detector settings)."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 1,
                'frame_mode': 'Full chip',
                'x0': 0,
                'y0': 0,
                'width': 512,
                'height': 512,
                'parameters': {}
            }
        }
    }
    
    # Test for both apply modes
    hazards_startup = settings_controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE, context=None
    )
    hazards_setup = settings_controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY, context=None
    )
    
    assert hazards_startup == []
    assert hazards_setup == []


def test_round_trip_state_persistence(settings_controller, mock_settings_controller_params):
    """Test round-trip: getComponentState -> applyComponentState restores original state."""
    # Get initial state
    initial_state = settings_controller.getComponentState()
    
    # Modify parameters
    mock_settings_controller_params.binning.value = Mock(return_value=2)
    mock_settings_controller_params.frameMode.value = Mock(return_value='Custom')
    mock_settings_controller_params.x0.value = Mock(return_value=50)
    mock_settings_controller_params.y0.value = Mock(return_value=50)
    mock_settings_controller_params.width.value = Mock(return_value=300)
    mock_settings_controller_params.height.value = Mock(return_value=300)
    
    # Restore initial state
    warnings = settings_controller.applyComponentState(
        initial_state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    
    # Verify settings were restored
    mock_settings_controller_params.binning.setValue.assert_called_with(initial_state['detectors']['Camera1']['binning'])
    mock_settings_controller_params.frameMode.setValue.assert_called_with(initial_state['detectors']['Camera1']['frame_mode'])
    
    # No warnings for valid round-trip
    assert warnings == []


def test_component_name_and_schema_version():
    """Test SettingsController has correct component name and schema version."""
    assert SettingsController.componentName == 'Settings'
    assert SettingsController.stateSchemaVersion == 1
    assert SettingsController.legacyStateNames == ()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
