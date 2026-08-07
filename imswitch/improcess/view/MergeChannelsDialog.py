"""Merge channels dialog: pick the results that become the channels."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors.channel_merge import (
    CHANNEL_AXIS_LABEL,
    merge_compatibility,
)
from imswitch.improcess.view.ResultInputList import ResultInputListWidget


class MergeChannelsDialog(QtWidgets.QDialog):
    """Choose which loaded results become the channels of one stack.

    Lists every loaded result rather than only the reconstruction-list
    selection — merging two reconstructions is the point of the operation, and
    requiring a ctrl-click selection first made it look like the tool only
    worked within one of them. The selection still pre-checks the inputs.
    Incompatible picks disable OK and show the reason.
    """

    def __init__(self, results, parent=None, *, preselected=None):
        super().__init__(parent)
        self.setWindowTitle("Merge channels")
        self.setMinimumWidth(500)

        self.inputWidget = ResultInputListWidget(
            list(results), self, preselected=preselected
        )

        self.nameEdit = QtWidgets.QLineEdit()
        self.compositeCheck = QtWidgets.QCheckBox(
            "Create composite (show channels as coloured layers)"
        )
        self.compositeCheck.setChecked(True)
        self.compositeCheck.setToolTip(
            "Render the merged channels as independently-scaled coloured "
            "layers instead of a greyscale stack with a channel slider"
        )

        form = QtWidgets.QFormLayout()
        form.addRow("Output name:", self.nameEdit)
        form.addRow("", self.compositeCheck)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Channels (checked, in channel order):"))
        layout.addWidget(self.inputWidget)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)

        self.inputWidget.sigInputsChanged.connect(self._refresh)
        self._refresh()

    # -- params -----------------------------------------------------------

    def checked_results(self) -> list:
        return self.inputWidget.checked_results()

    def selected_params(self) -> dict:
        return {
            "results": self.checked_results(),
            "name": self.nameEdit.text().strip() or "Merged channels",
            "axis_label": CHANNEL_AXIS_LABEL,
            "composite": self.compositeCheck.isChecked(),
        }

    @classmethod
    def get_params(cls, results, parent=None, preselected=None) -> dict | None:
        dialog = cls(results, parent=parent, preselected=preselected)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    # -- internals ----------------------------------------------------------

    def _refresh(self) -> None:
        if not self.nameEdit.isModified():
            self.nameEdit.setText("Merged channels")
        ok, reason = merge_compatibility(self.checked_results())
        self.statusLabel.setText("" if ok else reason)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ok)


__all__ = ["MergeChannelsDialog"]
