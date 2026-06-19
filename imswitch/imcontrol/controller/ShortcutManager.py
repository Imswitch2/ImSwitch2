from typing import Dict, List, Optional, Union, Any, Callable
from qtpy import QtCore, QtWidgets, QtGui

from imswitch.imcommon.model import initLogger, ShortcutAction, ShortcutScope


# Axis -> (plus key, minus key) for the positioner jog alias expansion.
_POSITIONER_AXIS_KEY_MAP = {
    'X': ('Right', 'Left'),
    'Y': ('Up', 'Down'),
    'Z': ('Y', 'A'),
}


def computePositionerJogDefaults(positioners) -> Dict[str, Optional[str]]:
    """Compute default jog key sequences per positioner axis action ID.

    Pure function (no Qt / no hardware) implementing the Phase 3c
    ``shortcutModifier`` alias expansion so it is unit-testable and shared by the
    production registration path:

    - ``shortcutModifier == "ctrl"``       -> Ctrl+Right/Left/Up/Down/Y/A
    - ``shortcutModifier == "ctrl-shift"`` -> Ctrl+Shift+ equivalents
    - no ``shortcutModifier``               -> legacy first-come (the first
      positioner claiming an axis gets the Ctrl+Arrow set; later ones unbound)
    - unknown axis / not claimed            -> ``None`` (unbound by default)

    Args:
        positioners: mapping of positionerName -> object with ``axes`` and
            ``shortcutModifier`` attributes (e.g. PositionerInfo).

    Returns:
        ``{ "positioner.<name>.<axis>.plus"|".minus": defaultKey | None }``.
    """
    defaults: Dict[str, Optional[str]] = {}
    if not positioners:
        return defaults

    # First pass: legacy first-come claims (only positioners with no modifier).
    legacyClaimedAxes: Dict[str, str] = {}
    normalized = []
    for name, info in positioners.items():
        mod = (getattr(info, 'shortcutModifier', None) or '').strip().lower().replace('+', '-')
        normalized.append((name, info, mod))
        if not mod:
            for axis in info.axes:
                legacyClaimedAxes.setdefault(axis.upper(), name)

    for name, info, mod in normalized:
        for axis in info.axes:
            axisUpper = axis.upper()
            keys = _POSITIONER_AXIS_KEY_MAP.get(axisUpper)

            if mod == 'ctrl' and keys:
                plusKey, minusKey = f'Ctrl+{keys[0]}', f'Ctrl+{keys[1]}'
            elif mod in ('ctrl-shift', 'shift-ctrl') and keys:
                plusKey, minusKey = f'Ctrl+Shift+{keys[0]}', f'Ctrl+Shift+{keys[1]}'
            elif not mod and keys and legacyClaimedAxes.get(axisUpper) == name:
                plusKey, minusKey = f'Ctrl+{keys[0]}', f'Ctrl+{keys[1]}'
            else:
                plusKey, minusKey = None, None

            defaults[f'positioner.{name}.{axis}.plus'] = plusKey
            defaults[f'positioner.{name}.{axis}.minus'] = minusKey

    return defaults


