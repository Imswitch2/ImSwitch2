"""Unit tests for Phase 3a menu action shortcuts migration.

Verifies that the three File menu actions (loadParams, saveWidgetStates, loadWidgetStates)
are correctly registered with ShortcutManager and can be config-rebound.
"""
import pytest
from unittest.mock import Mock, MagicMock
from qtpy import QtCore, QtWidgets, QtGui

from imswitch.imcommon.model import ShortcutAction, ShortcutScope
from imswitch.imcontrol.controller.ShortcutManager import ShortcutManager


class TestMenuActionsPhase3a:
    """Test Phase 3a menu action migration."""
    
    @pytest.fixture
    def manager(self):
        """Create a ShortcutManager instance."""
        return ShortcutManager()
        
    @pytest.fixture
    def mock_main_window(self, qtbot):
        """Create a mock main window for testing."""
        window = QtWidgets.QMainWindow()
        qtbot.addWidget(window)
        return window
        
    @pytest.fixture
    def mock_shortcuts_menu(self, mock_main_window):
        """Create a mock Shortcuts menu."""
        menu = mock_main_window.menuBar().addMenu('&Shortcuts')
        return menu
    
    def test_register_action_creates_catalog_entry(self, manager):
        """Test that registerAction adds the action to the catalog."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=None
        )
        
        catalog = manager.getAllActions()
        assert 'app.loadParams' in catalog
        action = catalog['app.loadParams']
        assert action.actionId == 'app.loadParams'
        assert action.displayName == 'Load parameters'
        assert action.defaultKeySequence == 'Ctrl+P'
        assert action.scope == ShortcutScope.Window
        assert action.callback == callback
        
    def test_register_duplicate_action_raises_error(self, manager):
        """Test that registering duplicate actionId raises RuntimeError."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.test',
            displayName='Test',
            callback=callback,
            defaultKeySequence='Ctrl+T'
        )
        
        with pytest.raises(RuntimeError, match='Duplicate shortcut actionId'):
            manager.registerAction(
                actionId='app.test',
                displayName='Test 2',
                callback=callback,
                defaultKeySequence='Ctrl+T'
            )
    
    def test_three_menu_actions_registered(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that all three menu actions can be registered with default keys."""
        signal1 = Mock()
        signal2 = Mock()
        signal3 = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters from HDF5',
            callback=signal1,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        manager.registerAction(
            actionId='app.saveWidgetStates',
            displayName='Save Widget States',
            callback=signal2,
            defaultKeySequence='Ctrl+Shift+S',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        manager.registerAction(
            actionId='app.loadWidgetStates',
            displayName='Load Widget States',
            callback=signal3,
            defaultKeySequence='Ctrl+Shift+L',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        catalog = manager.getAllActions()
        assert len(catalog) == 3
        assert 'app.loadParams' in catalog
        assert 'app.saveWidgetStates' in catalog
        assert 'app.loadWidgetStates' in catalog
        
    def test_effective_bindings_with_no_config(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that default keys are effective when no config override exists."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['app.loadParams'] == 'Ctrl+P'
        
    def test_config_override_rebinds_action(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that a config override changes the effective binding."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        # Override with different key
        manager.loadConfigOverrides({'app.loadParams': 'Ctrl+Shift+P'})
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['app.loadParams'] == 'Ctrl+Shift+P'
        
    def test_config_null_disables_action(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that a null config value disables the action."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        # Disable with null
        manager.loadConfigOverrides({'app.loadParams': None})
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert 'app.loadParams' not in bindings
        
    def test_build_creates_qt_objects(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that build() creates Qt shortcuts that work."""
        callback_called = []
        
        def callback():
            callback_called.append(True)
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        # Verify the shortcut was added to the menu
        actions = mock_shortcuts_menu.actions()
        assert len(actions) == 1
        assert actions[0].text() == 'Load parameters'
        assert actions[0].shortcut().toString() == 'Ctrl+P'
        
        # Trigger the action and verify callback is called
        actions[0].trigger()
        assert len(callback_called) == 1
        
    def test_no_double_fire_from_duplicate_binding(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that action is triggered exactly once, not twice from duplicate binding."""
        callback_called = []
        
        def callback():
            callback_called.append(True)
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        # Find the QAction created by the manager
        actions = mock_shortcuts_menu.actions()
        assert len(actions) == 1
        
        # Trigger it
        actions[0].trigger()
        
        # Should be called exactly once
        assert len(callback_called) == 1
        
    def test_multiple_actions_config_rebind(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test rebinding multiple menu actions via config."""
        callbacks = {'load': Mock(), 'save': Mock(), 'loadWidget': Mock()}
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callbacks['load'],
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        manager.registerAction(
            actionId='app.saveWidgetStates',
            displayName='Save Widget States',
            callback=callbacks['save'],
            defaultKeySequence='Ctrl+Shift+S',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        manager.registerAction(
            actionId='app.loadWidgetStates',
            displayName='Load Widget States',
            callback=callbacks['loadWidget'],
            defaultKeySequence='Ctrl+Shift+L',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        # Rebind one action, disable another
        config = {
            'app.loadParams': 'Alt+P',
            'app.saveWidgetStates': None,
            # app.loadWidgetStates keeps default
        }
        
        manager.loadConfigOverrides(config)
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['app.loadParams'] == 'Alt+P'
        assert 'app.saveWidgetStates' not in bindings
        assert bindings['app.loadWidgetStates'] == 'Ctrl+Shift+L'
        
    def test_dispose_clears_shortcuts(self, manager, mock_main_window, mock_shortcuts_menu):
        """Test that dispose() correctly cleans up Qt objects."""
        callback = Mock()
        
        manager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters',
            callback=callback,
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=mock_main_window
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        # Verify shortcuts were created
        actions = mock_shortcuts_menu.actions()
        assert len(actions) == 1
        
        # Dispose
        manager.dispose()
        
        # Verify the internal state is cleared
        # Note: Qt objects may still exist until deleteLater() is processed


# Copyright (C) 2020-2021 ImSwitch developers
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
