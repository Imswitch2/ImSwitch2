import dataclasses
from pathlib import Path
from typing import Any, Dict

import h5py
from qtpy import QtCore, QtWidgets
import zarr

from imswitch.imcommon.controller import MainController, PickDatasetsController
from imswitch.imcommon.model import (
    ostools, initLogger, generateAPI, generateShortcuts, SharedAttributes,
    isCriticalRestoreWarning,
    memory_limits,
    shutdownState,
)
from imswitch.imcommon.framework import Thread
from .server.ImSwitchServer import ImSwitchServer
from imswitch.imcontrol.model import configfiletools, getWidgetStatePersistence
from imswitch.imcontrol.view import guitools
from . import controllers
from .CommunicationChannel import CommunicationChannel
from .MasterController import MasterController
from .PickSetupController import PickSetupController
from .SetupModeController import SetupModeController
from .SmartMicroscopyModeService import SmartMicroscopyModeService
from .ShortcutManager import ShortcutManager
from .basecontrollers import ImConWidgetControllerFactory


_SERVER_THREAD_STOP_TIMEOUT_MS = 5000


def _activeSetupPath(options):
    """ Where this session's setup file lives, resolved, or None.

    Resolved because it is compared against paths the config editor wrote, and
    the same file can be spelled several ways. None rather than raising: this
    is bookkeeping for a menu item, and nothing about it is worth stopping
    imcontrol from starting over.
    """
    try:
        return str(Path(configfiletools.getSetupFilePath(options.setupFileName)).resolve())
    except Exception:  # noqa: BLE001
        return None


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
        self.__mainView.sigLoadParamsFromZarr.connect(self.loadParamsFromZarr)
        self.__mainView.sigPickSetup.connect(self.pickSetup)
        self.__mainView.sigClosing.connect(self.closeEvent)
        self.__mainView.sigSaveWidgetState.connect(self.saveWidgetState)
        self.__mainView.sigLoadWidgetState.connect(self.loadWidgetState)
        self.__mainView.sigOpenShortcutEditor.connect(self.openShortcutEditor)
        self.__mainView.sigOpenSessionNotes.connect(self.openSessionNotes)
        self.__mainView.sigOpenConfigEditor.connect(self.openConfigEditor)
        self.__mainView.sigOpenMemoryLimits.connect(self.openMemoryLimits)
        self.__mainView.memoryLimitsDialog.sigSaveRequested.connect(self.saveMemoryLimits)
        self.__mainView.sessionNotesDialog.sigNotesChanged.connect(self.setSessionNote)

        # The Config Studio, while it is open. One window at a time: a second
        # copy of the same file in a second editor is how edits get lost.
        self.__configEditor = None
        # The setup file this session is running on, resolved once here. What
        # the options file says can change underneath us -- the editor itself
        # can change it -- but this process keeps running what it loaded at
        # startup, and that is what the restart prompt has to reason about.
        self.__activeSetupPath = _activeSetupPath(options)

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel(self, self.__setupInfo)
        self.__masterController = None
        self.__factory = None
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

        # Smart microscopy mode-switching service. Event controllers receive the
        # handle here; Phase 3 decides when they start using it for transitions.
        self.smartModeService = SmartMicroscopyModeService(
            self.setupModeController,
            getattr(self.__setupInfo, 'smartMicroscopyModes', None),
            policyConfig=getattr(self.__setupInfo, 'smartMicroscopyModePolicies', None),
        )
        for controller in self.controllers.values():
            if hasattr(controller, 'setSmartModeService'):
                controller.setSmartModeService(self.smartModeService)

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
            actionId='app.loadParamsZarr',
            displayName='Load parameters from Zarr',
            callback=lambda: self.__mainView.sigLoadParamsFromZarr.emit(),
            defaultKeySequence='Ctrl+Alt+P',
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

        # Positioner-wide actions live in the same global shortcut catalog as jog actions.
        if 'Positioner' in self.controllers:
            positionerController = self.controllers['Positioner']
            positionerWidget = positionerController._widget
            self.__shortcutManager.registerAction(
                actionId='positioner.toggleCoarseFine',
                displayName='Positioner: toggle coarse/fine',
                callback=positionerController.toggleStepMode,
                defaultKeySequence=None,
                scope=ShortcutScope.Application,
                owner=positionerWidget,
                initiallyBound=False,
            )
            if positionerController._getJoystickPositionerName() is not None:
                self.__shortcutManager.registerAction(
                    actionId='positioner.toggleJoystick',
                    displayName='Positioner: toggle joystick',
                    callback=positionerController.toggleJoystick,
                    defaultKeySequence=None,
                    scope=ShortcutScope.Application,
                    owner=positionerWidget,
                    initiallyBound=False,
                )
        
        self.__shortcutManager.loadConfigOverrides(self.__setupInfo.shortcuts)
        self.__shortcutManager.computeEffectiveBindings()
        self.__shortcutManager.build(self.__mainView.shortcutsMenu, self.__mainView)
        
        # Update File menu to show effective shortcuts
        self.__mainView.updateMenuActionShortcuts(self.__shortcutManager.getEffectiveBindings())

        # Only the visible module tab's shortcut set may be live: all module
        # tabs share one top-level window, so Window/Application-scoped
        # bindings from a hidden tab would collide with the visible module's.
        if hasattr(self.__mainView, 'sigModuleVisibilityChanged'):
            self.__mainView.sigModuleVisibilityChanged.connect(
                self.__shortcutManager.setBindingsEnabled
            )
            self.__shortcutManager.setBindingsEnabled(self.__mainView.isVisible())

        # Inject ShortcutManager into SetupModesController (Phase 3d)
        if 'SetupModes' in self.controllers:
            self.controllers['SetupModes'].setShortcutManager(
                self.__shortcutManager,
                self.__mainView.shortcutsMenu,
                self.__mainView
            )

        # The panels only got their rows once their controllers ran, so the
        # dock proportions the view guessed while they were still empty are
        # re-derived here.  Before the saved layout is restored: a saved
        # layout wins over content-derived sizes.
        self.__mainView.applyContentAwareDockSizing()

        self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(self.__mainView)
        getWidgetStatePersistence().register('GuiLayout', self.__guiLayoutStateAdapter)

        # Auto-restore widget states after all controllers are ready
        try:
            restoreWarnings = []
            getWidgetStatePersistence().loadAllWidgetStates(
                'default', warnings_out=restoreWarnings
            )
            if restoreWarnings:
                self._showWidgetStateRestoreWarnings(
                    'Some settings could not be restored', restoreWarnings
                )
        except Exception as e:
            self.__logger.warning(f'Failed to auto-restore widget states: {e}')

        # Everything is built and any saved layout has been applied: settle the
        # dock proportions once the window is actually on screen.
        self.__mainView.scheduleInitialDockLayout()

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

        filePath = guitools.askForFilePath(
            self.__mainView, 'Open HDF5 file', nameFilter='HDF5 files (*.h5 *.hdf5)'
        )
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

    def loadParamsFromZarr(self):
        """ Set detector, positioner, laser etc. params from values saved in a
        user-picked Zarr snap/recording store. Twin of loadParamsFromHDF5 for
        the Zarr recording layout (a store is a directory, not a single file). """

        folderPath = guitools.askForFolderPath(self.__mainView, 'Open Zarr store')
        if not folderPath:
            return

        root = zarr.open(folderPath, mode='r')
        datasetsInStore = list(root.keys())
        if len(datasetsInStore) < 1:
            # Store does not contain any datasets
            return
        elif len(datasetsInStore) == 1:
            datasetToLoad = datasetsInStore[0]
        else:
            # Store contains multiple datasets
            self.pickDatasetsController.setDatasets(folderPath, datasetsInStore)
            if not self.__mainView.showPickDatasetsDialogBlocking():
                return

            datasetsSelected = self.pickDatasetsController.getSelectedDatasets()
            if len(datasetsSelected) != 1:
                return

            datasetToLoad = datasetsSelected[0]

        attrs = SharedAttributes.fromZarrStore(root, datasetToLoad)
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
        self._restartAfterShutdown()

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
            restoreWarnings = []
            persistence.load_from_file(filePath, warnings_out=restoreWarnings)
            self.__logger.info(f'Widget states loaded from {filePath}')
            reported = self._showWidgetStateRestoreWarnings(
                'Settings loaded with warnings', restoreWarnings
            )
            if not reported:
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

    def _showWidgetStateRestoreWarnings(self, title, warnings):
        """Make state-restore failures visible without crying wolf.

        Only warnings that mean hardware did not take a setting get a dialog.
        The routine ones -- a saved detector that this setup file doesn't have,
        a parameter that no longer exists -- are logged instead: they fire on
        every startup whenever a state file predates a setup change, and a
        dialog the operator dismisses by reflex is worse than no dialog at all
        when a trigger mode really has failed to apply.

        Returns True when a dialog was shown.
        """
        critical = [warning for warning in warnings if isCriticalRestoreWarning(warning)]
        informational = [warning for warning in warnings
                         if not isCriticalRestoreWarning(warning)]

        if informational:
            self.__logger.info(
                f'{title} -- saved settings that no longer apply: {informational}'
            )

        if not critical:
            return False

        self.__logger.warning(f'{title}: {critical}')
        warningText = '\n'.join(f'• {warning}' for warning in critical)
        QtWidgets.QMessageBox.warning(
            self.__mainView,
            title,
            'Some saved settings were not applied to the hardware. '
            'Check these details before acquiring data.\n\n'
            f'{warningText}',
        )
        return True

    def openShortcutEditor(self):
        """Open the keyboard shortcut editor dialog."""
        from imswitch.imcontrol.view.widgets.ShortcutEditorDialog import ShortcutEditorDialog
        
        dialog = ShortcutEditorDialog(self.__mainView, self.__shortcutManager, self.__setupInfo)
        dialog.exec_()

    def openSessionNotes(self):
        """Show the session-notes editor, seeded with the current note.

        Seeded rather than trusted to remember: the note lives in the shared
        attributes, which the rest of the application can also write -- loading
        parameters from a saved file brings that file's note along with
        everything else it restores.
        """
        dialog = self.__mainView.sessionNotesDialog
        dialog.setNotes(self.__commChannel.getSessionNote())
        self.__mainView.showSessionNotesDialog()

    def openMemoryLimits(self):
        """Show the memory-limits editor, seeded with what the options file holds."""
        options, _ = configfiletools.loadOptions()
        dialog = self.__mainView.memoryLimitsDialog
        dialog.setValues(getattr(options, 'memory', None))
        self.__mainView.showMemoryLimitsDialog()

    def saveMemoryLimits(self, values):
        """Save the memory limits to the options file and adopt them now.

        Refused while a recording runs: every queue check reads the limit in
        force, so lowering a queue below what it holds would fail the
        recording in progress. The dialog stays open with the reason.
        """
        dialog = self.__mainView.memoryLimitsDialog
        master = self.__masterController
        recordingManager = getattr(master, 'recordingManager', None)
        if recordingManager is not None and getattr(recordingManager, 'record', False):
            dialog.setStatus(
                'A recording is running. Stop it first: a smaller queue '
                'would fail the recording in progress.'
            )
            return

        from imswitch.imcontrol.model.Options import MemoryOptions
        try:
            memory = MemoryOptions(**{field: int(value) for field, value in values.items()})
            options, _ = configfiletools.loadOptions()
            configfiletools.saveOptions(dataclasses.replace(options, memory=memory))
        except Exception as e:
            self.__logger.error(f'Could not save the memory limits: {e}', exc_info=True)
            dialog.setStatus(f'Could not save the memory limits: {e}')
            return
        memory_limits.configure(memory, logger=self.__logger)
        dialog.setStatus('')
        dialog.accept()

    def openConfigEditor(self):
        """ Open the Config Studio on this microscope's setup files.

        Not modal: editing a setup file changes nothing about the running
        microscope, so there is no reason to lock the operator out of it while
        they work. Reused rather than reopened, so that two windows can never
        hold different edits of the same file.
        """
        if self.__configEditor is not None:
            self.__configEditor.show()
            self.__configEditor.raise_()
            self.__configEditor.activateWindow()
            return

        try:
            from imswitch.imcontrol.view.configeditor import MainWindow as ConfigEditor
            editor = ConfigEditor(
                start_folder=configfiletools.getSetupFilesDir(),
                parent=self.__mainView,
            )
        except Exception as e:
            self.__logger.error(f'Failed to open the config editor: {e}', exc_info=True)
            QtWidgets.QMessageBox.critical(
                self.__mainView,
                'Could not open the config editor',
                f'The hardware configuration editor failed to start:\n{e}',
            )
            return

        editor.sig_closed.connect(self._onConfigEditorClosed)
        self.__configEditor = editor
        editor.show()

    def _onConfigEditorClosed(self):
        """ Offer a restart if the editor changed what this session is running.

        Only if: a setup file ImSwitch is not using can be edited all day
        without any of it mattering, and a prompt that appears every time the
        editor closes is one the operator learns to dismiss unread.
        """
        editor = self.__configEditor
        if editor is None:
            return
        self.__configEditor = None

        needsRestart = (
            editor.active_config_changed
            or (self.__activeSetupPath is not None
                and self.__activeSetupPath in editor.saved_files())
        )
        editor.deleteLater()

        if not needsRestart:
            return

        # Off the editor's own closeEvent before touching the main window: the
        # answer may be to close the application, and doing that from inside a
        # widget that is still closing is asking for trouble.
        QtCore.QTimer.singleShot(0, self._promptRestartAfterConfigEdit)

    def _promptRestartAfterConfigEdit(self):
        proceed = guitools.askYesNoQuestion(
            self.__mainView,
            'Restart ImSwitch?',
            'ImSwitch reads the hardware configuration once, at startup, so the'
            ' changes you just saved are not in effect yet.\n\n'
            'Restart ImSwitch now?'
        )
        if not proceed:
            return
        self._restartAfterShutdown()

    def _restartAfterShutdown(self):
        """ Close the application, then come back up.

        Deliberately not ``ostools.restartSoftware()``: that replaces the
        process there and then, so detectors, stages and lasers are never
        finalized and the new process inherits whatever they were doing. Asking
        for the restart and closing the window runs the ordinary shutdown
        first, and ``launchApp`` re-execs once it is done.
        """
        ostools.restartAfterShutdown(self.__mainView.window().close)

    def setSessionNote(self, note: str) -> None:
        """Publish the operator's free-text note for this session.

        Recordings snapshot the shared attributes when they start, so this
        reaches every file saved after it and none saved before.
        """
        self.__commChannel.setSessionNote(note)

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

        visiblePositioners = {}
        for positionerName, positionerInfo in self.__setupInfo.positioners.items():
            if not positionerInfo.forPositioning or getattr(positionerInfo, 'hide', False):
                continue
            try:
                self.__masterController.positionersManager[positionerName]
            except Exception:
                continue
            # Keep shortcuts registered for configured/visible positioners even
            # when hardware is unavailable at startup. Runtime reconnect can then
            # make the existing widget + actions usable without rebuilding them.
            visiblePositioners[positionerName] = positionerInfo

        jogDefaults = computePositionerJogDefaults(visiblePositioners)

        for positionerName, positionerInfo in visiblePositioners.items():
            for axis in positionerInfo.axes:
                for direction, label in (('plus', '+'), ('minus', '-')):
                    actionId = f'positioner.{positionerName}.{axis}.{direction}'
                    defaultKey = jogDefaults.get(actionId)
                    self.__shortcutManager.registerAction(
                        actionId=actionId,
                        displayName=f'{positionerName} {axis} {label}',
                        callback=(lambda *_, pName=positionerName, ax=axis, d=direction:
                                  positionerWidget.stepAxis(pName, ax, d)),
                        defaultKeySequence=defaultKey,
                        scope=ShortcutScope.Application,
                        owner=positionerWidget,
                        initiallyBound=(defaultKey is not None)
                    )
    
    def closeEvent(self):
        self.__logger.info('Shutting down')
        try:
            saveWidgetState = self._shouldSaveWidgetStateOnClose()
        except Exception as e:
            self.__logger.warning(
                f'Failed to ask whether widget states should be saved; '
                f'saving by default: {e}'
            )
            saveWidgetState = True

        if saveWidgetState:
            try:
                getWidgetStatePersistence().saveAllWidgetStates('default')
            except Exception as e:
                self.__logger.warning(f'Failed to auto-save widget states: {e}')
        
        # Stop server thread before closing hardware managers. A server that is
        # still executing can retain API access into controllers and hardware,
        # so its bounded join is a hard prerequisite for manager finalization.
        serverStopped = True
        if hasattr(self, '_serverWorker') and hasattr(self, '_thread'):
            try:
                self.__logger.debug('Stopping server thread')
                self._serverWorker.stop()
                self._thread.quit()
                if isinstance(self._thread, QtCore.QThread):
                    # framework.Thread intentionally exposes wait() without a
                    # timeout. Call the Qt base implementation so shutdown never
                    # falls back to an unbounded join.
                    serverStopped = bool(
                        QtCore.QThread.wait(
                            self._thread,
                            _SERVER_THREAD_STOP_TIMEOUT_MS,
                        )
                    )
                else:
                    # Lightweight test/fallback threads may expose the bounded
                    # signature directly.
                    serverStopped = (
                        self._thread.wait(
                            _SERVER_THREAD_STOP_TIMEOUT_MS
                        )
                        is not False
                    )
                if not serverStopped:
                    self.__logger.error(
                        'Server thread did not stop within timeout'
                    )
            except Exception as e:
                serverStopped = False
                self.__logger.error(
                    f'Error stopping server thread: {e}',
                    exc_info=True,
                )
        
        controllersClosed = True
        if self.__factory is not None:
            controllersClosed = self.__factory.closeAllCreatedControllers(
                waitTimeoutS=30.0
            )
        if self.__masterController is not None and not serverStopped:
            self.__logger.error(
                'Skipping hardware-manager finalization because the server '
                'thread did not drain within the shutdown deadline.'
            )
            return False
        if (
            self.__masterController is not None
            and not shutdownState.hardwareFinalizationAllowed()
        ):
            # Same fail-closed rule as for the server thread and controller
            # workers: a script thread that did not drain may still be inside
            # a manager call.
            self.__logger.error(
                'Skipping hardware-manager finalization because a script did '
                'not stop within the shutdown deadline: '
                + '; '.join(shutdownState.reasons)
            )
            return False
        if self.__masterController is not None and controllersClosed is not False:
            hardwareClosed = self.__masterController.closeEvent()
            if hardwareClosed is False:
                self.__logger.error(
                    'Hardware-manager shutdown did not complete; application '
                    'close must remain fail-closed.'
                )
                return False
        elif self.__masterController is not None:
            # Fail closed: retained acquisition leases protect detector
            # refcounts, but they cannot make manager.finalize() safe while a
            # worker is inside a backend call.
            self.__logger.error(
                'Skipping hardware-manager finalization because controller '
                'workers did not drain within the shutdown deadline.'
            )
            return False
        return controllersClosed is not False

    def _shouldSaveWidgetStateOnClose(self):
        result = QtWidgets.QMessageBox.question(
            self.__mainView,
            'Save Widget State',
            'Save the current widget state as the default for the next startup?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        return result == QtWidgets.QMessageBox.Yes


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
