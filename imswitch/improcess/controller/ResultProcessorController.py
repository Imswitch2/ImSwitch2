"""Controller for generic result-based processor panels."""

from .basecontrollers import ImProcessWidgetController
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

        results, failures = run_processor(processor, inputs, params, self._logger)

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
        self._widget.setStatusText(summarize(results, failures))


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
