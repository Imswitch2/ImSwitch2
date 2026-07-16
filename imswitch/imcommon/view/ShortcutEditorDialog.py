from typing import Callable, Dict, List, Optional, Union
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.controller.ShortcutManager import ShortcutManager


class ShortcutEditorDialog(QtWidgets.QDialog):
    """Dialog for viewing and editing keyboard shortcut bindings.

    Module-agnostic: where the overrides are persisted is up to the caller via
    ``persistCallback(shortcutsMap)`` — imcontrol saves into the setup JSON,
    ImProcess into its own shortcuts file. Without a callback, OK applies the
    changes live for this session only.
    """

    def __init__(
        self,
        parent,
        shortcutManager: ShortcutManager,
        persistCallback: Optional[Callable[[Dict], None]] = None,
    ):
        super().__init__(parent)
        self._manager = shortcutManager
        self._persistCallback = persistCallback
        self._pendingChanges = {}  # actionId -> keySequence | None
        self._originalBindings = {}  # actionId -> keySequence | None (for Cancel)
        
        self.setWindowTitle('Configure Keyboard Shortcuts')
        self.setModal(True)
        self.resize(900, 600)
        
        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        
        # Instructions
        instructions = QtWidgets.QLabel(
            'Edit keyboard shortcuts below. Changes are applied live but only '
            'persisted when you click OK. Mode shortcuts (mode.*) are managed in '
            'the Setup Modes widget and shown here as read-only.'
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # Table
        self._table = QtWidgets.QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels([
            'Area', 'Action ID', 'Display Name', 'Default Key', 
            'Current Key', 'Status', 'Edit'
        ])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table)
        
        # Conflict warning label
        self._conflictLabel = QtWidgets.QLabel()
        self._conflictLabel.setStyleSheet('color: red; font-weight: bold;')
        self._conflictLabel.setWordWrap(True)
        self._conflictLabel.setVisible(False)
        layout.addWidget(self._conflictLabel)
        
        # Buttons
        buttonLayout = QtWidgets.QHBoxLayout()
        
        self._applyButton = QtWidgets.QPushButton('Apply')
        self._applyButton.setToolTip('Apply changes immediately (live rebind)')
        self._applyButton.clicked.connect(self._onApply)
        buttonLayout.addWidget(self._applyButton)
        
        buttonLayout.addStretch()
        
        self._okButton = QtWidgets.QPushButton('OK')
        self._okButton.setToolTip('Apply and persist changes to setup config, then close')
        self._okButton.clicked.connect(self._onOk)
        buttonLayout.addWidget(self._okButton)
        
        self._cancelButton = QtWidgets.QPushButton('Cancel')
        self._cancelButton.setToolTip('Discard changes and restore previous bindings')
        self._cancelButton.clicked.connect(self._onCancel)
        buttonLayout.addWidget(self._cancelButton)
        
        layout.addLayout(buttonLayout)
        
        self._populateTable()
        
    def _populateTable(self):
        """Populate table with all actions from the manager."""
        allActions = self._manager.getAllActions()
        effectiveBindings = self._manager.getEffectiveBindings()
        
        # Store original bindings for Cancel
        self._originalBindings = effectiveBindings.copy()
        
        # Sort by actionId for grouping
        sortedActions = sorted(allActions.items(), key=lambda x: x[0])
        
        self._table.setRowCount(len(sortedActions))
        self._table.setSortingEnabled(False)
        
        for row, (actionId, action) in enumerate(sortedActions):
            # Area (prefix before first dot)
            area = actionId.split('.')[0] if '.' in actionId else 'other'
            areaItem = QtWidgets.QTableWidgetItem(area)
            areaItem.setFlags(areaItem.flags() & ~QtCore.Qt.ItemIsEditable)
            self._table.setItem(row, 0, areaItem)
            
            # Action ID
            idItem = QtWidgets.QTableWidgetItem(actionId)
            idItem.setFlags(idItem.flags() & ~QtCore.Qt.ItemIsEditable)
            idItem.setData(QtCore.Qt.UserRole, actionId)
            self._table.setItem(row, 1, idItem)
            
            # Display Name
            nameItem = QtWidgets.QTableWidgetItem(action.displayName or actionId)
            nameItem.setFlags(nameItem.flags() & ~QtCore.Qt.ItemIsEditable)
            self._table.setItem(row, 2, nameItem)
            
            # Default Key
            defaultKey = self._formatKeySequence(action.defaultKeySequence)
            defaultItem = QtWidgets.QTableWidgetItem(defaultKey)
            defaultItem.setFlags(defaultItem.flags() & ~QtCore.Qt.ItemIsEditable)
            self._table.setItem(row, 3, defaultItem)
            
            # Current Key
            currentKey = self._formatKeySequence(effectiveBindings.get(actionId))
            currentItem = QtWidgets.QTableWidgetItem(currentKey)
            currentItem.setFlags(currentItem.flags() & ~QtCore.Qt.ItemIsEditable)
            self._table.setItem(row, 4, currentItem)
            
            # Status
            status = self._computeStatus(actionId, action.defaultKeySequence, effectiveBindings)
            statusItem = QtWidgets.QTableWidgetItem(status)
            statusItem.setFlags(statusItem.flags() & ~QtCore.Qt.ItemIsEditable)
            self._table.setItem(row, 5, statusItem)
            
            # Edit controls
            editWidget = self._createEditControls(actionId, currentKey, action)
            self._table.setCellWidget(row, 6, editWidget)
        
        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()
        
    def _createEditControls(self, actionId: str, currentKey: str, action) -> QtWidgets.QWidget:
        """Create edit controls for a row."""
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(2, 2, 2, 2)
        container.setLayout(layout)
        
        # Mode shortcuts are read-only
        isMode = actionId.startswith('mode.')
        
        if hasattr(QtWidgets, 'QKeySequenceEdit'):
            editor = QtWidgets.QKeySequenceEdit()
            if currentKey and currentKey != '(none)':
                editor.setKeySequence(QtGui.QKeySequence(currentKey))
            editor.setEnabled(not isMode)
            editor.keySequenceChanged.connect(
                lambda seq, aid=actionId: self._onKeySequenceChanged(aid, seq)
            )
        else:
            editor = QtWidgets.QLineEdit(currentKey if currentKey != '(none)' else '')
            editor.setPlaceholderText('Ctrl+X')
            editor.setEnabled(not isMode)
            editor.textChanged.connect(
                lambda text, aid=actionId: self._onKeySequenceChangedText(aid, text)
            )
        
        layout.addWidget(editor, stretch=1)
        
        # Reset button
        resetBtn = QtWidgets.QPushButton('Reset')
        resetBtn.setToolTip('Reset to code default')
        resetBtn.setEnabled(not isMode)
        resetBtn.clicked.connect(lambda checked, aid=actionId: self._onReset(aid))
        layout.addWidget(resetBtn)
        
        # Disable button
        disableBtn = QtWidgets.QPushButton('Disable')
        disableBtn.setToolTip('Disable this shortcut')
        disableBtn.setEnabled(not isMode)
        disableBtn.clicked.connect(lambda checked, aid=actionId: self._onDisable(aid))
        layout.addWidget(disableBtn)
        
        if isMode:
            modeNote = QtWidgets.QLabel('(Setup Modes)')
            modeNote.setStyleSheet('color: gray; font-size: 10px;')
            layout.addWidget(modeNote)
        
        return container
    
    def _formatKeySequence(self, keySeq: Union[str, List[str], None]) -> str:
        """Format key sequence(s) for display."""
        if keySeq is None:
            return '(none)'
        elif isinstance(keySeq, list):
            return ', '.join(keySeq) if keySeq else '(none)'
        else:
            return keySeq
    
    def _computeStatus(self, actionId: str, defaultKey, effectiveBindings: Dict) -> str:
        """Compute status string for an action."""
        effective = effectiveBindings.get(actionId)
        
        if effective is None:
            if defaultKey is None:
                return 'Unbound (default)'
            else:
                return 'Disabled'
        elif defaultKey is None:
            return 'Custom'
        elif self._normalizeForComparison(effective) == self._normalizeForComparison(defaultKey):
            return 'Default'
        else:
            return 'Overridden'
    
    def _normalizeForComparison(self, keySeq) -> str:
        """Normalize key sequence for comparison."""
        if keySeq is None:
            return ''
        elif isinstance(keySeq, list):
            return ','.join(sorted(keySeq))
        else:
            return keySeq
    
    def _onKeySequenceChanged(self, actionId: str, sequence: QtGui.QKeySequence):
        """Handle key sequence change from QKeySequenceEdit."""
        seqStr = sequence.toString().strip()
        if not seqStr:
            seqStr = None
        self._pendingChanges[actionId] = seqStr
        self._updateConflicts()
        self._updateRowDisplay(actionId)
    
    def _onKeySequenceChangedText(self, actionId: str, text: str):
        """Handle key sequence change from QLineEdit."""
        text = text.strip()
        if not text:
            text = None
        self._pendingChanges[actionId] = text
        self._updateConflicts()
        self._updateRowDisplay(actionId)
    
    def _onReset(self, actionId: str):
        """Reset action to code default."""
        self._pendingChanges[actionId] = '__RESET__'
        self._updateConflicts()
        self._updateRowDisplay(actionId)
    
    def _onDisable(self, actionId: str):
        """Disable action."""
        self._pendingChanges[actionId] = None
        self._updateConflicts()
        self._updateRowDisplay(actionId)
    
    def _updateRowDisplay(self, actionId: str):
        """Update the display of a specific row after changes."""
        for row in range(self._table.rowCount()):
            idItem = self._table.item(row, 1)
            if idItem and idItem.data(QtCore.Qt.UserRole) == actionId:
                # Compute effective binding with pending changes
                effectiveBindings = self._manager.getEffectiveBindings()
                allActions = self._manager.getAllActions()
                action = allActions.get(actionId)
                
                if actionId in self._pendingChanges:
                    change = self._pendingChanges[actionId]
                    if change == '__RESET__':
                        newKey = action.defaultKeySequence if action else None
                    else:
                        newKey = change
                else:
                    newKey = effectiveBindings.get(actionId)
                
                # Update Current Key column
                currentItem = self._table.item(row, 4)
                if currentItem:
                    currentItem.setText(self._formatKeySequence(newKey))
                
                # Update Status column
                statusItem = self._table.item(row, 5)
                if statusItem and action:
                    if actionId in self._pendingChanges:
                        change = self._pendingChanges[actionId]
                        if change == '__RESET__':
                            status = 'Default (pending)'
                        elif change is None:
                            status = 'Disabled (pending)'
                        else:
                            status = 'Modified (pending)'
                    else:
                        status = self._computeStatus(actionId, action.defaultKeySequence, effectiveBindings)
                    statusItem.setText(status)
                
                break
    
    def _updateConflicts(self):
        """Update conflict display based on pending changes."""
        # Build proposed bindings (original + pending)
        proposedBindings = self._originalBindings.copy()
        
        for actionId, change in self._pendingChanges.items():
            if change == '__RESET__':
                # Reset means use code default
                action = self._manager.getActionMetadata(actionId)
                if action:
                    proposedBindings[actionId] = action.defaultKeySequence
                else:
                    proposedBindings.pop(actionId, None)
            elif change is None:
                # Disable
                proposedBindings.pop(actionId, None)
            else:
                proposedBindings[actionId] = change
        
        # Get conflicts
        conflicts = self._manager.getConflicts(proposedBindings)
        
        if conflicts:
            conflictMsgs = []
            seen = set()
            for actionId, conflictingIds in conflicts.items():
                key = tuple(sorted([actionId] + conflictingIds))
                if key not in seen:
                    seen.add(key)
                    allIds = ', '.join([actionId] + conflictingIds)
                    keySeq = proposedBindings.get(actionId)
                    conflictMsgs.append(f'"{self._formatKeySequence(keySeq)}": {allIds}')
            
            self._conflictLabel.setText(
                'WARNING: Conflicts detected:\n' + '\n'.join(conflictMsgs)
            )
            self._conflictLabel.setVisible(True)
            
            # Highlight conflicted rows
            for row in range(self._table.rowCount()):
                idItem = self._table.item(row, 1)
                if idItem:
                    actionId = idItem.data(QtCore.Qt.UserRole)
                    if actionId in conflicts:
                        for col in range(self._table.columnCount()):
                            item = self._table.item(row, col)
                            if item:
                                item.setBackground(QtGui.QColor(255, 200, 200))
                    else:
                        for col in range(self._table.columnCount()):
                            item = self._table.item(row, col)
                            if item:
                                item.setBackground(QtGui.QColor(255, 255, 255))
        else:
            self._conflictLabel.setVisible(False)
            # Clear highlighting
            for row in range(self._table.rowCount()):
                for col in range(self._table.columnCount()):
                    item = self._table.item(row, col)
                    if item:
                        item.setBackground(QtGui.QColor(255, 255, 255))
    
    def _onApply(self):
        """Apply pending changes live via manager.rebind/reset."""
        for actionId, change in self._pendingChanges.items():
            if change == '__RESET__':
                self._manager.reset(actionId)
            else:
                self._manager.rebind(actionId, change)
        
        # Rebuild shortcuts
        self._manager.computeEffectiveBindings()
        self._manager.build(self.parent().shortcutsMenu, self.parent())
        
        # Update original bindings and clear pending
        self._originalBindings = self._manager.getEffectiveBindings()
        self._pendingChanges.clear()
        
        # Refresh display
        self._populateTable()
    
    def _onOk(self):
        """Apply changes and persist them via the module's callback."""
        # Apply live first
        self._onApply()

        # Build shortcuts map and hand it to the module-specific persistence.
        if self._persistCallback is not None:
            self._persistCallback(self._buildShortcutsMap())

        self.accept()
    
    def _onCancel(self):
        """Cancel changes and restore original bindings."""
        # Restore original bindings
        for actionId, keySeq in self._originalBindings.items():
            self._manager.rebind(actionId, keySeq)
        
        # Reset actions that were unbound originally
        effectiveBindings = self._manager.getEffectiveBindings()
        for actionId in effectiveBindings:
            if actionId not in self._originalBindings:
                self._manager.reset(actionId)
        
        # Rebuild
        self._manager.computeEffectiveBindings()
        self._manager.build(self.parent().shortcutsMenu, self.parent())
        
        self.reject()
    
    def _buildShortcutsMap(self) -> Dict[str, Union[str, List[str], None]]:
        """Build shortcuts map for setup config persistence.
        
        Only includes explicit overrides (different from code default) and disabled actions.
        Mode shortcuts are NOT included (they live in per-mode JSON).
        Reset actions are removed from the map.
        """
        shortcutsMap = {}
        allActions = self._manager.getAllActions()
        effectiveBindings = self._manager.getEffectiveBindings()
        
        for actionId, action in allActions.items():
            # Skip mode shortcuts
            if actionId.startswith('mode.'):
                continue
            
            effective = effectiveBindings.get(actionId)
            default = action.defaultKeySequence
            
            # If disabled (None) and had a default, persist as null
            if effective is None and default is not None:
                shortcutsMap[actionId] = None
            # If different from default, persist
            elif effective is not None and self._normalizeForComparison(effective) != self._normalizeForComparison(default):
                shortcutsMap[actionId] = effective
            # Otherwise, not in map (uses code default)
        
        return shortcutsMap


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
