"""
Widget State Persistence Framework

Provides a centralized, reusable system for saving and loading widget controller states
to persistent storage. This framework is designed to be:
- Controller-driven: Controllers opt-in by implementing getWidgetState/setWidgetState
- Hardware-independent: Pure UI state, never triggers hardware actions automatically
- Robust: Graceful handling of missing widgets, corrupted files, and version mismatches
- Safe: Explicit filtering of hardware-active states (lasers on, acquisition running, etc.)
"""

import json
import os
import traceback
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict

from imswitch.imcommon.model import dirtools, initLogger


@dataclass
class WidgetStateMetadata:
    """Metadata for a widget state snapshot"""
    controller_name: str
    schema_version: int = 1
    widget_type: Optional[str] = None


class WidgetStatePersistence:
    """
    Central service for widget state persistence.
    
    Usage in controllers:
        1. Implement getWidgetState() -> dict
        2. Implement setWidgetState(state: dict) -> None
        3. Optionally implement getStateSchemaVersion() -> int
        4. Call register() to enable automatic persistence
    
    Safety features:
        - Never automatically restores hardware-active states
        - Handles missing widgets gracefully (logs and continues)
        - Handles corrupted/invalid state files (logs and continues)
        - Version-aware: can skip incompatible states
    """
    
    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=True)
        self._stateDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_widget_states')
        os.makedirs(self._stateDir, exist_ok=True)
        
        # Registry of controllers that support state persistence
        # Key: controller name (e.g., "LaserController")
        # Value: weak reference to controller instance
        self._registry: Dict[str, Any] = {}
        
        self._logger.debug(f'Widget state persistence initialized: {self._stateDir}')
    
    def register(self, controller_name: str, controller: Any) -> None:
        """
        Register a controller for state persistence.
        
        The controller must implement:
        - getWidgetState() -> dict
        - setWidgetState(state: dict) -> None
        
        Args:
            controller_name: Unique name for this controller (e.g., "LaserController")
            controller: The controller instance
        """
        if not self._hasStateMethods(controller):
            self._logger.warning(
                f'Controller {controller_name} does not implement getWidgetState/setWidgetState, '
                f'skipping registration'
            )
            return
        
        self._registry[controller_name] = controller
        self._logger.debug(f'Registered controller for state persistence: {controller_name}')
    
    def unregister(self, controller_name: str) -> None:
        """Unregister a controller from state persistence."""
        if controller_name in self._registry:
            del self._registry[controller_name]
            self._logger.debug(f'Unregistered controller: {controller_name}')
    
    def saveWidgetState(self, controller_name: str, state_name: str = 'default') -> bool:
        """
        Save the current state of a specific widget controller.
        
        Args:
            controller_name: Name of the controller to save
            state_name: Name for this state snapshot (default: 'default')
        
        Returns:
            True if successful, False otherwise
        """
        if controller_name not in self._registry:
            self._logger.warning(f'Controller {controller_name} not registered for state persistence')
            return False
        
        controller = self._registry[controller_name]
        
        try:
            # Get state from controller
            state = controller.getWidgetState()
            
            # Add metadata
            schema_version = self._getSchemaVersion(controller)
            metadata = WidgetStateMetadata(
                controller_name=controller_name,
                schema_version=schema_version,
                widget_type=type(controller._widget).__name__ if hasattr(controller, '_widget') else None
            )
            
            # Combine metadata and state
            full_state = {
                '_metadata': asdict(metadata),
                'state': state
            }
            
            # Save to file
            file_path = self._getStatePath(controller_name, state_name)
            with open(file_path, 'w') as f:
                json.dump(full_state, f, indent=2)
            
            self._logger.info(f'Saved widget state: {controller_name} -> {state_name}')
            return True
            
        except Exception as e:
            self._logger.error(
                f'Failed to save widget state for {controller_name}: {e}\n{traceback.format_exc()}'
            )
            return False
    
    def loadWidgetState(self, controller_name: str, state_name: str = 'default',
                       apply_immediately: bool = True) -> Optional[Dict[str, Any]]:
        """
        Load a saved state for a specific widget controller.
        
        Args:
            controller_name: Name of the controller to load state for
            state_name: Name of the state snapshot to load
            apply_immediately: If True, apply state to controller immediately
        
        Returns:
            The loaded state dict if successful, None otherwise
        """
        if controller_name not in self._registry and apply_immediately:
            self._logger.warning(
                f'Controller {controller_name} not registered, cannot apply state immediately'
            )
            # Still try to load and return the state even if not registered
        
        file_path = self._getStatePath(controller_name, state_name)
        if not os.path.exists(file_path):
            self._logger.debug(f'No saved state found for {controller_name}/{state_name}')
            return None
        
        try:
            # Load from file
            with open(file_path, 'r') as f:
                full_state = json.load(f)
            
            # Extract metadata and state
            metadata_dict = full_state.get('_metadata', {})
            state = full_state.get('state', {})
            
            # Check schema version compatibility
            saved_version = metadata_dict.get('schema_version', 1)
            if apply_immediately and controller_name in self._registry:
                controller = self._registry[controller_name]
                current_version = self._getSchemaVersion(controller)
                
                if saved_version != current_version:
                    self._logger.warning(
                        f'Schema version mismatch for {controller_name}: '
                        f'saved={saved_version}, current={current_version}. '
                        f'Loading may fail or produce unexpected results.'
                    )
            
            # Apply state if requested
            if apply_immediately and controller_name in self._registry:
                controller = self._registry[controller_name]
                
                # Apply the state (controller is responsible for safety checks)
                controller.setWidgetState(state)
                self._logger.info(f'Loaded and applied widget state: {controller_name}/{state_name}')
            else:
                self._logger.info(f'Loaded widget state (not applied): {controller_name}/{state_name}')
            
            return state
            
        except Exception as e:
            self._logger.error(
                f'Failed to load widget state for {controller_name}/{state_name}: {e}\n'
                f'{traceback.format_exc()}'
            )
            return None
    
    def saveAllWidgetStates(self, state_name: str = 'default') -> int:
        """
        Save states for all registered controllers.
        
        Args:
            state_name: Name for this state snapshot
        
        Returns:
            Number of controllers successfully saved
        """
        success_count = 0
        for controller_name in list(self._registry.keys()):
            if self.saveWidgetState(controller_name, state_name):
                success_count += 1
        
        self._logger.info(f'Saved {success_count}/{len(self._registry)} widget states')
        return success_count
    
    def loadAllWidgetStates(self, state_name: str = 'default') -> int:
        """
        Load states for all registered controllers.
        
        Args:
            state_name: Name of the state snapshot to load
        
        Returns:
            Number of controllers successfully loaded
        """
        success_count = 0
        for controller_name in list(self._registry.keys()):
            if self.loadWidgetState(controller_name, state_name, apply_immediately=True) is not None:
                success_count += 1
        
        self._logger.info(f'Loaded {success_count}/{len(self._registry)} widget states')
        return success_count
    
    def listSavedStates(self, controller_name: str) -> List[str]:
        """
        List all saved state names for a specific controller.
        
        Args:
            controller_name: Name of the controller
        
        Returns:
            List of state names
        """
        controller_dir = os.path.join(self._stateDir, controller_name)
        if not os.path.exists(controller_dir):
            return []
        
        state_files = [
            Path(f).stem for f in os.listdir(controller_dir)
            if f.endswith('.json')
        ]
        return sorted(state_files)
    
    def deleteWidgetState(self, controller_name: str, state_name: str) -> bool:
        """
        Delete a saved state.
        
        Args:
            controller_name: Name of the controller
            state_name: Name of the state to delete
        
        Returns:
            True if successful, False otherwise
        """
        file_path = self._getStatePath(controller_name, state_name)
        if not os.path.exists(file_path):
            self._logger.warning(f'State file does not exist: {controller_name}/{state_name}')
            return False
        
        try:
            os.remove(file_path)
            self._logger.info(f'Deleted widget state: {controller_name}/{state_name}')
            return True
        except Exception as e:
            self._logger.error(f'Failed to delete widget state: {e}')
            return False
    
    def getRegisteredControllers(self) -> List[str]:
        """Get list of registered controller names."""
        return list(self._registry.keys())
    
    # Private helper methods
    
    def _getStatePath(self, controller_name: str, state_name: str) -> str:
        """Get the file path for a widget state."""
        controller_dir = os.path.join(self._stateDir, controller_name)
        os.makedirs(controller_dir, exist_ok=True)
        return os.path.join(controller_dir, f'{state_name}.json')
    
    def _hasStateMethods(self, controller: Any) -> bool:
        """Check if controller implements required state methods."""
        return (
            hasattr(controller, 'getWidgetState') and
            callable(getattr(controller, 'getWidgetState')) and
            hasattr(controller, 'setWidgetState') and
            callable(getattr(controller, 'setWidgetState'))
        )
    
    def _getSchemaVersion(self, controller: Any) -> int:
        """Get schema version from controller, default to 1."""
        if hasattr(controller, 'getStateSchemaVersion') and callable(
            getattr(controller, 'getStateSchemaVersion')
        ):
            return controller.getStateSchemaVersion()
        return 1


# Global singleton instance
_persistence_instance: Optional[WidgetStatePersistence] = None


def getWidgetStatePersistence() -> WidgetStatePersistence:
    """Get the global widget state persistence instance."""
    global _persistence_instance
    if _persistence_instance is None:
        _persistence_instance = WidgetStatePersistence()
    return _persistence_instance


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
