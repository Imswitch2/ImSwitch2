import dataclasses

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

        # Create API-only controllers (no widget needed)
        # WorkflowFacadeController provides build_facade_from_master via API
        self.workflowFacadeController = self.__factory.createController(
            controllers.WorkflowFacadeController,
            widget=None,  # API-only, no widget
        )
        
        # Generate API
        self.__api = None
        apiObjs = list(self.controllers.values()) + [self.__commChannel, self.workflowFacadeController]
        self.__api = generateAPI(
            apiObjs,
            missingAttributeErrorMsg=lambda attr: f'The imcontrol API does either not have any'
                                                  f' method {attr}, or the widget that defines it'
                                                  f' is not included in your currently active'
                                                  f' hardware setup file.'
        )
        # Generate Shorcuts
        self.__shortcuts = None
        shorcutObjs = list(self.__mainView.widgets.values())
        self.__shortcuts = generateShortcuts(shorcutObjs)
        self.__mainView.addShortcuts(self.__shortcuts)

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
    def shortcuts(self):
        return self.__shortcuts

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

    def closeEvent(self):
        self.__logger.info('Shutting down')
        try:
            getWidgetStatePersistence().saveAllWidgetStates('default')
        except Exception as e:
            self.__logger.warning(f'Failed to auto-save widget states: {e}')
        self.__factory.closeAllCreatedControllers()
        self.__masterController.closeEvent()


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
