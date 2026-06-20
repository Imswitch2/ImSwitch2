import dataclasses
from typing import Any, Dict

import h5py
from qtpy import QtWidgets

from imswitch.imcommon.controller import MainController, PickDatasetsController
from imswitch.imcommon.model import (
    ostools, initLogger, generateAPI, generateShortcuts, SharedAttributes
)
from imswitch.imcommon.framework import Thread
from .server import ImSwitchServer
from imswitch.imcontrol.model import configfiletools, getWidgetStatePersistence
from imswitch.imcontrol.view import guitools
from . import controllers
from .CommunicationChannel import CommunicationChannel
from .MasterController import MasterController
from .PickSetupController import PickSetupController
from .SetupModeController import SetupModeController
from .ShortcutManager import ShortcutManager
from .basecontrollers import ImConWidgetControllerFactory


class ImConMainController(MainController):
    def __init__(self, options, setupInfo, mainView, moduleCommChannel):
        self.__logger = initLogger(self)
        self.__logger.info('Initializing')

        self.__options = options
        self.__setupInfo = setupInfo
        self.__mainView = mainView
        self._moduleCommChannel = moduleCommChannel

        # Connect view signals
        self.__mainView.sigLoadParamsFromHDF5.connect(self.loadParamsFromHDF5)
        self.__mainView.sigPickSetup.connect(self.pickSetup)
        self.__mainView.sigClosing.connect(self.closeEvent)
        self.__mainView.sigSaveWidgetState.connect(self.saveWidgetState)
        self.__mainView.sigLoadWidgetState.connect(self.loadWidgetState)
        self.__mainView.sigOpenShortcutEditor.connect(self.openShortcutEditor)

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel(self, self.__setupInfo)
        self.__masterController = MasterController(self.__setupInfo, self.__commChannel,
                                                   self._moduleCommChannel)

        # List of Controllers for the GUI Widgets
        self.__factory = ImConWidgetControllerFactory(
            self.__setupInfo, self.__masterController, self.__commChannel, self._moduleCommChannel
        )
        self.pickSetupController = self.__factory.createController(
            PickSetupController, self.__mainView.pickSetupDialog
        )
        self.pickDatasetsController = self.__factory.createController(
            PickDatasetsController, self.__mainView.pickDatasetsDialog
        )

        self.controllers = {}

        # Extra kwargs forwarded to specific controllers that need view-layer objects
        _imageWidget = self.__mainView.widgets.get('Image')
        _toolManager = _imageWidget.toolManager if _imageWidget else None
        _extraKwargs = {
            'ViewerTools': {'imageWidget': _imageWidget},
            'LineProfile': {'imageToolManager': _toolManager},
        }

        for widgetKey, widget in self.__mainView.widgets.items():
            self.controllers[widgetKey] = self.__factory.createController(
                (getattr(controllers, f'{widgetKey}Controller')
                if widgetKey != 'Scan' else
                getattr(controllers, f'{widgetKey}Controller{self.__setupInfo.scan.scanWidgetType}')),
                widget,
                **_extraKwargs.get(widgetKey, {})
            )

        self.setupModeController = SetupModeController(self.controllers, self.__setupInfo)
        if 'SetupModes' in self.controllers:
            self.controllers['SetupModes'].setSetupModeController(self.setupModeController)

        # Create API-only controllers (no widget needed)
        # WorkflowFacadeController provides build_facade_from_master via API
        self.workflowFacadeController = self.__factory.createController(
            controllers.WorkflowFacadeController,
            widget=None,  # API-only, no widget
        )
        
        # Generate API
        self.__api = None
        apiObjs = (
            list(self.controllers.values())
            + [self.setupModeController, self.__commChannel, self.workflowFacadeController]
        )
        self.__api = generateAPI(
            apiObjs,
            missingAttributeErrorMsg=lambda attr: f'The imcontrol API does either not have any'
                                                  f' method {attr}, or the widget that defines it'
                                                  f' is not included in your currently active'
                                                  f' hardware setup file.'
        )
        # Generate Shortcuts via ShortcutManager
        shorcutObjs = []
        shorcutObjs.extend(self.__mainView.widgets.values())
        shorcutObjs.extend(self.controllers.values())
        
        # Scan positioner managers (includes GRBLStageManager, etc.)
        for _, positionerMgr in self.__masterController.positionersManager:
            shorcutObjs.append(positionerMgr)
        
        # Additional manager types can be added here as needed
        
        # Build catalog from decorated methods
        catalog = generateShortcuts(shorcutObjs)
        
        # Create ShortcutManager and wire it up
        self.__shortcutManager = ShortcutManager()
        self.__shortcutManager.collect(catalog)
        
        # Register menu actions (Phase 3a migration)
        from imswitch.imcommon.model import ShortcutScope
        self.__shortcutManager.registerAction(
            actionId='app.loadParams',
            displayName='Load parameters from HDF5',
            callback=lambda: self.__mainView.sigLoadParamsFromHDF5.emit(),
            defaultKeySequence='Ctrl+P',
            scope=ShortcutScope.Window,
            owner=self.__mainView
        )
        self.__shortcutManager.registerAction(
            actionId='app.saveWidgetStates',
            displayName='Save Widget States',
            callback=lambda: self.__mainView.sigSaveWidgetState.emit(),
            defaultKeySequence='Ctrl+Shift+S',
            scope=ShortcutScope.Window,
            owner=self.__mainView
        )
        self.__shortcutManager.registerAction(
            actionId='app.loadWidgetStates',
            displayName='Load Widget States',
            callback=lambda: self.__mainView.sigLoadWidgetState.emit(),
            defaultKeySequence='Ctrl+Shift+L',
            scope=ShortcutScope.Window,
            owner=self.__mainView
        )
        
        # Register LeicaStand F2 toggle (Phase 3b migration)
        if 'LeicaStand' in self.controllers:
            leicaController = self.controllers['LeicaStand']
            if hasattr(leicaController, '_widget') and hasattr(leicaController, 'toggleMode'):
                self.__shortcutManager.registerAction(
                    actionId='leica.toggleMode',
                    displayName='Leica: toggle mode',
                    callback=leicaController.toggleMode,
                    defaultKeySequence='F2',
                    scope=ShortcutScope.Window,
                    owner=leicaController._widget
                )
        
        # Register per-positioner axis jog actions (Phase 3c migration)
        self._registerPositionerJogActions()
        
        self.__shortcutManager.loadConfigOverrides(self.__setupInfo.shortcuts)
        self.__shortcutManager.computeEffectiveBindings()
        self.__shortcutManager.build(self.__mainView.shortcutsMenu, self.__mainView)
        
        # Update File menu to show effective shortcuts
        self.__mainView.updateMenuActionShortcuts(self.__shortcutManager.getEffectiveBindings())

        # Inject ShortcutManager into SetupModesController (Phase 3d)
        if 'SetupModes' in self.controllers:
            self.controllers['SetupModes'].setShortcutManager(
                self.__shortcutManager,
                self.__mainView.shortcutsMenu,
                self.__mainView
            )

        self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(self.__mainView)
        getWidgetStatePersistence().register('GuiLayout', self.__guiLayoutStateAdapter)

        # Auto-restore widget states after all controllers are ready
        try:
            getWidgetStatePersistence().loadAllWidgetStates('default')
        except Exception as e:
            self.__logger.warning(f'Failed to auto-restore widget states: {e}')

        if setupInfo.pyroServerInfo.active:
            self._serverWorker = ImSwitchServer(self.__api, setupInfo)
            self.__logger.debug(self.__api)
            self._thread = Thread()
            self._serverWorker.moveToThread(self._thread)
            self._thread.started.connect(self._serverWorker.run)
            self._thread.finished.connect(self._serverWorker.stop)
            self._thread.start()

    @property
    def api(self):
        return self.__api

    @property
    def shortcutManager(self):
        return self.__shortcutManager

    def loadParamsFromHDF5(self):
        """ Set detector, positioner, laser etc. params from values saved in a
        user-picked HDF5 snap/recording. """

        filePath = guitools.askForFilePath(self.__mainView, 'Open HDF5 file', nameFilter='*.hdf5')
        if not filePath:
            return

        with h5py.File(filePath) as file:
            datasetsInFile = file.keys()
            if len(datasetsInFile) < 1:
                # File does not contain any datasets
                return
            elif len(datasetsInFile) == 1:
                datasetToLoad = list(datasetsInFile)[0]
            else:
                # File contains multiple datasets
                self.pickDatasetsController.setDatasets(filePath, datasetsInFile)
                if not self.__mainView.showPickDatasetsDialogBlocking():
                    return

                datasetsSelected = self.pickDatasetsController.getSelectedDatasets()
                if len(datasetsSelected) != 1:
                    return

                datasetToLoad = datasetsSelected[0]

            attrs = SharedAttributes.fromHDF5File(file, datasetToLoad)
            self.__commChannel.sharedAttrs.update(attrs)

    def pickSetup(self):
        """ Let the user change which setup is used. """

        options, _ = configfiletools.loadOptions()

        self.pickSetupController.setSetups(configfiletools.getSetupList())
        self.pickSetupController.setSelectedSetup(options.setupFileName)
        if not self.__mainView.showPickSetupDialogBlocking():
            return
        setupFileName = self.pickSetupController.getSelectedSetup()
        if not setupFileName:
            return

        proceed = guitools.askYesNoQuestion(self.__mainView, 'Warning',
                                            'The software will restart. Continue?')
        if not proceed:
            return

        options = dataclasses.replace(options, setupFileName=setupFileName)
        configfiletools.saveOptions(options)
        ostools.restartSoftware()

    def saveWidgetState(self):
        """Save widget states to a JSON file selected by the user."""
        filePath = guitools.askForFilePath(
            self.__mainView, 
            'Save Widget States', 
            nameFilter='JSON files (*.json)',
            isSaving=True
        )
        if not filePath:
            return
        
        # Add .json extension if not present
        if not filePath.endswith('.json'):
            filePath = filePath + '.json'
        
        try:
            persistence = getWidgetStatePersistence()
            persistence.save_to_file(filePath)
            self.__logger.info(f'Widget states saved to {filePath}')
            QtWidgets.QMessageBox.information(
                self.__mainView, 
                'Save Successful', 
                f'Widget states saved successfully to:\n{filePath}'
            )
        except Exception as e:
            self.__logger.error(f'Failed to save widget states: {e}')
            QtWidgets.QMessageBox.critical(
                self.__mainView, 
                'Save Failed', 
                f'Failed to save widget states:\n{str(e)}'
            )
    
    def loadWidgetState(self):
        """Load widget states from a JSON file selected by the user."""
        filePath = guitools.askForFilePath(
            self.__mainView, 
            'Load Widget States', 
            nameFilter='JSON files (*.json)'
        )
        if not filePath:
            return
        
        try:
            persistence = getWidgetStatePersistence()
            persistence.load_from_file(filePath)
            self.__logger.info(f'Widget states loaded from {filePath}')
            QtWidgets.QMessageBox.information(
                self.__mainView, 
                'Load Successful', 
                f'Widget states loaded successfully from:\n{filePath}'
            )
        except Exception as e:
            self.__logger.error(f'Failed to load widget states: {e}')
            QtWidgets.QMessageBox.critical(
                self.__mainView, 
                'Load Failed', 
                f'Failed to load widget states:\n{str(e)}'
            )

    def openShortcutEditor(self):
        """Open the keyboard shortcut editor dialog."""
        from imswitch.imcontrol.view.widgets.ShortcutEditorDialog import ShortcutEditorDialog
        
        dialog = ShortcutEditorDialog(self.__mainView, self.__shortcutManager)
        dialog.exec_()

    def _registerPositionerJogActions(self):
        """Register dynamic per-positioner axis jog actions with shortcutModifier alias expansion.
        
        Phase 3c: Each positioner's each axis gets plus/minus actions with action IDs like
        `positioner.<name>.<axis>.plus`. The defaultKeySequence is computed from:
        - shortcutModifier "ctrl" -> Ctrl+Arrow/Y/A keys
        - shortcutModifier "ctrl-shift" -> Ctrl+Shift+Arrow/Y/A keys
        - no shortcutModifier -> legacy first-come behavior (first such positioner per axis gets Ctrl keys)
        
        Explicit config in the shortcuts map always overrides these defaults.
        """
        if 'Positioner' not in self.controllers:
            return
        
        positionerController = self.controllers['Positioner']
        positionerWidget = positionerController._widget
        
        if not hasattr(self.__setupInfo, 'positioners') or not self.__setupInfo.positioners:
            return
        
        # Compute default jog key sequences via the shared, unit-tested pure
        # function (shortcutModifier alias expansion + legacy first-come).
        from imswitch.imcommon.model import ShortcutScope
        from imswitch.imcontrol.controller.ShortcutManager import computePositionerJogDefaults

        jogDefaults = computePositionerJogDefaults(self.__setupInfo.positioners)

        for positionerName, positionerInfo in self.__setupInfo.positioners.items():
            for axis in positionerInfo.axes:
                for direction, label in (('plus', '+'), ('minus', '-')):
                    actionId = f'positioner.{positionerName}.{axis}.{direction}'
                    defaultKey = jogDefaults.get(actionId)
                    self.__shortcutManager.registerAction(
                        actionId=actionId,
                        displayName=f'{positionerName} {axis} {label}',
                        callback=(lambda pName=positionerName, ax=axis, d=direction:
                                  positionerWidget.stepAxis(pName, ax, d)),
                        defaultKeySequence=defaultKey,
                        scope=ShortcutScope.Application,
                        owner=positionerWidget,
                        initiallyBound=(defaultKey is not None)
                    )
    
    def closeEvent(self):
        self.__logger.info('Shutting down')
        try:
            getWidgetStatePersistence().saveAllWidgetStates('default')
        except Exception as e:
            self.__logger.warning(f'Failed to auto-save widget states: {e}')
        self.__factory.closeAllCreatedControllers()
        self.__masterController.closeEvent()


