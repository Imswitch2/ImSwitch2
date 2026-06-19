"""Unit tests for Phase 3b LeicaStand F2 shortcut migration.

Verifies that the LeicaStand toggleMode action is correctly registered
with ShortcutManager and can be config-rebound, replacing the manual QShortcut.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from qtpy import QtCore, QtWidgets, QtGui

from imswitch.imcommon.model import ShortcutAction, ShortcutScope
from imswitch.imcontrol.controller.ShortcutManager import ShortcutManager


class TestLeicaStandPhase3b:
    """Test Phase 3b LeicaStand F2 shortcut migration."""
    
    @pytest.fixture
    def manager(self):
        """Create a ShortcutManager instance."""
        return ShortcutManager()
        
    @pytest.fixture
    def mock_leica_widget(self, qtbot):
        """Create a mock LeicaStand widget for testing."""
        widget = QtWidgets.QWidget()
        qtbot.addWidget(widget)
        return widget
        
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
    
    def test_leica_toggle_mode_registered(self, manager, mock_leica_widget):
        """Test that leica.toggleMode action can be registered."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        catalog = manager.getAllActions()
        assert 'leica.toggleMode' in catalog
        action = catalog['leica.toggleMode']
        assert action.actionId == 'leica.toggleMode'
        assert action.displayName == 'Leica: toggle mode'
        assert action.defaultKeySequence == 'F2'
        assert action.scope == ShortcutScope.Window
        assert action.callback == callback
        assert action.owner == mock_leica_widget
        
    def test_default_key_f2_effective(self, manager, mock_leica_widget, mock_main_window, mock_shortcuts_menu):
        """Test that F2 is effective with no config override."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['leica.toggleMode'] == 'F2'
        
    def test_config_rebind_changes_key(self, manager, mock_leica_widget, mock_main_window, mock_shortcuts_menu):
        """Test that a config override rebinds the action."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        # Override with different key
        manager.loadConfigOverrides({'leica.toggleMode': 'F3'})
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['leica.toggleMode'] == 'F3'
        
    def test_config_null_disables_action(self, manager, mock_leica_widget, mock_main_window, mock_shortcuts_menu):
        """Test that a null config value disables the action."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        # Disable with null
        manager.loadConfigOverrides({'leica.toggleMode': None})
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert 'leica.toggleMode' not in bindings
        
    def test_build_creates_qt_shortcut(self, manager, mock_leica_widget, mock_main_window, mock_shortcuts_menu):
        """Test that build() creates a Qt shortcut that works."""
        callback_called = []
        
        def callback():
            callback_called.append(True)
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        # Verify the shortcut was added to the menu
        actions = mock_shortcuts_menu.actions()
        assert any(a.text() == 'Leica: toggle mode' for a in actions)
        
        # Find our action
        leica_action = next(a for a in actions if a.text() == 'Leica: toggle mode')
        assert leica_action.shortcut().toString() == 'F2'
        
        # Trigger the action and verify callback is called
        leica_action.trigger()
        assert len(callback_called) == 1
        
    def test_no_old_qshortcut_leftover(self):
        """Test that the old _init_shortcuts and _toggleModeShortcut are removed.
        
        This is a sanity check that verifies the migration was completed correctly.
        """
        from imswitch.imcontrol.controller.controllers import LeicaStandController
        
        # Check that _init_shortcuts method doesn't exist
        assert not hasattr(LeicaStandController, '_init_shortcuts'), \
            "LeicaStandController should not have _init_shortcuts method after Phase 3b migration"
        
        # Check the source code doesn't contain the old pattern
        import inspect
        source = inspect.getsource(LeicaStandController)
        assert '_toggleModeShortcut' not in source, \
            "LeicaStandController source should not contain _toggleModeShortcut after Phase 3b migration"
        assert 'QShortcut' not in source, \
            "LeicaStandController source should not contain QShortcut after Phase 3b migration"
        
    def test_owner_is_widget_not_controller(self, manager, mock_leica_widget):
        """Test that the owner is the widget, ensuring proper window context."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        catalog = manager.getAllActions()
        action = catalog['leica.toggleMode']
        
        # Verify the owner is the widget, not a controller
        assert action.owner == mock_leica_widget
        assert isinstance(action.owner, QtWidgets.QWidget)
        
    def test_multiple_rebinds(self, manager, mock_leica_widget, mock_main_window, mock_shortcuts_menu):
        """Test multiple sequential config rebinds."""
        callback = Mock()
        
        manager.registerAction(
            actionId='leica.toggleMode',
            displayName='Leica: toggle mode',
            callback=callback,
            defaultKeySequence='F2',
            scope=ShortcutScope.Window,
            owner=mock_leica_widget
        )
        
        # First rebind
        manager.loadConfigOverrides({'leica.toggleMode': 'F3'})
        manager.computeEffectiveBindings()
        assert manager.getEffectiveBindings()['leica.toggleMode'] == 'F3'
        
        # Second rebind (reset to default by not specifying in config)
        manager.loadConfigOverrides({})
        manager.computeEffectiveBindings()
        assert manager.getEffectiveBindings()['leica.toggleMode'] == 'F2'
        
        # Third rebind to null (disable)
        manager.loadConfigOverrides({'leica.toggleMode': None})
        manager.computeEffectiveBindings()
        assert 'leica.toggleMode' not in manager.getEffectiveBindings()


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
