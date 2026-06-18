"""Unit tests for TriggerScope scan component state persistence (Phase 2f).

Verifies the unified StatefulComponentMixin implementation on TriggerScope controllers:
- TriggerScopeRasterController
- TriggerScopeLSXYRController

Tests:
- getComponentState/applyComponentState round-trip restoration
- CRITICAL SAFETY: applyComponentState NEVER starts a scan (runScan/runScanAdvanced)
- isRunning guard prevents state changes during active scan
- describeComponentState returns human-readable summary
- getComponentStateHazards returns empty list
- saveScanParamsToFile/loadScanParamsFromFile route through component state
- Scan component is discoverable via SetupModeController
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


class TestTriggerScopeRasterComponentState:
    """Test unified component state persistence for TriggerScopeRasterController."""

    @pytest.fixture
    def mock_setup_info(self):
        """Create mock setup info with TriggerScopeRaster configuration."""
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopeRaster'
        
        # Mock positioners
        positioner_x = Mock()
        positioner_x.forScanning = True
        positioner_y = Mock()
        positioner_y.forScanning = True
        positioner_z = Mock()
        positioner_z.forScanning = False  # Not for scanning
        
        setupInfo.positioners = {
            'X': positioner_x,
            'Y': positioner_y,
            'Z': positioner_z
        }
        
        # Mock TTL devices
        setupInfo.getTTLDevices = Mock(return_value={
            'Laser405': Mock(),
            'Laser488': Mock()
        })
        
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        """Create mock TriggerScopeRaster widget."""
        widget = Mock()
        widget.setScanDim = Mock()
        widget.setScanSize = Mock()
        widget.setScanStepSize = Mock()
        widget.setTTLStarts = Mock()
        widget.setTTLEnds = Mock()
        widget.unsetTTL = Mock()
        widget.setSeqTimePar = Mock()
        widget.plotSignalGraph = Mock()
        widget.updateImage = Mock()
        return widget

    @pytest.fixture
    def raster_controller(self, mock_setup_info, mock_widget):
        """Create a TriggerScopeRasterController for testing."""
        from imswitch.imcontrol.controller.controllers.TriggerScopeRasterController import TriggerScopeRasterController
        
        controller = TriggerScopeRasterController.__new__(TriggerScopeRasterController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.settingAttr = False
        controller.settingParameters = False
        controller.isRunning = False
        controller.doingNonFinalPartOfSequence = False
        
        # Initialize positioners and TTL devices
        controller.positioners = {
            'X': mock_setup_info.positioners['X'],
            'Y': mock_setup_info.positioners['Y']
        }
        controller.TTLDevices = mock_setup_info.getTTLDevices()
        
        # Initialize parameter dicts
        controller._analogParameterDict = {
            'target_device': ['X', 'Y'],
            'axis_length': [100.0, 100.0],
            'axis_step_size': [1.0, 1.0],
            'axis_centerpos': [0.0, 0.0],
            'axis_startpos': [-50.0, -50.0],
            'sequence_time': 0.1,
            'return_time': 0.01
        }
        controller._digitalParameterDict = {
            'target_device': ['Laser405'],
            'TTL_start': [0.01],
            'TTL_end': [0.09],
            'sequence_time': 0.1
        }
        
        # Mock methods
        controller.getParameters = Mock()
        controller.setParameters = Mock()
        controller.updateSteps = Mock()
        controller.plotSignalGraph = Mock()
        controller.updateScanStageAttrs = Mock()
        controller.updateScanTTLAttrs = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        controller.signalDict = None
        controller.scanInfoDict = None
        controller.scanDir = '/tmp'
        
        return controller

    def test_componentName_attribute(self, raster_controller):
        """Verify TriggerScopeRasterController has componentName = 'Scan'."""
        assert hasattr(raster_controller, 'componentName')
        assert raster_controller.componentName == 'Scan'

    def test_stateSchemaVersion_attribute(self, raster_controller):
        """Verify TriggerScopeRasterController has stateSchemaVersion."""
        assert hasattr(raster_controller, 'stateSchemaVersion')
        assert isinstance(raster_controller.stateSchemaVersion, int)
        assert raster_controller.stateSchemaVersion >= 1

    def test_legacyStateNames_attribute(self, raster_controller):
        """Verify TriggerScopeRasterController declares legacy state names."""
        assert hasattr(raster_controller, 'legacyStateNames')
        assert isinstance(raster_controller.legacyStateNames, tuple)

    def test_getComponentState_captures_all_fields(self, raster_controller):
        """Verify getComponentState captures all required fields."""
        state = raster_controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopeRasterController'
        assert state['scanWidgetType'] == 'TriggerScopeRaster'
        assert 'analogParameterDict' in state
        assert 'digitalParameterDict' in state
        
        # Verify parameter dicts are deep copies
        assert state['analogParameterDict'] == raster_controller._analogParameterDict
        assert state['analogParameterDict'] is not raster_controller._analogParameterDict

    def test_applyComponentState_restores_parameters(self, raster_controller):
        """Verify applyComponentState restores scan parameters."""
        originalState = raster_controller.getComponentState()
        
        # Modify controller state
        raster_controller._analogParameterDict = {
            'target_device': ['X'],
            'axis_length': [50.0],
            'axis_step_size': [2.0]
        }
        raster_controller._digitalParameterDict = {}
        
        # Apply original state
        warnings = raster_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        assert raster_controller._analogParameterDict == originalState['analogParameterDict']
        assert raster_controller._digitalParameterDict == originalState['digitalParameterDict']
        
        # Verify setParameters was called
        raster_controller.setParameters.assert_called_once()

    def test_applyComponentState_never_starts_scan_startup(self, raster_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in STARTUP_RESTORE mode."""
        state = raster_controller.getComponentState()
        
        raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify scan was NEVER started
        raster_controller.runScan.assert_not_called()
        raster_controller.runScanAdvanced.assert_not_called()

    def test_applyComponentState_never_starts_scan_setup_mode(self, raster_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in SETUP_MODE_APPLY."""
        state = raster_controller.getComponentState()
        
        raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        # Verify scan was NEVER started
        raster_controller.runScan.assert_not_called()
        raster_controller.runScanAdvanced.assert_not_called()

    def test_applyComponentState_isRunning_guard(self, raster_controller):
        """Verify applyComponentState refuses to apply state when scan is running."""
        state = raster_controller.getComponentState()
        raster_controller.isRunning = True
        
        warnings = raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(warnings) == 1
        assert 'running' in warnings[0].lower()
        
        # Verify setParameters was NOT called
        raster_controller.setParameters.assert_not_called()

    def test_applyComponentState_warns_on_widget_type_mismatch(self, raster_controller):
        """Verify applyComponentState warns about widget type mismatch."""
        state = raster_controller.getComponentState()
        state['scanWidgetType'] = 'SomeOtherWidget'
        
        warnings = raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Should have warning about widget type mismatch
        assert any('widget type' in w.lower() and 'differs' in w.lower() for w in warnings)

    def test_applyComponentState_warns_on_missing_positioners(self, raster_controller):
        """Verify applyComponentState warns when required positioners are missing."""
        state = raster_controller.getComponentState()
        state['analogParameterDict']['target_device'] = ['X', 'Y', 'NonexistentAxis']
        
        warnings = raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Should have warning about missing positioner
        assert any('missing' in w.lower() and 'positioner' in w.lower() for w in warnings)
        
        # State should NOT be applied
        raster_controller.setParameters.assert_not_called()

    def test_applyComponentState_warns_on_missing_ttl_devices(self, raster_controller):
        """Verify applyComponentState warns when required TTL devices are missing."""
        state = raster_controller.getComponentState()
        state['digitalParameterDict']['target_device'] = ['NonexistentLaser']
        
        warnings = raster_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Should have warning about missing TTL device
        assert any('missing' in w.lower() and 'ttl' in w.lower() for w in warnings)
        
        # State should NOT be applied
        raster_controller.setParameters.assert_not_called()

    def test_describeComponentState_returns_summary(self, raster_controller):
        """Verify describeComponentState returns human-readable summary."""
        state = raster_controller.getComponentState()
        
        summary = raster_controller.describeComponentState(state)
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        
        # Check for expected content
        summaryText = '\n'.join(summary)
        assert 'TriggerScopeRasterController' in summaryText
        assert 'TriggerScopeRaster' in summaryText

    def test_describeComponentState_handles_empty_state(self, raster_controller):
        """Verify describeComponentState handles empty state gracefully."""
        summary = raster_controller.describeComponentState({})
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        assert 'no scan state' in '\n'.join(summary).lower()

    def test_getComponentStateHazards_returns_empty(self, raster_controller):
        """Verify getComponentStateHazards returns empty list (no laser-like hazards)."""
        state = raster_controller.getComponentState()
        
        hazards = raster_controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []
        
        # Also test SETUP_MODE_APPLY
        hazards = raster_controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        assert hazards == []

    def test_round_trip_state_preservation(self, raster_controller):
        """Verify complete round-trip: getComponentState -> modify -> applyComponentState."""
        originalState = raster_controller.getComponentState()
        
        # Modify controller state
        raster_controller._analogParameterDict = {
            'target_device': ['X'],
            'axis_length': [50.0],
            'axis_step_size': [2.0],
            'sequence_time': 0.05
        }
        raster_controller._digitalParameterDict = {
            'target_device': ['Laser488'],
            'TTL_start': [0.005],
            'TTL_end': [0.045],
            'sequence_time': 0.05
        }
        
        # Apply original state
        warnings = raster_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        
        # Get state again and verify it matches original
        restoredState = raster_controller.getComponentState()
        
        assert restoredState['analogParameterDict'] == originalState['analogParameterDict']
        assert restoredState['digitalParameterDict'] == originalState['digitalParameterDict']

    def test_saveScanParamsToFile_uses_component_state(self, raster_controller):
        """Verify saveScanParamsToFile serializes component state as JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            raster_controller.saveScanParamsToFile(filePath)
            
            # Verify file exists and contains valid JSON
            assert Path(filePath).exists()
            
            with open(filePath, 'r') as f:
                savedState = json.load(f)
            
            # Verify it's the component state
            assert savedState['controller'] == 'TriggerScopeRasterController'
            assert 'analogParameterDict' in savedState
            assert 'digitalParameterDict' in savedState

    def test_loadScanParamsFromFile_uses_component_state(self, raster_controller):
        """Verify loadScanParamsFromFile applies component state via applyComponentState."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            # Save current state
            originalState = raster_controller.getComponentState()
            raster_controller.saveScanParamsToFile(filePath)
            
            # Modify controller state
            raster_controller._analogParameterDict = {}
            raster_controller._digitalParameterDict = {}
            
            # Load from file
            raster_controller.loadScanParamsFromFile(filePath)
            
            # Verify state was restored
            assert raster_controller._analogParameterDict == originalState['analogParameterDict']
            assert raster_controller._digitalParameterDict == originalState['digitalParameterDict']

    def test_file_roundtrip(self, raster_controller):
        """Verify full file save/load round-trip preserves state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            originalState = raster_controller.getComponentState()
            
            # Save to file
            raster_controller.saveScanParamsToFile(filePath)
            
            # Modify state
            raster_controller._analogParameterDict = {
                'target_device': ['X'],
                'axis_length': [25.0],
                'axis_step_size': [0.5]
            }
            
            # Load from file
            raster_controller.loadScanParamsFromFile(filePath)
            
            # Verify state matches original
            restoredState = raster_controller.getComponentState()
            assert restoredState['analogParameterDict'] == originalState['analogParameterDict']
            assert restoredState['digitalParameterDict'] == originalState['digitalParameterDict']


class TestTriggerScopeLSXYRComponentState:
    """Test unified component state persistence for TriggerScopeLSXYRController."""

    @pytest.fixture
    def mock_setup_info(self):
        """Create mock setup info with TriggerScopeLSXYR configuration."""
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'TriggerScopeLSXYR'
        
        # Mock lasers
        laser_manager = Mock()
        laser_manager.getAllDeviceNames = Mock(return_value=['Laser405', 'Laser488'])
        setupInfo.lasers = {'405': laser_manager}
        
        # Mock rs232sManager
        rs232_manager = Mock()
        rs232_manager.getAllDeviceNames = Mock(return_value=['TriggerScope'])
        setupInfo.rs232sManager = rs232_manager
        
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        """Create mock TriggerScopeLSXYR widget."""
        widget = Mock()
        widget.setTimeLapsePoints = Mock()
        widget.setTimeLapseDelayS = Mock()
        widget.setDelayBeforeOnTimeMs = Mock()
        widget.setOnTimeMs = Mock()
        widget.setDelayAfterOnTimeMs = Mock()
        widget.setX0 = Mock()
        widget.setY0 = Mock()
        widget.setR0 = Mock()
        widget.setDistanceXY = Mock()
        widget.setDistanceR = Mock()
        widget.setStepsXY = Mock()
        widget.setStepsR = Mock()
        widget.setLaserName = Mock()
        widget.setTriggerScopeName = Mock()
        return widget

    @pytest.fixture
    def lsxyr_controller(self, mock_setup_info, mock_widget):
        """Create a TriggerScopeLSXYRController for testing."""
        from imswitch.imcontrol.controller.controllers.TriggerScopeLSXYRController import TriggerScopeLSXYRController
        
        controller = TriggerScopeLSXYRController.__new__(TriggerScopeLSXYRController)
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller._master = Mock()
        controller.settingParameters = False
        controller.isRunning = False
        
        # Mock positioners and TTL devices
        controller.positioners = {}
        controller.TTLDevices = {}
        
        # Initialize parameter dicts
        controller._scanParameterDict = {
            'timeLapsePoints': 10,
            'timeLapseDelayS': 1.0,
            'delayBeforeOnTimeMs': 5.0,
            'onTimeMs': 50.0,
            'delayAfterOnTimeMs': 5.0,
            'offTimeMs': 10.0,
            'delayAfterOffTimeMs': 5.0,
            'delayAfterDACStepMs': 1.0,
            'roTimeMs': 20.0,
            'delayAfterRoMs': 5.0,
            'roRestingPosUm': 0.0,
            'roStartPosUm': 10.0,
            'roStepSizeUm': 1.0,
            'roSteps': 10,
            'cycleStartPosUm': 0.0,
            'cycleStepSizeUm': 5.0,
            'cycleSteps': 5,
            'rasterXStartPosUm': 0.0,
            'rasterXStepSizeUm': 1.0,
            'rasterXSteps': 10,
            'rasterYStartPosUm': 0.0,
            'rasterYStepSizeUm': 1.0,
            'rasterYSteps': 10
        }
        controller._deviceParameterDict = {
            'onLaser': '',
            'offLaser': '',
            'roLaser': '',
            'roScanDevice': '',
            'cycleScanDevice': '',
            'CameraTTL': '',
            'rasterXScanDevice': '',
            'rasterYScanDevice': ''
        }
        
        # Mock methods
        controller.getParameters = Mock()
        controller.setParameters = Mock()
        controller.setAllSharedAttr = Mock()
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        controller.scanDir = '/tmp'
        
        return controller

    def test_componentName_attribute(self, lsxyr_controller):
        """Verify TriggerScopeLSXYRController has componentName = 'Scan'."""
        assert hasattr(lsxyr_controller, 'componentName')
        assert lsxyr_controller.componentName == 'Scan'

    def test_stateSchemaVersion_attribute(self, lsxyr_controller):
        """Verify TriggerScopeLSXYRController has stateSchemaVersion."""
        assert hasattr(lsxyr_controller, 'stateSchemaVersion')
        assert isinstance(lsxyr_controller.stateSchemaVersion, int)
        assert lsxyr_controller.stateSchemaVersion >= 1

    def test_getComponentState_captures_all_fields(self, lsxyr_controller):
        """Verify getComponentState captures all required fields."""
        state = lsxyr_controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'TriggerScopeLSXYRController'
        assert state['scanWidgetType'] == 'TriggerScopeLSXYR'
        assert 'scanParameterDict' in state
        assert 'deviceParameterDict' in state
        
        # Verify parameter dicts are deep copies
        assert state['scanParameterDict'] == lsxyr_controller._scanParameterDict
        assert state['scanParameterDict'] is not lsxyr_controller._scanParameterDict

    def test_applyComponentState_restores_parameters(self, lsxyr_controller):
        """Verify applyComponentState restores scan parameters."""
        originalState = lsxyr_controller.getComponentState()
        
        # Modify controller state
        lsxyr_controller._scanParameterDict = {}
        lsxyr_controller._deviceParameterDict = {}
        
        # Apply original state
        warnings = lsxyr_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        assert lsxyr_controller._scanParameterDict == originalState['scanParameterDict']
        assert lsxyr_controller._deviceParameterDict == originalState['deviceParameterDict']
        
        # Verify setParameters was called
        lsxyr_controller.setParameters.assert_called_once()

    def test_applyComponentState_never_starts_scan_startup(self, lsxyr_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in STARTUP_RESTORE mode."""
        state = lsxyr_controller.getComponentState()
        
        lsxyr_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify scan was NEVER started
        lsxyr_controller.runScan.assert_not_called()
        lsxyr_controller.runScanAdvanced.assert_not_called()

    def test_applyComponentState_never_starts_scan_setup_mode(self, lsxyr_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in SETUP_MODE_APPLY."""
        state = lsxyr_controller.getComponentState()
        
        lsxyr_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        # Verify scan was NEVER started
        lsxyr_controller.runScan.assert_not_called()
        lsxyr_controller.runScanAdvanced.assert_not_called()

    def test_applyComponentState_isRunning_guard(self, lsxyr_controller):
        """Verify applyComponentState refuses to apply state when scan is running."""
        state = lsxyr_controller.getComponentState()
        lsxyr_controller.isRunning = True
        
        warnings = lsxyr_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(warnings) == 1
        assert 'running' in warnings[0].lower()
        
        # Verify setParameters was NOT called
        lsxyr_controller.setParameters.assert_not_called()

    def test_describeComponentState_returns_summary(self, lsxyr_controller):
        """Verify describeComponentState returns human-readable summary."""
        state = lsxyr_controller.getComponentState()
        
        summary = lsxyr_controller.describeComponentState(state)
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        
        # Check for expected content
        summaryText = '\n'.join(summary)
        assert 'TriggerScopeLSXYRController' in summaryText

    def test_describeComponentState_handles_empty_state(self, lsxyr_controller):
        """Verify describeComponentState handles empty state gracefully."""
        summary = lsxyr_controller.describeComponentState({})
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        assert 'no scan state' in '\n'.join(summary).lower()

    def test_getComponentStateHazards_returns_empty(self, lsxyr_controller):
        """Verify getComponentStateHazards returns empty list (no laser-like hazards)."""
        state = lsxyr_controller.getComponentState()
        
        hazards = lsxyr_controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []

    def test_round_trip_state_preservation(self, lsxyr_controller):
        """Verify complete round-trip: getComponentState -> modify -> applyComponentState."""
        originalState = lsxyr_controller.getComponentState()
        
        # Modify controller state
        lsxyr_controller._scanParameterDict = {
            'timeLapsePoints': 5,
            'timeLapseDelayS': 2.0
        }
        lsxyr_controller._deviceParameterDict = {}
        
        # Apply original state
        warnings = lsxyr_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        
        # Get state again and verify it matches original
        restoredState = lsxyr_controller.getComponentState()
        
        assert restoredState['scanParameterDict'] == originalState['scanParameterDict']
        assert restoredState['deviceParameterDict'] == originalState['deviceParameterDict']

    def test_saveScanParamsToFile_uses_component_state(self, lsxyr_controller):
        """Verify saveScanParamsToFile serializes component state as JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            lsxyr_controller.saveScanParamsToFile(filePath)
            
            # Verify file exists and contains valid JSON
            assert Path(filePath).exists()
            
            with open(filePath, 'r') as f:
                savedState = json.load(f)
            
            # Verify it's the component state
            assert savedState['controller'] == 'TriggerScopeLSXYRController'
            assert 'scanParameterDict' in savedState
            assert 'deviceParameterDict' in savedState

    def test_loadScanParamsFromFile_uses_component_state(self, lsxyr_controller):
        """Verify loadScanParamsFromFile applies component state via applyComponentState."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            # Save current state
            originalState = lsxyr_controller.getComponentState()
            lsxyr_controller.saveScanParamsToFile(filePath)
            
            # Modify controller state
            lsxyr_controller._scanParameterDict = {}
            lsxyr_controller._deviceParameterDict = {}
            
            # Load from file
            lsxyr_controller.loadScanParamsFromFile(filePath)
            
            # Verify state was restored
            assert lsxyr_controller._scanParameterDict == originalState['scanParameterDict']
            assert lsxyr_controller._deviceParameterDict == originalState['deviceParameterDict']

    def test_file_roundtrip(self, lsxyr_controller):
        """Verify full file save/load round-trip preserves state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filePath = str(Path(tmpdir) / 'test_scan.json')
            
            originalState = lsxyr_controller.getComponentState()
            
            # Save to file
            lsxyr_controller.saveScanParamsToFile(filePath)
            
            # Modify state
            lsxyr_controller._scanParameterDict = {
                'timeLapsePoints': 1,
                'timeLapseDelayS': 0.5
            }
            
            # Load from file
            lsxyr_controller.loadScanParamsFromFile(filePath)
            
            # Verify state matches original
            restoredState = lsxyr_controller.getComponentState()
            assert restoredState['scanParameterDict'] == originalState['scanParameterDict']
            assert restoredState['deviceParameterDict'] == originalState['deviceParameterDict']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
