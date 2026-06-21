"""Unit tests for scan component state persistence (Phase 2e).

Verifies the unified StatefulComponentMixin implementation on SuperScanController:
- getComponentState/applyComponentState round-trip restoration
- CRITICAL SAFETY: applyComponentState NEVER starts a scan (runScan/runScanAdvanced)
- isRunning guard prevents state changes during active scan
- describeComponentState returns human-readable summary
- getComponentStateHazards returns empty list
- Scan component is discoverable via SetupModeController
"""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch, call
from imswitch.imcontrol.controller.basecontrollers import SuperScanController, ComponentStateApplyMode


# Create a concrete test implementation of SuperScanController
class ConcreteScanController(SuperScanController):
    """Concrete scan controller for testing."""
    
    def __init__(self, *args, **kwargs):
        # Skip normal init to avoid complex dependencies
        pass
    
    def getParameters(self):
        pass
    
    def setParameters(self):
        pass
    
    def runScanAdvanced(self, sigScanStartingEmitted=False):
        pass
    
    def scanDone(self):
        pass
    
    def emitScanSignal(self):
        pass
    
    def updatePixels(self, *args):
        pass
    
    def saveScanParamsToFile(self, *args):
        pass
    
    def loadScanParamsFromFile(self, *args):
        pass


