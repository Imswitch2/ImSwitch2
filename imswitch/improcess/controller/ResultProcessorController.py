"""Controller for generic result-based processor panels."""

from .basecontrollers import ImProcessWidgetController


class ResultProcessorController(ImProcessWidgetController):
    """Apply a registered Processor to the widget-selected result input."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.sigCurrentResultChanged.connect(self._widget.setCurrentResult)
        self._widget.sigRunRequested.connect(self.runProcessor)

    def runProcessor(self, input_result, params: dict) -> None:
        processor = self._widget.processor
        try:
            output = processor.apply(input_result, params)
        except Exception as exc:
            self._logger.exception(
                "Failed to run processor %s on %s",
                getattr(processor, "id", type(processor).__name__),
                getattr(input_result, "name", type(input_result).__name__),
            )
            self._widget.setStatusText(str(exc))
            return

        display_name = getattr(output, "name", "") or f"{input_result.name}_{processor.id}"
        self._commChannel.sigResultProduced.emit(output, display_name)
        self._commChannel.sigCurrentResultChanged.emit(output)
        self._widget.setCurrentResult(output)
        self._widget.setStatusText(f"Created {display_name}.")
