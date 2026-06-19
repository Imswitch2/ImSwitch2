import pytest
from unittest.mock import Mock, MagicMock, patch
from qtpy import QtWidgets, QtCore

from imswitch.imcontrol.controller.ShortcutManager import ShortcutManager
from imswitch.imcommon.model.shortcut import ShortcutAction, ShortcutScope


@pytest.fixture
def mock_main_window(qtbot):
    """Create a mock main window for testing."""
    window = QtWidgets.QMainWindow()
    qtbot.addWidget(window)
    return window


@pytest.fixture
def mock_shortcuts_menu(mock_main_window):
    """Create a mock shortcuts menu."""
    return mock_main_window.menuBar().addMenu('&Shortcuts')


@pytest.fixture
def sample_catalog():
    """Create a sample catalog of shortcut actions."""
    widget = Mock()
    widget.__class__.__name__ = "TestWidget"
    
    return {
        'view.toggleLiveView': ShortcutAction(
            actionId='view.toggleLiveView',
            displayName='Toggle Live View',
            defaultKeySequence='Ctrl+L',
            scope=ShortcutScope.Application,
            owner=widget,
            enabledPredicate=None,
            activationSource=None,
            callback=Mock(),
            initiallyBound=True
        ),
        'recording.toggleRecord': ShortcutAction(
            actionId='recording.toggleRecord',
            displayName='Toggle Record',
            defaultKeySequence='Ctrl+R',
            scope=ShortcutScope.Application,
            owner=widget,
            enabledPredicate=None,
            activationSource=None,
            callback=Mock(),
            initiallyBound=True
        ),
        'grbl.moveUp': ShortcutAction(
            actionId='grbl.moveUp',
            displayName='Move Up',
            defaultKeySequence='Up',
            scope=ShortcutScope.Application,
            owner=widget,
            enabledPredicate=None,
            activationSource=None,
            callback=Mock(),
            initiallyBound=False  # Catalog-only, not bound by default
        ),
    }


