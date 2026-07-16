"""Controller for generic result-based processor panels."""

from .basecontrollers import ImProcessWidgetController
from imswitch.improcess.processors.base import normalize_processor_output
from imswitch.improcess.view.bulk_confirm import confirm_bulk_publish


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
            results = normalize_processor_output(output)
        except Exception as exc:
            self._logger.exception(
                "Failed to run processor %s on %s",
                getattr(processor, "id", type(processor).__name__),
                getattr(input_result, "name", type(input_result).__name__),
            )
            self._widget.setStatusText(str(exc))
            return

        # Safety valve: any processor (built-in or drop-in plugin) producing a
        # flood of results/layers must be confirmed before it hits napari.
        if not confirm_bulk_publish(self._widget, results):
            self._widget.setStatusText(
                f"Cancelled: would have created {len(results)} results."
            )
            return

        last_result = None
        for index, result in enumerate(results):
            display_name = (
                getattr(result, "name", "")
                or f"{input_result.name}_{processor.id}_{index}"
            )
            self._commChannel.sigResultProduced.emit(result, display_name)
            last_result = result

        if last_result is not None:
            self._commChannel.sigCurrentResultChanged.emit(last_result)
            self._widget.setCurrentResult(last_result)
        if len(results) == 1:
            self._widget.setStatusText(f"Created {getattr(results[0], 'name', 'result')}.")
        else:
            self._widget.setStatusText(f"Created {len(results)} results.")
