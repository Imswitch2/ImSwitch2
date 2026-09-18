from imswitch.imcommon.controller import MainController
from imswitch.imcommon.model import (
    apiGate, generateAPI, initLogger, pythontools, shutdownState,
)
from imswitch.imscripting.model import getActionsScope
from .CommunicationChannel import CommunicationChannel
from .ImScrMainViewController import ImScrMainViewController
from .basecontrollers import ImScrWidgetControllerFactory


class ImScrMainController(MainController):
    """ Main controller of imscripting. """

    def __init__(self, mainView, moduleCommChannel, multiModuleWindowController,
                 moduleMainControllers):
        self.__mainView = mainView
        self.__moduleCommChannel = moduleCommChannel
        self.__scriptScope = self._createScriptScope(moduleCommChannel, multiModuleWindowController,
                                                     moduleMainControllers)

        # Connect view signals
        self.__mainView.sigClosing.connect(self.closeEvent)

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel()

        # List of Controllers for the GUI Widgets
        self.__factory = ImScrWidgetControllerFactory(
            self.__scriptScope, self.__commChannel, self.__moduleCommChannel
        )

        self.mainViewController = self.__factory.createController(
            ImScrMainViewController, self.__mainView
        )

        # Connect signals from ModuleCommunicationChannel
        self.__moduleCommChannel.sigRunScript.connect(self.__commChannel.sigRunScript)

    def _createScriptScope(self, moduleCommChannel, multiModuleWindowController,
                           moduleMainControllers):
        """ Generates a scope of objects that are intended to be accessible by scripts. """

        scope = {}
        scope.update({
            'moduleCommChannel': moduleCommChannel,
            'mainWindow': generateAPI([multiModuleWindowController]),
            'controllers': pythontools.dictToROClass(moduleMainControllers),
            'api': pythontools.dictToROClass(
                {key: controller.api
                 for key, controller in moduleMainControllers.items()
                 if hasattr(controller, 'api')}
            )
        })
        scope.update(getActionsScope(scope.copy()))

        return scope

    #: Hard cap for draining a running script at application exit (Q-12).
    SHUTDOWN_DRAIN_CAP_MS = 10000

    def prepareShutdown(self):
        """ Application shutdown phase, run before any module's closeEvent
        (see imcommon.applaunch.shutdownModules): close the API gate so no
        script or remote call can reach controllers any more, then drain the
        script thread with a bound. The outcome is recorded in the shared
        ShutdownState; imcontrol refuses to finalize hardware managers while
        a script thread is still alive. """
        logger = initLogger(self)
        shutdownState.begin()
        apiGate.close()
        executor = self._scriptExecutor()
        if executor is None:
            shutdownState.recordScriptingDrain(True)
            return True
        running = executor.getCurrentResult()
        drained = executor.shutdown(timeoutMs=self.SHUTDOWN_DRAIN_CAP_MS)
        if drained:
            shutdownState.recordScriptingDrain(True)
        else:
            script = getattr(running, 'script_path', None) or '(unsaved script)'
            reason = (
                f'The script {script} did not stop within '
                f'{self.SHUTDOWN_DRAIN_CAP_MS / 1000:g} s'
            )
            logger.error(reason + '; hardware managers will not be finalized.')
            shutdownState.recordScriptingDrain(False, reason)
        return drained

    def _scriptExecutor(self):
        mainViewController = getattr(self, 'mainViewController', None)
        editorController = getattr(mainViewController, 'editorController', None)
        return getattr(editorController, 'scriptExecutor', None)

    def closeEvent(self):
        self.__factory.closeAllCreatedControllers()


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
