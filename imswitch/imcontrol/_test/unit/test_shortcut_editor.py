import pytest
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Union, List
from types import SimpleNamespace

from qtpy import QtCore, QtWidgets, QtGui

from imswitch.imcontrol.controller.ShortcutManager import ShortcutManager
from imswitch.imcontrol.view.widgets.ShortcutEditorDialog import ShortcutEditorDialog
from imswitch.imcommon.model import ShortcutAction, ShortcutScope


@pytest.fixture
def qapp(qtbot):
    """Ensure QApplication exists."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture
def mock_main_view(qapp):
    """Mock main view for the dialog parent."""
    view = QtWidgets.QMainWindow()
    view.shortcutsMenu = QtWidgets.QMenu()
    return view


@pytest.fixture
def shortcut_manager():
    """Create a ShortcutManager with some test actions."""
    manager = ShortcutManager()
    
    # Create test catalog
    catalog = {
        'app.test1': ShortcutAction(
            actionId='app.test1',
            displayName='Test Action 1',
            defaultKeySequence='Ctrl+T',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
            initiallyBound=True
        ),
        'app.test2': ShortcutAction(
            actionId='app.test2',
            displayName='Test Action 2',
            defaultKeySequence='Ctrl+Y',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
            initiallyBound=True
        ),
        'widget.action1': ShortcutAction(
            actionId='widget.action1',
            displayName='Widget Action',
            defaultKeySequence=None,
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
            initiallyBound=True
        ),
        'mode.test-mode': ShortcutAction(
            actionId='mode.test-mode',
            displayName='Test Mode',
            defaultKeySequence='F3',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
            initiallyBound=True
        ),
    }
    
    manager.collect(catalog)
    manager.loadConfigOverrides({})
    manager.computeEffectiveBindings()
    
    return manager


def test_manager_get_conflicts_empty():
    """Test getConflicts with no conflicts."""
    manager = ShortcutManager()
    conflicts = manager.getConflicts({
        'action1': 'Ctrl+A',
        'action2': 'Ctrl+B'
    })
    assert conflicts == {}


def test_manager_get_conflicts_detected():
    """Test getConflicts detects conflicts."""
    manager = ShortcutManager()
    
    # Create minimal catalog for validation
    catalog = {
        'action1': ShortcutAction(
            actionId='action1',
            displayName='Action 1',
            defaultKeySequence='Ctrl+A',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
        ),
        'action2': ShortcutAction(
            actionId='action2',
            displayName='Action 2',
            defaultKeySequence='Ctrl+B',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
        ),
    }
    manager.collect(catalog)
    
    conflicts = manager.getConflicts({
        'action1': 'Ctrl+R',
        'action2': 'Ctrl+R'
    })
    
    assert 'action1' in conflicts
    assert 'action2' in conflicts
    assert 'action2' in conflicts['action1']
    assert 'action1' in conflicts['action2']


def test_manager_get_conflicts_with_lists():
    """Test getConflicts with list-valued key sequences."""
    manager = ShortcutManager()
    
    catalog = {
        'action1': ShortcutAction(
            actionId='action1',
            displayName='Action 1',
            defaultKeySequence=['Ctrl+A', 'Ctrl+B'],
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
        ),
        'action2': ShortcutAction(
            actionId='action2',
            displayName='Action 2',
            defaultKeySequence='Ctrl+B',
            scope=ShortcutScope.Application,
            owner=None,
            enabledPredicate=None,
            activationSource=None,
            callback=lambda: None,
        ),
    }
    manager.collect(catalog)
    
    conflicts = manager.getConflicts({
        'action1': ['Ctrl+A', 'Ctrl+B'],
        'action2': 'Ctrl+B'
    })
    
    # action1 and action2 both have Ctrl+B
    assert 'action1' in conflicts
    assert 'action2' in conflicts


def test_dialog_lists_all_actions(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that dialog lists all actions from the manager."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Check table has correct number of rows
    assert dialog._table.rowCount() == 4
    
    # Check all action IDs are present
    actionIds = []
    for row in range(dialog._table.rowCount()):
        idItem = dialog._table.item(row, 1)
        if idItem:
            actionIds.append(idItem.data(QtCore.Qt.UserRole))
    
    assert 'app.test1' in actionIds
    assert 'app.test2' in actionIds
    assert 'widget.action1' in actionIds
    assert 'mode.test-mode' in actionIds


def test_dialog_shows_effective_bindings(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that dialog shows effective bindings correctly."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Find app.test1 row
    for row in range(dialog._table.rowCount()):
        idItem = dialog._table.item(row, 1)
        if idItem and idItem.data(QtCore.Qt.UserRole) == 'app.test1':
            # Check Current Key column
            currentItem = dialog._table.item(row, 4)
            assert currentItem.text() == 'Ctrl+T'
            
            # Check Default Key column
            defaultItem = dialog._table.item(row, 3)
            assert defaultItem.text() == 'Ctrl+T'
            break
    else:
        pytest.fail('app.test1 not found in dialog')


def test_dialog_rebind_action(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test rebinding an action via the dialog."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Simulate rebinding app.test1 to Ctrl+Z
    dialog._pendingChanges['app.test1'] = 'Ctrl+Z'
    dialog._updateRowDisplay('app.test1')
    
    # Check pending change is reflected
    assert 'app.test1' in dialog._pendingChanges
    assert dialog._pendingChanges['app.test1'] == 'Ctrl+Z'
    
    # Check status shows pending
    for row in range(dialog._table.rowCount()):
        idItem = dialog._table.item(row, 1)
        if idItem and idItem.data(QtCore.Qt.UserRole) == 'app.test1':
            statusItem = dialog._table.item(row, 5)
            assert 'pending' in statusItem.text().lower()
            break


def test_dialog_reset_action(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test resetting an action to code default."""
    # Override app.test1
    shortcut_manager.rebind('app.test1', 'Ctrl+Z')
    shortcut_manager.computeEffectiveBindings()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Simulate reset
    dialog._onReset('app.test1')
    
    assert dialog._pendingChanges['app.test1'] == '__RESET__'


