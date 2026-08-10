"""Controller for generic result-based processor panels."""

from .basecontrollers import ImProcessWidgetController
from imswitch.improcess.processors.base import normalize_processor_output
from imswitch.improcess.view.bulk_confirm import confirm_bulk_publish


class ResultProcessorController(ImProcessWidgetController):
    """Apply a registered Processor to the widget-selected result input(s)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.sigCurrentResultChanged.connect(self._widget.setCurrentResult)
        self._commChannel.sigResultsChanged.connect(self.resultsChanged)
        self._widget.sigRunRequested.connect(self.runProcessor)
        self.resultsChanged()

    def resultsChanged(self) -> None:
        """Push the loaded/selected results at panels that can use them."""
        setter = getattr(self._widget, "setAvailableResults", None)
        if not callable(setter):
            return
        try:
            setter(
                self._commChannel.getAllResults(),
                self._commChannel.getSelectedResults(),
            )
        except Exception:
            self._logger.debug(
                "Could not refresh available results for %s",
                getattr(self._widget, "processor", self._widget),
                exc_info=True,
            )

    def runProcessor(self, inputs, params: dict) -> None:
        processor = self._widget.processor
        inputs = list(inputs) if isinstance(inputs, (list, tuple)) else [inputs]
        if not inputs:
            self._widget.setStatusText("No processor input selected.")
            return

        if getattr(processor, "max_inputs", 1) != 1:
            results, failures = _run_multi(processor, inputs, params, self._logger)
        else:
            results, failures = _run_batch(processor, inputs, params, self._logger)

        if not results:
            self._widget.setStatusText(
                failures[0][1] if failures else "The processor produced no results."
            )
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
                or f"{getattr(inputs[0], 'name', 'result')}_{processor.id}_{index}"
            )
            self._commChannel.sigResultProduced.emit(result, display_name)
            last_result = result

        if last_result is not None:
            self._commChannel.sigCurrentResultChanged.emit(last_result)
            self._widget.setCurrentResult(last_result)
        self._widget.setStatusText(_summary(results, failures))


def _run_multi(processor, inputs, params: dict, logger):
    """One run consuming every input; the arity contract puts them in params."""
    try:
        output = processor.apply(inputs[0], {**params, "results": inputs})
        return list(normalize_processor_output(output)), []
    except Exception as exc:
        logger.exception(
            "Failed to run processor %s on %d inputs",
            getattr(processor, "id", type(processor).__name__),
            len(inputs),
        )
        return [], [(inputs[0], str(exc))]


def _run_batch(processor, inputs, params: dict, logger):
    """One run per input. A failure is reported, not fatal: one bad result
    part-way through a sweep must not throw away the ones that worked."""
    results = []
    failures = []
    for input_result in inputs:
        try:
            output = processor.apply(input_result, params)
            results.extend(normalize_processor_output(output))
        except Exception as exc:
            logger.exception(
                "Failed to run processor %s on %s",
                getattr(processor, "id", type(processor).__name__),
                getattr(input_result, "name", type(input_result).__name__),
            )
            failures.append((input_result, str(exc)))
    return results, failures


def _summary(results, failures) -> str:
    created = (
        f"Created {getattr(results[0], 'name', 'result')}."
        if len(results) == 1
        else f"Created {len(results)} results."
    )
    if not failures:
        return created
    name = getattr(failures[0][0], "name", "one input")
    return f"{created} {len(failures)} failed — '{name}': {failures[0][1]}"
