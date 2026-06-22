from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


class ShortcutScope(Enum):
    """Scope of a keyboard shortcut."""
    Application = "application"  # Global to application
    Window = "window"  # Window-specific
    WidgetLocal = "widget_local"  # Widget-specific
    PressRelease = "press_release"  # Press/release pair (special)


@dataclass
class ShortcutAction:
    """Catalogued keyboard shortcut action."""
    actionId: str
    displayName: str
    defaultKeySequence: Union[str, List[str], None]
    scope: ShortcutScope
    owner: Any
    enabledPredicate: Optional[Callable[[], bool]]
    activationSource: Optional[Dict[str, Any]]
    callback: Callable
    initiallyBound: bool = True


class shortcut:
    """Decorator for shortcuts with extended metadata support."""

    def __init__(self, key=None, name=None, *, actionId=None, defaultKey=None,
                 displayName=None, scope=ShortcutScope.Application,
                 enabledPredicate=None, activationSource=None, initiallyBound=True):
        # Legacy positional form: @shortcut("Ctrl+R", "Record")
        if key is not None and name is not None and actionId is None:
            self.legacy_mode = True
            self.key = key
            self.name = name
            self.actionId = None  # Will be derived from method name
            self.defaultKey = key
            self.displayName = name
        # New keyword form: @shortcut(actionId=..., defaultKey=..., ...)
        else:
            self.legacy_mode = False
            self.key = defaultKey  # For backward compat attributes
            self.name = displayName
            self.actionId = actionId
            self.defaultKey = defaultKey
            self.displayName = displayName
        
        self.scope = scope
        self.enabledPredicate = enabledPredicate
        self.activationSource = activationSource
        self.initiallyBound = initiallyBound

    def __call__(self, func):
        func._Shortcut = True
        func._Key = self.key  # Deprecated, kept for transition
        func._Name = self.name  # Deprecated, kept for transition
        func._ActionId = self.actionId
        func._DefaultKey = self.defaultKey
        func._DisplayName = self.displayName
        func._Scope = self.scope
        func._EnabledPredicate = self.enabledPredicate
        func._ActivationSource = self.activationSource
        func._InitiallyBound = self.initiallyBound
        func._LegacyMode = self.legacy_mode
        return func


def generateShortcuts(objs):
    """Generates an actionId-keyed dict of ShortcutAction records from decorated methods.
    
    Args:
        objs: List of objects to scan for @shortcut-decorated methods
        
    Returns:
        Dict[str, ShortcutAction]: Mapping from actionId to ShortcutAction
        
    Raises:
        RuntimeError: If duplicate actionId is detected (programming error)
    """
    actions = {}
    
    for obj in objs:
        owner_class = obj.__class__.__name__
        
        for attr_name in dir(obj):
            attr = getattr(obj, attr_name)

            if not callable(attr):
                continue

            if not hasattr(attr, '_Shortcut') or not attr._Shortcut:
                continue

            # Derive actionId for legacy decorators
            if hasattr(attr, '_LegacyMode') and attr._LegacyMode:
                action_id = f"{owner_class}.{attr_name}"
            else:
                action_id = attr._ActionId
                if action_id is None:
                    # No actionId provided, derive one
                    action_id = f"{owner_class}.{attr_name}"

            # Check for duplicates (hard error)
            if action_id in actions:
                raise RuntimeError(
                    f"Duplicate shortcut actionId '{action_id}' detected. "
                    f"Already registered by {actions[action_id].owner.__class__.__name__}, "
                    f"conflict with {owner_class}.{attr_name}. "
                    f"This is a programming error - each actionId must be unique."
                )

            # Build ShortcutAction record
            action = ShortcutAction(
                actionId=action_id,
                displayName=attr._DisplayName if hasattr(attr, '_DisplayName') else attr._Name,
                defaultKeySequence=attr._DefaultKey if hasattr(attr, '_DefaultKey') else attr._Key,
                scope=attr._Scope if hasattr(attr, '_Scope') else ShortcutScope.Application,
                owner=obj,
                enabledPredicate=attr._EnabledPredicate if hasattr(attr, '_EnabledPredicate') else None,
                activationSource=attr._ActivationSource if hasattr(attr, '_ActivationSource') else None,
                callback=attr,
                initiallyBound=attr._InitiallyBound if hasattr(attr, '_InitiallyBound') else True
            )
            
            actions[action_id] = action

    return actions


def getBoundShortcuts(actions):
    """Filter actions to only those that should be initially bound.
    
    Args:
        actions: Dict[str, ShortcutAction] from generateShortcuts
        
    Returns:
        Dict[str, ShortcutAction]: Only actions with initiallyBound=True and non-None defaultKeySequence
    """
    return {
        action_id: action
        for action_id, action in actions.items()
        if action.initiallyBound and action.defaultKeySequence is not None
    }


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
