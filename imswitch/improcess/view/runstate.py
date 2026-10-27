"""The Run / Cancel state a processor panel shows while a run is going.

Shared by the generic processor panel and the hand-built ones (Segmentation, PSF
resolution, Colocalization), so all of them behave the same: Run goes off and
Cancel appears; the inputs can still change meanwhile and each change recomputes
whether Run may be pressed, which is remembered and applied when the run ends,
never while it is going.
"""

from __future__ import annotations

from qtpy import QtWidgets

CANCEL_TOOLTIP = (
    "Stop the run. Nothing is published; a processor that cannot be asked "
    "politely is interrupted after a moment."
)


class RunState:
    """Owns a panel's Cancel button and decides whether its Run button may be on.

    ``layout`` is the box layout ``runButton`` sits in; the Cancel button is put
    right after it. ``onCancel`` is called when Cancel is pressed (the panel
    emits its ``sigCancelRequested``).
    """

    def __init__(self, runButton: QtWidgets.QPushButton, layout, onCancel):
        self.runButton = runButton
        self.cancelButton = QtWidgets.QPushButton("Cancel")
        self.cancelButton.setToolTip(CANCEL_TOOLTIP)
        self.cancelButton.hide()
        layout.insertWidget(layout.indexOf(runButton) + 1, self.cancelButton)
        self._onCancel = onCancel
        self._running = False
        self._allowed = runButton.isEnabled()
        self.cancelButton.clicked.connect(self._cancelClicked)

    @property
    def running(self) -> bool:
        return self._running

    def setRunning(self, running: bool) -> None:
        self._running = bool(running)
        self.cancelButton.setVisible(self._running)
        self.cancelButton.setEnabled(self._running)
        self.cancelButton.setText("Cancel")
        self._apply()

    def setRunEnabled(self, allowed: bool) -> None:
        """Whether Run may be pressed, were nothing running."""
        self._allowed = bool(allowed)
        self._apply()

    def _apply(self) -> None:
        self.runButton.setEnabled(self._allowed and not self._running)

    def _cancelClicked(self) -> None:
        self.cancelButton.setEnabled(False)
        self.cancelButton.setText("Cancelling…")
        self._onCancel()


__all__ = ["RunState", "CANCEL_TOOLTIP"]