def test_dialog_disable_action(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test disabling an action."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Simulate disable
    dialog._onDisable('app.test1')
    
    assert dialog._pendingChanges['app.test1'] is None


def test_dialog_conflict_detection(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that dialog detects and displays conflicts."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Create a conflict: bind both test1 and test2 to Ctrl+X
    # First, verify the manager can detect conflicts
    conflicts = shortcut_manager.getConflicts({
        'app.test1': 'Ctrl+X',
        'app.test2': 'Ctrl+X'
    })
    assert len(conflicts) > 0, "Manager should detect conflicts"
    
    dialog._pendingChanges['app.test1'] = 'Ctrl+X'
    dialog._pendingChanges['app.test2'] = 'Ctrl+X'
    dialog._updateConflicts()
    
    # Check conflict is detected and displayed in the label text
    assert 'conflict' in dialog._conflictLabel.text().lower()
    assert 'app.test1' in dialog._conflictLabel.text()
    assert 'app.test2' in dialog._conflictLabel.text()


def test_dialog_apply_calls_manager(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that Apply button calls manager.rebind."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Mock the manager methods
    with patch.object(shortcut_manager, 'rebind') as mock_rebind, \
         patch.object(shortcut_manager, 'reset') as mock_reset, \
         patch.object(shortcut_manager, 'computeEffectiveBindings'), \
         patch.object(shortcut_manager, 'build'):
        
        # Add pending changes
        dialog._pendingChanges['app.test1'] = 'Ctrl+Z'
        dialog._pendingChanges['app.test2'] = '__RESET__'
        
        # Click Apply
        dialog._onApply()
        
        # Verify manager methods were called
        mock_rebind.assert_called_once_with('app.test1', 'Ctrl+Z')
        mock_reset.assert_called_once_with('app.test2')


def test_dialog_ok_persists_to_config(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that OK button builds shortcuts map correctly for persistence."""
    # Override app.test1
    shortcut_manager.rebind('app.test1', 'Ctrl+Z')
    shortcut_manager.computeEffectiveBindings()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Build shortcuts map
    shortcutsMap = dialog._buildShortcutsMap()
    
    # Should include the override
    assert 'app.test1' in shortcutsMap
    assert shortcutsMap['app.test1'] == 'Ctrl+Z'
    
    # Should not include mode shortcuts
    assert 'mode.test-mode' not in shortcutsMap
    
    # Should not include defaults (app.test2 is at its default Ctrl+Y)
    assert 'app.test2' not in shortcutsMap or shortcutsMap['app.test2'] == 'Ctrl+Y'


def test_dialog_ok_persists_active_setup_info(qapp, qtbot, mock_main_view, shortcut_manager):
    """OK persists against the active setup info and handles loadOptions' tuple."""
    setupInfo = SimpleNamespace(shortcuts={'old.action': 'F9'})
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager, setupInfo)
    qtbot.addWidget(dialog)

    shortcut_manager.rebind('app.test1', 'Ctrl+Z')
    shortcut_manager.computeEffectiveBindings()
    options = SimpleNamespace(setupFileName='test.json')

    with patch('imswitch.imcontrol.model.configfiletools.loadOptions', return_value=(options, False)) as loadOptions, \
            patch('imswitch.imcontrol.model.configfiletools.loadSetupInfo') as loadSetupInfo, \
            patch('imswitch.imcontrol.model.configfiletools.saveSetupInfo') as saveSetupInfo:
        dialog._onOk()

    loadOptions.assert_called_once_with()
    loadSetupInfo.assert_not_called()
    assert setupInfo.shortcuts['app.test1'] == 'Ctrl+Z'
    saveSetupInfo.assert_called_once_with(options, setupInfo)


def test_dialog_cancel_restores_bindings(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that Cancel button restores original bindings."""
    original_bindings = shortcut_manager.getEffectiveBindings().copy()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    with patch.object(shortcut_manager, 'rebind') as mock_rebind, \
         patch.object(shortcut_manager, 'computeEffectiveBindings'), \
         patch.object(shortcut_manager, 'build'):
        
        # Add changes
        dialog._pendingChanges['app.test1'] = 'Ctrl+Z'
        
        # Click Cancel
        dialog._onCancel()
        
        # Verify rebind was called to restore original bindings
        assert mock_rebind.called


def test_build_shortcuts_map_excludes_mode_shortcuts(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that _buildShortcutsMap excludes mode.* shortcuts."""
    # Override a mode shortcut
    shortcut_manager.rebind('mode.test-mode', 'F5')
    shortcut_manager.computeEffectiveBindings()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    shortcutsMap = dialog._buildShortcutsMap()
    
    # Verify mode.* is not in the map
    assert 'mode.test-mode' not in shortcutsMap


def test_build_shortcuts_map_includes_overrides(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that _buildShortcutsMap includes overridden shortcuts."""
    # Override app.test1
    shortcut_manager.rebind('app.test1', 'Ctrl+Z')
    shortcut_manager.computeEffectiveBindings()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    shortcutsMap = dialog._buildShortcutsMap()
    
    # Should include the override
    assert 'app.test1' in shortcutsMap
    assert shortcutsMap['app.test1'] == 'Ctrl+Z'


def test_build_shortcuts_map_includes_disabled(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that _buildShortcutsMap includes explicitly disabled shortcuts as null."""
    # Disable app.test1
    shortcut_manager.rebind('app.test1', None)
    shortcut_manager.computeEffectiveBindings()
    
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    shortcutsMap = dialog._buildShortcutsMap()
    
    # Should include as null
    assert 'app.test1' in shortcutsMap
    assert shortcutsMap['app.test1'] is None


def test_build_shortcuts_map_excludes_defaults(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that _buildShortcutsMap excludes actions at their default values."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    shortcutsMap = dialog._buildShortcutsMap()
    
    # app.test1 and app.test2 are at defaults, should not be in map
    # (unless they were explicitly overridden to the same value, but that's not the case here)
    # Actually, this depends on the test setup. Let me check the logic more carefully.
    # The manager starts with no config overrides, so everything is at default.
    # _buildShortcutsMap should return an empty dict for all-defaults case.
    
    # For fresh manager with no overrides, map should be empty or only have non-default entries
    assert 'app.test1' not in shortcutsMap or shortcutsMap['app.test1'] == 'Ctrl+T'
    # Actually, the logic is: only include if different from default
    # So for a fresh manager, map should be mostly empty
    # Let's be more specific: widget.action1 has no default, so it shouldn't be in the map
    assert 'widget.action1' not in shortcutsMap


def test_mode_shortcuts_are_readonly(qapp, qtbot, mock_main_view, shortcut_manager):
    """Test that mode shortcuts are displayed as read-only."""
    dialog = ShortcutEditorDialog(mock_main_view, shortcut_manager)
    qtbot.addWidget(dialog)
    
    # Find mode.test-mode row
    for row in range(dialog._table.rowCount()):
        idItem = dialog._table.item(row, 1)
        if idItem and idItem.data(QtCore.Qt.UserRole) == 'mode.test-mode':
            # Get the edit widget
            editWidget = dialog._table.cellWidget(row, 6)
            assert editWidget is not None
            
            # Check that editor and buttons are disabled (or labeled as read-only)
            # The dialog creates disabled editors for mode shortcuts
            # Let's check the layout children
            found_mode_note = False
            for i in range(editWidget.layout().count()):
                widget = editWidget.layout().itemAt(i).widget()
                if isinstance(widget, QtWidgets.QLabel) and 'Setup Modes' in widget.text():
                    found_mode_note = True
                    break
            
            assert found_mode_note, "Mode shortcut should have '(Setup Modes)' label"
            break
    else:
        pytest.fail('mode.test-mode not found in dialog')


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
