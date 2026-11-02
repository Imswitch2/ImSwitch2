from qtpy import QtCore

from imswitch.imcontrol.model.devices import DeviceLifecycleAction

from ..basecontrollers import ImConWidgetController


_BUSY_TEXT = {
    DeviceLifecycleAction.RECONNECT: "Reconnecting device…",
    DeviceLifecycleAction.CONNECT: "Connecting device…",
    DeviceLifecycleAction.DISCONNECT: "Disconnecting device…",
    DeviceLifecycleAction.PROBE: "Checking device…",
}


class _LifecycleWorker(QtCore.QThread):
    sigCompleted = QtCore.Signal(object)
    sigFailed = QtCore.Signal(str)

    def __init__(self, lifecycleService, hardware_id, action=DeviceLifecycleAction.RECONNECT):
        super().__init__()
        self._lifecycleService = lifecycleService
        self._hardwareId = hardware_id
        self._action = DeviceLifecycleAction(action)

    def run(self):
        try:
            result = getattr(self._lifecycleService, self._action.value)(self._hardwareId)
        except Exception as exc:
            self.sigFailed.emit(str(exc))
        else:
            self.sigCompleted.emit(result)


class HardwareStatusController(ImConWidgetController):
    """Feeds the hardware-status window and dispatches opt-in lifecycle
    actions (reconnect; connect / disconnect for transient instruments)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reconnectWorker = None
        self._reconnectMessage = ""
        self._closing = False
        self._statusListener = None
        self._widget.sigRefreshRequested.connect(self.refresh)
        self._widget.sigReconnectRequested.connect(self.reconnect)
        self._widget.sigConnectRequested.connect(self.connect)
        self._widget.sigDisconnectRequested.connect(self.disconnect)
        self._widget.sigProbeRequested.connect(self.probe)
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None and hasattr(lifecycleService, 'addStatusListener'):
            # Instrument faults arrive on run / poller threads.
            self._statusListener = lambda _hardware_id: (
                self._invokeOnControllerThreadIfNeeded(self.refresh)
            )
            lifecycleService.addStatusListener(self._statusListener)
        self.refresh()
        QtCore.QTimer.singleShot(0, self.refresh)

    def refresh(self):
        if self.__dict__.get('_closing'):
            return
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        getIds = getattr(lifecycleService, 'getActionableHardwareIds', None)
        actionable = {
            action: getIds(action) if callable(getIds) else ()
            for action in DeviceLifecycleAction
        }
        self._widget.setStatuses(
            self._master.deviceSupervisor.getHardwareStatuses(),
            reconnectableHardwareIds=actionable[DeviceLifecycleAction.RECONNECT],
            connectableHardwareIds=actionable[DeviceLifecycleAction.CONNECT],
            disconnectableHardwareIds=actionable[DeviceLifecycleAction.DISCONNECT],
            probeableHardwareIds=actionable[DeviceLifecycleAction.PROBE],
        )

    def reconnect(self, hardware_id):
        self._start(hardware_id, DeviceLifecycleAction.RECONNECT)

    def connect(self, hardware_id):
        self._start(hardware_id, DeviceLifecycleAction.CONNECT)

    def disconnect(self, hardware_id):
        self._start(hardware_id, DeviceLifecycleAction.DISCONNECT)

    def probe(self, hardware_id):
        self._start(hardware_id, DeviceLifecycleAction.PROBE)

    def _start(self, hardware_id, action):
        verb = action.value.capitalize()
        if self._closing:
            self._widget.setReconnectBusy(False, f"{verb} refused: shutting down")
            return
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is None:
            self._widget.setReconnectBusy(False, "Device lifecycle service unavailable")
            return
        worker = self._reconnectWorker
        if worker is not None and worker.isRunning():
            return

        self._reconnectMessage = ""
        self._widget.setReconnectBusy(True, _BUSY_TEXT[action])
        worker = _LifecycleWorker(lifecycleService, hardware_id, action)
        self._reconnectWorker = worker
        worker.sigCompleted.connect(self._reconnectCompleted)
        worker.sigFailed.connect(
            lambda message, verb=verb: self._reconnectFailed(message, verb)
        )
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

    def _reconnectFailed(self, message, verb="Reconnect"):
        self.refresh()
        self._reconnectMessage = f"{verb} blocked/failed: {message}"

    def _reconnectThreadFinished(self, worker):
        worker.deleteLater()
        if self._reconnectWorker is worker:
            self._reconnectWorker = None
            self._widget.setReconnectBusy(False, self._reconnectMessage)

    # Shutdown barrier: a lifecycle operation still running may install a new
    # backend, so hardware managers must not be finalized until it returned.
    def closeEvent(self) -> bool:
        self._closing = True
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        beginShutdown = getattr(lifecycleService, 'beginShutdown', None)
        if callable(beginShutdown):
            beginShutdown()
        listener = self.__dict__.get('_statusListener')
        if listener is not None and lifecycleService is not None:
            lifecycleService.removeStatusListener(listener)
            self._statusListener = None
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Whether no lifecycle worker (and no service operation) is running."""
        worker = self.__dict__.get('_reconnectWorker')
        if worker is not None and worker.isRunning():
            return False
        lifecycleService = getattr(self.__dict__.get('_master'), 'deviceLifecycleService', None)
        inFlight = getattr(lifecycleService, 'operationsInFlight', None)
        return not (callable(inFlight) and inFlight())
