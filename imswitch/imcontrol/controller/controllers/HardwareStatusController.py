from qtpy import QtCore

from ..basecontrollers import ImConWidgetController


class _ReconnectWorker(QtCore.QThread):
    sigCompleted = QtCore.Signal(object)
    sigFailed = QtCore.Signal(str)

    def __init__(self, lifecycleService, hardware_id):
        super().__init__()
        self._lifecycleService = lifecycleService
        self._hardwareId = hardware_id

    def run(self):
        try:
            result = self._lifecycleService.reconnect(self._hardwareId)
        except Exception as exc:
            self.sigFailed.emit(str(exc))
        else:
            self.sigCompleted.emit(result)


class HardwareStatusController(ImConWidgetController):
    """Feeds the hardware-status window and dispatches opt-in reconnects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reconnectWorker = None
        self._reconnectMessage = ""
        self._closing = False
        self._widget.sigRefreshRequested.connect(self.refresh)
        self._widget.sigReconnectRequested.connect(self.reconnect)
        self.refresh()
        QtCore.QTimer.singleShot(0, self.refresh)

    def refresh(self):
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        reconnectable = (
            lifecycleService.getReconnectableHardwareIds()
            if lifecycleService is not None
            else ()
        )
        self._widget.setStatuses(
            self._master.deviceSupervisor.getHardwareStatuses(),
            reconnectableHardwareIds=reconnectable,
        )

    def reconnect(self, hardware_id):
        if self._closing:
            self._widget.setReconnectBusy(False, "Reconnect refused: shutting down")
            return
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is None:
            self._widget.setReconnectBusy(False, "Reconnect service unavailable")
            return
        worker = self._reconnectWorker
        if worker is not None and worker.isRunning():
            return

        self._reconnectMessage = ""
        self._widget.setReconnectBusy(True, "Reconnecting device…")
        worker = _ReconnectWorker(lifecycleService, hardware_id)
        self._reconnectWorker = worker
        worker.sigCompleted.connect(self._reconnectCompleted)
        worker.sigFailed.connect(self._reconnectFailed)
        worker.finished.connect(
            lambda worker=worker: self._reconnectThreadFinished(worker)
        )
        worker.start()

    def _reconnectCompleted(self, result):
        self.refresh()
        message = result.summary
        if not result.success and result.details:
            message = f"{message}: {result.details}"
        self._reconnectMessage = message

    def _reconnectFailed(self, message):
        self.refresh()
        self._reconnectMessage = f"Reconnect blocked/failed: {message}"

    def _reconnectThreadFinished(self, worker):
        worker.deleteLater()
        if self._reconnectWorker is worker:
            self._reconnectWorker = None
            self._widget.setReconnectBusy(False, self._reconnectMessage)

    # Shutdown barrier: a reconnect still running may install a new backend,
    # so hardware managers must not be finalized until it has returned.
    def closeEvent(self) -> bool:
        self._closing = True
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        beginShutdown = getattr(lifecycleService, 'beginShutdown', None)
        if callable(beginShutdown):
            beginShutdown()
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Whether no reconnect worker (and no service operation) is running."""
        worker = self.__dict__.get('_reconnectWorker')
        if worker is not None and worker.isRunning():
            return False
        lifecycleService = getattr(self.__dict__.get('_master'), 'deviceLifecycleService', None)
        inFlight = getattr(lifecycleService, 'operationsInFlight', None)
        return not (callable(inFlight) and inFlight())
