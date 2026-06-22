"""
Phase 4a State Persistence Tests

Verifies that the unified component state persistence registry works correctly
after decommissioning the legacy compatibility layer:
- All controllers implement StatefulComponentMixin
- STARTUP_RESTORE vs SETUP_MODE_APPLY safety policy is enforced
- Alias resolution for legacy file keys still works
- Round-trip persistence for both widget and setup-mode consumers

Phase 4a: All controllers have migrated to the unified interface. Legacy
compatibility layers (getWidgetState/setWidgetState, SetupModeMixin) have
been removed.
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
    StatefulComponentMixin,
)


class FakeUnifiedController(StatefulComponentMixin):
    """Fake controller implementing StatefulComponentMixin interface."""
    
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


class TestUnifiedInterface:
    """Test unified StatefulComponentMixin interface."""
    
    def test_unified_register_and_snapshot(self, clean_registry, temp_state_dir):
        """Unified controller registers and snapshots correctly."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = clean_registry.snapshotComponent('TestUnified')
        assert state is not None
        assert state == {'param': 'test', 'hardware_active': False}
    
    def test_unified_apply(self, clean_registry, temp_state_dir):
        """Unified controller applies state correctly."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        new_state = {'param': 'modified', 'hardware_active': True}
        warnings = clean_registry.applyComponentState(
            'TestUnified',
            new_state,
            apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert warnings == []
        assert controller.last_applied_state == new_state
        assert controller.last_apply_mode == ComponentStateApplyMode.STARTUP_RESTORE
        assert controller.state_data == new_state
    
    def test_unified_round_trip_via_file(self, clean_registry, temp_state_dir):
        """Unified controller round-trips through save_to_file/load_from_file."""
        controller = FakeUnifiedController()
        controller.state_data = {'param': 'original', 'hardware_active': False}
        clean_registry.register('TestUnified', controller)
        
        # Save state
        os.makedirs(temp_state_dir, exist_ok=True)
        file_path = temp_state_dir / 'test_bundle.json'
        clean_registry.save_to_file(str(file_path))
        
        # Modify state
        controller.state_data = {'param': 'modified', 'hardware_active': True}
        
        # Load state
        count = clean_registry.load_from_file(str(file_path))
        assert count == 1
        assert controller.state_data == {'param': 'original', 'hardware_active': False}
    
    def test_unified_save_and_load_all(self, clean_registry, temp_state_dir):
        """Unified controller works with saveAllWidgetStates/loadAllWidgetStates."""
        controller = FakeUnifiedController()
        controller.state_data = {'param': 'test_value', 'hardware_active': False}
        clean_registry.register('TestUnified', controller)
        
        # Save all states
        clean_registry.saveAllWidgetStates()
        
        # Modify state
        controller.state_data = {'param': 'modified', 'hardware_active': True}
        
        # Load all states
        clean_registry.loadAllWidgetStates()
        assert controller.state_data == {'param': 'test_value', 'hardware_active': False}


class TestAliasResolution:
    """Test alias resolution for backward compatibility with old file keys."""
    
    def test_canonical_name_registration(self, clean_registry, temp_state_dir):
        """Controllers register under canonical name (Phase 4a - all controllers use canonical names)."""
        controller = FakeUnifiedController()
        # In Phase 4a, all controllers register under their canonical name
        clean_registry.register('TestUnified', controller)
        
        # Should be registered under canonical name
        assert 'TestUnified' in clean_registry._registry
        assert clean_registry._registry['TestUnified'] is controller
        
        # Snapshot via canonical name
        state = clean_registry.snapshotComponent('TestUnified')
        assert state == {'param': 'test', 'hardware_active': False}
    
    def test_old_bundle_file_loads_via_static_aliases(self, clean_registry, temp_state_dir):
        """Old bundle file with static alias keys (e.g., LaserController->Laser) loads correctly.
        
        Note: In Phase 4a, controllers register under canonical names only. Aliases are
        only used for loading old file keys via the static CANONICAL_ALIASES table.
        """
        # Use a controller that mimics the Laser controller
        controller = FakeUnifiedController()
        controller.componentName = 'Laser'  # Canonical name
        clean_registry.register('Laser', controller)
        
        # Create old-format bundle with legacy key 'LaserController' (maps to 'Laser')
        old_bundle = {
            'LaserController': {  # Legacy key from CANONICAL_ALIASES
                '_metadata': {
                    'controller_name': 'LaserController',
                    'canonical_name': 'Laser',
                    'schema_version': 1
                },
                'state': {'param': 'legacy_laser_value', 'hardware_active': False}
            }
        }
        
        file_path = temp_state_dir / 'old_bundle.json'
        os.makedirs(temp_state_dir, exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(old_bundle, f)
        
        # Load should resolve alias 'LaserController' -> 'Laser'
        count = clean_registry.load_from_file(str(file_path))
        assert count == 1
        assert controller.state_data == {'param': 'legacy_laser_value', 'hardware_active': False}


class TestApplyModeSafety:
    """Test STARTUP_RESTORE vs SETUP_MODE_APPLY safety policy."""
    
    def test_startup_restore_does_not_activate_hardware(self, clean_registry, temp_state_dir):
        """STARTUP_RESTORE mode does NOT activate hardware even if state says active."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        # Apply state with hardware_active=True in STARTUP_RESTORE mode
        state_with_activation = {'param': 'test', 'hardware_active': True}
        clean_registry.applyComponentState(
            'TestUnified',
            state_with_activation,
            apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        # State should be restored, but hardware should NOT be activated
        assert controller.state_data == state_with_activation
        assert controller.last_apply_mode == ComponentStateApplyMode.STARTUP_RESTORE
        assert len(controller.activation_calls) == 0, \
            "STARTUP_RESTORE must NOT activate hardware"
    
    def test_setup_mode_apply_activates_hardware(self, clean_registry, temp_state_dir):
        """SETUP_MODE_APPLY mode activates hardware if state says active."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        # Apply state with hardware_active=True in SETUP_MODE_APPLY mode
        state_with_activation = {'param': 'test', 'hardware_active': True}
        clean_registry.applyComponentState(
            'TestUnified',
            state_with_activation,
            apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        # State should be restored AND hardware should be activated
        assert controller.state_data == state_with_activation
        assert controller.last_apply_mode == ComponentStateApplyMode.SETUP_MODE_APPLY
        assert len(controller.activation_calls) == 1
        assert controller.activation_calls[0] == 'activate'
    
    def test_default_apply_mode_is_startup_restore(self, clean_registry, temp_state_dir):
        """Loading from file defaults to STARTUP_RESTORE mode (safe default)."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        # Create bundle with active hardware
        bundle = {
            'TestUnified': {
                '_metadata': {
                    'controller_name': 'TestUnified',
                    'canonical_name': 'TestUnified',
                    'schema_version': 1
                },
                'state': {'param': 'test', 'hardware_active': True}
            }
        }
        
        file_path = temp_state_dir / 'bundle.json'
        os.makedirs(temp_state_dir, exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(bundle, f)
        
        # Load (should default to STARTUP_RESTORE)
        clean_registry.load_from_file(str(file_path))
        
        # Should NOT activate hardware
        assert controller.last_apply_mode == ComponentStateApplyMode.STARTUP_RESTORE
        assert len(controller.activation_calls) == 0


class TestDescribeAndHazards:
    """Test describe and hazards delegation."""
    
    def test_describe_component_state(self, clean_registry, temp_state_dir):
        """describeComponentState delegates to controller."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'foo', 'hardware_active': True}
        description = clean_registry.describeComponentState('TestUnified', state)
        assert description == ["TestUnified: param=foo, active=True"]
    
    def test_get_component_state_hazards(self, clean_registry, temp_state_dir):
        """getComponentStateHazards delegates to controller."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'test', 'hardware_active': True}
        hazards = clean_registry.getComponentStateHazards(
            'TestUnified',
            state,
            apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY
        )
        
        assert len(hazards) == 1
        assert hazards[0]['severity'] == 'warning'
        assert hazards[0]['kind'] == 'test_hardware_activation'
    
    def test_get_component_state_hazards_safe_mode(self, clean_registry, temp_state_dir):
        """getComponentStateHazards in STARTUP_RESTORE shows no hazards."""
        controller = FakeUnifiedController()
        clean_registry.register('TestUnified', controller)
        
        state = {'param': 'test', 'hardware_active': True}
        hazards = clean_registry.getComponentStateHazards(
            'TestUnified',
            state,
            apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
        )
        
        assert len(hazards) == 0


class TestValidation:
    """Test registration validation."""
    
    def test_controller_must_implement_unified_interface(self, clean_registry, temp_state_dir):
        """Controllers without getComponentState/applyComponentState are rejected."""
        class BadController:
            pass
        
        controller = BadController()
        with pytest.raises(ValueError, match="must implement.*getComponentState.*applyComponentState"):
            clean_registry.register('BadController', controller)
    
    def test_non_serializable_state_rejected(self, clean_registry, temp_state_dir):
        """Non-JSON-serializable state is rejected during save."""
        controller = FakeUnifiedController()
        controller.state_data = {'func': lambda x: x}  # Not JSON-serializable
        clean_registry.register('TestUnified', controller)
        
        os.makedirs(temp_state_dir, exist_ok=True)
        file_path = temp_state_dir / 'bad_bundle.json'
        # Should complete without crashing (state is skipped with warning)
        clean_registry.save_to_file(str(file_path))
        
        # Bundle should be empty or not contain this controller
        with open(file_path, 'r') as f:
            bundle = json.load(f)
        assert 'TestUnified' not in bundle