class TestScanComponentState:
    """Test unified scan component state persistence."""

    @pytest.fixture
    def mock_setup_info(self):
        """Create mock setup info with scan configuration."""
        setupInfo = Mock()
        setupInfo.scan = Mock()
        setupInfo.scan.scanWidgetType = 'ScanWidgetAdvanced'
        return setupInfo

    @pytest.fixture
    def mock_widget(self):
        """Create mock scan widget with all required methods."""
        widget = Mock()
        widget.repeatEnabled = Mock(return_value=True)
        widget.setRepeatEnabled = Mock()
        widget.isScanMode = Mock(return_value=True)
        widget.isContLaserMode = Mock(return_value=False)
        widget.setScanMode = Mock()
        widget.setContLaserMode = Mock()
        return widget

    @pytest.fixture
    def mock_positioners(self):
        """Create mock positioners dictionary."""
        return {
            'X': Mock(),
            'Y': Mock(),
            'Z': Mock()
        }

    @pytest.fixture
    def mock_ttl_devices(self):
        """Create mock TTL devices dictionary."""
        return {
            'Laser1': Mock(),
            'Laser2': Mock()
        }

    @pytest.fixture
    def scan_controller(self, mock_setup_info, mock_widget, mock_positioners, mock_ttl_devices):
        """Create a ConcreteScanController for testing."""
        controller = ConcreteScanController()
        controller._setupInfo = mock_setup_info
        controller._widget = mock_widget
        controller._logger = Mock()
        controller._commChannel = Mock()
        controller.positioners = mock_positioners
        controller.TTLDevices = mock_ttl_devices
        controller.isRunning = False
        controller.settingAttr = False
        controller.settingParameters = False
        
        # Initialize parameter dicts
        controller._analogParameterDict = {
            'target_device': ['X', 'Y'],
            'axis_length': [100, 100],
            'axis_step_size': [1.0, 1.0],
            'axis_centerpos': [0.0, 0.0],
            'axis_startpos': [-50.0, -50.0],
            'scan_dim_target_device': ['X', 'Y'],
            'sequence_time': 0.1
        }
        controller._digitalParameterDict = {
            'target_device': ['Laser1'],
            'TTL_start': [0.01],
            'TTL_end': [0.09],
            'Nx': 100,
            'Ny': 100,
            'advanced_mode': False
        }
        controller._positionersScan = ['X', 'Y']
        
        # Mock methods that would normally be inherited/implemented
        controller.getParameters = Mock()
        controller.setParameters = Mock()
        controller.updateScanStageAttrs = Mock()
        controller.updateScanTTLAttrs = Mock()
        controller.signalDict = None
        controller.scanInfoDict = None
        controller.runScan = Mock()
        controller.runScanAdvanced = Mock()
        
        return controller

    def test_getComponentState_captures_all_fields(self, scan_controller):
        """Verify getComponentState captures all required fields."""
        state = scan_controller.getComponentState()
        
        assert isinstance(state, dict)
        assert state['controller'] == 'ConcreteScanController'
        assert state['scanWidgetType'] == 'ScanWidgetAdvanced'
        assert 'analogParameterDict' in state
        assert 'digitalParameterDict' in state
        assert 'positionersScan' in state
        assert 'mode' in state
        
        # Verify mode dict
        mode = state['mode']
        assert mode['repeatEnabled'] is True
        assert mode['scanMode'] is True
        assert mode['contLaserMode'] is False
        
        # Verify parameter dicts are deep copies
        assert state['analogParameterDict'] == scan_controller._analogParameterDict
        assert state['analogParameterDict'] is not scan_controller._analogParameterDict
        
    def test_applyComponentState_restores_parameters(self, scan_controller):
        """Verify applyComponentState restores scan parameters."""
        # Get original state
        originalState = scan_controller.getComponentState()
        
        # Modify controller state
        scan_controller._analogParameterDict = {}
        scan_controller._digitalParameterDict = {}
        scan_controller._positionersScan = []
        
        # Apply original state
        warnings = scan_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        assert scan_controller._analogParameterDict == originalState['analogParameterDict']
        assert scan_controller._digitalParameterDict == originalState['digitalParameterDict']
        assert scan_controller._positionersScan == originalState['positionersScan']
        
        # Verify setParameters was called
        scan_controller.setParameters.assert_called_once()
        
    def test_applyComponentState_never_starts_scan_startup(self, scan_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in STARTUP_RESTORE mode."""
        state = scan_controller.getComponentState()
        
        # Mock runScan and runScanAdvanced
        scan_controller.runScan = Mock()
        scan_controller.runScanAdvanced = Mock()
        
        # Apply state
        scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify scan was NEVER started
        scan_controller.runScan.assert_not_called()
        scan_controller.runScanAdvanced.assert_not_called()
        
    def test_applyComponentState_never_starts_scan_setup_mode(self, scan_controller):
        """CRITICAL SAFETY: applyComponentState NEVER calls runScan in SETUP_MODE_APPLY."""
        state = scan_controller.getComponentState()
        
        # Mock runScan and runScanAdvanced
        scan_controller.runScan = Mock()
        scan_controller.runScanAdvanced = Mock()
        
        # Apply state
        scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        # Verify scan was NEVER started
        scan_controller.runScan.assert_not_called()
        scan_controller.runScanAdvanced.assert_not_called()

    def test_return_to_center_after_scan_uses_positioner_metadata(self, scan_controller):
        """Only positioners marked in setup metadata are reset after a scan."""
        scan_controller._setupInfo.positioners = {
            'X': SimpleNamespace(
                managerProperties={
                    'returnToCenterAfterScan': True,
                    'returnToCenterAfterScanAxis': 'Z',
                }
            ),
            'Y': SimpleNamespace(managerProperties={}),
        }
        scan_controller._analogParameterDict = {
            'target_device': ['X', 'Y'],
            'axis_centerpos': [12.5, 99.0],
        }
        xManager = Mock()
        yManager = Mock()
        scan_controller._master = Mock()
        scan_controller._master.positionersManager = {'X': xManager, 'Y': yManager}

        scan_controller._resetReturnToCenterPositionersAfterScan()

        xManager.setPosition.assert_called_once_with(12.5, 'Z')
        yManager.setPosition.assert_not_called()

    def test_applyComponentState_isRunning_guard(self, scan_controller):
        """Verify applyComponentState returns warning when scan is running."""
        scan_controller.isRunning = True
        state = scan_controller.getComponentState()
        
        warnings = scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(warnings) == 1
        assert 'currently running' in warnings[0].lower()
        
        # Verify setParameters was NOT called
        scan_controller.setParameters.assert_not_called()

    def test_applyComponentState_handles_missing_positioners(self, scan_controller):
        """Verify applyComponentState warns about missing positioners."""
        state = scan_controller.getComponentState()
        state['analogParameterDict']['target_device'] = ['X', 'Y', 'MissingAxis']
        
        warnings = scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(warnings) > 0
        assert any('missing' in w.lower() and 'positioner' in w.lower() for w in warnings)
        
        # Verify state was NOT applied
        scan_controller.setParameters.assert_not_called()

    def test_applyComponentState_handles_missing_ttl_devices(self, scan_controller):
        """Verify applyComponentState warns about missing TTL devices."""
        state = scan_controller.getComponentState()
        state['digitalParameterDict']['target_device'] = ['Laser1', 'MissingLaser']
        
        warnings = scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(warnings) > 0
        assert any('missing' in w.lower() and 'ttl' in w.lower() for w in warnings)
        
        # Verify state was NOT applied
        scan_controller.setParameters.assert_not_called()

    def test_applyComponentState_widget_type_mismatch_warning(self, scan_controller):
        """Verify applyComponentState warns about widget type mismatch."""
        state = scan_controller.getComponentState()
        state['scanWidgetType'] = 'ScanWidgetBase'
        
        warnings = scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Should have warning about widget type mismatch
        assert any('widget type' in w.lower() and 'differs' in w.lower() for w in warnings)

    def test_applyComponentState_restores_mode_flags(self, scan_controller):
        """Verify applyComponentState restores mode flags (scanMode, contLaserMode, repeatEnabled)."""
        state = scan_controller.getComponentState()
        state['mode'] = {
            'repeatEnabled': False,
            'scanMode': False,
            'contLaserMode': True
        }
        
        warnings = scan_controller.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # Verify widget methods were called
        scan_controller._widget.setContLaserMode.assert_called_once()
        scan_controller._widget.setRepeatEnabled.assert_called_once_with(False)

    def test_describeComponentState_returns_summary(self, scan_controller):
        """Verify describeComponentState returns human-readable summary."""
        state = scan_controller.getComponentState()
        
        summary = scan_controller.describeComponentState(state)
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        
        # Check for expected content
        summaryText = '\n'.join(summary)
        assert 'controller:' in summaryText.lower() or 'SuperScanController' in summaryText
        assert 'widget type:' in summaryText.lower() or 'ScanWidgetAdvanced' in summaryText

    def test_describeComponentState_handles_empty_state(self, scan_controller):
        """Verify describeComponentState handles empty state gracefully."""
        summary = scan_controller.describeComponentState({})
        
        assert isinstance(summary, list)
        assert len(summary) > 0
        assert 'no scan state' in '\n'.join(summary).lower()

    def test_getComponentStateHazards_returns_empty(self, scan_controller):
        """Verify getComponentStateHazards returns empty list (no laser-like hazards)."""
        state = scan_controller.getComponentState()
        
        hazards = scan_controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert hazards == []
        
        # Also test SETUP_MODE_APPLY
        hazards = scan_controller.getComponentStateHazards(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        assert hazards == []

    def test_round_trip_state_preservation(self, scan_controller):
        """Verify complete round-trip: getComponentState -> modify -> applyComponentState."""
        # Get original state
        originalState = scan_controller.getComponentState()
        
        # Create a new controller with different state
        scan_controller._analogParameterDict = {
            'target_device': ['Z'],
            'axis_length': [50],
            'axis_step_size': [0.5],
            'sequence_time': 0.05
        }
        scan_controller._digitalParameterDict = {
            'target_device': ['Laser2'],
            'TTL_start': [0.005],
            'TTL_end': [0.045]
        }
        scan_controller._positionersScan = ['Z']
        
        # Apply original state
        warnings = scan_controller.applyComponentState(
            originalState,
            applyMode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        
        # Get state again and verify it matches original
        restoredState = scan_controller.getComponentState()
        
        assert restoredState['analogParameterDict'] == originalState['analogParameterDict']
        assert restoredState['digitalParameterDict'] == originalState['digitalParameterDict']
        assert restoredState['positionersScan'] == originalState['positionersScan']
        assert restoredState['mode'] == originalState['mode']

    def test_componentName_attribute(self, scan_controller):
        """Verify SuperScanController has componentName = 'Scan'."""
        assert hasattr(SuperScanController, 'componentName')
        assert SuperScanController.componentName == 'Scan'

    def test_stateSchemaVersion_attribute(self, scan_controller):
        """Verify SuperScanController has stateSchemaVersion."""
        assert hasattr(SuperScanController, 'stateSchemaVersion')
        assert isinstance(SuperScanController.stateSchemaVersion, int)
        assert SuperScanController.stateSchemaVersion >= 1

    def test_legacyStateNames_attribute(self, scan_controller):
        """Verify SuperScanController declares legacy state names."""
        assert hasattr(SuperScanController, 'legacyStateNames')
        legacyNames = SuperScanController.legacyStateNames
        
        assert 'ScanController' in legacyNames
        assert 'ScanControllerAdvanced' in legacyNames
        assert 'ScanControllerMoNaLISA' in legacyNames
        assert 'ScanControllerPointScan' in legacyNames


class TestScanDiscovery:
    """Test that Scan component is discoverable."""

    def test_scan_component_name_attribute(self):
        """Verify SuperScanController has componentName = 'Scan'."""
        assert hasattr(SuperScanController, 'componentName')
        assert SuperScanController.componentName == 'Scan'

    def test_scan_state_schema_version(self):
        """Verify SuperScanController has stateSchemaVersion."""
        assert hasattr(SuperScanController, 'stateSchemaVersion')
        assert isinstance(SuperScanController.stateSchemaVersion, int)
        assert SuperScanController.stateSchemaVersion >= 1

    def test_scan_legacy_state_names(self):
        """Verify SuperScanController declares legacy state names."""
        assert hasattr(SuperScanController, 'legacyStateNames')
        legacyNames = SuperScanController.legacyStateNames
        
        assert 'ScanController' in legacyNames
        assert 'ScanControllerAdvanced' in legacyNames
        assert 'ScanControllerMoNaLISA' in legacyNames
        assert 'ScanControllerPointScan' in legacyNames


class TestScanFormatters:
    """Test the lifted scan state formatter helpers."""

    @pytest.fixture
    def scan_controller(self):
        """Create minimal controller for testing formatters."""
        controller = ConcreteScanController()
        return controller

    def test_fmt_basic_types(self, scan_controller):
        """Test _fmt handles basic types correctly."""
        assert scan_controller._fmt(None) == "None"
        assert scan_controller._fmt(True) == "ON"
        assert scan_controller._fmt(False) == "OFF"
        assert scan_controller._fmt(3.14159) == "3.142"
        assert scan_controller._fmt("test") == "test"
        assert scan_controller._fmt([1, 2, 3]) == "[1, 2, 3]"

    def test_fmtMilliseconds(self, scan_controller):
        """Test _fmtMilliseconds converts seconds to ms."""
        result = scan_controller._fmtMilliseconds(0.1)
        assert "100" in result
        assert "ms" in result

    def test_scanListValue(self, scan_controller):
        """Test _scanListValue extracts list values correctly."""
        assert scan_controller._scanListValue([1, 2, 3], 0) == 1
        assert scan_controller._scanListValue([1, 2, 3], 2) == 3
        assert scan_controller._scanListValue([1, 2, 3], 5, "default") == "default"
        assert scan_controller._scanListValue("single", 0) == "single"
        assert scan_controller._scanListValue("single", 5) == "single"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
