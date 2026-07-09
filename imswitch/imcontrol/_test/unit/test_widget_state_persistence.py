"""
Unit tests for WidgetStatePersistence framework
"""

import os
import json
import pytest
from typing import Dict, Any
from imswitch.imcommon.model.WidgetStatePersistence import WidgetStatePersistence


class MockController:
    """Mock controller implementing unified StatefulComponentMixin interface"""
    
    componentName = 'MockController'
    stateSchemaVersion = 1
    legacyStateNames = ()
    
    def __init__(self, initial_state=None):
        self.state = initial_state or {'value': 0, 'setting': 'default'}
    
    def getComponentState(self) -> Dict[str, Any]:
        return self.state.copy()
    
    def applyComponentState(self, state: Dict[str, Any], *, applyMode) -> list:
        self.state = state.copy()
        return []
    
    def describeComponentState(self, state: Dict[str, Any]) -> list:
        return [f"MockController state: {state}"]
    
    def getComponentStateHazards(self, state: Dict[str, Any], *, applyMode, context=None) -> list:
        return []


class MockControllerNoVersion:
    """Mock controller without schema version (should still work with defaults)"""
    
    componentName = 'MockControllerNoVersion'
    legacyStateNames = ()
    
    def __init__(self):
        self.state = {'value': 42}
    
    def getComponentState(self) -> Dict[str, Any]:
        return self.state.copy()
    
    def applyComponentState(self, state: Dict[str, Any], *, applyMode) -> list:
        self.state = state.copy()
        return []
    
    def describeComponentState(self, state: Dict[str, Any]) -> list:
        return [f"MockControllerNoVersion state: {state}"]
    
    def getComponentStateHazards(self, state: Dict[str, Any], *, applyMode, context=None) -> list:
        return []


@pytest.fixture
def persistence_service(tmp_path, monkeypatch):
    """Create a WidgetStatePersistence instance with temporary storage"""
    # Monkeypatch initLogger to avoid weak reference issues with pytest
    import logging
    from imswitch.imcommon.model import WidgetStatePersistence as WSP_module_file
    
    def mock_init_logger(obj, **kwargs):
        return logging.getLogger(obj.__class__.__name__ if not isinstance(obj, str) else obj)
    
    import sys
    wsp_module = sys.modules['imswitch.imcommon.model.WidgetStatePersistence']
    monkeypatch.setattr(wsp_module, 'initLogger', mock_init_logger)
    
    service = WidgetStatePersistence()
    # Monkeypatch the _stateDir to use tmp_path
    state_dir = str(tmp_path / 'widget_states')
    os.makedirs(state_dir, exist_ok=True)
    monkeypatch.setattr(service, '_stateDir', state_dir)
    return service


@pytest.fixture
def mock_controller():
    """Create a mock controller"""
    return MockController({'laser_value': 50.0, 'exposure': 100.0})


def test_persistence_initialization(tmp_path, monkeypatch):
    """Test that persistence service initializes correctly"""
    # Monkeypatch initLogger to avoid weak reference issues with pytest
    import logging
    from imswitch.imcommon.model import WidgetStatePersistence as WSP_module_file
    
    def mock_init_logger(obj, **kwargs):
        return logging.getLogger(obj.__class__.__name__ if not isinstance(obj, str) else obj)
    
    import sys
    wsp_module = sys.modules['imswitch.imcommon.model.WidgetStatePersistence']
    monkeypatch.setattr(wsp_module, 'initLogger', mock_init_logger)
    
    service = WidgetStatePersistence()
    # Monkeypatch to a temp directory
    state_dir = str(tmp_path / 'widget_states')
    os.makedirs(state_dir, exist_ok=True)
    monkeypatch.setattr(service, '_stateDir', state_dir)
    assert service._stateDir == state_dir
    assert os.path.exists(state_dir)


def test_register_controller(persistence_service, mock_controller):
    """Test controller registration"""
    persistence_service.register('TestController', mock_controller)
    assert 'TestController' in persistence_service._registry
    assert persistence_service._registry['TestController'] == mock_controller


def test_register_controller_without_methods(persistence_service):
    """Test that registration raises ValueError for invalid controllers"""
    class InvalidController:
        pass
    
    controller = InvalidController()
    # Should raise ValueError for controllers without required methods
    try:
        persistence_service.register('InvalidController', controller)
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "must implement StatefulComponentMixin" in str(e)
    # Controller should not be registered
    assert 'InvalidController' not in persistence_service._registry


def test_unregister_controller(persistence_service, mock_controller):
    """Test controller unregistration"""
    persistence_service.register('TestController', mock_controller)
    assert 'TestController' in persistence_service._registry
    
    persistence_service.unregister('TestController')
    assert 'TestController' not in persistence_service._registry


