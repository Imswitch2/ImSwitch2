"""Controller for the ImProcess console: binds its namespace to the results list."""

from __future__ import annotations

from imswitch.improcess.processors.python_step.console import ConsoleSession

from .basecontrollers import ImProcessWidgetController


class PythonConsoleController(ImProcessWidgetController):
    """Fills the console widget's namespace and keeps it following the list.

    ``data`` and the names with it are bound to what is selected in the results
    list (the current result when nothing is), rebound when the selection moves
    and before a command runs, never while a ``publish`` is in progress and
    never when nothing changed. ``publish()`` adds to the list through the
    communication channel, like every other producer.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current = None
        self._session = ConsoleSession(
            selected=self._selectedResults,
            current=lambda: self._current,
            publish=self._publishResult,
            namespace=self._widget.namespace(),
        )
        self._commChannel.sigCurrentResultChanged.connect(self._currentChanged)
        self._commChannel.sigResultsChanged.connect(self._session.refresh_if_changed)
        self._widget.sigCommandStarting.connect(self._session.refresh_if_changed)

    @property
    def session(self) -> ConsoleSession:
        return self._session

    def seed(self, current=None) -> None:
        """Tell a console opened after a result was chosen which one is current."""
        self._current = current
        self._session.refresh_if_changed()

    def _currentChanged(self, result) -> None:
        self._current = result
        self._session.refresh_if_changed()

    def _selectedResults(self) -> list:
        entries = self._commChannel.getSelectedResults()
        return [
            entry[1] if isinstance(entry, tuple) and len(entry) == 2 else entry
            for entry in entries
        ]

    def _publishResult(self, result, name: str) -> None:
        self._commChannel.sigResultProduced.emit(result, name)
