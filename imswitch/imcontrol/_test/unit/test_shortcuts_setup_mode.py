import pytest
from unittest.mock import Mock, MagicMock, patch, call
from qtpy import QtWidgets

from imswitch.imcontrol.controller.controllers.SetupModesController import SetupModesController
from imswitch.imcontrol.controller.ShortcutManager import ShortcutManager


@pytest.fixture
def mock_widget(qtbot):
    """Create a mock SetupModesWidget."""
    widget = Mock()
    widget.setBackendAvailable = Mock()
    widget.setModes = Mock()
    widget.getSelectedModeName = Mock(return_value=None)
    widget.showError = Mock()
    widget.showWarnings = Mock(return_value=False)
    widget.showInspectModesDialog = Mock()
    widget.showSaveModeDialog = Mock(return_value=None)
    widget.askOverwriteMode = Mock(return_value=True)
    widget.askShortcutConflict = Mock(return_value=True)
    widget.showUpdateModeDialog = Mock(return_value=False)
    widget.showDeleteModeDialog = Mock(return_value=False)
    
    # Mock signals
    widget.sigModeSelected = Mock()
    widget.sigReloadMode = Mock()
    widget.sigInspectModes = Mock()
    widget.sigUpdateMode = Mock()
    widget.sigSaveAsMode = Mock()
    widget.sigRenameMode = Mock()
    widget.sigDuplicateMode = Mock()
    widget.sigSetShortcut = Mock()
    widget.sigSafetySettings = Mock()
    widget.sigDeleteMode = Mock()
    widget.sigRevealFolder = Mock()
    
    # Mock signal connect method
    for attr_name in dir(widget):
        attr = getattr(widget, attr_name)
        if attr_name.startswith('sig') and hasattr(attr, 'connect'):
            attr.connect = Mock()
    
    return widget


@pytest.fixture
def mock_setup_mode_controller():
    """Create a mock SetupModeController."""
    controller = Mock()
    controller.listSetupModes = Mock(return_value=['Mode1', 'Mode2'])
    controller.getSetupMode = Mock(side_effect=lambda name: {
        'name': name,
        'description': f'Description for {name}',
        'shortcut': 'F5' if name == 'Mode1' else 'F6',
        'includedComponents': ['Settings', 'Lasers'],
        'state': {}
    })
    controller.saveSetupMode = Mock(return_value={'warnings': []})
    controller.updateSetupModeMetadata = Mock()
    controller.deleteSetupMode = Mock()
    controller.getSetupModeComponents = Mock(return_value=['Settings', 'Lasers'])
    controller.snapshotSetupModeState = Mock(return_value={'warnings': []})
    return controller


@pytest.fixture
def mock_shortcut_manager():
    """Create a mock ShortcutManager."""
    manager = Mock(spec=ShortcutManager)
    manager.addOrUpdateAction = Mock()
    manager.removeAction = Mock()
    return manager


@pytest.fixture
def mock_main_window(qtbot):
    """Create a mock main window."""
    window = QtWidgets.QMainWindow()
    qtbot.addWidget(window)
    return window


@pytest.fixture
def mock_shortcuts_menu(mock_main_window):
    """Create a mock shortcuts menu."""
    return mock_main_window.menuBar().addMenu('&Shortcuts')


@pytest.fixture
def controller(mock_widget):
    """Create a SetupModesController instance."""
    mock_setup_info = Mock()
    mock_comm_channel = Mock()
    mock_master = Mock()
    mock_factory = Mock()
    mock_module_comm_channel = Mock()
    
    with patch('imswitch.imcontrol.controller.controllers.SetupModesController.dirtools'):
        controller = SetupModesController(
            setupInfo=mock_setup_info,
            commChannel=mock_comm_channel,
            master=mock_master,
            widget=mock_widget,
            factory=mock_factory,
            moduleCommChannel=mock_module_comm_channel
        )
        return controller


