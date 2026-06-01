"""Controller for the generic ImProcess graph panel."""

from .basecontrollers import ImProcessWidgetController


class GraphController(ImProcessWidgetController):
    """Render plot payloads for the currently selected processing result."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.sigCurrentResultChanged.connect(self.currentResultChanged)

    def currentResultChanged(self, result) -> None:
        if result is None:
            self._widget.clear()
            return

        plot_payloads = getattr(result, "plot_payloads", None)
        if plot_payloads is None:
            self._widget.clear()
            return

        self._widget.setPlotPayloads(list(plot_payloads()))