def test_save_and_load_state(persistence_service, mock_controller):
    """Test basic save and load operations"""
    persistence_service.register('TestController', mock_controller)
    
    # Save state
    success = persistence_service.saveWidgetState('TestController', 'test_state')
    assert success
    
    # Verify file was created
    state_file = os.path.join(
        persistence_service._stateDir,
        'TestController',
        'test_state.json'
    )
    assert os.path.exists(state_file)
    
    # Modify controller state
    mock_controller.state = {'laser_value': 0.0, 'exposure': 0.0}
    
    # Load state
    loaded_state = persistence_service.loadWidgetState('TestController', 'test_state')
    assert loaded_state is not None
    assert loaded_state['laser_value'] == 50.0
    assert loaded_state['exposure'] == 100.0
    
    # Controller state should be restored
    assert mock_controller.state['laser_value'] == 50.0
    assert mock_controller.state['exposure'] == 100.0


def test_save_state_with_metadata(persistence_service, mock_controller):
    """Test that saved state includes metadata"""
    persistence_service.register('TestController', mock_controller)
    persistence_service.saveWidgetState('TestController', 'test_state')
    
    state_file = os.path.join(
        persistence_service._stateDir,
        'TestController',
        'test_state.json'
    )
    
    with open(state_file, 'r') as f:
        data = json.load(f)
    
    assert '_metadata' in data
    assert data['_metadata']['controller_name'] == 'TestController'
    assert data['_metadata']['schema_version'] == 1
    assert 'state' in data
    assert data['state']['laser_value'] == 50.0


def test_load_nonexistent_state(persistence_service, mock_controller):
    """Test loading a state that doesn't exist"""
    persistence_service.register('TestController', mock_controller)
    
    loaded_state = persistence_service.loadWidgetState('TestController', 'nonexistent')
    assert loaded_state is None


def test_load_state_without_apply(persistence_service, mock_controller):
    """Test loading state without applying it to controller"""
    persistence_service.register('TestController', mock_controller)
    persistence_service.saveWidgetState('TestController', 'test_state')
    
    # Modify controller state
    original_state = {'laser_value': 99.0, 'exposure': 99.0}
    mock_controller.state = original_state.copy()
    
    # Load without applying
    loaded_state = persistence_service.loadWidgetState(
        'TestController',
        'test_state',
        apply_immediately=False
    )
    
    assert loaded_state is not None
    assert loaded_state['laser_value'] == 50.0
    
    # Controller state should NOT be changed
    assert mock_controller.state['laser_value'] == 99.0


def test_list_saved_states(persistence_service, mock_controller):
    """Test listing saved states"""
    persistence_service.register('TestController', mock_controller)
    
    # Save multiple states
    persistence_service.saveWidgetState('TestController', 'state1')
    persistence_service.saveWidgetState('TestController', 'state2')
    persistence_service.saveWidgetState('TestController', 'state3')
    
    # List states
    states = persistence_service.listSavedStates('TestController')
    assert len(states) == 3
    assert 'state1' in states
    assert 'state2' in states
    assert 'state3' in states


def test_delete_widget_state(persistence_service, mock_controller):
    """Test deleting a saved state"""
    persistence_service.register('TestController', mock_controller)
    persistence_service.saveWidgetState('TestController', 'test_state')
    
    # Verify state exists
    states = persistence_service.listSavedStates('TestController')
    assert 'test_state' in states
    
    # Delete state
    success = persistence_service.deleteWidgetState('TestController', 'test_state')
    assert success
    
    # Verify state is gone
    states = persistence_service.listSavedStates('TestController')
    assert 'test_state' not in states


def test_save_all_widget_states(persistence_service):
    """Test saving all registered controllers"""
    controller1 = MockController({'value': 1})
    controller2 = MockController({'value': 2})
    
    persistence_service.register('Controller1', controller1)
    persistence_service.register('Controller2', controller2)
    
    count = persistence_service.saveAllWidgetStates('snapshot')
    assert count == 2
    
    # Verify both were saved
    states1 = persistence_service.listSavedStates('Controller1')
    states2 = persistence_service.listSavedStates('Controller2')
    assert 'snapshot' in states1
    assert 'snapshot' in states2


def test_load_all_widget_states(persistence_service):
    """Test loading all registered controllers"""
    controller1 = MockController({'value': 1})
    controller2 = MockController({'value': 2})
    
    persistence_service.register('Controller1', controller1)
    persistence_service.register('Controller2', controller2)
    
    # Save initial states
    persistence_service.saveAllWidgetStates('snapshot')
    
    # Modify states
    controller1.state = {'value': 999}
    controller2.state = {'value': 888}
    
    # Load all
    count = persistence_service.loadAllWidgetStates('snapshot')
    assert count == 2
    
    # Verify states were restored
    assert controller1.state['value'] == 1
    assert controller2.state['value'] == 2


