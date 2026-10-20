import json
import os
import re
import traceback

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.workflows.smart_mode_workflow import (
    normalize_smart_mode_enabled_config,
    normalize_smart_mode_policy_config,
    normalize_smart_mode_role_config,
    validate_smart_mode_config,
)
from ..basecontrollers import ImConWidgetController


class SetupModesController(ImConWidgetController):
    """UI controller for the compact setup modes widget."""

    defaultSafetySettings = {
        "warnAboveLaserPowerThreshold": True,
        "laserPowerThresholdMw": 50.0,
        "confirmHighPowerShortcutApply": True,
        "suppressedWarnings": [],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._setupModeController = None
        self._shortcutManager = None
        self._shortcutsMenu = None
        self._mainWindow = None
        self._smartModeService = None
        self._registeredModeActionIds = set()
        self._activeModeName = None
        self._safetySettingsPath = os.path.join(
            dirtools.UserFileDirs.Root, "imcontrol_setup_mode_settings.json"
        )
        self._safetySettings = self._loadSafetySettings()

        self._widget.setBackendAvailable(False)
        self._widget.sigModeSelected.connect(self.modeSelected)
        self._widget.sigReloadMode.connect(self.reloadSelectedMode)
        self._widget.sigInspectModes.connect(self.inspectModes)
        self._widget.sigManagerLoadMode.connect(self.loadModeFromManager)
        self._widget.sigManagerComponentsMode.connect(self.editModeComponents)
        self._widget.sigManagerEditDescription.connect(self.editDescriptionByName)
        self._widget.sigManagerRenameMode.connect(self.renameModeByName)
        self._widget.sigManagerDuplicateMode.connect(self.duplicateModeByName)
        self._widget.sigManagerSetShortcut.connect(self.setShortcutByName)
        self._widget.sigManagerDeleteMode.connect(self.deleteModeByName)
        self._widget.sigUpdateMode.connect(self.updateCurrentMode)
        self._widget.sigSaveAsMode.connect(self.saveModeAs)
        self._widget.sigRenameMode.connect(self.renameMode)
        self._widget.sigDuplicateMode.connect(self.duplicateMode)
        self._widget.sigSetShortcut.connect(self.setShortcut)
        self._widget.sigSafetySettings.connect(self.editSafetySettings)
        self._widget.sigSmartMicroscopyRoles.connect(self.editSmartMicroscopyRoles)
        self._widget.sigDeleteMode.connect(self.deleteMode)
        self._widget.sigRevealFolder.connect(self.revealModesFolder)

    def setSetupModeController(self, setupModeController):
        self._setupModeController = setupModeController
        self._widget.setBackendAvailable(True)
        self.refreshModes()

    def setShortcutManager(self, shortcutManager, shortcutsMenu, mainWindow):
        """Inject the ShortcutManager for routing mode shortcuts (Phase 3d).
        
        Args:
            shortcutManager: The unified ShortcutManager instance
            shortcutsMenu: The &Shortcuts menu for adding mode actions
            mainWindow: The main window for Application-scoped shortcuts
        """
        self._shortcutManager = shortcutManager
        self._shortcutsMenu = shortcutsMenu
        self._mainWindow = mainWindow

    def setSmartModeService(self, smartModeService):
        """Inject the live smart-microscopy mode service."""
        self._smartModeService = smartModeService

    def closeEvent(self):
        self._clearShortcuts()

    def refreshModes(self, selectedName=None):
        if self._setupModeController is None:
            self._widget.setBackendAvailable(False)
            self._widget.setModes([])
            return

        try:
            modeNames = self._setupModeController.listSetupModes()
            summaries = []
            for modeName in modeNames:
                try:
                    summaries.append(self._makeModeSummary(
                        self._setupModeController.getSetupMode(modeName)
                    ))
                except Exception:
                    self._logger.error(f'Failed to read setup mode "{modeName}"')
                    self._logger.error(traceback.format_exc())

            self._widget.setBackendAvailable(True)
            self._widget.setModes(summaries, selectedName)
            self._rebuildShortcuts(summaries)
        except Exception as e:
            self._logger.error("Failed to refresh setup modes")
            self._logger.error(traceback.format_exc())
            self._widget.showError("Setup modes", f"Could not refresh setup modes: {e}")

    def modeSelected(self, modeName):
        if not modeName:
            return

        if not self._applyMode(modeName, source="selection"):
            self.refreshModes(self._activeModeName)

    def reloadSelectedMode(self):
        modeName = self._widget.getSelectedModeName()
        if not modeName:
            return

        if not self._applyMode(modeName, source="reload"):
            self.refreshModes(self._activeModeName)

    def inspectModes(self):
        if self._setupModeController is None:
            return

        modeDetails = self._collectModeDetails()
        if modeDetails is None:
            return

        self._widget.showSetupModeManagerDialog(
            modeDetails,
            selectedName=self._widget.getSelectedModeName(),
            activeName=self._activeModeName,
        )

    def _collectModeDetails(self):
        if self._setupModeController is None:
            return []

        try:
            return [
                self._makeInspectModeDetails(
                    self._setupModeController.getSetupMode(modeName)
                )
                for modeName in self._setupModeController.listSetupModes()
            ]
        except Exception as e:
            self._logger.error("Failed to inspect setup modes")
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Setup modes", f"Could not inspect setup modes: {e}"
            )
            return None

    def _refreshModeManager(self, selectedName=None):
        modeDetails = self._collectModeDetails()
        if modeDetails is None:
            return
        self._widget.updateSetupModeManager(
            modeDetails, selectedName=selectedName, activeName=self._activeModeName
        )

    def loadModeFromManager(self, modeName):
        if not modeName:
            return
        self._applyMode(modeName, source="manager")

    def editModeComponents(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        mode = self._getMode(modeName)
        if not mode:
            return

        state = mode.get("state") or {}
        components = [
            {"name": componentName, "label": self._componentLabel(componentName)}
            for componentName in state.keys()
        ]
        if not components:
            self._widget.showWarnings(
                "Setup mode components",
                [f'Setup mode "{modeName}" has no saved component state.'],
            )
            return

        included = self._includedComponentsForMode(mode)
        selected = self._widget.showModeComponentsDialog(
            modeName, components, included
        )
        if selected is None:
            return

        try:
            self._setupModeController.setSetupModeIncludedComponents(
                modeName, selected
            )
        except Exception as e:
            self._logger.error(
                f'Failed to update included components for setup mode "{modeName}"'
            )
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Setup mode components",
                f"Could not update setup mode components: {e}",
            )
            return

        mainSelection = self._widget.getSelectedModeName()
        self.refreshModes(mainSelection)
        self._refreshModeManager(modeName)

    def editDescriptionByName(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        mode = self._getMode(modeName)
        if not mode:
            return

        description = self._widget.showDescriptionDialog(
            modeName, mode.get("description", "")
        )
        if description is None or description == mode.get("description", ""):
            return

        mainSelection = self._widget.getSelectedModeName()
        try:
            self._setupModeController.updateSetupModeMetadata(
                modeName, description=description
            )
        except Exception as e:
            self._logger.error(
                f'Failed to update description for setup mode "{modeName}"'
            )
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Edit description", f"Could not update description: {e}"
            )
            return

        self.refreshModes(mainSelection)
        self._refreshModeManager(modeName)

    def saveModeAs(self):
        if self._setupModeController is None:
            return

        try:
            components = self._setupModeController.getSetupModeComponents()
        except Exception as e:
            self._logger.error("Failed to list setup mode components")
            self._logger.error(traceback.format_exc())
            self._widget.showError("Setup modes", f"Could not list setup mode components: {e}")
            return

        if not components:
            self._widget.showWarnings(
                "Setup modes",
                ["No setup-mode aware widgets are available in this setup."]
            )
            return

        selectedName = self._widget.getSelectedModeName()
        selectedMode = self._getMode(selectedName) if selectedName else {}
        selectedComponents = (
            self._includedComponentsForMode(selectedMode)
            if selectedMode
            else components
        )
        selectedState = selectedMode.get("state") or {}
        componentCaptureOptions = {}
        for componentName in components:
            options = self._setupModeController.getSetupModeCaptureOptions(
                componentName, selectedState.get(componentName)
            )
            if options:
                componentCaptureOptions[componentName] = options

        result = self._widget.showSaveModeDialog(
            selectedName or "",
            selectedMode.get("description", ""),
            selectedMode.get("shortcut", ""),
            components,
            selectedComponents,
            componentCaptureOptions=componentCaptureOptions,
        )
        if result is None:
            return

        modeName = result["name"]
        if self._modeExists(modeName) and not self._widget.askOverwriteMode(modeName):
            return

        shortcut = self._normalizeShortcut(result.get("shortcut"))
        if not self._ensureShortcutAvailable(shortcut, modeName):
            return

        try:
            saveResult = self._setupModeController.saveSetupMode(
                modeName,
                componentNames=result["components"],
                description=result.get("description", ""),
                shortcut=shortcut or "",
                captureOptions=result.get("captureOptions"),
            )
        except Exception as e:
            self._logger.error(f'Failed to save setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Save setup mode", f"Could not save setup mode: {e}")
            return

        self._activeModeName = modeName
        self.refreshModes(modeName)
        warnings = saveResult.get("warnings") or []
        if warnings:
            self._showWarnings("Setup mode saved with warnings", warnings)

    def updateCurrentMode(self):
        modeName = self._widget.getSelectedModeName()
        if not modeName or self._setupModeController is None:
            return

        savedMode = self._getMode(modeName)
        if not savedMode:
            return

        savedState = savedMode.get("state") or {}
        componentNames = self._includedComponentsForMode(savedMode)
        if not componentNames:
            self._widget.showWarnings(
                "Update setup mode",
                [f'Setup mode "{modeName}" has no included components.']
            )
            return

        # Capture canonical runtime state exactly once. Everything shown in the
        # dialog and eventually written is derived from this immutable snapshot.
        try:
            runtimeSnapshot = self._setupModeController.snapshotSetupModeRuntimeState(
                componentNames
            )
            captureOptions = {
                componentName: self._setupModeController.getSetupModeUpdateCaptureOptions(
                    componentName, savedState.get(componentName)
                )
                for componentName in componentNames
            }
            candidateSnapshot = self._setupModeController.prepareSetupModeStateSnapshot(
                runtimeSnapshot,
                captureOptions=captureOptions,
                previousStateByComponent=savedState,
            )
        except Exception as e:
            self._logger.error(f'Failed to snapshot setup mode update "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Update setup mode", f"Could not snapshot current state: {e}"
            )
            return

        summaries = self._buildUpdateSummaries(
            savedMode, candidateSnapshot, captureOptions
        )
        warnings = candidateSnapshot.get("warnings") or []
        result = self._widget.showUpdateModeDialog(modeName, summaries, warnings)
        if result is None:
            return

        selectedComponents = set(result.get('components') or [])
        selectedParts = result.get('selectedParts') or {}
        finalCaptureOptions = dict(captureOptions)
        finalCaptureOptions.update(result.get('captureOptions') or {})

        try:
            # Re-adapt the *same* canonical snapshot if the user changed a
            # binding policy in the dialog. This performs no hardware I/O.
            finalCandidate = self._setupModeController.prepareSetupModeStateSnapshot(
                runtimeSnapshot,
                captureOptions=finalCaptureOptions,
                previousStateByComponent=savedState,
            )
            finalState = dict(savedState)
            candidateState = finalCandidate.get('state') or {}
            for componentName in componentNames:
                if componentName not in selectedComponents:
                    continue
                if componentName not in candidateState:
                    continue
                finalState[componentName] = (
                    self._setupModeController.mergeSetupModeComponentState(
                        componentName,
                        savedState.get(componentName),
                        candidateState[componentName],
                        selectedParts.get(componentName),
                    )
                )

            saveResult = self._setupModeController.saveSetupModeFromState(
                modeName,
                finalState,
                includedComponents=componentNames,
                description=None,
                shortcut=None,
            )
        except Exception as e:
            self._logger.error(f'Failed to update setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Update setup mode", f"Could not update setup mode: {e}"
            )
            return

        self._activeModeName = modeName
        self.refreshModes(modeName)
        saveWarnings = list(finalCandidate.get('warnings') or [])
        saveWarnings.extend(saveResult.get('warnings') or [])
        if saveWarnings:
            # Keep ordering while avoiding duplicate snapshot/preparation warnings.
            self._showWarnings(
                "Setup mode updated with warnings", list(dict.fromkeys(saveWarnings))
            )

    def renameMode(self):
        self.renameModeByName(self._widget.getSelectedModeName())

    def renameModeByName(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        newName = self._widget.showNameDialog("Rename setup mode", "New name:", modeName)
        if not newName or newName == modeName:
            return

        if self._modeExists(newName):
            self._widget.showWarnings(
                "Rename setup mode",
                [f'Setup mode "{newName}" already exists.']
            )
            return

        mainSelection = self._widget.getSelectedModeName()
        try:
            self._setupModeController.renameSetupMode(modeName, newName)
        except Exception as e:
            self._logger.error(f'Failed to rename setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Rename setup mode", f"Could not rename setup mode: {e}")
            return

        if self._activeModeName == modeName:
            self._activeModeName = newName
        if mainSelection == modeName:
            mainSelection = newName
        self.refreshModes(mainSelection)
        self._refreshModeManager(newName)

    def duplicateMode(self):
        self.duplicateModeByName(self._widget.getSelectedModeName())

    def duplicateModeByName(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        suggestedName = self._uniqueCopyName(modeName)
        newName = self._widget.showNameDialog(
            "Duplicate setup mode", "New mode name:", suggestedName
        )
        if not newName:
            return

        if self._modeExists(newName):
            self._widget.showWarnings(
                "Duplicate setup mode",
                [f'Setup mode "{newName}" already exists.']
            )
            return

        mainSelection = self._widget.getSelectedModeName()
        try:
            self._setupModeController.duplicateSetupMode(
                modeName, newName, shortcut=""
            )
        except Exception as e:
            self._logger.error(f'Failed to duplicate setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Duplicate setup mode", f"Could not duplicate setup mode: {e}")
            return

        self.refreshModes(mainSelection)
        self._refreshModeManager(newName)

    def setShortcut(self):
        self.setShortcutByName(self._widget.getSelectedModeName())

    def setShortcutByName(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        mode = self._getMode(modeName)
        shortcut = self._widget.showShortcutDialog(modeName, mode.get("shortcut", ""))
        if shortcut is None:
            return

        shortcut = self._normalizeShortcut(shortcut)
        if not self._ensureShortcutAvailable(shortcut, modeName):
            return

        mainSelection = self._widget.getSelectedModeName()
        try:
            self._setupModeController.updateSetupModeMetadata(
                modeName, shortcut=shortcut or ""
            )
        except Exception as e:
            self._logger.error(f'Failed to update shortcut for setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Set shortcut", f"Could not update shortcut: {e}")
            return

        self.refreshModes(mainSelection)
        self._refreshModeManager(modeName)

    def editSafetySettings(self):
        result = self._widget.showSafetySettingsDialog(self._safetySettings)
        if result is None:
            return

        suppressedWarnings = list(self._safetySettings.get("suppressedWarnings", []) or [])
        self._safetySettings = dict(self.defaultSafetySettings)
        self._safetySettings.update(result)
        self._safetySettings["suppressedWarnings"] = suppressedWarnings
        if self._safetySettings.pop("clearSuppressedWarnings", False):
            self._safetySettings["suppressedWarnings"] = []
        self._saveSafetySettings()

    def editSmartMicroscopyRoles(self):
        """Edit smart microscopy role-to-mode mappings via dialog."""
        if self._setupModeController is None:
            self._widget.showError(
                "Smart microscopy roles",
                "Setup mode controller is not available."
            )
            return

        try:
            workflowNames = self._buildWorkflowNames()
            availableModes = self._setupModeController.listSetupModes()
            currentConfig = self._getCurrentSmartModeConfig()
        except Exception as e:
            self._logger.error("Failed to prepare smart microscopy roles dialog")
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Smart microscopy roles",
                f"Could not load current configuration: {e}"
            )
            return

        result = self._widget.showSmartMicroscopyRolesDialog(
            workflowNames, availableModes, currentConfig
        )
        if result is None:
            return

        validationProblems = self._validateSmartModeConfig(result, availableModes)
        if validationProblems:
            self._widget.showWarnings(
                "Smart microscopy roles validation",
                validationProblems
            )

        try:
            self._applySmartModeConfig(result)
        except Exception as e:
            self._logger.error("Failed to save smart microscopy roles")
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Smart microscopy roles",
                f"Could not save configuration: {e}"
            )
            return

        self._widget.showInformation(
            "Smart microscopy roles",
            "Smart microscopy role configuration saved successfully."
        )
        self.refreshModes(self._widget.getSelectedModeName())

    def deleteMode(self):
        self.deleteModeByName(self._widget.getSelectedModeName())

    def deleteModeByName(self, modeName):
        if not modeName or self._setupModeController is None:
            return

        if not self._widget.askDeleteMode(modeName):
            return

        mainSelection = self._widget.getSelectedModeName()
        try:
            self._setupModeController.deleteSetupMode(modeName)
        except Exception as e:
            self._logger.error(f'Failed to delete setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Delete setup mode", f"Could not delete setup mode: {e}")
            return

        if self._activeModeName == modeName:
            self._activeModeName = None
        if mainSelection == modeName:
            mainSelection = self._activeModeName
        self.refreshModes(mainSelection)
        self._refreshModeManager(None)

    def revealModesFolder(self):
        if self._setupModeController is None:
            return

        try:
            folder = self._setupModeController.getSetupModeStorageDir()
            opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
            if not opened:
                self._widget.showInformation("Setup modes folder", folder)
        except Exception as e:
            self._logger.error("Failed to open setup mode folder")
            self._logger.error(traceback.format_exc())
            self._widget.showError("Setup modes folder", f"Could not open folder: {e}")

    def _applyMode(self, modeName, source):
        if self._setupModeController is None:
            return False

        mode = self._getMode(modeName)
        if not mode:
            return False

        # Obtain hazards via getModeHazards delegator
        from ..basecontrollers import ComponentStateApplyMode
        
        thresholdMw = self._asFloat(
            self._safetySettings.get(
                "laserPowerThresholdMw",
                self.defaultSafetySettings["laserPowerThresholdMw"]
            )
        )
        if thresholdMw is None:
            thresholdMw = self.defaultSafetySettings["laserPowerThresholdMw"]

        context = {
            "laserPowerThresholdMw": thresholdMw,
            "suppressedWarnings": self._safetySettings.get("suppressedWarnings", []),
            "applySource": source,
        }

        hazards = self._setupModeController.getSetupModeHazards(
            modeName,
            ComponentStateApplyMode.SETUP_MODE_APPLY,
            context
        )

        # Filter high-power laser hazards (preserve existing suppression logic)
        highPowerHazards = [
            h for h in hazards
            if h.get("kind") == "high_laser_power"
        ]

        # Apply suppression filtering
        suppressedWarnings = self._safetySettings.get("suppressedWarnings", [])
        unsuppressedHazards = []
        for hazard in highPowerHazards:
            details = hazard.get("details", {})
            componentName = hazard.get("componentName", "Laser")
            laserName = details.get("laserName", "")
            suppressionKey = f"{componentName}:{laserName}:high_power"
            if suppressionKey not in suppressedWarnings:
                unsuppressedHazards.append(hazard)

        # Preserve existing high-power gate logic
        shouldWarnHighPower = (
            unsuppressedHazards
            and self._safetySettings.get("warnAboveLaserPowerThreshold", True)
            and (
                source != "shortcut"
                or self._safetySettings.get("confirmHighPowerShortcutApply", True)
            )
        )

        if shouldWarnHighPower:
            # Convert hazards to old highPowerEntries format for confirmHighPowerApply
            highPowerEntries = []
            for hazard in unsuppressedHazards:
                details = hazard.get("details", {})
                highPowerEntries.append({
                    "laserName": details.get("laserName", ""),
                    "value": details.get("value", 0),
                    "units": details.get("units", "mW"),
                })
            
            if not self._widget.confirmHighPowerApply(modeName, highPowerEntries, thresholdMw):
                return False

        try:
            warnings = self._setupModeController.loadSetupMode(modeName)
        except Exception as e:
            self._logger.error(f'Failed to apply setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Apply setup mode", f"Could not apply setup mode: {e}")
            return False

        if warnings:
            self._showWarnings("Setup mode applied with warnings", warnings)

        self._activeModeName = modeName
        self.refreshModes(modeName)
        self._refreshModeManager(modeName)
        return True

    def _getMode(self, modeName):
        if not modeName or self._setupModeController is None:
            return {}

        try:
            return self._setupModeController.getSetupMode(modeName)
        except Exception as e:
            self._logger.error(f'Failed to read setup mode "{modeName}"')
            self._logger.error(traceback.format_exc())
            self._widget.showError("Setup modes", f"Could not read setup mode: {e}")
            return {}

    @staticmethod
    def _includedComponentsForMode(mode):
        """Resolve active components without treating an explicit [] as legacy."""
        state = mode.get("state") or {}
        if "includedComponents" in mode:
            return list(mode.get("includedComponents") or [])
        return list(state.keys())

    def _modeExists(self, modeName):
        try:
            return modeName in self._setupModeController.listSetupModes()
        except Exception:
            self._logger.error("Failed to list setup modes")
            self._logger.error(traceback.format_exc())
            return False

    def _makeModeSummary(self, mode):
        name = mode["name"]
        shortcut = self._normalizeShortcut(mode.get("shortcut"))
        displayName = f"{name} ({shortcut})" if shortcut else name
        includedComponents = self._includedComponentsForMode(mode)

        return {
            "name": name,
            "displayName": displayName,
            "description": mode.get("description", ""),
            "shortcut": shortcut,
            "includedComponents": includedComponents,
            "includedComponentLabels": [
                self._componentLabel(componentName)
                for componentName in includedComponents
            ],
            "smartModeRoles": self._summarizeSmartModeRoles(name),
        }

    def _makeInspectModeDetails(self, mode):
        details = self._makeModeSummary(mode)
        state = mode.get("state") or {}
        included = self._includedComponentsForMode(mode)
        includedSet = set(included)
        savedComponents = list(state.keys())
        inactiveComponents = [
            componentName
            for componentName in savedComponents
            if componentName not in includedSet
        ]
        details.update({
            "createdAt": mode.get("createdAt"),
            "updatedAt": mode.get("updatedAt"),
            "savedComponents": savedComponents,
            "inactiveComponents": inactiveComponents,
            "inactiveComponentLabels": [
                self._componentLabel(componentName)
                for componentName in inactiveComponents
            ],
            "stateSummary": self._summarizeSavedState(mode),
        })

        return details

    def _summarizeSavedState(self, mode):
        """Describe every remembered payload, marking dormant components clearly."""
        state = mode.get("state") or {}
        includedSet = set(self._includedComponentsForMode(mode))
        summaries = []

        for componentName, componentState in state.items():
            label = self._componentLabel(componentName)
            if componentName not in includedSet:
                label = f"{label} (inactive)"
            summaries.append(f"{label}:")

            # Delegate to backend (registry's describeComponentState)
            componentSummary = self._setupModeController.describeModeComponent(
                componentName, componentState
            )
            if componentSummary:
                for line in componentSummary:
                    summaries.append(f"  {line}")
            else:
                summaries.append("  (no description available)")

            summaries.append("")

        if summaries and summaries[-1] == "":
            summaries.pop()
        return summaries

    def _buildUpdateSummaries(self, savedMode, snapshot, captureOptions):
        """Build structured per-component data for the selective Update dialog."""
        savedState = savedMode.get("state") or {}
        currentState = snapshot.get("state") or {}
        componentNames = self._includedComponentsForMode(savedMode)
        structuredDiff = self._setupModeController.diffModeComponentsStructured(
            savedState, currentState
        )

        summaries = []
        for componentName in componentNames:
            available = componentName in currentState
            diff = structuredDiff.get(componentName) or {
                'changed': False,
                'lines': [],
                'sections': [],
                'notice': None,
            }
            if not available:
                diff = dict(diff)
                diff['notice'] = 'Unavailable in the current setup; saved state will be preserved.'
            summaries.append({
                'name': componentName,
                'label': self._componentLabel(componentName),
                'available': available,
                'selected': available,
                'diff': diff,
                'captureOptions': captureOptions.get(componentName),
            })
        return summaries

    def _componentLabel(self, componentName):
        if self._setupModeController is not None:
            try:
                label = self._setupModeController.getSetupModeComponentLabel(componentName)
                if label:
                    return label
            except Exception:
                self._logger.debug(
                    "Failed to resolve setup-mode component label for %s",
                    componentName,
                    exc_info=True,
                )

        return self._humanizeComponentName(componentName)

    @staticmethod
    def _humanizeComponentName(componentName):
        label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(componentName)).strip()
        return label or str(componentName)

    def _showWarnings(self, title, warnings):
        visibleWarnings = self._filterSuppressedWarnings(warnings)
        if not visibleWarnings:
            return

        shouldSuppress = self._widget.showWarnings(
            title, visibleWarnings, allowSuppress=True
        )
        if shouldSuppress:
            self._suppressWarnings(visibleWarnings)

    def _filterSuppressedWarnings(self, warnings):
        suppressed = set(self._safetySettings.get("suppressedWarnings", []) or [])
        visibleWarnings = []
        seen = set()

        for warning in warnings:
            warning = str(warning)
            if warning in suppressed or warning in seen:
                continue
            visibleWarnings.append(warning)
            seen.add(warning)

        return visibleWarnings

    def _suppressWarnings(self, warnings):
        suppressedWarnings = list(self._safetySettings.get("suppressedWarnings", []) or [])
        suppressedSet = set(suppressedWarnings)

        for warning in warnings:
            warning = str(warning)
            if warning not in suppressedSet:
                suppressedWarnings.append(warning)
                suppressedSet.add(warning)

        self._safetySettings["suppressedWarnings"] = suppressedWarnings

    def _rebuildShortcuts(self, modeSummaries):
        """Rebuild mode shortcuts via the ShortcutManager (Phase 3d).
        
        Routes all mode shortcuts through the manager to fix the disposal leak
        (setParent(None) causes "Ambiguous shortcut overload" warnings).
        Mode shortcuts are registered as priority 1 (explicit user config),
        so they win in mode-vs-global conflicts per §7.4.
        """
        self._clearShortcuts()

        # Only route through manager if it's been injected
        if self._shortcutManager is None or self._shortcutsMenu is None or self._mainWindow is None:
            self._logger.warning('ShortcutManager not injected; mode shortcuts disabled')
            return

        from imswitch.imcommon.model import ShortcutScope

        for summary in modeSummaries:
            shortcutText = self._normalizeShortcut(summary.get("shortcut"))
            if not shortcutText:
                continue

            shortcut = QtGui.QKeySequence(shortcutText)
            if self._isEmptyShortcut(shortcut):
                continue

            modeName = summary["name"]
            actionId = f'mode.{modeName}'

            # Register mode shortcut with priority 1 (explicit user config)
            # Preserve existing behavior: source="shortcut" for safety confirmation
            self._shortcutManager.addOrUpdateAction(
                actionId=actionId,
                displayName=f'Apply Mode: {modeName}',
                callback=lambda mn=modeName: self._applyMode(mn, source="shortcut"),
                keySequence=shortcutText,
                scope=ShortcutScope.Application,
                owner=self._widget,
                priority=1,
                shortcutsMenu=self._shortcutsMenu,
                mainWindow=self._mainWindow
            )
            self._registeredModeActionIds.add(actionId)

    def _clearShortcuts(self):
        """Clear all mode shortcuts via the ShortcutManager (Phase 3d).
        
        Replaces the old setParent(None) leak with proper disposal.
        """
        if self._shortcutManager is None:
            self._registeredModeActionIds.clear()
            return

        # Remove exactly the mode actions THIS controller registered (robust to
        # renamed/deleted modes, which no longer appear in listSetupModes()).
        for actionId in self._registeredModeActionIds:
            self._shortcutManager.removeAction(actionId)
        self._registeredModeActionIds.clear()

    def _ensureShortcutAvailable(self, shortcut, targetModeName):
        if not shortcut:
            return True

        conflictModeName = self._findShortcutConflict(shortcut, targetModeName)
        if conflictModeName is None:
            return True

        if not self._widget.askShortcutConflict(shortcut, conflictModeName):
            return False

        self._setupModeController.updateSetupModeMetadata(
            conflictModeName, shortcut=""
        )
        return True

    def _findShortcutConflict(self, shortcut, targetModeName):
        shortcutKey = self._shortcutCompareKey(shortcut)
        if shortcutKey is None:
            return None

        for modeName in self._setupModeController.listSetupModes():
            if modeName == targetModeName:
                continue

            try:
                modeShortcut = self._setupModeController.getSetupMode(modeName).get("shortcut")
            except Exception:
                continue

            if self._shortcutCompareKey(modeShortcut) == shortcutKey:
                return modeName

        return None

    def _normalizeShortcut(self, shortcut):
        if shortcut is None:
            return None

        shortcutText = str(shortcut).strip()
        if not shortcutText:
            return None

        sequence = QtGui.QKeySequence(shortcutText)
        if self._isEmptyShortcut(sequence):
            return None

        return self._shortcutToText(sequence) or shortcutText

    def _shortcutCompareKey(self, shortcut):
        shortcut = self._normalizeShortcut(shortcut)
        if not shortcut:
            return None

        sequence = QtGui.QKeySequence(shortcut)
        try:
            text = sequence.toString(QtGui.QKeySequence.PortableText)
        except TypeError:
            text = sequence.toString()

        return (text or shortcut).lower()

    def _shortcutToText(self, sequence):
        try:
            return sequence.toString(QtGui.QKeySequence.NativeText).strip()
        except TypeError:
            return sequence.toString().strip()

    def _isEmptyShortcut(self, sequence):
        try:
            return sequence.isEmpty()
        except AttributeError:
            try:
                return sequence.count() == 0
            except Exception:
                return not sequence.toString()

    def _uniqueCopyName(self, modeName):
        baseName = f"{modeName} copy"
        candidate = baseName
        index = 2

        while self._modeExists(candidate):
            candidate = f"{baseName} {index}"
            index += 1

        return candidate

    def _asFloat(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _loadSafetySettings(self):
        settings = dict(self.defaultSafetySettings)

        try:
            with open(self._safetySettingsPath, "r", encoding="utf-8") as file:
                savedSettings = json.load(file)
            if isinstance(savedSettings, dict):
                savedSettings = dict(savedSettings)
                savedSettings.pop("suppressedWarnings", None)
                savedSettings.pop("clearSuppressedWarnings", None)
                settings.update(savedSettings)
        except FileNotFoundError:
            pass
        except Exception:
            self._logger.error("Failed to load setup mode safety settings")
            self._logger.error(traceback.format_exc())

        settings["suppressedWarnings"] = []
        return settings

    def _saveSafetySettings(self):
        try:
            persistentSettings = dict(self._safetySettings)
            persistentSettings.pop("suppressedWarnings", None)
            persistentSettings.pop("clearSuppressedWarnings", None)

            tmpPath = self._safetySettingsPath + ".tmp"
            with open(tmpPath, "w", encoding="utf-8") as file:
                json.dump(persistentSettings, file, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(tmpPath, self._safetySettingsPath)
        except Exception:
            self._logger.error("Failed to save setup mode safety settings")
            self._logger.error(traceback.format_exc())
            self._widget.showError(
                "Setup mode safety",
                "Could not save setup mode safety settings."
            )

    def _summarizeSmartModeRoles(self, modeName):
        """Build summary of which workflows use this mode in which roles.

        Args:
            modeName: Name of the setup mode

        Returns:
            List[str]: Human-readable lines describing workflow role assignments,
            empty if this mode is not used in any smart microscopy workflow.
        """
        if not modeName:
            return []

        summary = []
        currentModes = getattr(self._setupInfo, 'smartMicroscopyModes', None) or {}
        currentEnabled = getattr(self._setupInfo, 'smartMicroscopyModeSwitchingEnabled', None) or {}

        for workflowName, workflowModes in currentModes.items():
            roles = []
            for role, assignedMode in workflowModes.items():
                if assignedMode == modeName:
                    roles.append(role)

            if roles:
                enabled = currentEnabled.get(workflowName, False)
                enabledText = "enabled" if enabled else "disabled"
                rolesText = ", ".join(sorted(roles))
                summary.append(f"{workflowName}: {rolesText} ({enabledText})")

        return summary

    def _buildWorkflowNames(self):
        """Build list of workflow names from controllers and setupInfo.

        Returns:
            List[str]: Sorted unique workflow names from controllers with
            SMART_MODE_WORKFLOW plus any workflow already in setupInfo smart
            microscopy configuration.
        """
        workflowNames = set()

        if self._setupModeController is not None:
            getWorkflowNames = getattr(
                self._setupModeController,
                'getSmartMicroscopyWorkflowNames',
                None,
            )
            if getWorkflowNames is not None:
                workflowNames.update(getWorkflowNames())
            else:
                controllers = getattr(self._setupModeController, '_controllers', {}) or {}
                for controller in controllers.values():
                    workflowName = getattr(controller, 'SMART_MODE_WORKFLOW', None)
                    if workflowName:
                        workflowNames.add(workflowName)

        currentModes = getattr(self._setupInfo, 'smartMicroscopyModes', None) or {}
        for workflowName in currentModes.keys():
            workflowNames.add(workflowName)

        currentPolicies = getattr(self._setupInfo, 'smartMicroscopyModePolicies', None) or {}
        for workflowName in currentPolicies.keys():
            workflowNames.add(workflowName)

        currentEnabled = getattr(self._setupInfo, 'smartMicroscopyModeSwitchingEnabled', None) or {}
        for workflowName in currentEnabled.keys():
            workflowNames.add(workflowName)

        return sorted(workflowNames)

    def _getCurrentSmartModeConfig(self):
        """Extract current smart microscopy configuration from setupInfo.

        Returns:
            Dict with 'modes', 'policies', 'enabled' keys containing current
            configuration.
        """
        modes = getattr(self._setupInfo, 'smartMicroscopyModes', None) or {}
        policies = getattr(self._setupInfo, 'smartMicroscopyModePolicies', None) or {}
        enabled = getattr(self._setupInfo, 'smartMicroscopyModeSwitchingEnabled', None) or {}

        return {
            'modes': {
                workflowName: dict(workflowModes or {})
                for workflowName, workflowModes in modes.items()
            } if modes else {},
            'policies': dict(policies) if policies else {},
            'enabled': dict(enabled) if enabled else {},
        }

    def _validateSmartModeConfig(self, config, availableModes):
        """Validate smart mode configuration against available modes.

        Args:
            config: Dict with 'modes', 'policies', 'enabled' keys
            availableModes: List of available setup mode names

        Returns:
            List[str]: Human-readable validation problems, empty if valid
        """
        return validate_smart_mode_config(config, availableModes)

    def _applySmartModeConfig(self, config):
        """Apply smart mode configuration to setupInfo and persist.

        Args:
            config: Dict with 'modes', 'policies', 'enabled' keys
        """
        modes = self._normalizeSmartModeRoleConfig(config.get('modes', {}))
        policies = self._normalizeSmartModePolicyConfig(config.get('policies', {}))
        enabled = self._normalizeSmartModeEnabledConfig(config.get('enabled', {}))

        self._replaceSetupInfoMapping('smartMicroscopyModes', modes)
        self._replaceSetupInfoMapping('smartMicroscopyModePolicies', policies)
        self._replaceSetupInfoMapping('smartMicroscopyModeSwitchingEnabled', enabled)

        if self._smartModeService is not None:
            self._smartModeService.updateConfig(
                self._setupInfo.smartMicroscopyModes,
                self._setupInfo.smartMicroscopyModePolicies,
            )

        options = configfiletools.loadOptions()[0]
        configfiletools.saveSetupInfo(options, self._setupInfo)

    def _normalizeSmartModeRoleConfig(self, modes):
        return normalize_smart_mode_role_config(modes)

    def _normalizeSmartModePolicyConfig(self, policies):
        return normalize_smart_mode_policy_config(policies)

    def _normalizeSmartModeEnabledConfig(self, enabled):
        return normalize_smart_mode_enabled_config(enabled)

    def _replaceSetupInfoMapping(self, attrName, mapping):
        existing = getattr(self._setupInfo, attrName, None)
        if not mapping:
            setattr(self._setupInfo, attrName, None)
            return

        if isinstance(existing, dict):
            existing.clear()
            existing.update(mapping)
            setattr(self._setupInfo, attrName, existing)
        else:
            setattr(self._setupInfo, attrName, mapping)


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