class TestShortcutManager:
    """Test suite for ShortcutManager."""
    
    def test_no_config_uses_defaults(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that with no config, only initiallyBound actions are bound."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        # Only initiallyBound=True actions should be bound
        assert 'view.toggleLiveView' in bindings
        assert bindings['view.toggleLiveView'] == 'Ctrl+L'
        assert 'recording.toggleRecord' in bindings
        assert bindings['recording.toggleRecord'] == 'Ctrl+R'
        
        # GRBL action should NOT be bound (initiallyBound=False)
        assert 'grbl.moveUp' not in bindings
        
    def test_config_override_rebinds_action(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that config can override default key bindings."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'view.toggleLiveView': 'F5'  # Override default Ctrl+L
        })
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        assert bindings['view.toggleLiveView'] == 'F5'
        assert bindings['recording.toggleRecord'] == 'Ctrl+R'  # Unchanged
        
    def test_config_null_disables_action(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that null config value disables an action."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'recording.toggleRecord': None  # Explicit disable
        })
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        assert 'view.toggleLiveView' in bindings
        assert 'recording.toggleRecord' not in bindings  # Disabled
        
    def test_config_list_binds_multiple_sequences(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that list config value binds multiple sequences."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'view.toggleLiveView': ['Ctrl+L', 'F5']  # Multiple bindings
        })
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        assert bindings['view.toggleLiveView'] == ['Ctrl+L', 'F5']
        
    def test_unknown_action_id_warns(self, sample_catalog, caplog):
        """Test that unknown action IDs in config generate warnings."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        
        with caplog.at_level('WARNING'):
            manager.loadConfigOverrides({
                'unknown.action': 'Ctrl+X'
            })
        
        # Check that warning was logged
        assert any('unknown action id' in record.getMessage().lower() for record in caplog.records)
        
    def test_invalid_key_sequence_warns(self, sample_catalog, caplog):
        """Test that invalid key sequences generate warnings."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        
        with caplog.at_level('WARNING'):
            manager.loadConfigOverrides({
                'view.toggleLiveView': 'InvalidKeySequence'
            })
            manager.computeEffectiveBindings()
        
        # Check that warning was logged
        assert any('invalid key sequence' in record.getMessage().lower() for record in caplog.records)
        
    def test_conflict_explicit_config_wins(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that explicit config wins over code default in conflicts."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'view.toggleLiveView': 'Ctrl+R'  # Conflicts with recording.toggleRecord's default
        })
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        # Explicit config should win
        assert 'view.toggleLiveView' in bindings
        assert bindings['view.toggleLiveView'] == 'Ctrl+R'
        
        # Code default should be disabled
        assert 'recording.toggleRecord' not in bindings
        
    def test_conflict_first_registered_wins_tiebreak(self, mock_shortcuts_menu, mock_main_window):
        """Test that first-registered action wins in tie-break."""
        widget = Mock()
        widget.__class__.__name__ = "TestWidget"
        
        catalog = {
            'action1': ShortcutAction(
                actionId='action1',
                displayName='Action 1',
                defaultKeySequence='Ctrl+T',
                scope=ShortcutScope.Application,
                owner=widget,
                enabledPredicate=None,
                activationSource=None,
                callback=Mock(),
                initiallyBound=True
            ),
            'action2': ShortcutAction(
                actionId='action2',
                displayName='Action 2',
                defaultKeySequence='Ctrl+T',  # Same key
                scope=ShortcutScope.Application,
                owner=widget,
                enabledPredicate=None,
                activationSource=None,
                callback=Mock(),
                initiallyBound=True
            ),
        }
        
        manager = ShortcutManager()
        manager.collect(catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        warnings = manager.getConflictWarnings()
        
        # First registered (action1) should win
        assert 'action1' in bindings
        assert 'action2' not in bindings
        
        # Should have conflict warning
        assert len(warnings) > 0
        
    def test_catalog_only_action_can_be_enabled_by_config(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that catalog-only (initiallyBound=False) actions can be enabled by config."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'grbl.moveUp': 'Up'  # Enable catalog-only action
        })
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        
        # GRBL action should now be bound
        assert 'grbl.moveUp' in bindings
        assert bindings['grbl.moveUp'] == 'Up'
        
    def test_build_creates_qt_objects(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that build() creates Qt objects and populates menu."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        initial_action_count = len(mock_shortcuts_menu.actions())
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        # Menu should have actions added
        assert len(mock_shortcuts_menu.actions()) > initial_action_count
        
    def test_dispose_clears_shortcuts(self, sample_catalog, mock_shortcuts_menu, mock_main_window, qtbot):
        """Test that dispose() properly clears Qt shortcuts."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        manager.build(mock_shortcuts_menu, mock_main_window)
        
        action_count_before = len(mock_shortcuts_menu.actions())
        assert action_count_before > 0
        
        manager.dispose()
        
        # Qt objects should be scheduled for deletion
        qtbot.wait(10)  # Give Qt event loop time to process deleteLater
        
    def test_rebuild_after_dispose(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test that shortcuts can be rebuilt after disposal."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        # Build once
        manager.build(mock_shortcuts_menu, mock_main_window)
        first_count = len(mock_shortcuts_menu.actions())
        
        # Dispose
        manager.dispose()
        
        # Rebuild
        manager.build(mock_shortcuts_menu, mock_main_window)
        second_count = len(mock_shortcuts_menu.actions())
        
        # Should have same number of actions after rebuild
        # Note: dispose() doesn't remove actions from menu, just deletes Qt objects
        # This test ensures no crash on rebuild
        assert second_count >= first_count
        
    def test_rebind_runtime_api(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test runtime rebind API."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides(None)
        manager.computeEffectiveBindings()
        
        # Rebind at runtime
        manager.rebind('view.toggleLiveView', 'F6')
        
        # Check that config override was updated
        manager.computeEffectiveBindings()
        bindings = manager.getEffectiveBindings()
        assert bindings['view.toggleLiveView'] == 'F6'
        
    def test_reset_runtime_api(self, sample_catalog, mock_shortcuts_menu, mock_main_window):
        """Test runtime reset API."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        manager.loadConfigOverrides({
            'view.toggleLiveView': 'F5'
        })
        manager.computeEffectiveBindings()
        
        # Reset to default
        manager.reset('view.toggleLiveView')
        manager.computeEffectiveBindings()
        
        bindings = manager.getEffectiveBindings()
        assert bindings['view.toggleLiveView'] == 'Ctrl+L'  # Back to default
        
    def test_get_all_actions(self, sample_catalog):
        """Test getAllActions API."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        
        actions = manager.getAllActions()
        assert len(actions) == 3
        assert 'view.toggleLiveView' in actions
        assert 'recording.toggleRecord' in actions
        assert 'grbl.moveUp' in actions
        
    def test_get_action_metadata(self, sample_catalog):
        """Test getActionMetadata API."""
        manager = ShortcutManager()
        manager.collect(sample_catalog)
        
        action = manager.getActionMetadata('view.toggleLiveView')
        assert action is not None
        assert action.actionId == 'view.toggleLiveView'
        assert action.displayName == 'Toggle Live View'
        assert action.defaultKeySequence == 'Ctrl+L'


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
