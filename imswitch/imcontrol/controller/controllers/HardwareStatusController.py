from ..basecontrollers import ImConWidgetController


class HardwareStatusController(ImConWidgetController):
    """Feeds the global read-only hardware status window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._widget.sigRefreshRequested.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self._widget.setStatuses(self._master.deviceSupervisor.getAllStatuses())