class _GuiLayoutStateAdapter:
    """Persistence adapter for passive imcontrol dock layout state.
    
    GuiLayout is STARTUP_RESTORE-only and layout-only: applyComponentState
    restores dock/splitter layout in both modes (layout is passive), and it is
    excluded from setup modes by SetupModeController discovery.
    """
    
    # Unified interface attributes
    componentName = 'GuiLayout'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, view: Any) -> None:
        self._view = view

    def getComponentState(self) -> Dict[str, Any]:
        """Snapshot the current GUI layout state."""
        return self._view.getLayoutState()

    def applyComponentState(
        self,
        state: Dict[str, Any],
        *,
        applyMode: Any,
    ) -> list:
        """Restore GUI layout state without triggering hardware actions.
        
        GuiLayout is passive and applies in both modes (layout restoration
        does not activate hardware).
        
        Args:
            state: Layout state dict from getComponentState()
            applyMode: ComponentStateApplyMode (ignored, layout is passive)
        
        Returns:
            Empty list (no warnings, layout is passive)
        """
        self._view.setLayoutState(state)
        return []

    def describeComponentState(self, state: Dict[str, Any]) -> list[str]:
        """Generate human-readable summary of saved GUI layout state.
        
        Args:
            state: Layout state dict from getComponentState()
        
        Returns:
            Simple summary line
        """
        return ["GUI layout: dock/splitter configuration saved"]

    def getComponentStateHazards(
        self,
        state: Dict[str, Any],
        *,
        applyMode: Any,
        context: Dict[str, Any] | None = None,
    ) -> list[dict]:
        """Identify hazards in GUI layout state (always none).
        
        Args:
            state: Layout state dict from getComponentState()
            applyMode: ComponentStateApplyMode (ignored)
            context: Optional context (ignored)
        
        Returns:
            Empty list (layout restoration has no hazards)
        """
        return []


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
