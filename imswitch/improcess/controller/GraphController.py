"""Controller for the generic ImProcess graph panel."""

from .basecontrollers import ImProcessWidgetController


class GraphController(ImProcessWidgetController):
    """Render plot payloads for the currently selected processing result."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.sigCurrentResultChanged.connect(self.currentResultChanged)
        preview = getattr(self._commChannel, "sigWorkflowPreview", None)
        if preview is not None:
            preview.connect(self.workflowPreview)

    def workflowPreview(self, result, _name="") -> None:
        """A workflow-editor preview: plot it when it has something to plot."""
        payloads = getattr(result, "plot_payloads", None)
        if payloads is None:
            return
        plots = list(payloads())
        if plots:
            self._widget.setPlotPayloads(plots)

    def currentResultChanged(self, result) -> None:
        if result is None:
            self._widget.clear()
            return

        plot_payloads = getattr(result, "plot_payloads", None)
        if plot_payloads is None:
            self._widget.clear()
            return

        self._widget.setPlotPayloads(list(plot_payloads()))

