from abc import ABCMeta

import sip
from qtpy import QtCore

import imswitch.imcommon.framework.base as base


class QObjectMeta(type(QtCore.QObject), ABCMeta):
    pass


class Mutex(QtCore.QMutex, base.Mutex, metaclass=QObjectMeta):
    pass


class Signal(base.Signal):
    def __new__(cls, *argtypes) -> base.Signal:
        return QtCore.Signal(*argtypes)


class SignalInterface(QtCore.QObject, base.SignalInterface, metaclass=QObjectMeta):
    pass


class Thread(QtCore.QThread, base.Thread, metaclass=QObjectMeta):
    def quit(self) -> None:
        if not self.__isWrappedCObjDeleted():
            super().quit()

    def wait(self, timeoutMs=None) -> bool:
        if self.__isWrappedCObjDeleted():
            return True
        if timeoutMs is None:
            super().wait()
            return True
        return bool(super().wait(max(0, int(timeoutMs))))

    def __isWrappedCObjDeleted(self) -> bool:
        try:
            sip.unwrapinstance(self)
        except RuntimeError:
            return True
        return False


class Timer(QtCore.QTimer, base.Timer, metaclass=QObjectMeta):
    pass


class Worker(QtCore.QObject, base.Worker, metaclass=QObjectMeta):
    pass


class FrameworkUtils(base.FrameworkUtils):
    @staticmethod
    def processPendingEventsCurrThread():
        dispatcher = QtCore.QAbstractEventDispatcher.instance(
            QtCore.QThread.currentThread()
        )
        if dispatcher is None:
            # A plain Python thread has no Qt event dispatcher; nothing to pump.
            return
        dispatcher.processEvents(QtCore.QEventLoop.AllEvents)
