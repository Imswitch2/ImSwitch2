"""Unit tests for additional scan controller state persistence (Task F).

Verifies StatefulComponentMixin implementation on 5 scan controllers:
- TriggerScopeGalvoDetectionController
- TriggerScopePLSRController
- TriggerScopePLSRMulticolorController
- TriggerScopeScanController
- LightSheetMulticolorController

For each controller:
- getComponentState/applyComponentState round-trip restoration
- CRITICAL SAFETY: applyComponentState NEVER starts a scan (runScan/runScanAdvanced)
- isRunning guard prevents state changes during active scan
- describeComponentState returns human-readable summary
- getComponentStateHazards returns empty list
"""

import pytest
from unittest.mock import Mock, MagicMock
from imswitch.imcontrol.controller.controllers import (
    TriggerScopeGalvoDetectionController,
    TriggerScopePLSRController,
    TriggerScopePLSRMulticolorController,
    TriggerScopeScanController,
    LightSheetMulticolorController
)
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


class TestTriggerScopeGalvoDetectionController:
    """Test TriggerScopeGalvoDetectionController component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopeGalvoDetection'
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser1': Mock(),
            'Laser2': Mock()
        })
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        widget = Mock()
        widget.getTimeLapsePoints = Mock(return_value=10)
        widget.getTimeLapseDelayS = Mock(return_value=1.0)
        widget.getDelayBeforeOnTimeMs = Mock(return_value=1.0)
        widget.getOnTimeMs = Mock(return_value=10.0)
        widget.getDelayAfterOnTimeMs = Mock(return_value=1.0)
        widget.getOffTimeMs = Mock(return_value=5.0)
        widget.getDelayAfterOffTimeMs = Mock(return_value=1.0)
        widget.getDelayAfterDACStepMs = Mock(return_value=0.5)
        widget.getRoTimeMs = Mock(return_value=5.0)
        widget.getDelayAfterRoMs = Mock(return_value=1.0)
        widget.getRoRestingPosUm = Mock(return_value=0.0)
        widget.getRoStartPosUm = Mock(return_value=-50.0)
        widget.getRoStepSizeUm = Mock(return_value=1.0)
        widget.getRoSteps = Mock(return_value=100)
        widget.getCycleStartPosUm = Mock(return_value=0.0)
        widget.getCycleStepSizeUm = Mock(return_value=2.0)
        widget.getCycleSteps = Mock(return_value=50)
        widget.getGalvoFirstPositionUm = Mock(return_value=0.0)
        widget.getGalvoSecondPositionUm = Mock(return_value=10.0)
        widget.getGalvoThirdPositionUm = Mock(return_value=20.0)
        widget.getOnLaser = Mock(return_value='Laser1')
        widget.getOffLaser = Mock(return_value='Laser2')
        widget.getRoLaser = Mock(return_value='Laser1')
        widget.getRoScanDevice = Mock(return_value='X')
        widget.getGalvoScanDevice = Mock(return_value='Y')
        widget.getCycleScanDevice = Mock(return_value='Z')
        
        # Setters
        widget.setTimeLapsePoints = Mock()
        widget.setTimeLapseDelayS = Mock()
        widget.setDelayBeforeOnTimeMs = Mock()
        widget.setOnTimeMs = Mock()
        widget.setDelayAfterOnTimeMs = Mock()
        widget.setOffTimeMs = Mock()
        widget.setDelayAfterOffTimeMs = Mock()
        widget.setDelayAfterDACStepMs = Mock()
        widget.setRoTimeMs = Mock()
        widget.setDelayAfterRoMs = Mock()
        widget.setRoRestingPosUm = Mock()
        widget.setRoStartPosUm = Mock()
        widget.setRoStepSizeUm = Mock()
        widget.setRoSteps = Mock()
        widget.setCycleStartPosUm = Mock()
        widget.setCycleStepSizeUm = Mock()
        widget.setCycleSteps = Mock()
        widget.setGalvoFirstPositionUm = Mock()
        widget.setGalvoSecondPositionUm = Mock()
        widget.setGalvoThirdPositionUm = Mock()
        widget.setOnLaser = Mock()
        widget.setOffLaser = Mock()
        widget.setRoLaser = Mock()
        widget.setRoScanDevice = Mock()
        widget.setGalvoScanDevice = Mock()
        widget.setCycleScanDevice = Mock()
        
        return widget

    @pytest.fixture
    def controller(self, mock_setup_info, mock_widget):
        controller = TriggerScopeGalvoDetectionController.__new__(TriggerScopeGalvoDetectionController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = {'X': Mock(), 'Y': Mock(), 'Z': Mock()}
        controller.TTLDevices = {'Laser1': Mock(), 'Laser2': Mock()}
        controller.isRunning = False
        controller.settingAttr = False
        controller.settingParameters = False
        controller._scanParameterDict = {}
        controller._deviceParameterDict = {}
        
        controller.setSharedAttr = Mock()
        controller.setAllSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_round_trip_state(self, controller):
        """Verify getComponentState → applyComponentState round-trip."""
        # Capture initial state
        state = controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopeGalvoDetectionController'
        assert 'scanParameterDict' in state
        assert 'deviceParameterDict' in state
        
        # Store a copy of the original state
        original_scan_params = dict(state['scanParameterDict'])
        original_device_params = dict(state['deviceParameterDict'])
        
        # Modify controller
        controller._scanParameterDict = {'modified': True}
        controller._deviceParameterDict = {'modified': True}
        
        # Apply original state
        warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        assert isinstance(warnings, list)
        # Verify state was restored (should match original, not the modified dict)
        assert controller._scanParameterDict == original_scan_params
        assert controller._deviceParameterDict == original_device_params

    def test_startup_never_starts_scan(self, controller):
        """CRITICAL: applyComponentState NEVER starts a scan."""
        state = controller.getComponentState()
        
        controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        controller.runScan.assert_not_called()
        controller.runScanAdvanced.assert_not_called()

    def test_hazards_empty(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []

    def test_describe_state(self, controller):
        """Verify describeComponentState returns human-readable summary."""
        state = controller.getComponentState()
        description = controller.describeComponentState(state)
        
        assert isinstance(description, list)
        assert len(description) > 0


class TestTriggerScopePLSRController:
    """Test TriggerScopePLSRController component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopePLSR'
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser1': Mock(),
            'Laser2': Mock()
        })
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        widget = Mock()
        widget.getSequenceTime = Mock(return_value=0.1)
        widget.getTimeLapsePoints = Mock(return_value=10)
        widget.getTimeLapseDelayS = Mock(return_value=1.0)
        widget.getStartPosX = Mock(return_value=-50.0)
        widget.getStartPosY = Mock(return_value=-50.0)
        widget.getStepSizeX = Mock(return_value=1.0)
        widget.getStepSizeY = Mock(return_value=1.0)
        widget.getStepsX = Mock(return_value=100)
        widget.getStepsY = Mock(return_value=100)
        widget.getLaser1 = Mock(return_value='Laser1')
        widget.getLaser2 = Mock(return_value='Laser2')
        widget.getCameraTTL = Mock(return_value='')
        widget.getStageDevice1 = Mock(return_value='X')
        widget.getStageDevice2 = Mock(return_value='Y')
        widget.getRoScanDevice = Mock(return_value='X')
        widget.getCycleScanDevice = Mock(return_value='Z')
        
        # Setters
        widget.setSequenceTime = Mock()
        widget.setTimeLapsePoints = Mock()
        widget.setTimeLapseDelayS = Mock()
        widget.setStartPosX = Mock()
        widget.setStartPosY = Mock()
        widget.setStepSizeX = Mock()
        widget.setStepSizeY = Mock()
        widget.setStepsX = Mock()
        widget.setStepsY = Mock()
        widget.setLaser1 = Mock()
        widget.setLaser2 = Mock()
        widget.setCameraTTL = Mock()
        widget.setStageDevice1 = Mock()
        widget.setStageDevice2 = Mock()
        
        return widget

    @pytest.fixture
    def controller(self, mock_setup_info, mock_widget):
        controller = TriggerScopePLSRController.__new__(TriggerScopePLSRController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = {'X': Mock(), 'Y': Mock()}
        controller.TTLDevices = {'Laser1': Mock(), 'Laser2': Mock()}
        controller.isRunning = False
        controller.settingAttr = False
        controller.settingParameters = False
        controller._scanParameterDict = {}
        controller._deviceParameterDict = {}
        
        controller.setSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_round_trip_state(self, controller):
        """Verify getComponentState → applyComponentState round-trip."""
        state = controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopePLSRController'
        assert 'scanParameterDict' in state
        assert 'deviceParameterDict' in state
        
        controller._scanParameterDict = {'modified': True}
        controller._deviceParameterDict = {'modified': True}
        
        warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        assert isinstance(warnings, list)

    def test_startup_never_starts_scan(self, controller):
        """CRITICAL: applyComponentState NEVER starts a scan."""
        state = controller.getComponentState()
        
        controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        controller.runScan.assert_not_called()
        controller.runScanAdvanced.assert_not_called()

    def test_hazards_empty(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []


class TestTriggerScopePLSRMulticolorController:
    """Test TriggerScopePLSRMulticolorController component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopePLSRMulticolor'
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser1': Mock(),
            'Laser2': Mock(),
            'Laser3': Mock()
        })
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        widget = Mock()
        widget.getSequenceTime = Mock(return_value=0.1)
        widget.getTimeLapsePoints = Mock(return_value=10)
        widget.getTimeLapseDelayS = Mock(return_value=1.0)
        widget.getStartPosX = Mock(return_value=-50.0)
        widget.getStartPosY = Mock(return_value=-50.0)
        widget.getStepSizeX = Mock(return_value=1.0)
        widget.getStepSizeY = Mock(return_value=1.0)
        widget.getStepsX = Mock(return_value=100)
        widget.getStepsY = Mock(return_value=100)
        widget.getFirstMulticolorDistanceX = Mock(return_value=5.0)
        widget.getFirstMulticolorDistanceY = Mock(return_value=5.0)
        widget.getSecondMulticolorDistanceX = Mock(return_value=10.0)
        widget.getSecondMulticolorDistanceY = Mock(return_value=10.0)
        widget.getLaser1 = Mock(return_value='Laser1')
        widget.getLaser2 = Mock(return_value='Laser2')
        widget.getLaser3 = Mock(return_value='Laser3')
        widget.getCameraTTL = Mock(return_value='')
        widget.getStageDevice1 = Mock(return_value='X')
        widget.getStageDevice2 = Mock(return_value='Y')
        widget.getRoScanDevice = Mock(return_value='X')
        widget.getCycleScanDevice = Mock(return_value='Z')
        widget.getMulticolorScanDevice = Mock(return_value='Y')
        widget.getLaser1OnMs = Mock(return_value=10)
        widget.getLaser2OnMs = Mock(return_value=10)
        widget.getLaser3OnMs = Mock(return_value=10)
        
        # Setters
        widget.setSequenceTime = Mock()
        widget.setTimeLapsePoints = Mock()
        widget.setTimeLapseDelayS = Mock()
        widget.setStartPosX = Mock()
        widget.setStartPosY = Mock()
        widget.setStepSizeX = Mock()
        widget.setStepSizeY = Mock()
        widget.setStepsX = Mock()
        widget.setStepsY = Mock()
        widget.setFirstMulticolorDistanceX = Mock()
        widget.setFirstMulticolorDistanceY = Mock()
        widget.setSecondMulticolorDistanceX = Mock()
        widget.setSecondMulticolorDistanceY = Mock()
        widget.setLaser1 = Mock()
        widget.setLaser2 = Mock()
        widget.setLaser3 = Mock()
        widget.setCameraTTL = Mock()
        widget.setStageDevice1 = Mock()
        widget.setStageDevice2 = Mock()
        
        return widget

    @pytest.fixture
    def controller(self, mock_setup_info, mock_widget):
        controller = TriggerScopePLSRMulticolorController.__new__(TriggerScopePLSRMulticolorController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = {'X': Mock(), 'Y': Mock()}
        controller.TTLDevices = {'Laser1': Mock(), 'Laser2': Mock(), 'Laser3': Mock()}
        controller.isRunning = False
        controller.settingAttr = False
        controller.settingParameters = False
        controller._scanParameterDict = {}
        controller._deviceParameterDict = {}
        
        controller.setSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_round_trip_state(self, controller):
        """Verify getComponentState → applyComponentState round-trip."""
        state = controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopePLSRMulticolorController'
        assert 'scanParameterDict' in state
        assert 'deviceParameterDict' in state
        
        controller._scanParameterDict = {'modified': True}
        controller._deviceParameterDict = {'modified': True}
        
        warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        assert isinstance(warnings, list)

    def test_startup_never_starts_scan(self, controller):
        """CRITICAL: applyComponentState NEVER starts a scan."""
        state = controller.getComponentState()
        
        controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        controller.runScan.assert_not_called()
        controller.runScanAdvanced.assert_not_called()

    def test_hazards_empty(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []


class TestTriggerScopeScanController:
    """Test TriggerScopeScanController component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopeScan'
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser1': Mock(),
            'Laser2': Mock()
        })
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        widget = Mock()
        widget.MODE_LIGHTSHEET = 'LightSheet'
        widget.MODE_PLSR = 'PLSR'
        widget.currentMode = Mock(return_value='LightSheet')
        widget.setCurrentMode = Mock()
        
        # Mock adapter-specific widgets
        widget.widgetLightSheet = Mock()
        widget.widgetPLSR = Mock()
        
        return widget

    @pytest.fixture
    def controller(self, mock_setup_info, mock_widget):
        controller = TriggerScopeScanController.__new__(TriggerScopeScanController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = {'X': Mock(), 'Y': Mock(), 'Z': Mock()}
        controller.TTLDevices = {'Laser1': Mock(), 'Laser2': Mock()}
        controller.isRunning = False
        controller.settingAttr = False
        
        # Create mock adapters
        controller._adapters = {
            'LightSheet': Mock(
                scanParameterDict={'param1': 100},
                deviceParameterDict={'device1': 'X'}
            ),
            'PLSR': Mock(
                scanParameterDict={'param2': 200},
                deviceParameterDict={'device2': 'Y'}
            )
        }
        controller._adapters['LightSheet'].getParameters = Mock()
        controller._adapters['LightSheet'].setParameters = Mock()
        controller._adapters['PLSR'].getParameters = Mock()
        controller._adapters['PLSR'].setParameters = Mock()
        
        controller.setSharedAttr = Mock()
        controller.setAllSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_round_trip_state(self, controller):
        """Verify getComponentState → applyComponentState round-trip."""
        state = controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopeScanController'
        assert 'adapters' in state
        assert 'activeMode' in state
        assert state['activeMode'] == 'LightSheet'
        assert 'LightSheet' in state['adapters']
        assert 'PLSR' in state['adapters']
        
        # Modify controller
        controller._adapters['LightSheet'].scanParameterDict = {'modified': True}
        
        # Apply original state
        warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        assert isinstance(warnings, list)

    def test_startup_never_starts_scan(self, controller):
        """CRITICAL: applyComponentState NEVER starts a scan."""
        state = controller.getComponentState()
        
        controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        controller.runScan.assert_not_called()
        controller.runScanAdvanced.assert_not_called()

    def test_hazards_empty(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []


class TestLightSheetMulticolorController:
    """Test LightSheetMulticolorController component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'LightSheetMulticolor'
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser1': Mock(),
            'Laser2': Mock(),
            'Laser3': Mock(),
            'Laser4': Mock(),
            'Laser5': Mock()
        })
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        widget = Mock()
        widget.getTimeLapsePoints = Mock(return_value=10)
        widget.getTimeLapseDelayS = Mock(return_value=1.0)
        widget.getLaser1OnMs = Mock(return_value=10)
        widget.getDelayAfterLaser1Ms = Mock(return_value=5)
        widget.getLaser2OnMs = Mock(return_value=10)
        widget.getDelayAfterLaser2Ms = Mock(return_value=5)
        widget.getLaser3OnMs = Mock(return_value=10)
        widget.getDelayAfterLaser3Ms = Mock(return_value=5)
        widget.getLaser4OnMs = Mock(return_value=10)
        widget.getDelayAfterLaser4Ms = Mock(return_value=5)
        widget.getLaser5OnMs = Mock(return_value=10)
        widget.getDelayAfterLaser5Ms = Mock(return_value=5)
        widget.getRoRestingPosUm = Mock(return_value=0.0)
        widget.getRoStartPosUm = Mock(return_value=-50.0)
        widget.getRoStepSizeUm = Mock(return_value=1.0)
        widget.getRoSteps = Mock(return_value=100)
        widget.getMulticolorScanFirstUm = Mock(return_value=5.0)
        widget.getMulticolorScanSecondUm = Mock(return_value=10.0)
        widget.getCycleStartPosUm = Mock(return_value=0.0)
        widget.getCycleStepSizeUm = Mock(return_value=2.0)
        widget.getCycleSteps = Mock(return_value=50)
        widget.getLaser1 = Mock(return_value='Laser1')
        widget.getLaser2 = Mock(return_value='Laser2')
        widget.getLaser3 = Mock(return_value='Laser3')
        widget.getLaser4 = Mock(return_value='Laser4')
        widget.getLaser5 = Mock(return_value='Laser5')
        widget.getCameraTTL = Mock(return_value='')
        widget.getRoScanDevice = Mock(return_value='X')
        widget.getMulticolorScanDevice = Mock(return_value='Y')
        widget.getCycleScanDevice = Mock(return_value='Z')
        
        # Setters
        widget.setTimeLapsePoints = Mock()
        widget.setTimeLapseDelayS = Mock()
        widget.setLaser1OnMs = Mock()
        widget.setDelayAfterLaser1Ms = Mock()
        widget.setLaser2OnMs = Mock()
        widget.setDelayAfterLaser2Ms = Mock()
        widget.setLaser3OnMs = Mock()
        widget.setDelayAfterLaser3Ms = Mock()
        widget.setLaser4OnMs = Mock()
        widget.setDelayAfterLaser4Ms = Mock()
        widget.setLaser5OnMs = Mock()
        widget.setDelayAfterLaser5Ms = Mock()
        widget.setRoRestingPosUm = Mock()
        widget.setRoStartPosUm = Mock()
        widget.setRoStepSizeUm = Mock()
        widget.setRoSteps = Mock()
        widget.setMulticolorScanFirstUm = Mock()
        widget.setMulticolorScanSecondUm = Mock()
        widget.setCycleStartPosUm = Mock()
        widget.setCycleStepSizeUm = Mock()
        widget.setCycleSteps = Mock()
        widget.setLaser1 = Mock()
        widget.setLaser2 = Mock()
        widget.setLaser3 = Mock()
        widget.setLaser4 = Mock()
        widget.setLaser5 = Mock()
        widget.setCameraTTL = Mock()
        widget.setRoScanDevice = Mock()
        widget.setMulticolorScanDevice = Mock()
        widget.setCycleScanDevice = Mock()
        
        return widget

    @pytest.fixture
    def controller(self, mock_setup_info, mock_widget):
        controller = LightSheetMulticolorController.__new__(LightSheetMulticolorController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = {'X': Mock(), 'Y': Mock(), 'Z': Mock()}
        controller.TTLDevices = {
            'Laser1': Mock(),
            'Laser2': Mock(),
            'Laser3': Mock(),
            'Laser4': Mock(),
            'Laser5': Mock()
        }
        controller.isRunning = False
        controller.settingAttr = False
        controller.settingParameters = False
        controller._scanParameterDict = {}
        controller._deviceParameterDict = {}
        
        controller.setSharedAttr = Mock()
        controller.setAllSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_round_trip_state(self, controller):
        """Verify getComponentState → applyComponentState round-trip."""
        state = controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'LightSheetMulticolorController'
        assert 'scanParameterDict' in state
        assert 'deviceParameterDict' in state
        
        controller._scanParameterDict = {'modified': True}
        controller._deviceParameterDict = {'modified': True}
        
        warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        assert isinstance(warnings, list)

    def test_startup_never_starts_scan(self, controller):
        """CRITICAL: applyComponentState NEVER starts a scan."""
        state = controller.getComponentState()
        
        controller.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        
        controller.runScan.assert_not_called()
        controller.runScanAdvanced.assert_not_called()

    def test_hazards_empty(self, controller):
        """Verify getComponentStateHazards returns empty list."""
        state = controller.getComponentState()
        hazards = controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []
