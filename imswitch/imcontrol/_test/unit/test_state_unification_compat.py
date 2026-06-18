"""
Phase 1 State Unification Compatibility Tests

Verifies that the unified component state persistence registry (Phase 1)
preserves all existing functionality:
- Legacy getWidgetState/setWidgetState controllers still work
- Legacy SetupModeMixin controllers still work
- Old file formats load via alias resolution
- STARTUP_RESTORE vs SETUP_MODE_APPLY safety policy is enforced
- GuiLayout and other special cases still work

These tests ensure no controller breaks during Phase 1 deployment, before
any controller has migrated to the new StatefulComponentMixin interface.
"""

import json
import os
import tempfile
import shutil
from pathlib import Path

import pytest

from imswitch.imcontrol.model import WidgetStatePersistence, getWidgetStatePersistence
from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    SetupModeMixin,
    StatefulComponentMixin,
)


class FakeLegacyWidgetStateController:
    """Fake controller implementing legacy getWidgetState/setWidgetState interface."""
    
    def __init__(self):
        self.state_data = {'value': 42, 'enabled': False}
        self.last_set_state = None
    
    def getWidgetState(self):
        return dict(self.state_data)
    
    def setWidgetState(self, state):
        self.last_set_state = state
        self.state_data = dict(state)


class FakeLegacySetupModeController(SetupModeMixin):
    """Fake controller implementing legacy SetupModeMixin interface."""
    
    def __init__(self):
        self.state_data = {'power': 100, 'active': False}
        self.last_applied_state = None
        self.apply_count = 0
        self.activation_calls = []  # Track activation calls for safety testing
    
    def getSetupModeState(self):
        return dict(self.state_data)
    
    def applySetupModeState(self, state):
        self.last_applied_state = state
        self.apply_count += 1
        self.state_data = dict(state)
        
        # Simulate activation if 'active' is True
        if state.get('active'):
            self.activation_calls.append('activate')
        
        return []  # No warnings


class FakeNewUnifiedController(StatefulComponentMixin):
    """Fake controller implementing new StatefulComponentMixin interface."""
    
    stateSchemaVersion = 1
    componentName = 'TestUnified'
    legacyStateNames = ('TestLegacy',)
    
    def __init__(self):
        self.state_data = {'param': 'test', 'hardware_active': False}
        self.last_applied_state = None
        self.last_apply_mode = None
        self.activation_calls = []
    
    def getComponentState(self):
        return dict(self.state_data)
    
    def applyComponentState(self, state, *, applyMode):
        self.last_applied_state = state
        self.last_apply_mode = applyMode
        self.state_data = dict(state)
        
        # Enforce apply-mode safety: only activate in SETUP_MODE_APPLY
        if state.get('hardware_active'):
            if applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
                self.activation_calls.append('activate')
            # In STARTUP_RESTORE, we restore the parameter but do NOT activate
        
        return []
    
    def describeComponentState(self, state):
        return [f"TestUnified: param={state.get('param')}, active={state.get('hardware_active')}"]
    
    def getComponentStateHazards(self, state, *, applyMode, context=None):
        hazards = []
        if state.get('hardware_active') and applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
            hazards.append({
                'severity': 'warning',
                'kind': 'test_hardware_activation',
                'message': 'Hardware will be activated',
            })
        return hazards


class FakeDualInterfaceController(SetupModeMixin):
    """
    Fake controller implementing BOTH legacy widget interface AND setup-mode interface.
    
    Mirrors real Scan controllers (SuperScanController subclasses) that implement:
    - getWidgetState/setWidgetState (widget-shaped payload)
    - getSetupModeState/applySetupModeState via SetupModeMixin (setup-mode-shaped payload)
    
    Phase 1 regression test: the unified registry must route widget-persistence consumers
    to the widget interface and setup-mode consumers to the setup-mode interface.
    """
    
    def __init__(self):
        # Widget state (distinct keys from setup-mode state)
        self.widget_state_data = {
            'version': 1,
            'scan_mode': 'xy',
            'repeat': False,
            'scan_dims': [256, 256],
            'analogParameterDict': {'laser': 100},
            'digitalParameterDict': {'trigger': True}
        }
        self.last_widget_set_state = None
        self.widget_set_count = 0
        
        # Setup-mode state (distinct keys from widget state)
        self.setup_mode_state_data = {
            'controller': 'ScanController',
            'scanWidgetType': 'xy_scan',
            'analogParameterDict': {'laser': 100},
            'digitalParameterDict': {'trigger': True},
            'positionersScan': {},
            'mode': {
                'repeatEnabled': False,
                'scanMode': 'xy',
                'contLaserMode': False
            }
        }
        self.last_setup_mode_applied_state = None
        self.setup_mode_apply_count = 0
    
    # Widget interface (should be used by widget persistence consumer)
    def getWidgetState(self):
        return dict(self.widget_state_data)
    
    def setWidgetState(self, state):
        self.last_widget_set_state = state
        self.widget_set_count += 1
        self.widget_state_data = dict(state)
    
    # Setup-mode interface (should be used by setup-mode consumer)
    def getSetupModeState(self):
        return dict(self.setup_mode_state_data)
    
    def applySetupModeState(self, state):
        self.last_setup_mode_applied_state = state
        self.setup_mode_apply_count += 1
        self.setup_mode_state_data = dict(state)
        return []  # No warnings


@pytest.fixture
def temp_state_dir(monkeypatch, tmp_path):
    """Provide a temporary state directory for testing."""
    # Monkey-patch dirtools.UserFileDirs.Root to use temp directory
    import imswitch.imcommon.model.dirtools as dirtools
    monkeypatch.setattr(dirtools.UserFileDirs, 'Root', str(tmp_path))
    
    # Registry will create imcontrol_widget_states subdirectory
    state_dir = tmp_path / 'imcontrol_widget_states'
    
    yield state_dir


@pytest.fixture(autouse=False)
def clean_registry(temp_state_dir):
    """Provide a clean registry instance for each test."""
    # Reset global singleton before test
    import imswitch.imcontrol.model.WidgetStatePersistence as wsp_module
    wsp_module._persistence_instance = None
    
    # Create new registry (will use the patched dirtools.UserFileDirs.Root)
    registry = getWidgetStatePersistence()
    
    # Double-check it's clean
    assert len(registry._registry) == 0, "Registry should start empty"
    assert len(registry._registrationKeys) == 0, "Registration keys should start empty"
    
    yield registry
    
    # Clean up after test
    registry._registry.clear()
    registry._registrationKeys.clear()
    wsp_module._persistence_instance = None


class TestLegacyWidgetStateCompatibility:
    """Test that legacy getWidgetState/setWidgetState controllers still work."""
    
    def test_legacy_widget_state_register_and_snapshot(self, clean_registry, temp_state_dir):
        """Legacy controller registers and snapshots correctly."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('TestWidget', controller)
        
        state = clean_registry.snapshotComponent('TestWidget')
        assert state is not None
        assert state == {'value': 42, 'enabled': False}
    
    def test_legacy_widget_state_apply(self, clean_registry, temp_state_dir):
        """Legacy controller applies state correctly."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('TestWidget', controller)
        
        new_state = {'value': 99, 'enabled': True}
        warnings = clean_registry.applyComponentState('TestWidget', new_state)
        
        assert warnings == []
        assert controller.state_data == new_state
    
    def test_legacy_widget_state_save_load_file(self, clean_registry, temp_state_dir):
        """Legacy controller saves to and loads from per-controller file."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('TestWidget', controller)
        
        # Save
        success = clean_registry.saveWidgetState('TestWidget', 'default')
        assert success
        
        # Verify file exists
        file_path = temp_state_dir / 'TestWidget' / 'default.json'
        assert file_path.exists()
        
        # Modify controller state
        controller.state_data = {'value': 999, 'enabled': True}
        
        # Load
        loaded_state = clean_registry.loadWidgetState('TestWidget', 'default', apply_immediately=True)
        assert loaded_state is not None
        assert controller.state_data == {'value': 42, 'enabled': False}


class TestLegacySetupModeCompatibility:
    """Test that legacy SetupModeMixin controllers still work."""
    
    def test_setup_mode_mixin_register_and_snapshot(self, clean_registry, temp_state_dir):
        """SetupModeMixin controller registers and snapshots correctly."""
        controller = FakeLegacySetupModeController()
        clean_registry.register('TestSetupMode', controller)
        
        state = clean_registry.snapshotComponent('TestSetupMode')
        assert state is not None
        assert state == {'power': 100, 'active': False}
    
    def test_setup_mode_mixin_apply(self, clean_registry, temp_state_dir):
        """SetupModeMixin controller applies state correctly."""
        controller = FakeLegacySetupModeController()
        clean_registry.register('TestSetupMode', controller)
        
        new_state = {'power': 50, 'active': True}
        warnings = clean_registry.applyComponentState(
            'TestSetupMode',
            new_state,
            apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        assert warnings == []
        assert controller.state_data == new_state
        assert controller.apply_count == 1


class TestAliasResolution:
    """Test that legacy component names resolve to canonical names."""
    
    def test_legacy_alias_registers_to_canonical(self, clean_registry, temp_state_dir):
        """Legacy registration key resolves to canonical name."""
        controller = FakeLegacyWidgetStateController()
        
        # Register with legacy key
        clean_registry.register('LaserController', controller)
        
        # Should be accessible by canonical name
        assert 'Laser' in clean_registry.getRegisteredControllers()
        
        # Should snapshot via canonical name
        state = clean_registry.snapshotComponent('Laser')
        assert state is not None
    
    def test_scan_controller_aliases(self, clean_registry, temp_state_dir):
        """All Scan controller aliases resolve to canonical 'Scan'."""
        aliases = ['ScanController', 'ScanControllerAdvanced', 
                   'ScanControllerMoNaLISA', 'ScanControllerPointScan']
        
        for alias in aliases:
            controller = FakeLegacyWidgetStateController()
            controller.state_data = {'alias': alias}
            clean_registry.register(alias, controller)
            
            # All should resolve to 'Scan'
            assert 'Scan' in clean_registry.getRegisteredControllers()
            
            # Can snapshot via canonical name
            state = clean_registry.snapshotComponent('Scan')
            assert state == {'alias': alias}
            
            # Clean up for next iteration
            clean_registry.unregister(alias)
    
    def test_old_bundle_file_loads_via_aliases(self, clean_registry, temp_state_dir):
        """Old exported bundle with legacy keys loads correctly."""
        controller1 = FakeLegacyWidgetStateController()
        controller2 = FakeLegacySetupModeController()
        
        clean_registry.register('LaserController', controller1)
        clean_registry.register('ScanControllerAdvanced', controller2)
        
        # Create a bundle file with legacy keys
        temp_state_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = temp_state_dir / 'test_bundle.json'
        bundle = {
            'LaserController': {
                '_metadata': {'controller_name': 'LaserController', 'schema_version': 1},
                'state': {'value': 123, 'enabled': True},
            },
            'ScanControllerAdvanced': {
                '_metadata': {'controller_name': 'ScanControllerAdvanced', 'schema_version': 1},
                'state': {'power': 75, 'active': False},
            },
        }
        with open(bundle_path, 'w') as f:
            json.dump(bundle, f)
        
        # Load bundle
        count = clean_registry.load_from_file(str(bundle_path))
        assert count == 2
        
        # Verify states applied via alias resolution
        assert controller1.state_data == {'value': 123, 'enabled': True}
        assert controller2.state_data == {'power': 75, 'active': False}


class TestApplyModeSafetyEnforcement:
    """Test that STARTUP_RESTORE vs SETUP_MODE_APPLY safety policy is enforced."""
    
    def test_startup_restore_does_not_activate_hardware(self, clean_registry, temp_state_dir):
        """STARTUP_RESTORE mode does not trigger hardware activation."""
        controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        # State with hardware_active=True
        state_with_activation = {'param': 'test', 'hardware_active': True}
        
        # Apply in STARTUP_RESTORE mode
        warnings = clean_registry.applyComponentState(
            'TestUnified',
            state_with_activation,
            apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        # State is restored, but activation should NOT have been called
        assert controller.state_data == state_with_activation
        assert controller.activation_calls == []  # No activation in STARTUP_RESTORE
    
    def test_setup_mode_apply_activates_hardware(self, clean_registry, temp_state_dir):
        """SETUP_MODE_APPLY mode allows hardware activation."""
        controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        # State with hardware_active=True
        state_with_activation = {'param': 'test', 'hardware_active': True}
        
        # Apply in SETUP_MODE_APPLY mode
        warnings = clean_registry.applyComponentState(
            'TestUnified',
            state_with_activation,
            apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        assert warnings == []
        # State is restored AND activation should have been called
        assert controller.state_data == state_with_activation
        assert 'activate' in controller.activation_calls  # Activation in SETUP_MODE_APPLY
    
    def test_default_apply_mode_is_startup_restore(self, clean_registry, temp_state_dir):
        """When apply_mode is None, defaults to STARTUP_RESTORE."""
        controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'test', 'hardware_active': False}
        
        # Apply without specifying mode
        warnings = clean_registry.applyComponentState('TestUnified', state, apply_mode=None)
        
        assert warnings == []
        assert controller.last_apply_mode == ComponentStateApplyMode.STARTUP_RESTORE


class TestPhase3DelegationHooks:
    """Test Phase 3a delegation hooks (summary, hazards) work correctly."""
    
    def test_describe_component_state_new_interface(self, clean_registry, temp_state_dir):
        """describeComponentState delegates to new interface correctly."""
        controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'test_value', 'hardware_active': True}
        summary = clean_registry.describeComponentState('TestUnified', state)
        
        assert summary == ["TestUnified: param=test_value, active=True"]
    
    def test_describe_component_state_legacy_fallback(self, clean_registry, temp_state_dir):
        """describeComponentState falls back for legacy controllers."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('TestWidget', controller)
        
        state = {'value': 42, 'enabled': False}
        summary = clean_registry.describeComponentState('TestWidget', state)
        
        # Should get a generic fallback summary
        assert len(summary) > 0
        assert 'TestWidget' in summary[0] or '2' in summary[0]  # "2 state keys" or similar
    
    def test_get_component_state_hazards_new_interface(self, clean_registry, temp_state_dir):
        """getComponentStateHazards delegates to new interface correctly."""
        controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'test', 'hardware_active': True}
        hazards = clean_registry.getComponentStateHazards(
            'TestUnified',
            state,
            apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY,
            context=None
        )
        
        assert len(hazards) == 1
        assert hazards[0]['kind'] == 'test_hardware_activation'
        assert hazards[0]['severity'] == 'warning'
    
    def test_get_component_state_hazards_legacy_fallback(self, clean_registry, temp_state_dir):
        """getComponentStateHazards returns empty list for legacy controllers."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('TestWidget', controller)
        
        state = {'value': 42, 'enabled': False}
        hazards = clean_registry.getComponentStateHazards(
            'TestWidget',
            state,
            apply_mode=ComponentStateApplyMode.STARTUP_RESTORE,
            context=None
        )
        
        assert hazards == []  # Legacy controllers have no hazard detection


class TestBackwardCompatibilityScenarios:
    """End-to-end scenarios testing backward compatibility."""
    
    def test_old_widget_state_file_loads(self, clean_registry, temp_state_dir):
        """Old per-controller state file (with legacy key) loads correctly."""
        controller = FakeLegacyWidgetStateController()
        clean_registry.register('LaserController', controller)
        
        # Create an old-style state file under legacy key
        legacy_dir = temp_state_dir / 'LaserController'
        legacy_dir.mkdir(parents=True, exist_ok=True)
        old_file = legacy_dir / 'default.json'
        old_state = {
            '_metadata': {'controller_name': 'LaserController', 'schema_version': 1},
            'state': {'value': 555, 'enabled': True},
        }
        with open(old_file, 'w') as f:
            json.dump(old_state, f)
        
        # Load via legacy key
        loaded = clean_registry.loadWidgetState('LaserController', 'default', apply_immediately=True)
        
        assert loaded is not None
        assert controller.state_data == {'value': 555, 'enabled': True}
    
    def test_save_all_and_load_all_preserves_states(self, clean_registry, temp_state_dir):
        """saveAllWidgetStates and loadAllWidgetStates round-trip correctly."""
        controller1 = FakeLegacyWidgetStateController()
        controller2 = FakeLegacySetupModeController()
        
        controller1.state_data = {'value': 111, 'enabled': True}
        controller2.state_data = {'power': 222, 'active': False}
        
        clean_registry.register('LaserController', controller1)
        clean_registry.register('ScanController', controller2)
        
        # Save all
        save_count = clean_registry.saveAllWidgetStates('test_state')
        assert save_count == 2
        
        # Modify states
        controller1.state_data = {'value': 999, 'enabled': False}
        controller2.state_data = {'power': 999, 'active': True}
        
        # Load all
        load_count = clean_registry.loadAllWidgetStates('test_state')
        assert load_count == 2
        
        # Verify original states restored
        assert controller1.state_data == {'value': 111, 'enabled': True}
        assert controller2.state_data == {'power': 222, 'active': False}
    
    def test_gui_layout_special_case(self, clean_registry, temp_state_dir):
        """GuiLayout registers and round-trips correctly (no alias, STARTUP_RESTORE-only)."""
        controller = FakeLegacyWidgetStateController()
        controller.state_data = {'layout': 'docked', 'sizes': [100, 200]}
        
        # GuiLayout should register exactly as 'GuiLayout' (no alias)
        clean_registry.register('GuiLayout', controller)
        
        # Should be accessible by exact name
        assert 'GuiLayout' in clean_registry.getRegisteredControllers()
        
        # Save and load
        success = clean_registry.saveWidgetState('GuiLayout', 'default')
        assert success
        
        controller.state_data = {'layout': 'floating', 'sizes': [50, 50]}
        
        loaded = clean_registry.loadWidgetState('GuiLayout', 'default', apply_immediately=True)
        assert loaded is not None
        assert controller.state_data == {'layout': 'docked', 'sizes': [100, 200]}


class TestDualInterfaceRouting:
    """
    Test consumer-aware routing for dual-interface controllers (Phase 1 regression fix).
    
    Controllers implementing BOTH getWidgetState/setWidgetState AND setup-mode interface
    (via SetupModeMixin bridge) must be routed correctly:
    - Widget persistence consumer -> widget interface
    - Setup-mode consumer -> setup-mode interface
    """
    
    def test_widget_persistence_uses_widget_interface(self, clean_registry, temp_state_dir):
        """Widget persistence (saveWidgetState/loadWidgetState) uses widget interface, not setup-mode."""
        controller = FakeDualInterfaceController()
        clean_registry.register('Scan', controller)
        
        # Save via widget persistence API
        success = clean_registry.saveWidgetState('Scan', 'default')
        assert success is True
        
        # Check on-disk payload has widget-shaped keys, not setup-mode keys
        state_path = temp_state_dir / 'Scan' / 'default.json'
        assert state_path.exists()
        with open(state_path, 'r') as f:
            saved = json.load(f)
        
        state_dict = saved['state']
        # Widget keys should be present
        assert 'version' in state_dict
        assert 'scan_mode' in state_dict
        assert 'repeat' in state_dict
        assert 'scan_dims' in state_dict
        # Setup-mode keys should NOT be present
        assert 'controller' not in state_dict
        assert 'scanWidgetType' not in state_dict
        assert 'mode' not in state_dict
        
        # Load via widget persistence API
        controller.widget_state_data = {'version': 99, 'scan_mode': 'z', 'repeat': True, 'scan_dims': [128, 128]}
        loaded = clean_registry.loadWidgetState('Scan', 'default', apply_immediately=True)
        assert loaded is not None
        
        # Assert setWidgetState was called, NOT applySetupModeState
        assert controller.widget_set_count == 1
        assert controller.setup_mode_apply_count == 0
        assert controller.last_widget_set_state is not None
        assert controller.last_setup_mode_applied_state is None
        
        # State should be restored via widget interface
        assert controller.widget_state_data['version'] == 1
        assert controller.widget_state_data['scan_mode'] == 'xy'
    
    def test_widget_persistence_all_uses_widget_interface(self, clean_registry, temp_state_dir):
        """saveAllWidgetStates/loadAllWidgetStates uses widget interface for dual controllers."""
        controller = FakeDualInterfaceController()
        clean_registry.register('Scan', controller)
        
        # Save all widget states
        count = clean_registry.saveAllWidgetStates('default')
        assert count == 1
        
        # Check on-disk payload has widget-shaped keys
        state_path = temp_state_dir / 'Scan' / 'default.json'
        with open(state_path, 'r') as f:
            saved = json.load(f)
        assert 'scan_mode' in saved['state']
        assert 'controller' not in saved['state']
        
        # Load all widget states
        controller.widget_state_data = {'version': 99, 'scan_mode': 'z'}
        count = clean_registry.loadAllWidgetStates('default')
        assert count == 1
        
        # Assert widget interface was used
        assert controller.widget_set_count == 1
        assert controller.setup_mode_apply_count == 0
    
    def test_widget_persistence_file_export_uses_widget_interface(self, clean_registry, temp_state_dir):
        """save_to_file/load_from_file uses widget interface for dual controllers."""
        controller = FakeDualInterfaceController()
        clean_registry.register('Scan', controller)
        
        # Ensure parent directory exists
        temp_state_dir.mkdir(parents=True, exist_ok=True)
        export_path = temp_state_dir / 'export.json'
        
        # Export to file
        clean_registry.save_to_file(str(export_path))
        
        # Check file has widget-shaped keys
        with open(export_path, 'r') as f:
            bundle = json.load(f)
        assert 'Scan' in bundle
        assert 'scan_mode' in bundle['Scan']['state']
        assert 'controller' not in bundle['Scan']['state']
        
        # Import from file
        controller.widget_state_data = {'version': 99, 'scan_mode': 'z'}
        count = clean_registry.load_from_file(str(export_path))
        assert count == 1
        
        # Assert widget interface was used
        assert controller.widget_set_count == 1
        assert controller.setup_mode_apply_count == 0
    
    def test_setup_mode_uses_setup_mode_interface(self, clean_registry, temp_state_dir):
        """SetupModeController uses setup-mode interface for dual controllers."""
        from imswitch.imcontrol.controller.SetupModeController import SetupModeController
        
        controller = FakeDualInterfaceController()
        clean_registry.register('Scan', controller)
        
        # Create SetupModeController
        controllers = {'Scan': controller}
        setup_mode_ctrl = SetupModeController(controllers)
        
        # Snapshot via setup-mode API
        snapshot = setup_mode_ctrl.snapshotSetupModeState(componentNames=['Scan'])
        
        # Check setup-mode-shaped keys are present
        scan_state = snapshot['state']['Scan']
        assert 'controller' in scan_state
        assert 'scanWidgetType' in scan_state
        assert 'mode' in scan_state
        # Widget keys should NOT be present
        assert 'version' not in scan_state
        assert 'scan_dims' not in scan_state
        
        # Save setup mode
        result = setup_mode_ctrl.saveSetupMode('test_mode', componentNames=['Scan'])
        assert result['mode']['name'] == 'test_mode'
        assert 'Scan' in result['mode']['state']
        
        # Modify state
        controller.setup_mode_state_data = {
            'controller': 'Different',
            'scanWidgetType': 'different',
            'mode': {'scanMode': 'different'}
        }
        
        # Load setup mode
        warnings = setup_mode_ctrl.loadSetupMode('test_mode', componentNames=['Scan'])
        assert len(warnings) == 0
        
        # Assert setup-mode interface was used, NOT widget interface
        assert controller.setup_mode_apply_count == 1
        assert controller.widget_set_count == 0
        assert controller.last_setup_mode_applied_state is not None
        assert controller.last_widget_set_state is None
        
        # State should be restored via setup-mode interface
        assert controller.setup_mode_state_data['controller'] == 'ScanController'
        assert controller.setup_mode_state_data['scanWidgetType'] == 'xy_scan'
    
    def test_old_widget_file_applies_via_widget_interface(self, clean_registry, temp_state_dir):
        """Old widget-format file for dual controller applies via setWidgetState."""
        controller = FakeDualInterfaceController()
        clean_registry.register('Scan', controller)
        
        # Create an old widget-format file (pre-Phase 1)
        old_state_path = temp_state_dir / 'Scan' / 'old_format.json'
        old_state_path.parent.mkdir(parents=True, exist_ok=True)
        old_state = {
            '_metadata': {
                'controller_name': 'ScanController',
                'canonical_name': 'Scan',
                'schema_version': 1
            },
            'state': {
                'version': 2,
                'scan_mode': 'xyz',
                'repeat': True,
                'scan_dims': [512, 512, 10],
                'analogParameterDict': {'laser': 200},
                'digitalParameterDict': {'trigger': False}
            }
        }
        with open(old_state_path, 'w') as f:
            json.dump(old_state, f)
        
        # Load old file
        loaded = clean_registry.loadWidgetState('Scan', 'old_format', apply_immediately=True)
        assert loaded is not None
        
        # Assert widget interface was used (widget keys consumed, no setup-mode mismatch)
        assert controller.widget_set_count == 1
        assert controller.setup_mode_apply_count == 0
        assert controller.widget_state_data['version'] == 2
        assert controller.widget_state_data['scan_mode'] == 'xyz'
        assert controller.widget_state_data['repeat'] is True
    
    def test_genuinely_unified_controller_not_affected(self, clean_registry, temp_state_dir):
        """Genuinely migrated controller (direct getComponentState) still uses unified interface."""
        unified_controller = FakeNewUnifiedController()
        clean_registry.register('TestUnified', unified_controller)
        
        # Widget persistence should use unified interface (no widget interface available)
        success = clean_registry.saveWidgetState('TestUnified', 'default')
        assert success is True
        
        state_path = temp_state_dir / 'TestUnified' / 'default.json'
        with open(state_path, 'r') as f:
            saved = json.load(f)
        
        # Should have unified controller's state shape
        assert 'param' in saved['state']
        assert 'hardware_active' in saved['state']
        
        # Load should apply via unified interface
        unified_controller.state_data = {'param': 'different', 'hardware_active': False}
        loaded = clean_registry.loadWidgetState('TestUnified', 'default', apply_immediately=True)
        assert loaded is not None
        
        # Assert unified interface was used (applyComponentState called with correct mode)
        assert unified_controller.last_applied_state is not None
        assert unified_controller.last_apply_mode == ComponentStateApplyMode.STARTUP_RESTORE


class TestJSONSerializability:
    """Test that registry enforces JSON-serializability."""
    
    def test_non_serializable_state_rejected(self, clean_registry, temp_state_dir):
        """Controller returning non-JSON-serializable state is handled gracefully."""
        class BadController:
            def getWidgetState(self):
                return {'func': lambda x: x}  # Not JSON-serializable
            
            def setWidgetState(self, state):
                pass
        
        controller = BadController()
        clean_registry.register('BadController', controller)
        
        # Snapshot should return None (with error logged)
        state = clean_registry.snapshotComponent('BadController')
        assert state is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
