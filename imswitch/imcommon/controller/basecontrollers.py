import traceback
import time
import weakref

from imswitch.imcommon.framework import FrameworkUtils, SignalInterface
from imswitch.imcommon.model import initLogger


class MainController:
    def closeEvent(self):
        pass


class WidgetController(SignalInterface):
    """ Superclass for all WidgetControllers. """

    def __init__(self, widget, factory, moduleCommChannel, *args, **kwargs):
        self._widget = widget
        self._factory = factory
        self._moduleCommChannel = moduleCommChannel
        self._logger = initLogger(self)
        super().__init__()

    def closeEvent(self):
        pass

    @classmethod
    def create(cls, widget, moduleCommChannel):
        """ Initialize a factory and create this controller with it. Returns
        the created controller. """
        factory = WidgetControllerFactory(moduleCommChannel)
        return factory.createController(cls, widget)


class WidgetControllerFactory:
    """ Factory class for creating a WidgetController object. """

    def __init__(self, moduleCommChannel, *args, **kwargs):
        self.__moduleCommChannel = moduleCommChannel
        self.__args = args
        self.__kwargs = kwargs
        self.__createdControllers = []

        self.__logger = initLogger(self, tryInheritParent=True)

    def createController(self, controllerClass, widget, *args, **kwargs):
        controller = controllerClass(*self.__args, *args,
                                     widget=widget, factory=self,
                                     moduleCommChannel=self.__moduleCommChannel,
                                     **self.__kwargs, **kwargs)
        self.__createdControllers.append(weakref.ref(controller))
        return controller

    def closeAllCreatedControllers(self, waitTimeoutS=0):
        pending = []
        for controllerRef in self.__createdControllers:
            controller = controllerRef()
            if controller is not None:
                try:
                    closeResult = controller.closeEvent()
                    shutdownComplete = getattr(
                        controller, 'shutdownComplete', None
                    )
                    if (
                        closeResult is False
                        or (
                            callable(shutdownComplete)
                            and not shutdownComplete()
                        )
                    ):
                        pending.append(controller)
                except Exception:
                    self.__logger.error(f'Error closing {type(controller).__name__}')
                    self.__logger.error(traceback.format_exc())
                    pending.append(controller)

        deadline = time.monotonic() + max(0.0, float(waitTimeoutS))
        while pending and time.monotonic() < deadline:
            # Scan completion, QThread finished signals, and zero-delay
            # terminal callbacks are queued onto the GUI thread.  A blocking
            # shutdown poll that only sleeps would prevent the very callbacks
            # it is waiting for from running.
            try:
                FrameworkUtils.processPendingEventsCurrThread()
            except Exception:
                # Unit/fallback runtimes can have no Qt event dispatcher.
                pass
            stillPending = []
            for controller in pending:
                shutdownComplete = getattr(
                    controller, 'shutdownComplete', None
                )
                try:
                    if not callable(shutdownComplete) or not shutdownComplete():
                        stillPending.append(controller)
                except Exception:
                    self.__logger.error(
                        f'Error checking shutdown of '
                        f'{type(controller).__name__}'
                    )
                    self.__logger.error(traceback.format_exc())
                    stillPending.append(controller)
            pending = stillPending
            if pending:
                time.sleep(0.05)

        if pending:
            self.__logger.error(
                'Controller shutdown barrier timed out; hardware managers '
                'must not be finalized while these workers are still active: '
                + ', '.join(type(controller).__name__ for controller in pending)
            )
            return False
        return True


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
