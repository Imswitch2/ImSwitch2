"""
Unified Component State Persistence Registry (Phase 4a)

Provides a single registry for both global state persistence (startup/shutdown,
file import/export) and setup modes (named hardware configurations). Supports:

- Unified component state contract (StatefulComponentMixin with ComponentStateApplyMode)
- Canonical component names with legacy alias resolution
- Apply-mode safety enforcement (STARTUP_RESTORE vs SETUP_MODE_APPLY)
- Summary/diff/hazard delegation hooks for UI integration

Phase 4a: All controllers have migrated to the unified interface. Legacy compatibility
layers (getWidgetState/setWidgetState, SetupModeMixin) have been removed.
"""

import json
import os
import traceback
from pathlib import Path
from typing import Dict, Any, Optional, List

from imswitch.imcommon.model import dirtools, initLogger


class WidgetStatePersistence:
    """
    Unified component state persistence registry (Phase 4a).
    
    All controllers implement StatefulComponentMixin with the unified interface:
    getComponentState, applyComponentState, describeComponentState, getComponentStateHazards.
    
    Usage:
        1. Inherit StatefulComponentMixin, set componentName and legacyStateNames
        2. Implement the four required methods
        3. Call register(canonicalName, controller)
        4. Registry handles apply-mode enforcement and alias resolution
    
    Safety features:
        - Apply-mode enforcement (STARTUP_RESTORE vs SETUP_MODE_APPLY)
        - Alias resolution (old file keys -> canonical component names)
        - JSON-serializability assertions before save
    """
    
    # Canonical component name -> legacy alias mapping (from spec Section 3.2)
    # Used for resolving old file keys and registration names to canonical names
    CANONICAL_ALIASES = {
        'Laser': ('LaserController',),
        'Settings': ('SettingsController',),
        'Scan': ('ScanController', 'ScanControllerAdvanced', 'ScanControllerMoNaLISA', 'ScanControllerPointScan'),
        'FlipMirror': tuple(),  # No widget-persistence alias; only setup-mode
        'SLM': tuple(),
        'SLMs': tuple(),
        'LeicaStand': tuple(),
        'Positioner': ('PositionerController',),
        'Rotator': ('RotatorController',),
        'Recording': ('RecordingController',),
        'BeadRec': ('BeadRecController',),
        'Image': ('ImageController',),
        'GuiLayout': ('GuiLayout',),  # Registered exactly as 'GuiLayout', no alias
    }
    
    # Reverse lookup: any registration key (canonical or legacy) -> canonical name
    _ALIAS_TO_CANONICAL = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        _ALIAS_TO_CANONICAL[canonical] = canonical
        for alias in aliases:
            _ALIAS_TO_CANONICAL[alias] = canonical
    
    def __init__(self):
        self._logger = initLogger(self)
        
        # Registry: canonical component name -> controller instance
        self._registry: Dict[str, Any] = {}
        
        # Track which registration keys were used (for alias deprecation warnings)
        self._registrationKeys: Dict[str, str] = {}  # canonical_name -> original_registration_key
        
        # Allow test override of state directory
        self.__stateDirOverride: str | None = None
        
        self._logger.debug(f'Unified component state registry initialized')
    
    @property
    def _stateDir(self) -> str:
        """Get the state directory path (computed dynamically for test compatibility)."""
        if self.__stateDirOverride is not None:
            return self.__stateDirOverride
        state_dir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_widget_states')
        os.makedirs(state_dir, exist_ok=True)
        return state_dir
    
    @_stateDir.setter
    def _stateDir(self, value: str) -> None:
        """Allow tests to override the state directory."""
        self.__stateDirOverride = value
    
    def isRegistered(self, component_name: str) -> bool:
        """Check if a component is registered (alias-aware).
        
        Args:
            component_name: Component name (canonical or legacy alias)
        
        Returns:
            True if component is registered, False otherwise
        """
        canonical_name = self._resolveCanonicalName(component_name)
        return canonical_name in self._registry
    
    def register(self, controller_name: str, controller: Any) -> None:
        """
        Register a controller for state persistence (unified interface).
        
        The controller must implement StatefulComponentMixin with all four required methods:
        getComponentState, applyComponentState, describeComponentState, getComponentStateHazards.
        
        Args:
            controller_name: Component name (canonical or legacy alias)
            controller: The controller instance implementing StatefulComponentMixin
            
        Raises:
            ValueError: If controller does not implement the unified interface
        """
        if not self._hasStateMethods(controller):
            raise ValueError(
                f'Controller {controller_name} must implement StatefulComponentMixin '
                f'(getComponentState and applyComponentState methods)'
            )
        
        # Resolve to canonical name via alias table
        canonical_name = self._resolveCanonicalName(controller_name)
        
        # Warn if using a deprecated legacy key
        if controller_name != canonical_name:
            self._logger.debug(
                f'Registering {controller_name} -> {canonical_name} (legacy alias resolved)'
            )
        
        # Store in registry by canonical name
        self._registry[canonical_name] = controller
        self._registrationKeys[canonical_name] = controller_name
        
        self._logger.debug(f'Registered component for state persistence: {canonical_name}')
    
    def unregister(self, controller_name: str) -> None:
        """Unregister a controller from state persistence (accepts aliases)."""
        canonical_name = self._resolveCanonicalName(controller_name)
        if canonical_name in self._registry:
            del self._registry[canonical_name]
            del self._registrationKeys[canonical_name]
            self._logger.debug(f'Unregistered component: {canonical_name}')
    
    def snapshotComponent(self, component_name: str) -> Optional[dict]:
        """Snapshot a single component's state (unified interface).
        
        Args:
            component_name: Canonical or legacy component name
        
        Returns:
            Component state dict, or None if component not registered or snapshot fails
        """
        canonical_name = self._resolveCanonicalName(component_name)
        controller = self._registry.get(canonical_name)
        if controller is None:
            self._logger.warning(f'Component {component_name} not registered')
            return None
        
        try:
            state = controller.getComponentState()
            
            # Assert JSON-serializable
            try:
                json.dumps(state)
            except (TypeError, ValueError) as e:
                self._logger.error(
                    f'Component {canonical_name} state is not JSON-serializable: {e}'
                )
                return None
            
            return state
        
        except Exception as e:
            self._logger.error(
                f'Failed to snapshot component {canonical_name}: {e}\n{traceback.format_exc()}'
            )
            return None
    
    def applyComponentState(
        self,
        component_name: str,
        state: dict,
        apply_mode: Optional[Any] = None
    ) -> List[str]:
        """Apply state to a component (unified interface).
        
        Args:
            component_name: Canonical or legacy component name
            state: Component state dict
            apply_mode: ComponentStateApplyMode enum value (STARTUP_RESTORE or SETUP_MODE_APPLY)
                If None, defaults to STARTUP_RESTORE
        
        Returns:
            List of warning strings (empty list = success)
        """
        canonical_name = self._resolveCanonicalName(component_name)
        controller = self._registry.get(canonical_name)
        if controller is None:
            return [f'Component {component_name} not registered']
        
        # Lazy-load ComponentStateApplyMode enum
        if apply_mode is None:
            from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode
            apply_mode = ComponentStateApplyMode.STARTUP_RESTORE
        
        try:
            warnings = controller.applyComponentState(state, applyMode=apply_mode)
            return warnings if isinstance(warnings, list) else []
        
        except Exception as e:
            self._logger.error(
                f'Failed to apply state to {canonical_name}: {e}\n{traceback.format_exc()}'
            )
            return [f'Failed to apply {canonical_name}: {e}']
    
    # Legacy API methods (preserved for backward compatibility)
    
    def saveWidgetState(self, controller_name: str, state_name: str = 'default') -> bool:
        """
        Save the current state of a specific widget controller (legacy API).
        
        Args:
            controller_name: Name of the controller to save (accepts aliases)
            state_name: Name for this state snapshot (default: 'default')
        
        Returns:
            True if successful, False otherwise
        """
        canonical_name = self._resolveCanonicalName(controller_name)
        if canonical_name not in self._registry:
            self._logger.warning(f'Controller {controller_name} not registered for state persistence')
            return False
        
        controller = self._registry[canonical_name]
        
        try:
            # Snapshot via unified interface
            state = self.snapshotComponent(canonical_name)
            if state is None:
                return False
            
            full_state = {
                '_metadata': {
                    'controller_name': controller_name,  # Preserve original key for legacy files
                    'canonical_name': canonical_name,
                    'schema_version': self._getSchemaVersion(controller),
                },
                'state': state,
            }
            
            # Save to legacy path: imcontrol_widget_states/<controller_name>/default.json
            # Use the ORIGINAL registration key for the file path (not canonical name)
            file_path = self._getStatePath(controller_name, state_name)
            with open(file_path, 'w') as f:
                json.dump(full_state, f, indent=2)
            
            self._logger.debug(f'Saved widget state: {controller_name} -> {state_name}')
            return True
            
        except Exception as e:
            self._logger.error(
                f'Failed to save widget state for {controller_name}: {e}\n{traceback.format_exc()}'
            )
            return False
    
    def loadWidgetState(self, controller_name: str, state_name: str = 'default',
                       apply_immediately: bool = True) -> Optional[Dict[str, Any]]:
        """
        Load a saved state for a specific widget controller (legacy API).
        
        Args:
            controller_name: Name of the controller to load state for (accepts aliases)
            state_name: Name of the state snapshot to load
            apply_immediately: If True, apply state to controller immediately
        
        Returns:
            The loaded state dict if successful, None otherwise
        """
        canonical_name = self._resolveCanonicalName(controller_name)
        
        if canonical_name not in self._registry and apply_immediately:
            self._logger.warning(
                f'Controller {controller_name} not registered, cannot apply state immediately'
            )
        
        # Try loading from the requested controller_name path first
        file_path = self._getStatePath(controller_name, state_name)
        if not os.path.exists(file_path):
            # Fall back to canonical name path if different
            if controller_name != canonical_name:
                file_path = self._getStatePath(canonical_name, state_name)
            if not os.path.exists(file_path):
                self._logger.debug(f'No saved state found for {controller_name}/{state_name}')
                return None
        
        try:
            with open(file_path, 'r') as f:
                full_state = json.load(f)
            
            metadata_dict = full_state.get('_metadata', {})
            state = full_state.get('state', {})
            
            saved_version = metadata_dict.get('schema_version', 1)
            if apply_immediately and canonical_name in self._registry:
                controller = self._registry[canonical_name]
                current_version = self._getSchemaVersion(controller)
                
                if saved_version != current_version:
                    self._logger.warning(
                        f'Schema version mismatch for {controller_name}: '
                        f'saved={saved_version}, current={current_version}. '
                        f'Loading may fail or produce unexpected results.'
                    )
            
            if apply_immediately and canonical_name in self._registry:
                from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode
                warnings = self.applyComponentState(
                    canonical_name,
                    state,
                    apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
                )
                if warnings:
                    self._logger.warning(
                        f'Warnings while applying {controller_name}/{state_name}: {warnings}'
                    )
                self._logger.debug(f'Loaded and applied widget state: {controller_name}/{state_name}')
            else:
                self._logger.debug(f'Loaded widget state (not applied): {controller_name}/{state_name}')
            
            return state
            
        except Exception as e:
            self._logger.error(
                f'Failed to load widget state for {controller_name}/{state_name}: {e}\n'
                f'{traceback.format_exc()}'
            )
            return None
    
    def saveAllWidgetStates(self, state_name: str = 'default') -> int:
        """
        Save states for all registered controllers (legacy API).
        
        Args:
            state_name: Name for this state snapshot
        
        Returns:
            Number of controllers successfully saved
        """
        success_count = 0
        for canonical_name in list(self._registry.keys()):
            # Use original registration key for file path
            original_key = self._registrationKeys.get(canonical_name, canonical_name)
            if self.saveWidgetState(original_key, state_name):
                success_count += 1
        
        self._logger.info(f'Saved {success_count}/{len(self._registry)} widget states')
        return success_count
    
    def loadAllWidgetStates(self, state_name: str = 'default') -> int:
        """
        Load states for all registered controllers (legacy API).
        
        Args:
            state_name: Name of the state snapshot to load
        
        Returns:
            Number of controllers successfully loaded
        """
        success_count = 0
        for canonical_name in list(self._registry.keys()):
            # Try original registration key first
            original_key = self._registrationKeys.get(canonical_name, canonical_name)
            if self.loadWidgetState(original_key, state_name, apply_immediately=True) is not None:
                success_count += 1
        
        self._logger.info(f'Loaded {success_count}/{len(self._registry)} widget states')
        return success_count
    
    def listSavedStates(self, controller_name: str) -> List[str]:
        """
        List all saved state names for a specific controller (legacy API).
        
        Args:
            controller_name: Name of the controller (accepts aliases)
        
        Returns:
            List of state names
        """
        canonical_name = self._resolveCanonicalName(controller_name)
        
        # Try both the requested name and canonical name directories
        state_files = set()
        for name in [controller_name, canonical_name]:
            controller_dir = os.path.join(self._stateDir, name)
            if os.path.exists(controller_dir):
                state_files.update([
                    Path(f).stem for f in os.listdir(controller_dir)
                    if f.endswith('.json')
                ])
        
        return sorted(state_files)
    
    def deleteWidgetState(self, controller_name: str, state_name: str) -> bool:
        """
        Delete a saved state (legacy API).
        
        Args:
            controller_name: Name of the controller (accepts aliases)
            state_name: Name of the state to delete
        
        Returns:
            True if successful, False otherwise
        """
        canonical_name = self._resolveCanonicalName(controller_name)
        
        # Try both paths
        for name in [controller_name, canonical_name]:
            file_path = self._getStatePath(name, state_name)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    self._logger.info(f'Deleted widget state: {name}/{state_name}')
                    return True
                except Exception as e:
                    self._logger.error(f'Failed to delete widget state: {e}')
                    return False
        
        self._logger.warning(f'State file does not exist: {controller_name}/{state_name}')
        return False
    
    def getRegisteredControllers(self) -> List[str]:
        """Get list of registered controller names (returns canonical names)."""
        return list(self._registry.keys())
    
    def save_to_file(self, file_path: str) -> None:
        """Export all registered controller states to a single JSON file (legacy API).

        Raises:
            IOError / json.JSONDecodeError on failure.
        """
        bundle: Dict[str, Any] = {}
        for canonical_name, controller in self._registry.items():
            try:
                # Export via unified interface
                state = self.snapshotComponent(canonical_name)
                if state is None:
                    continue
                
                # Use original registration key as the bundle key for backward compatibility
                original_key = self._registrationKeys.get(canonical_name, canonical_name)
                bundle[original_key] = {
                    '_metadata': {
                        'controller_name': original_key,
                        'canonical_name': canonical_name,
                        'schema_version': self._getSchemaVersion(controller)
                    },
                    'state': state,
                }
            except Exception as e:
                self._logger.warning(f'Skipping {canonical_name} during export: {e}')

        with open(file_path, 'w') as f:
            json.dump(bundle, f, indent=2)
        self._logger.info(f'Widget states exported to {file_path}')

    def load_from_file(self, file_path: str) -> int:
        """Import controller states from a file written by save_to_file() (legacy API).

        Resolves legacy keys via alias table.

        Returns:
            Number of controllers successfully restored.
        """
        with open(file_path, 'r') as f:
            bundle = json.load(f)

        count = 0
        from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode
        
        for name, entry in bundle.items():
            canonical_name = self._resolveCanonicalName(name)
            controller = self._registry.get(canonical_name)
            if controller is None:
                self._logger.debug(f'Skipping {name}: not registered (canonical: {canonical_name})')
                continue
            try:
                # Import via unified interface
                warnings = self.applyComponentState(
                    canonical_name,
                    entry.get('state', {}),
                    apply_mode=ComponentStateApplyMode.STARTUP_RESTORE
                )
                if warnings:
                    self._logger.warning(f'Warnings while importing {name}: {warnings}')
                count += 1
            except Exception as e:
                self._logger.warning(f'Failed to apply imported state for {name}: {e}')

        self._logger.info(f'Widget states imported from {file_path}: {count} applied')
        return count
    
    # Phase 3a delegation hooks (stubs for now; to be used by SetupModesController later)
    
    def describeComponentState(self, component_name: str, state: dict) -> List[str]:
        """Get human-readable summary of a component state (Phase 3a hook).
        
        Args:
            component_name: Canonical or legacy component name
            state: Component state dict
        
        Returns:
            List of summary strings
        """
        canonical_name = self._resolveCanonicalName(component_name)
        controller = self._registry.get(canonical_name)
        if controller is None:
            return [f'Component {component_name} not registered']
        
        try:
            if hasattr(controller, 'describeComponentState') and callable(controller.describeComponentState):
                return controller.describeComponentState(state)
            else:
                # Fallback for legacy controllers
                return [f'{canonical_name}: {len(state)} state keys']
        except Exception as e:
            self._logger.error(f'Failed to describe {canonical_name} state: {e}')
            return [f'{canonical_name}: (summary failed)']
    
    def getComponentStateHazards(
        self,
        component_name: str,
        state: dict,
        apply_mode: Any,
        context: Optional[dict] = None
    ) -> List[dict]:
        """Get hazard warnings for a component state (Phase 3a hook).
        
        Args:
            component_name: Canonical or legacy component name
            state: Component state dict
            apply_mode: ComponentStateApplyMode enum value
            context: Optional UI context (thresholds, suppressed warnings, etc.)
        
        Returns:
            List of hazard record dicts (see spec Section 5.3)
        """
        canonical_name = self._resolveCanonicalName(component_name)
        controller = self._registry.get(canonical_name)
        if controller is None:
            return []
        
        try:
            if hasattr(controller, 'getComponentStateHazards') and callable(controller.getComponentStateHazards):
                return controller.getComponentStateHazards(state, applyMode=apply_mode, context=context)
            else:
                # No hazards from legacy controllers
                return []
        except Exception as e:
            self._logger.error(f'Failed to get hazards for {canonical_name}: {e}')
            return []
    
    # Private helper methods
    
    def _resolveCanonicalName(self, name: str) -> str:
        """Resolve a registration key or file key to its canonical component name.
        
        Args:
            name: Component name (canonical, legacy alias, or unknown)
        
        Returns:
            Canonical name if found in alias table, otherwise the input name unchanged
        """
        return self._ALIAS_TO_CANONICAL.get(name, name)
    
    def _hasStateMethods(self, controller: Any) -> bool:
        """Check if controller implements the unified state persistence interface."""
        return (
            hasattr(controller, 'getComponentState') and callable(getattr(controller, 'getComponentState'))
            and hasattr(controller, 'applyComponentState') and callable(getattr(controller, 'applyComponentState'))
        )
    
    def _getStatePath(self, controller_name: str, state_name: str) -> str:
        """Get the file path for a widget state (legacy path structure)."""
        controller_dir = os.path.join(self._stateDir, controller_name)
        os.makedirs(controller_dir, exist_ok=True)
        return os.path.join(controller_dir, f'{state_name}.json')
    
    def _getSchemaVersion(self, controller: Any) -> int:
        """Get schema version from controller, default to 1."""
        # Try new interface first
        if hasattr(controller, 'stateSchemaVersion'):
            version = getattr(controller, 'stateSchemaVersion')
            if isinstance(version, int):
                return version
        
        # Fall back to legacy getStateSchemaVersion method
        if hasattr(controller, 'getStateSchemaVersion') and callable(
            getattr(controller, 'getStateSchemaVersion')
        ):
            return controller.getStateSchemaVersion()
        
        return 1


# Global singleton instance
_persistence_instance: Optional[WidgetStatePersistence] = None


def getWidgetStatePersistence() -> WidgetStatePersistence:
    """Get the global widget state persistence instance (singleton factory)."""
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