class TestSetupModesShortcuts:
    """Test suite for SetupModesController shortcut routing via ShortcutManager (Phase 3d)."""
    
    def test_inject_shortcut_manager(self, controller, mock_shortcut_manager, 
                                     mock_shortcuts_menu, mock_main_window):
        """Test that ShortcutManager can be injected into SetupModesController."""
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)
        
        assert controller._shortcutManager is mock_shortcut_manager
        assert controller._shortcutsMenu is mock_shortcuts_menu
        assert controller._mainWindow is mock_main_window
        
    def test_rebuild_shortcuts_routes_through_manager(self, controller, mock_setup_mode_controller,
                                                      mock_shortcut_manager, mock_shortcuts_menu, 
                                                      mock_main_window):
        """Test that _rebuildShortcuts routes mode shortcuts through the manager."""
        controller.setSetupModeController(mock_setup_mode_controller)
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)
        
        # Simulate refreshing modes
        modeSummaries = [
            {'name': 'Mode1', 'description': 'Desc1', 'shortcut': 'F5'},
            {'name': 'Mode2', 'description': 'Desc2', 'shortcut': 'F6'},
        ]
        
        controller._rebuildShortcuts(modeSummaries)
        
        # Should have called addOrUpdateAction for each mode with a shortcut
        assert mock_shortcut_manager.addOrUpdateAction.call_count == 2
        
        # Verify the calls
        calls = mock_shortcut_manager.addOrUpdateAction.call_args_list
        assert calls[0].kwargs['actionId'] == 'mode.Mode1'
        assert calls[0].kwargs['keySequence'] == 'F5'
        assert calls[0].kwargs['displayName'] == 'Apply Mode: Mode1'
        
        assert calls[1].kwargs['actionId'] == 'mode.Mode2'
        assert calls[1].kwargs['keySequence'] == 'F6'
        assert calls[1].kwargs['displayName'] == 'Apply Mode: Mode2'
        
    def test_clear_shortcuts_removes_via_manager(self, controller, mock_setup_mode_controller,
                                                 mock_shortcut_manager, mock_shortcuts_menu, 
                                                 mock_main_window):
        """Test that _clearShortcuts removes exactly the mode shortcuts this
        controller registered (robust to rename/delete)."""
        controller.setSetupModeController(mock_setup_mode_controller)
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)

        # Register two mode shortcuts so they are tracked, then clear.
        controller._rebuildShortcuts([
            {'name': 'Mode1', 'description': '', 'shortcut': 'F5'},
            {'name': 'Mode2', 'description': '', 'shortcut': 'F6'},
        ])
        mock_shortcut_manager.removeAction.reset_mock()

        controller._clearShortcuts()

        # Should have removed exactly the two registered mode actions.
        assert mock_shortcut_manager.removeAction.call_count == 2
        removed = {c.args[0] for c in mock_shortcut_manager.removeAction.call_args_list}
        assert removed == {'mode.Mode1', 'mode.Mode2'}
        # Tracking set is emptied after clearing.
        assert controller._registeredModeActionIds == set()
        
    def test_mode_shortcut_callback_preserves_source(self, controller, mock_setup_mode_controller,
                                                     mock_shortcut_manager, mock_shortcuts_menu, 
                                                     mock_main_window):
        """Test that mode shortcut callbacks pass source='shortcut' for safety confirmation."""
        controller.setSetupModeController(mock_setup_mode_controller)
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)
        
        modeSummaries = [
            {'name': 'Mode1', 'description': 'Desc1', 'shortcut': 'F5'},
        ]
        
        controller._rebuildShortcuts(modeSummaries)
        
        # Extract the callback that was registered
        callback = mock_shortcut_manager.addOrUpdateAction.call_args_list[0].kwargs['callback']
        
        # Mock the _applyMode method to verify it's called with source='shortcut'
        with patch.object(controller, '_applyMode') as mock_apply:
            callback()
            mock_apply.assert_called_once_with('Mode1', source='shortcut')
            
    def test_rebuild_without_manager_warns(self, controller, mock_setup_mode_controller, caplog):
        """Test that rebuilding shortcuts without injected manager logs a warning."""
        controller.setSetupModeController(mock_setup_mode_controller)
        # Don't inject the shortcut manager
        
        modeSummaries = [
            {'name': 'Mode1', 'description': 'Desc1', 'shortcut': 'F5'},
        ]
        
        with caplog.at_level('WARNING'):
            controller._rebuildShortcuts(modeSummaries)
            
        # Should have logged a warning
        assert any('ShortcutManager not injected' in record.getMessage() 
                  for record in caplog.records)
        
    def test_close_event_clears_shortcuts(self, controller, mock_setup_mode_controller,
                                         mock_shortcut_manager, mock_shortcuts_menu, 
                                         mock_main_window):
        """Test that closeEvent clears the registered mode shortcuts via the manager."""
        controller.setSetupModeController(mock_setup_mode_controller)
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)

        controller._rebuildShortcuts([
            {'name': 'Mode1', 'description': '', 'shortcut': 'F5'},
            {'name': 'Mode2', 'description': '', 'shortcut': 'F6'},
        ])
        mock_shortcut_manager.removeAction.reset_mock()

        controller.closeEvent()

        # Should have removed the two registered mode actions.
        assert mock_shortcut_manager.removeAction.call_count == 2
        
    def test_empty_shortcut_not_registered(self, controller, mock_setup_mode_controller,
                                          mock_shortcut_manager, mock_shortcuts_menu, 
                                          mock_main_window):
        """Test that modes with empty shortcuts are not registered."""
        controller.setSetupModeController(mock_setup_mode_controller)
        controller.setShortcutManager(mock_shortcut_manager, mock_shortcuts_menu, mock_main_window)
        
        modeSummaries = [
            {'name': 'Mode1', 'description': 'Desc1', 'shortcut': ''},
            {'name': 'Mode2', 'description': 'Desc2', 'shortcut': None},
            {'name': 'Mode3', 'description': 'Desc3', 'shortcut': 'F5'},
        ]
        
        controller._rebuildShortcuts(modeSummaries)
        
        # Only Mode3 should be registered
        assert mock_shortcut_manager.addOrUpdateAction.call_count == 1
        assert mock_shortcut_manager.addOrUpdateAction.call_args_list[0].kwargs['actionId'] == 'mode.Mode3'


# Copyright (C) 2026 ImSwitch developers
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
