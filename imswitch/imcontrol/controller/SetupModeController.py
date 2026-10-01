import datetime
import json
import os
import traceback
from dataclasses import dataclass, field
from typing import List
from urllib.parse import quote

from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.workflows.smart_mode_workflow import SMART_MICROSCOPY_ROLES

from .basecontrollers import (
    ComponentStateApplyMode,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


@dataclass(frozen=True)
class ApplyOutcome:
    """Structured result of applying a setup mode.

    Carries the recoverable ``warnings`` (identical strings/order to the legacy
    ``loadSetupMode`` return value) alongside structured component status:

    - ``failedComponents``: component names whose ``applyComponentState`` raised.
    - ``warningComponents``: component names that produced warnings without
      raising, or could not be fully applied for setup-mode bookkeeping reasons.

    The non-interactive ``SmartMicroscopyModeService`` reads these fields
    structurally rather than parsing warning strings to decide whether a
    hardware-component apply failed (see plan §2 failure contract).
    """

    warnings: List[str] = field(default_factory=list)
    failedComponents: List[str] = field(default_factory=list)
    warningComponents: List[str] = field(default_factory=list)


class SetupModeController:
    """Backend for saving and applying named imcontrol setup modes.

    A setup mode is a JSON file containing state snapshots from controllers
    that implement StatefulComponentMixin. This controller intentionally does not know
    details of lasers, scans, detectors, etc.; each component controller owns
    its own serialization and restore behavior.
    """

    schemaVersion = 1
    # Components declare their setup-mode application order through
    # ``setupModeApplyPriority``. This controller intentionally has no
    # component-name apply list, so adding an Olympus stand or another
    # beam-path component is a local controller decision.
    applyOrder = ()
    defaultApplyPriority = SetupModeApplyPriority.DEFAULT

    def __init__(self, controllers, setupInfo=None):
        self._controllers = controllers
        self._setupInfo = setupInfo
        self._logger = initLogger(self)

        # Single source of truth for the most recently cleanly applied setup
        # mode. Both the interactive SetupModesController and the non-interactive
        # SmartMicroscopyModeService apply through loadSetupMode, so both share
        # this value and can de-dup redundant reapplication (Design Principle 4).
        self._lastAppliedModeName = None

        self._modeDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_setup_modes')
        os.makedirs(self._modeDir, exist_ok=True)

    def getLastAppliedModeName(self):
        """Return the name of the most recently cleanly applied setup mode."""
        return self._lastAppliedModeName

    @APIExport()
    def getSetupModeStorageDir(self):
        """Return the directory where setup mode JSON files are stored."""
        return self._modeDir

    @APIExport()
    def getSetupModeComponents(self):
        """Return component names that currently support setup modes."""
        return sorted(self._getModeAwareControllers().keys())

    @APIExport()
    def getSetupModeComponentLabel(self, componentName):
        """Return a human-readable label declared by a setup-mode component."""
        controller = self._getModeAwareControllers().get(componentName)
        label = getattr(controller, 'setupModeDisplayName', None)
        return label or componentName

    def getSmartMicroscopyWorkflowNames(self):
        """Return smart-microscopy workflow names declared by controllers."""
        workflowNames = set()
        for controller in self._controllers.values():
            workflowName = getattr(controller, 'SMART_MODE_WORKFLOW', None)
            if workflowName:
                workflowNames.add(workflowName)
        return sorted(workflowNames)

    @APIExport()
    def isSetupModeHardwareCritical(self, componentName):
        """Return whether a setup-mode component changes safety-critical hardware."""
        controller = self._getModeAwareControllers().get(componentName)
        return bool(getattr(controller, 'setupModeHardwareCritical', False))

    @APIExport()
    def listSetupModes(self):
        """Return names of all saved setup modes."""
        modeNames = []

        for fileName in os.listdir(self._modeDir):
            if not fileName.endswith('.json'):
                continue

            path = os.path.join(self._modeDir, fileName)
            try:
                mode = self._readModeFile(path)
            except Exception:
                self._logger.error(f'Failed to read setup mode file: {path}')
                self._logger.error(traceback.format_exc())
                continue

            name = mode.get('name')
            if name:
                modeNames.append(name)

        return sorted(modeNames)

    @APIExport()
    def getSetupMode(self, name):
        """Return a saved setup mode by name."""
        return self._loadModeByName(name)

    @APIExport()
    def snapshotSetupModeState(self, componentNames=None):
        """Snapshot selected setup-mode components without saving them."""
        modeAwareControllers = self._getModeAwareControllers()
        componentNames = self._normalizeComponentNames(componentNames, modeAwareControllers)
        return self._snapshotComponents(componentNames, modeAwareControllers)

    @APIExport()
    def saveSetupMode(self, name, componentNames=None, description=None, shortcut=None):
        """Snapshot selected setup-mode components and save them as name.

        Args:
            name: Mode name.
            componentNames: Optional iterable of widget/component keys. If
                omitted, all currently supported setup-mode components are
                saved.
            description: Optional user description. If omitted while
                overwriting an existing mode, the previous description is
                preserved.
            shortcut: Optional shortcut text. If omitted while overwriting an
                existing mode, the previous shortcut is preserved. Pass an
                empty string to clear the shortcut.

        Returns:
            dict with keys "mode" and "warnings".
        """
        modeName = self._validateModeName(name)
        modeAwareControllers = self._getModeAwareControllers()
        componentNames = self._normalizeComponentNames(componentNames, modeAwareControllers)
        snapshot = self._snapshotComponents(componentNames, modeAwareControllers)

        now = self._nowIso()
        createdAt = now
        existingMode = None
        try:
            existingMode = self._loadModeByName(modeName)
            createdAt = existingMode.get('createdAt', now)
        except FileNotFoundError:
            pass

        if description is None and existingMode is not None:
            description = existingMode.get('description', '')
        if shortcut is None and existingMode is not None:
            shortcut = existingMode.get('shortcut')

        mode = {
            'schemaVersion': self.schemaVersion,
            'name': modeName,
            'description': description or '',
            'shortcut': self._normalizeShortcut(shortcut),
            'createdAt': createdAt,
            'updatedAt': now,
            'includedComponents': snapshot['includedComponents'],
            'state': snapshot['state'],
        }

        self._writeMode(mode)
        return {'mode': mode, 'warnings': snapshot['warnings']}

    @APIExport()
    def updateSetupModeMetadata(self, name, description=None, shortcut=None):
        """Update user-editable metadata for a saved setup mode."""
        mode = self._loadModeByName(name)

        if description is not None:
            mode['description'] = description or ''
        if shortcut is not None:
            mode['shortcut'] = self._normalizeShortcut(shortcut)

        mode['updatedAt'] = self._nowIso()
        self._writeMode(mode)
        return mode

    @APIExport()
    def renameSetupMode(self, name, newName):
        """Rename a saved setup mode and return the updated mode."""
        mode = self._loadModeByName(name)
        newModeName = self._validateModeName(newName)
        oldPath = self._modePathForName(name)
        newPath = self._modePathForName(newModeName)

        if os.path.exists(newPath):
            raise FileExistsError(f'Setup mode "{newModeName}" already exists')

        mode['name'] = newModeName
        mode['updatedAt'] = self._nowIso()
        self._writeMode(mode)

        if os.path.exists(oldPath):
            os.remove(oldPath)

        return mode

    @APIExport()
    def duplicateSetupMode(self, name, newName, description=None, shortcut=''):
        """Duplicate a saved setup mode and return the new mode."""
        sourceMode = self._loadModeByName(name)
        newModeName = self._validateModeName(newName)

        if os.path.exists(self._modePathForName(newModeName)):
            raise FileExistsError(f'Setup mode "{newModeName}" already exists')

        now = self._nowIso()
        mode = dict(sourceMode)
        mode['name'] = newModeName
        mode['description'] = (
            sourceMode.get('description', '') if description is None else description or ''
        )
        mode['shortcut'] = self._normalizeShortcut(shortcut)
        mode['createdAt'] = now
        mode['updatedAt'] = now

        self._writeMode(mode)
        return mode

    def applySetupMode(self, name, componentNames=None):
        """Apply a saved setup mode and return a structured outcome.

        This holds the real per-component apply loop. It produces exactly the
        same ``warnings`` strings (in the same order) as the legacy
        ``loadSetupMode`` did, and additionally records, in
        ``failedComponents``, the name of each component whose apply *raised*
        (the catastrophic ``except Exception`` branch). Components that return
        warnings without raising are recorded in ``warningComponents`` so
        non-interactive callers can distinguish "fully applied" from "applied
        with degraded component state" without parsing warning text.

        Args:
            name: Saved mode name.
            componentNames: Optional iterable of widget/component keys. If
                omitted, the mode's saved component list is applied.

        Returns:
            ApplyOutcome: ``warnings``, ``failedComponents``, and
            ``warningComponents``.
        """
        mode = self._loadModeByName(name)
        modeAwareControllers = self._getModeAwareControllers()
        modeState = mode.get('state', {})

        isFullModeApply = componentNames is None
        if isFullModeApply:
            componentNames = mode.get('includedComponents') or list(modeState.keys())
        else:
            componentNames = self._normalizeComponentNames(componentNames, modeAwareControllers)

        componentNames = self._orderedComponentNames(componentNames, modeAwareControllers)
        warnings = []
        failedComponents = []
        warningComponents = []

        # Any setup-mode apply attempt may leave hardware in an intermediate
        # state if a later component fails. Clear the clean-mode marker before
        # touching components; set it again only after a full clean apply.
        self._lastAppliedModeName = None

        # Use unified registry for apply
        registry = getWidgetStatePersistence()

        for componentName in componentNames:
            if componentName not in modeState:
                warnings.append(f'Setup mode "{mode["name"]}" has no state for "{componentName}".')
                self._appendUnique(warningComponents, componentName)
                continue

            controller = modeAwareControllers.get(componentName)
            if controller is None:
                warnings.append(f'Setup mode component "{componentName}" is not available.')
                self._appendUnique(warningComponents, componentName)
                continue

            try:
                # Apply via unified registry
                if registry is not None and registry.isRegistered(componentName):
                    componentWarnings = registry.applyComponentState(
                        componentName,
                        modeState[componentName],
                        apply_mode=ComponentStateApplyMode.SETUP_MODE_APPLY
                    )
                else:
                    componentWarnings = [f'{componentName} has no apply method']
            except Exception as e:
                self._logger.error(f'Failed to apply setup mode component: {componentName}')
                self._logger.error(traceback.format_exc())
                warnings.append(f'Failed to apply "{componentName}": {e}')
                failedComponents.append(componentName)
                continue

            if componentWarnings:
                warnings.extend([f'{componentName}: {warning}' for warning in componentWarnings])
                self._appendUnique(warningComponents, componentName)

        if isFullModeApply and not warnings:
            self._lastAppliedModeName = mode['name']

        return ApplyOutcome(
            warnings=warnings,
            failedComponents=failedComponents,
            warningComponents=warningComponents,
        )

    @staticmethod
    def _appendUnique(values, value):
        if value not in values:
            values.append(value)

    @APIExport(runOnUIThread=True)
    def loadSetupMode(self, name, componentNames=None):
        """Apply a saved setup mode.

        Thin wrapper over :meth:`applySetupMode` preserving the legacy return
        value so the interactive ``SetupModesController`` and existing tests are
        unchanged.

        Args:
            name: Saved mode name.
            componentNames: Optional iterable of widget/component keys. If
                omitted, the mode's saved component list is applied.

        Returns:
            list of warning strings.
        """
        return self.applySetupMode(name, componentNames).warnings

    @APIExport()
    def deleteSetupMode(self, name):
        """Delete a saved setup mode. Returns True if a file was removed."""
        path = self._modePathForName(name)
        if not os.path.exists(path):
            return False

        os.remove(path)
        return True

    def describeModeComponent(self, componentName, state):
        """Generate a human-readable summary of a component's saved state.

        Delegates to the registry's describeComponentState method.

        Args:
            componentName: Name of the component (e.g., 'Laser', 'Settings').
            state: Component state dict.

        Returns:
            list[str]: Human-readable summary lines.
        """
        registry = getWidgetStatePersistence()
        if registry is None or not registry.isRegistered(componentName):
            return []
        
        try:
            return registry.describeComponentState(componentName, state)
        except Exception as e:
            self._logger.error(f'Failed to describe component state for "{componentName}": {e}')
            self._logger.error(traceback.format_exc())
            return [f"(error describing state: {e})"]

    def getModeHazards(self, stateByComponent, applyMode, context=None):
        """Identify potential hazards in a mode state before applying it.

        Delegates to the registry's getComponentStateHazards method for each
        component and aggregates the results.

        Args:
            stateByComponent: dict mapping component names to state dicts.
            applyMode: ComponentStateApplyMode value.
            context: Optional context dict (see spec §5.2).

        Returns:
            list[dict]: Aggregated hazard records (see spec §5.3).
        """
        registry = getWidgetStatePersistence()
        hazards = []

        for componentName, state in stateByComponent.items():
            if registry is None or not registry.isRegistered(componentName):
                continue

            try:
                componentHazards = registry.getComponentStateHazards(
                    componentName,
                    state,
                    apply_mode=applyMode,
                    context=context
                )
                # Tag each hazard with componentName if not already present
                for hazard in componentHazards:
                    if 'componentName' not in hazard:
                        hazard['componentName'] = componentName
                    hazards.append(hazard)
            except Exception as e:
                self._logger.error(f'Failed to get hazards for "{componentName}": {e}')
                self._logger.error(traceback.format_exc())

        return hazards

    def diffModeComponents(self, oldStateByComponent, newStateByComponent):
        """Compute per-component textual diffs between two mode states.

        Args:
            oldStateByComponent: dict mapping component names to old state dicts.
            newStateByComponent: dict mapping component names to new state dicts.

        Returns:
            dict: {componentName: list[str]} where each list contains changed lines.
        """
        registry = getWidgetStatePersistence()
        diffs = {}

        allComponents = set(oldStateByComponent.keys()) | set(newStateByComponent.keys())

        for componentName in allComponents:
            oldState = oldStateByComponent.get(componentName)
            newState = newStateByComponent.get(componentName)

            if oldState == newState:
                continue

            if registry is None or not registry.isRegistered(componentName):
                # Fallback for unregistered components
                diffs[componentName] = ["(component not registered, cannot diff)"]
                continue

            try:
                oldLines = set(registry.describeComponentState(componentName, oldState)) if oldState else set()
                newLines = set(registry.describeComponentState(componentName, newState)) if newState else set()

                removed = oldLines - newLines
                added = newLines - oldLines

                changes = []
                for line in sorted(removed):
                    changes.append(f"- {line}")
                for line in sorted(added):
                    changes.append(f"+ {line}")

                if changes:
                    diffs[componentName] = changes
            except Exception as e:
                self._logger.error(f'Failed to diff component "{componentName}": {e}')
                self._logger.error(traceback.format_exc())
                diffs[componentName] = [f"(error diffing: {e})"]

        return diffs

    def _getModeAwareControllers(self):
        """Discover the controllers that support setup modes.

        Per spec D1: every StatefulComponentMixin component is mode-eligible
        (each mode's includedComponents selects the subset it affects), except
        GuiLayout (window-dock-layout adapter, STARTUP_RESTORE-only, no hardware
        semantics). Discovery is over the controllers this SetupModeController
        was given — not the persistence registry — so it does not depend on
        registry state.
        """
        modeAwareControllers = {}

        for componentName, controller in self._controllers.items():
            if componentName == 'GuiLayout':
                continue
            if isinstance(controller, StatefulComponentMixin):
                modeAwareControllers[componentName] = controller

        return modeAwareControllers

    def _snapshotComponents(self, componentNames, modeAwareControllers):
        warnings = []
        includedComponents = []
        state = {}
        
        # Use unified registry for snapshot
        registry = getWidgetStatePersistence()

        for componentName in componentNames:
            controller = modeAwareControllers.get(componentName)
            if controller is None:
                warnings.append(f'Setup mode component "{componentName}" is not available.')
                continue

            try:
                # Snapshot via unified registry
                if registry is not None and registry.isRegistered(componentName):
                    componentState = registry.snapshotComponent(componentName)
                else:
                    componentState = None
                
                if componentState is None:
                    warnings.append(f'Failed to snapshot "{componentName}"')
                    continue
                
                # Registry already asserts JSON-serializable, but double-check for safety
                self._assertJSONSerializable(componentState, componentName)
            except Exception as e:
                self._logger.error(f'Failed to snapshot setup mode component: {componentName}')
                self._logger.error(traceback.format_exc())
                warnings.append(f'Failed to snapshot "{componentName}": {e}')
                continue

            state[componentName] = componentState
            includedComponents.append(componentName)

        return {
            'includedComponents': includedComponents,
            'state': state,
            'warnings': warnings,
        }

    def _normalizeComponentNames(self, componentNames, modeAwareControllers):
        if componentNames is None:
            return sorted(modeAwareControllers.keys())

        if isinstance(componentNames, str):
            return [componentNames]

        try:
            return list(componentNames)
        except TypeError:
            raise TypeError('componentNames must be None, a string, or an iterable of strings')

    def _orderedComponentNames(self, componentNames, modeAwareControllers=None):
        componentNames = list(componentNames)
        if modeAwareControllers is None:
            modeAwareControllers = self._getModeAwareControllers()

        return [
            name
            for _, name in sorted(
                enumerate(componentNames),
                key=lambda item: (
                    self._componentApplyPriority(item[1], modeAwareControllers),
                    item[0],
                ),
            )
        ]

    def _componentApplyPriority(self, componentName, modeAwareControllers):
        controller = modeAwareControllers.get(componentName)
        priority = getattr(controller, 'setupModeApplyPriority', self.defaultApplyPriority)

        try:
            return int(priority)
        except (TypeError, ValueError):
            self._logger.warning(
                f'Setup mode component "{componentName}" has invalid '
                f'setupModeApplyPriority "{priority}"; using default order.'
            )
            return self.defaultApplyPriority

    def _validateModeName(self, name):
        if not isinstance(name, str):
            raise TypeError('Setup mode name must be a string')

        modeName = name.strip()
        if not modeName:
            raise ValueError('Setup mode name must not be empty')

        return modeName

    def _normalizeShortcut(self, shortcut):
        if shortcut is None:
            return None
        if not isinstance(shortcut, str):
            raise TypeError('Setup mode shortcut must be a string or None')

        shortcut = shortcut.strip()
        return shortcut if shortcut else None

    def _modePathForName(self, name):
        modeName = self._validateModeName(name)
        safeFileName = quote(modeName, safe='') + '.json'
        return os.path.join(self._modeDir, safeFileName)

    def _loadModeByName(self, name):
        path = self._modePathForName(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f'Setup mode "{name}" does not exist')

        return self._readModeFile(path)

    def _readModeFile(self, path):
        with open(path, 'r', encoding='utf-8') as file:
            mode = json.load(file)

        schemaVersion = mode.get('schemaVersion')
        if schemaVersion != self.schemaVersion:
            raise ValueError(
                f'Unsupported setup mode schema version "{schemaVersion}" in {path}'
            )

        if not mode.get('name'):
            raise ValueError(f'Setup mode file has no name: {path}')

        if not isinstance(mode.get('state', {}), dict):
            raise ValueError(f'Setup mode state must be a dict: {path}')

        return mode

    def _writeMode(self, mode):
        path = self._modePathForName(mode['name'])
        tmpPath = path + '.tmp'

        with open(tmpPath, 'w', encoding='utf-8') as file:
            json.dump(mode, file, indent=2, sort_keys=True)
            file.write('\n')

        os.replace(tmpPath, path)

    def _assertJSONSerializable(self, state, componentName):
        try:
            json.dumps(state)
        except TypeError as e:
            raise TypeError(
                f'Setup mode state for "{componentName}" is not JSON-serializable: {e}'
            )

    def _nowIso(self):
        return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


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
