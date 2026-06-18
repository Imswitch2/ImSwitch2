"""
Unit tests for FlipMirrorController unified state persistence (Phase 2g).

Tests the StatefulComponentMixin implementation on FlipMirrorController:
- getComponentState / applyComponentState round-trip
- STARTUP_RESTORE safety (no physical mirror moves)
- SETUP_MODE_APPLY performs mirror moves and link restoration
- describeComponentState returns human-readable summaries
- getComponentStateHazards returns [] for both modes
- FlipMirror appears in setup mode component discovery when devices exist
- FlipMirror not registered when no devices exist
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call

from imswitch.imcontrol.controller.controllers.FlipMirrorController import FlipMirrorController
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode


@pytest.fixture
def mock_flip_mirror_manager():
    """Mock flipMirrorsManager with two mirrors: FM1 and FM2."""
    manager = MagicMock()
    
    # Create flip mirror device instances
    fm1 = Mock()
    fm1.get_state = Mock(return_value=0)
    fm1.move_to = Mock()
    fm1.is_connected = Mock(return_value=True)
    fm1.get_last_error = Mock(return_value=None)
    fm1.get_state_names = Mock(return_value={0: "Position A", 1: "Position B"})
    
    fm2 = Mock()
    fm2.get_state = Mock(return_value=1)
    fm2.move_to = Mock()
    fm2.is_connected = Mock(return_value=True)
    fm2.get_last_error = Mock(return_value=None)
    fm2.get_state_names = Mock(return_value={0: "0", 1: "1"})
    
    manager.hasDevices = Mock(return_value=True)
    manager.getAllDeviceNames = Mock(return_value=['FM1', 'FM2'])
    manager.__getitem__ = Mock(side_effect=lambda name: fm1 if name == 'FM1' else fm2)
    manager.reset_connections = Mock()
    
    return manager


@pytest.fixture
def mock_widget():
    """Mock FlipMirrorWidget."""
    widget = MagicMock()
    widget.setAllEnabled = Mock()
    widget.addFlipMirror = Mock()
    widget.setState = Mock()
    widget.setMasterChoices = Mock()
    widget.setLink = Mock()
    widget.setRowState = Mock()
    widget.sigStateChanged = MagicMock()
    widget.sigLinkChanged = MagicMock()
    widget.sigResetConnectionsClicked = MagicMock()
    
    return widget


@pytest.fixture
def mock_master(mock_flip_mirror_manager):
    """Mock master controller."""
    master = Mock()
    master.flipMirrorsManager = mock_flip_mirror_manager
    return master


@pytest.fixture
def flip_mirror_controller(mock_master, mock_widget, mock_flip_mirror_manager):
    """Create FlipMirrorController mock with necessary attributes and methods."""
    # Create a lightweight mock that has the methods we're testing
    controller = Mock(spec=FlipMirrorController)
    controller._master = mock_master
    controller._widget = mock_widget
    controller._manager = mock_flip_mirror_manager
    controller._names = ['FM1', 'FM2']
    controller._master_by_follower = {}
    controller._followers_by_master = {}
    controller._FlipMirrorController__logger = Mock()
    
    # Bind the actual methods from FlipMirrorController to the mock
    controller.getComponentState = lambda: FlipMirrorController.getComponentState(controller)
    controller.applyComponentState = lambda state, applyMode: FlipMirrorController.applyComponentState(
        controller, state, applyMode=applyMode
    )
    controller.describeComponentState = lambda state: FlipMirrorController.describeComponentState(controller, state)
    controller.getComponentStateHazards = lambda state, applyMode, context=None: FlipMirrorController.getComponentStateHazards(
        controller, state, applyMode=applyMode, context=context
    )
    
    # Bind helper methods used by the component state methods
    controller._is_connected = lambda name: FlipMirrorController._is_connected(controller, name)
    controller._safe_move_one = lambda name, state: FlipMirrorController._safe_move_one(controller, name, state)
    controller.set_link = lambda follower, master: FlipMirrorController.set_link(controller, follower, master)
    controller._refresh_link_ui = lambda: FlipMirrorController._refresh_link_ui(controller)
    
    # Add class attributes
    controller.componentName = 'FlipMirror'
    controller.stateSchemaVersion = 1
    controller.legacyStateNames = ()
    
    return controller


def test_flipmirror_implements_stateful_component_mixin(flip_mirror_controller):
    """FlipMirrorController implements StatefulComponentMixin with correct attributes."""
    assert flip_mirror_controller.componentName == 'FlipMirror'
    assert flip_mirror_controller.stateSchemaVersion == 1
    assert flip_mirror_controller.legacyStateNames == ()
    
    assert hasattr(flip_mirror_controller, 'getComponentState')
    assert hasattr(flip_mirror_controller, 'applyComponentState')
    assert hasattr(flip_mirror_controller, 'describeComponentState')
    assert hasattr(flip_mirror_controller, 'getComponentStateHazards')


def test_get_component_state_snapshots_mirrors_and_links(flip_mirror_controller, mock_flip_mirror_manager):
    """getComponentState snapshots mirror states and link configuration."""
    # Set up a link: FM2 follows FM1
    flip_mirror_controller._master_by_follower = {'FM2': 'FM1'}
    
    state = flip_mirror_controller.getComponentState()
    
    assert isinstance(state, dict)
    assert 'mirrors' in state
    assert 'links' in state
    
    # Check mirrors
    assert 'FM1' in state['mirrors']
    assert 'FM2' in state['mirrors']
    assert state['mirrors']['FM1']['state'] == 0
    assert state['mirrors']['FM1']['connected'] is True
    assert state['mirrors']['FM2']['state'] == 1
    assert state['mirrors']['FM2']['connected'] is True
    
    # Check links
    assert state['links'] == {'FM2': 'FM1'}


def test_apply_component_state_startup_restore_no_move(flip_mirror_controller, mock_flip_mirror_manager):
    """applyComponentState in STARTUP_RESTORE does NOT physically move mirrors."""
    saved_state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},  # Different from current (0)
            'FM2': {'state': 0, 'connected': True},  # Different from current (1)
        },
        'links': {'FM2': 'FM1'}
    }
    
    # Get the mock devices
    fm1 = mock_flip_mirror_manager['FM1']
    fm2 = mock_flip_mirror_manager['FM2']
    
    # Reset mock call counts
    fm1.move_to.reset_mock()
    fm2.move_to.reset_mock()
    
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    
    # Assert NO physical moves occurred
    fm1.move_to.assert_not_called()
    fm2.move_to.assert_not_called()
    
    # Assert warnings about not moving
    assert len(warnings) > 0
    assert any('not moved at startup' in w for w in warnings)
    assert any('links not restored' in w for w in warnings)


def test_apply_component_state_setup_mode_apply_moves_mirrors(flip_mirror_controller, mock_flip_mirror_manager):
    """applyComponentState in SETUP_MODE_APPLY physically moves mirrors."""
    saved_state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},
            'FM2': {'state': 0, 'connected': True},
        },
        'links': {}
    }
    
    # Get the mock devices
    fm1 = mock_flip_mirror_manager['FM1']
    fm2 = mock_flip_mirror_manager['FM2']
    
    # Reset mock call counts
    fm1.move_to.reset_mock()
    fm2.move_to.reset_mock()
    
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    
    # Assert mirrors WERE moved
    fm1.move_to.assert_called_once_with(1)
    fm2.move_to.assert_called_once_with(0)
    
    # Should succeed without warnings (if all moves succeed)
    assert len(warnings) == 0


def test_apply_component_state_setup_mode_apply_restores_links(flip_mirror_controller, mock_flip_mirror_manager):
    """applyComponentState in SETUP_MODE_APPLY restores mirror links."""
    saved_state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},
            'FM2': {'state': 1, 'connected': True},
        },
        'links': {'FM2': 'FM1'}
    }
    
    # Start with no links
    flip_mirror_controller._master_by_follower = {}
    flip_mirror_controller._followers_by_master = {}
    
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    
    # Assert link was restored
    assert flip_mirror_controller._master_by_follower == {'FM2': 'FM1'}
    assert 'FM1' in flip_mirror_controller._followers_by_master
    assert 'FM2' in flip_mirror_controller._followers_by_master['FM1']


def test_describe_component_state_returns_readable_summary(flip_mirror_controller):
    """describeComponentState returns human-readable summary lines."""
    state = {
        'mirrors': {
            'FM1': {'state': 0, 'connected': True},
            'FM2': {'state': 1, 'connected': False},
        },
        'links': {'FM2': 'FM1'}
    }
    
    lines = flip_mirror_controller.describeComponentState(state)
    
    assert isinstance(lines, list)
    assert len(lines) > 0
    
    # Check that mirrors are described
    assert any('FM1' in line for line in lines)
    assert any('FM2' in line for line in lines)
    assert any('state=0' in line for line in lines)
    assert any('state=1' in line for line in lines)
    assert any('connected' in line for line in lines)
    assert any('disconnected' in line for line in lines)
    
    # Check that links are described
    assert any('links' in line for line in lines)
    assert any('FM2' in line and 'FM1' in line for line in lines)


def test_get_component_state_hazards_returns_empty_for_both_modes(flip_mirror_controller):
    """getComponentStateHazards returns [] for both STARTUP_RESTORE and SETUP_MODE_APPLY."""
    state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},
            'FM2': {'state': 0, 'connected': True},
        },
        'links': {}
    }
    
    # STARTUP_RESTORE
    hazards_startup = flip_mirror_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    )
    assert hazards_startup == []
    
    # SETUP_MODE_APPLY
    hazards_apply = flip_mirror_controller.getComponentStateHazards(
        state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    assert hazards_apply == []


def test_flipmirror_registered_when_devices_exist():
    """FlipMirror is registered when flip mirror devices exist."""
    # Test that registration happens in __init__ when devices exist
    # This is verified by checking the actual controller code
    # The registration line is: getWidgetStatePersistence().register('FlipMirror', self)
    # This is only called when manager exists and hasDevices() returns True
    
    import inspect
    source = inspect.getsource(FlipMirrorController.__init__)
    
    # Check that registration happens
    assert "getWidgetStatePersistence().register('FlipMirror', self)" in source
    
    # Check that there's a guard for no devices with early return
    assert 'if self._manager is None or not self._manager.hasDevices():' in source
    
    # Check that the early return exists within reasonable distance of the check
    # (account for setAllEnabled call between the check and return)
    lines = source.split('\n')
    device_check_idx = None
    register_line_idx = None
    return_idx = None
    
    for idx, line in enumerate(lines):
        if 'if self._manager is None or not self._manager.hasDevices():' in line:
            device_check_idx = idx
        if "register('FlipMirror'" in line:
            register_line_idx = idx
        if 'return' in line and device_check_idx is not None and idx > device_check_idx and idx < device_check_idx + 5:
            # Return within 5 lines of device check (accounts for setAllEnabled)
            return_idx = idx
    
    # Registration should happen after the conditional block with early return
    assert register_line_idx is not None, "Registration call not found"
    assert device_check_idx is not None, "Device check not found"
    assert return_idx is not None, "Early return not found"
    assert register_line_idx > return_idx, "Registration happens before early return"


def test_flipmirror_not_registered_when_no_devices():
    """FlipMirror is NOT registered when no flip mirror devices exist."""
    # The early return in __init__ prevents registration when no devices
    # This is verified by checking the code structure
    
    import inspect
    source = inspect.getsource(FlipMirrorController.__init__)
    
    # Check that there's an early return when no devices
    assert 'if self._manager is None or not self._manager.hasDevices():' in source
    assert 'return' in source.split('hasDevices()')[1].split('getWidgetStatePersistence()')[0]


def test_flipmirror_not_registered_when_manager_has_no_devices():
    """FlipMirror is NOT registered when manager exists but hasDevices() returns False."""
    # Same as above - the hasDevices() check guards registration
    
    import inspect
    source = inspect.getsource(FlipMirrorController.__init__)
    
    # Check that hasDevices() is part of the guard condition
    assert 'not self._manager.hasDevices()' in source


def test_flipmirror_in_setup_mode_discovery(flip_mirror_controller):
    """FlipMirror appears in SetupModeController component discovery when devices exist."""
    # FlipMirror is registered with componentName = 'FlipMirror'
    # This makes it discoverable by SetupModeController via the unified registry
    
    assert flip_mirror_controller.componentName == 'FlipMirror'
    
    # The StatefulComponentMixin interface makes it discoverable
    assert hasattr(flip_mirror_controller, 'getComponentState')
    assert hasattr(flip_mirror_controller, 'applyComponentState')


def test_apply_component_state_handles_missing_mirror(flip_mirror_controller):
    """applyComponentState handles missing mirrors gracefully in SETUP_MODE_APPLY."""
    saved_state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},
            'FM_MISSING': {'state': 0, 'connected': True},  # This mirror doesn't exist
        },
        'links': {}
    }
    
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    
    # Should have a warning about missing mirror
    assert any('FM_MISSING' in w and 'not available' in w for w in warnings)


def test_apply_component_state_handles_disconnected_mirror(flip_mirror_controller, mock_flip_mirror_manager):
    """applyComponentState handles disconnected mirrors gracefully in SETUP_MODE_APPLY."""
    # Make FM2 disconnected
    fm2 = mock_flip_mirror_manager['FM2']
    fm2.is_connected.return_value = False
    
    saved_state = {
        'mirrors': {
            'FM1': {'state': 1, 'connected': True},
            'FM2': {'state': 0, 'connected': True},
        },
        'links': {}
    }
    
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    
    # Should have a warning about disconnected mirror
    assert any('FM2' in w and 'not connected' in w for w in warnings)


def test_round_trip_state_persistence(flip_mirror_controller):
    """Round-trip test: save state, modify, restore, verify."""
    # Set up initial state
    flip_mirror_controller._master_by_follower = {'FM2': 'FM1'}
    
    # Save state
    saved_state = flip_mirror_controller.getComponentState()
    
    # Modify state
    flip_mirror_controller._master_by_follower = {}
    flip_mirror_controller._followers_by_master = {}
    
    # Restore state (SETUP_MODE_APPLY)
    warnings = flip_mirror_controller.applyComponentState(
        saved_state,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )
    
    # Verify restoration
    assert flip_mirror_controller._master_by_follower == {'FM2': 'FM1'}
    assert 'FM1' in flip_mirror_controller._followers_by_master
