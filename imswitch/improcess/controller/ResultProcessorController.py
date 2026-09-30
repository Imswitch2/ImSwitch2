"""Controller for generic result-based processor panels."""

from .basecontrollers import ImProcessWidgetController
from .processor_runner import ProcessorRunner
from imswitch.improcess.processors.run import (
    restriction_for,
    run_batch,
    run_multi,
    run_processor,
    run_restricted,
    summarize,
)
from imswitch.improcess.view.bulk_confirm import confirm_bulk_publish

# The run helpers moved to ``processors.run`` so headless callers share them.
# The old private names stay importable from here for one release.
_run_multi = run_multi
_run_batch = run_batch
_run_restricted = run_restricted
_restriction_for = restriction_for
_summary = summarize


def call_param_hook(panel, name: str, *args, logger=None) -> None:
    """Call ``name`` on a panel's parameter widget if it declares it.

    The hooks are opt-in, like ``setResult``: ``before_run()`` when a run starts,
    ``output_appended(text)`` for what it prints as it prints it (asking for it
    also asks for the output to be streamed), ``after_run(results, failures)``
    when it ends. A widget that raises here must not stop the run or the
    results being published.
    """
    hook = getattr(getattr(panel, "paramWidget", None), name, None)
    if not callable(hook):
        return
    try:
        hook(*args)
    except Exception:
        if logger is not None:
            logger.exception(
                "The parameter widget of %s failed in %s",
                getattr(getattr(panel, "processor", None), "id", "a processor"),
                name,
            )


def notify_param_widget(panel, results, failures, logger) -> None:
    """Tell a panel's parameter widget how the run went, if it asks to know."""
    call_param_hook(panel, "after_run", results, failures, logger=logger)


def finish_run(panel, commChannel, processor, inputs, results, failures, logger) -> None:
    """Report a finished run to the panel and publish what it made.

    Shared by the asynchronous path (called when the worker reports) and the
    inline one, so both publish the same way.
    """
    notify_param_widget(panel, results, failures, logger)

    if not results:
        panel.setStatusText(
            failures[0][1] if failures else "The processor produced no results."
        )
        return

    # Safety valve: any processor (built-in or drop-in plugin) producing a
    # flood of results/layers must be confirmed before it hits napari.
    if not confirm_bulk_publish(panel, results):
        panel.setStatusText(f"Cancelled: would have created {len(results)} results.")
        return

    last_result = None
    for index, result in enumerate(results):
        display_name = (
            getattr(result, "name", "")
            or f"{getattr(inputs[0], 'name', 'result')}_{processor.id}_{index}"
        )
        commChannel.sigResultProduced.emit(result, display_name)
        last_result = result

    if last_result is not None:
        commChannel.sigCurrentResultChanged.emit(last_result)
        panel.setCurrentResult(last_result)
    panel.setStatusText(summarize(results, failures))


class ResultProcessorController(ImProcessWidgetController):
    """Apply a registered Processor to the widget-selected result input(s).

    The run happens on a thread of its own (:class:`ProcessorRunner`), so the
    window stays alive, **Cancel** works and a processor's output can stream into
    the panel; the results are published here, on the GUI thread, when it ends.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._runner = ProcessorRunner()
        self._runInputs = None
        self._runner.sigOutput.connect(self._runOutput)
        self._runner.sigFinished.connect(self._runFinished)
        self._commChannel.sigCurrentResultChanged.connect(self._widget.setCurrentResult)
        self._commChannel.sigResultsChanged.connect(self.resultsChanged)
        self._widget.sigRunRequested.connect(self.runProcessor)
        cancelRequested = getattr(self._widget, "sigCancelRequested", None)
        if cancelRequested is not None:
            cancelRequested.connect(self.cancelRun)
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

        # Read through __dict__: on a controller whose base __init__ has not run,
        # a plain getattr raises instead of answering None.
        runner = self.__dict__.get("_runner")
        if runner is None:
            # Nothing to run on: a headless caller, or a controller built without
            # its __init__ (tests do). The run is inline, as it always was.
            results, failures = run_processor(processor, inputs, params, self._logger)
            finish_run(self._widget, self._commChannel, processor, inputs, results, failures, self._logger)
            return

        if runner.isRunning():
            self._widget.setStatusText(f"{processor.name} is already running.")
            return
        stream = callable(getattr(getattr(self._widget, "paramWidget", None), "output_appended", None))
        call_param_hook(self._widget, "before_run", logger=self._logger)
        if not runner.start(processor, inputs, params, self._logger, stream_output=stream):
            self._widget.setStatusText(f"{processor.name} is already running.")
            return
        self._runInputs = inputs
        setRunning = getattr(self._widget, "setRunning", None)
        if callable(setRunning):
            setRunning(True)
        self._widget.setStatusText(f"Running {processor.name}…")

    def cancelRun(self) -> None:
        """Stop the running processor; what it had made is discarded."""
        if self._runner.cancel():
            self._widget.setStatusText("Cancelling…")

    def shutdown(self, wait_ms: int = 3000) -> bool:
        """Stop a running processor and wait for its thread (application exit)."""
        return self._runner.shutdown(wait_ms)

    def _runOutput(self, text: str) -> None:
        call_param_hook(self._widget, "output_appended", text, logger=self._logger)

    def _runFinished(self, outcome) -> None:
        inputs, self._runInputs = self._runInputs, None
        setRunning = getattr(self._widget, "setRunning", None)
        if callable(setRunning):
            setRunning(False)
        if outcome.cancelled:
            notify_param_widget(self._widget, [], [(None, "Cancelled.")], self._logger)
            self._widget.setStatusText("Cancelled: nothing was published.")
            return
        finish_run(
            self._widget, self._commChannel, self._widget.processor, inputs or [],
            outcome.results, outcome.failures, self._logger,
        )


# Copyright (C) 2020-2026 ImSwitch developers
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