class ShortcutManager:
    """Unified keyboard shortcut manager.
    
    Owns the complete shortcut lifecycle: collection, config merge, conflict
    detection, Qt object binding, disposal, and runtime API for rebinding.
    """
    
    def __init__(self):
        self.__logger = initLogger(self)
        self._catalog = {}  # actionId -> ShortcutAction
        self._configOverrides = {}  # actionId -> (keySequence | list[keySequence] | None)
        self._effectiveBindings = {}  # actionId -> (keySequence | list[keySequence] | None)
        self._qtObjects = {}  # actionId -> list of QShortcut or QAction
        self._conflictWarnings = []  # List of conflict messages
        
    def collect(self, catalog: Dict[str, ShortcutAction]) -> None:
        """Collect action catalog from decorated methods.
        
        Args:
            catalog: Dict mapping actionId to ShortcutAction (from generateShortcuts)
        """
        self._catalog = catalog.copy()
        self.__logger.debug(f'Collected {len(self._catalog)} shortcut actions')
        
    def registerAction(
        self,
        actionId: str,
        displayName: str,
        callback: Callable,
        defaultKeySequence: Union[str, List[str], None] = None,
        scope: ShortcutScope = ShortcutScope.Application,
        owner: Optional[QtCore.QObject] = None,
        enabledPredicate: Optional[Callable[[], bool]] = None,
        activationSource: Optional[Dict[str, Any]] = None,
        initiallyBound: bool = True
    ) -> None:
        """Register a non-decorator action directly with the manager.
        
        Used for menu actions and other shortcuts that cannot use the decorator.
        
        Args:
            actionId: Stable, namespaced action identifier
            displayName: Human-readable name for the Shortcuts menu
            callback: Function to call when the shortcut is activated
            defaultKeySequence: Default key(s) from code, or None if unbound by default
            scope: Shortcut scope (Application, Window, WidgetLocal, PressRelease)
            owner: Qt object for lifecycle (parent for the QShortcut/QAction)
            enabledPredicate: Optional function returning bool for action availability
            activationSource: Optional metadata passed to the callback
            initiallyBound: Whether the action should be bound by default
        """
        if actionId in self._catalog:
            raise RuntimeError(
                f"Duplicate shortcut actionId '{actionId}' detected. "
                f"Already registered by {self._catalog[actionId].owner.__class__.__name__}. "
                f"This is a programming error - each actionId must be unique."
            )
        
        action = ShortcutAction(
            actionId=actionId,
            displayName=displayName,
            defaultKeySequence=defaultKeySequence,
            scope=scope,
            owner=owner,
            enabledPredicate=enabledPredicate,
            activationSource=activationSource,
            callback=callback,
            initiallyBound=initiallyBound
        )
        
        self._catalog[actionId] = action
        self.__logger.debug(f'Registered action {actionId}')
        
    def loadConfigOverrides(self, shortcuts: Optional[Dict[str, Union[str, List[str], None]]]) -> None:
        """Load shortcuts configuration from setup config.
        
        Args:
            shortcuts: Optional dict from setup config 'shortcuts' section
        """
        if shortcuts is None:
            shortcuts = {}
            
        self._configOverrides = {}
        for actionId, keySeq in shortcuts.items():
            # Validate key sequences
            if keySeq is None:
                self._configOverrides[actionId] = None
            elif isinstance(keySeq, list):
                validated = []
                for seq in keySeq:
                    if self._validateKeySequence(seq):
                        validated.append(seq)
                if validated:
                    self._configOverrides[actionId] = validated
                else:
                    self.__logger.warning(f'All key sequences invalid for {actionId}, ignoring')
            elif isinstance(keySeq, str):
                if self._validateKeySequence(keySeq):
                    self._configOverrides[actionId] = keySeq
            else:
                self.__logger.warning(f'Invalid key sequence type for {actionId}: {type(keySeq)}')
                
        # Warn about unknown action IDs in config
        for actionId in self._configOverrides:
            if actionId not in self._catalog:
                self.__logger.warning(
                    f'Setup config references unknown action ID: {actionId}. '
                    f'This action will be ignored.'
                )
                
        self.__logger.debug(f'Loaded {len(self._configOverrides)} config overrides')
    
    def computeEffectiveBindings(self) -> None:
        """Compute effective bindings by merging code defaults with config.
        
        Merge order: code defaults < setup config overrides
        Handles conflict detection and priority resolution.
        """
        self._effectiveBindings = {}
        self._conflictWarnings = []
        
        # Start with code defaults for actions that should be initially bound
        for actionId, action in self._catalog.items():
            if action.initiallyBound and action.defaultKeySequence is not None:
                self._effectiveBindings[actionId] = action.defaultKeySequence
                
        # Apply config overrides
        for actionId, keySeq in self._configOverrides.items():
            if actionId in self._catalog:
                if keySeq is None:
                    # Explicit disable
                    self._effectiveBindings.pop(actionId, None)
                else:
                    self._effectiveBindings[actionId] = keySeq
                    
        # Detect and resolve conflicts
        self._resolveConflicts()
        
        self.__logger.debug(f'Computed {len(self._effectiveBindings)} effective bindings')
        
    def _resolveConflicts(self) -> None:
        """Detect key sequence conflicts and apply priority resolution.
        
        Priority order (highest to lowest):
        1. Explicit config binding
        2. Code default
        3. First-registered wins (tie-break)
        """
        # Build reverse map: keySequence -> list of (actionId, priority)
        keyToActions = {}
        
        for actionId, keySeq in self._effectiveBindings.items():
            sequences = [keySeq] if isinstance(keySeq, str) else keySeq
            
            # Determine priority
            if actionId in self._configOverrides and self._configOverrides[actionId] is not None:
                priority = 1  # Explicit config
            else:
                priority = 2  # Code default
                
            for seq in sequences:
                normalized = self._normalizeKeySequence(seq)
                if normalized not in keyToActions:
                    keyToActions[normalized] = []
                keyToActions[normalized].append((actionId, priority, seq))
                
        # Find and resolve conflicts
        actionsToDisable = set()
        
        for normalized, actions in keyToActions.items():
            if len(actions) > 1:
                # Sort by priority (lower number = higher priority), then by registration order
                actions.sort(key=lambda x: (x[1], list(self._catalog.keys()).index(x[0])))
                
                winner = actions[0]
                losers = actions[1:]
                
                for loser in losers:
                    actionId, priority, seq = loser
                    actionsToDisable.add(actionId)
                    
                    priorityNames = {1: 'explicit config', 2: 'code default'}
                    warningMsg = (
                        f'Shortcut conflict: Actions {[a[0] for a in actions]} all request '
                        f'key sequence "{winner[2]}". Action "{winner[0]}" '
                        f'({priorityNames.get(winner[1], "unknown")}) takes precedence; '
                        f'action "{actionId}" is disabled.'
                    )
                    self.__logger.warning(warningMsg)
                    self._conflictWarnings.append(warningMsg)
                    
        # Remove disabled actions
        for actionId in actionsToDisable:
            self._effectiveBindings.pop(actionId, None)
    
    def build(self, shortcutsMenu: QtWidgets.QMenu, mainWindow: QtWidgets.QMainWindow) -> None:
        """Build Qt shortcut/action objects and populate the Shortcuts menu.
        
        Args:
            shortcutsMenu: The &Shortcuts menu to populate
            mainWindow: The main window for Application-scoped shortcuts
        """
        self.dispose()  # Clear any existing shortcuts
        
        for actionId, keySeq in self._effectiveBindings.items():
            action = self._catalog.get(actionId)
            if action is None:
                continue
                
            sequences = [keySeq] if isinstance(keySeq, str) else keySeq
            
            for seq in sequences:
                self._createQtObject(action, seq, shortcutsMenu, mainWindow)
                
        self.__logger.debug(f'Built {sum(len(objs) for objs in self._qtObjects.values())} Qt shortcuts')
        
    def _createQtObject(self, action: ShortcutAction, keySeq: str, 
                       shortcutsMenu: QtWidgets.QMenu, mainWindow: QtWidgets.QMainWindow) -> None:
        """Create a QShortcut or QAction for the given action."""
        # Determine parent/context
        if action.scope == ShortcutScope.Application:
            parent = mainWindow
            context = QtCore.Qt.ApplicationShortcut
        elif action.scope == ShortcutScope.Window:
            parent = action.owner if action.owner else mainWindow
            context = QtCore.Qt.WindowShortcut
        elif action.scope == ShortcutScope.WidgetLocal:
            parent = action.owner if action.owner else mainWindow
            context = QtCore.Qt.WidgetShortcut
        else:  # PressRelease or unknown
            parent = action.owner if action.owner else mainWindow
            context = QtCore.Qt.WidgetShortcut
            
        # Create QAction for menu entry
        qtAction = QtWidgets.QAction(action.displayName, parent)
        qtAction.setShortcut(keySeq)
        qtAction.triggered.connect(action.callback)
        
        # Add to menu
        shortcutsMenu.addAction(qtAction)
        
        # Store for disposal
        if action.actionId not in self._qtObjects:
            self._qtObjects[action.actionId] = []
        self._qtObjects[action.actionId].append(qtAction)
        
    def dispose(self) -> None:
        """Dispose all Qt shortcut objects correctly (no setParent(None) leak)."""
        for actionId, objs in self._qtObjects.items():
            for obj in objs:
                # Disconnect signals (QShortcut uses activated, QAction uses triggered)
                try:
                    if hasattr(obj, 'activated'):
                        obj.activated.disconnect()
                    elif hasattr(obj, 'triggered'):
                        obj.triggered.disconnect()
                except (AttributeError, TypeError, RuntimeError):
                    pass  # Already disconnected or object deleted
                    
                # Disable and schedule for deletion
                try:
                    obj.setEnabled(False)
                except (AttributeError, RuntimeError):
                    pass  # Object may already be deleted
                obj.deleteLater()
                
        self._qtObjects.clear()
        self.__logger.debug('Disposed all shortcuts')
        
    # Runtime API for Phase 4 editor
    
    def getEffectiveBindings(self) -> Dict[str, Union[str, List[str], None]]:
        """Get all effective bindings after merge and conflict resolution."""
        return self._effectiveBindings.copy()
        
    def getActionMetadata(self, actionId: str) -> Optional[ShortcutAction]:
        """Get metadata for a specific action."""
        return self._catalog.get(actionId)
        
    def getAllActions(self) -> Dict[str, ShortcutAction]:
        """Get the full action catalog."""
        return self._catalog.copy()
        
    def rebind(self, actionId: str, keySequence: Union[str, List[str], None]) -> None:
        """Rebind an action at runtime (for editor).
        
        Args:
            actionId: Action to rebind
            keySequence: New key sequence(s), or None to disable
        """
        if actionId not in self._catalog:
            self.__logger.warning(f'Cannot rebind unknown action: {actionId}')
            return
            
        # Update in-memory override
        if keySequence is None:
            self._configOverrides[actionId] = None
        else:
            self._configOverrides[actionId] = keySequence
            
        self.__logger.debug(f'Rebound action {actionId} to {keySequence}')
        
    def reset(self, actionId: str) -> None:
        """Reset an action to its code default."""
        if actionId not in self._catalog:
            self.__logger.warning(f'Cannot reset unknown action: {actionId}')
            return
            
        # Remove override
        self._configOverrides.pop(actionId, None)
        self.__logger.debug(f'Reset action {actionId} to code default')
        
    def addOrUpdateAction(
        self,
        actionId: str,
        displayName: str,
        callback: Callable,
        keySequence: Union[str, List[str], None],
        scope: ShortcutScope = ShortcutScope.Application,
        owner: Optional[QtCore.QObject] = None,
        priority: int = 1,
        shortcutsMenu: Optional[QtWidgets.QMenu] = None,
        mainWindow: Optional[QtWidgets.QMainWindow] = None
    ) -> None:
        """Add or update a single action at runtime (for dynamic shortcuts like mode switches).
        
        This is used for shortcuts that change dynamically (e.g., when modes are added/renamed/deleted).
        The action is registered/updated in the catalog, bound with the specified priority,
        and Qt objects are created immediately if menu/window are provided.
        
        Args:
            actionId: Stable, namespaced action identifier
            displayName: Human-readable name for the Shortcuts menu
            callback: Function to call when the shortcut is activated
            keySequence: Key sequence(s) to bind, or None if unbound
            scope: Shortcut scope (Application, Window, WidgetLocal, PressRelease)
            owner: Qt object for lifecycle (parent for the QShortcut/QAction)
            priority: Priority level (1=explicit/highest, 2=code default)
            shortcutsMenu: Optional menu to add the action to
            mainWindow: Optional main window for Application-scoped shortcuts
        """
        # Remove existing Qt objects for this action
        self.removeAction(actionId, catalogOnly=True)
        
        # Register the action in the catalog
        action = ShortcutAction(
            actionId=actionId,
            displayName=displayName,
            defaultKeySequence=keySequence,
            scope=scope,
            owner=owner,
            enabledPredicate=None,
            activationSource=None,
            callback=callback,
            initiallyBound=True
        )
        self._catalog[actionId] = action
        
        # Set the effective binding with the specified priority
        if keySequence is not None:
            self._effectiveBindings[actionId] = keySequence
            
            # If menu/window provided, create Qt objects immediately
            if shortcutsMenu is not None and mainWindow is not None:
                sequences = [keySequence] if isinstance(keySequence, str) else keySequence
                for seq in sequences:
                    self._createQtObject(action, seq, shortcutsMenu, mainWindow)
        
        self.__logger.debug(f'Added/updated action {actionId} with key {keySequence}')
        
    def removeAction(self, actionId: str, catalogOnly: bool = False) -> None:
        """Remove an action at runtime (for dynamic shortcuts like mode switches).
        
        Disposes the Qt objects for this action and optionally removes it from the catalog.
        
        Args:
            actionId: Action to remove
            catalogOnly: If True, only removes from catalog but keeps Qt objects
        """
        # Dispose Qt objects for this action
        if not catalogOnly and actionId in self._qtObjects:
            objs = self._qtObjects[actionId]
            for obj in objs:
                # Disconnect signals
                try:
                    if hasattr(obj, 'activated'):
                        obj.activated.disconnect()
                    elif hasattr(obj, 'triggered'):
                        obj.triggered.disconnect()
                except (AttributeError, TypeError, RuntimeError):
                    pass
                    
                # Disable and schedule for deletion
                try:
                    obj.setEnabled(False)
                except (AttributeError, RuntimeError):
                    pass
                obj.deleteLater()
                
            del self._qtObjects[actionId]
        
        # Remove from catalog and bindings
        self._catalog.pop(actionId, None)
        self._effectiveBindings.pop(actionId, None)
        self._configOverrides.pop(actionId, None)
        
        self.__logger.debug(f'Removed action {actionId}')
        
    def getConflictWarnings(self) -> List[str]:
        """Get list of conflict warnings from last build."""
        return self._conflictWarnings.copy()
        
    # Helper methods
    
    def _validateKeySequence(self, keySeq: str) -> bool:
        """Validate a key sequence using QKeySequence."""
        if not keySeq:
            return False
            
        qkeyseq = QtGui.QKeySequence(keySeq)
        # Check both isEmpty and toString - some invalid sequences have empty toString
        if qkeyseq.isEmpty() or not qkeyseq.toString():
            self.__logger.warning(f'Invalid key sequence: {keySeq}')
            return False
        return True
        
    def _normalizeKeySequence(self, keySeq: str) -> str:
        """Normalize a key sequence for conflict detection."""
        qkeyseq = QtGui.QKeySequence(keySeq)
        return qkeyseq.toString()


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