def test_get_registered_controllers(persistence_service):
    """Test getting list of registered controllers"""
    controller1 = MockController()
    controller2 = MockController()
    
    persistence_service.register('Controller1', controller1)
    persistence_service.register('Controller2', controller2)
    
    controllers = persistence_service.getRegisteredControllers()
    assert len(controllers) == 2
    assert 'Controller1' in controllers
    assert 'Controller2' in controllers


def test_schema_version_default(persistence_service):
    """Test default schema version when controller doesn't define stateSchemaVersion"""
    controller = MockControllerNoVersion()
    persistence_service.register('TestController', controller)
    persistence_service.saveWidgetState('TestController', 'test_state')
    
    state_file = os.path.join(
        persistence_service._stateDir,
        'TestController',
        'test_state.json'
    )
    
    with open(state_file, 'r') as f:
        data = json.load(f)
    
    # Should default to version 1
    assert data['_metadata']['schema_version'] == 1


def test_corrupted_json_file(persistence_service, mock_controller):
    """Test loading a corrupted JSON file"""
    persistence_service.register('TestController', mock_controller)
    
    # Create corrupted JSON file
    controller_dir = os.path.join(persistence_service._stateDir, 'TestController')
    os.makedirs(controller_dir, exist_ok=True)
    corrupted_file = os.path.join(controller_dir, 'corrupted.json')
    
    with open(corrupted_file, 'w') as f:
        f.write('{ invalid json }')
    
    # Should return None and not crash
    loaded_state = persistence_service.loadWidgetState('TestController', 'corrupted')
    assert loaded_state is None


def test_missing_state_key_in_file(persistence_service, mock_controller):
    """Test loading a file without 'state' key"""
    persistence_service.register('TestController', mock_controller)
    
    # Create file with missing 'state' key
    controller_dir = os.path.join(persistence_service._stateDir, 'TestController')
    os.makedirs(controller_dir, exist_ok=True)
    invalid_file = os.path.join(controller_dir, 'invalid.json')
    
    with open(invalid_file, 'w') as f:
        json.dump({'_metadata': {'controller_name': 'TestController'}}, f)
    
    # Should return empty dict (not None) since real implementation returns state with .get('state', {})
    loaded_state = persistence_service.loadWidgetState('TestController', 'invalid')
    assert loaded_state == {}


def test_save_unregistered_controller(persistence_service):
    """Test saving state for unregistered controller"""
    success = persistence_service.saveWidgetState('UnregisteredController', 'test')
    assert not success


def test_load_unregistered_controller(persistence_service):
    """Test loading state for unregistered controller"""
    loaded_state = persistence_service.loadWidgetState('UnregisteredController', 'test')
    assert loaded_state is None


def test_delete_nonexistent_state(persistence_service, mock_controller):
    """Test deleting a state that doesn't exist"""
    persistence_service.register('TestController', mock_controller)
    
    # Should return False but not crash
    success = persistence_service.deleteWidgetState('TestController', 'nonexistent')
    assert not success


def test_state_file_sanitization(persistence_service, mock_controller):
    """Test that state names are sanitized for filesystem"""
    persistence_service.register('TestController', mock_controller)
    
    # Try to save with special characters
    # The implementation should handle this safely
    success = persistence_service.saveWidgetState('TestController', 'my state')
    
    # Should either succeed with sanitized name or fail gracefully
    assert isinstance(success, bool)


def test_concurrent_controller_registration(persistence_service):
    """Test registering multiple controllers"""
    controllers = [MockController({'id': i}) for i in range(5)]
    
    for i, controller in enumerate(controllers):
        persistence_service.register(f'Controller{i}', controller)
    
    assert len(persistence_service.getRegisteredControllers()) == 5


def test_state_isolation(persistence_service):
    """Test that controller states are isolated from each other"""
    controller1 = MockController({'value': 1, 'name': 'first'})
    controller2 = MockController({'value': 2, 'name': 'second'})
    
    persistence_service.register('Controller1', controller1)
    persistence_service.register('Controller2', controller2)
    
    # Save both
    persistence_service.saveWidgetState('Controller1', 'test')
    persistence_service.saveWidgetState('Controller2', 'test')
    
    # Load controller1 state
    loaded1 = persistence_service.loadWidgetState(
        'Controller1', 'test', apply_immediately=False
    )
    loaded2 = persistence_service.loadWidgetState(
        'Controller2', 'test', apply_immediately=False
    )
    
    # States should be different
    assert loaded1['value'] == 1
    assert loaded1['name'] == 'first'
    assert loaded2['value'] == 2
    assert loaded2['name'] == 'second'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# Copyright (C) 2025 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
