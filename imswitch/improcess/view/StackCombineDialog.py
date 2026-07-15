"""Stack/Combine dialog for merging selected results into one output."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.combine import (
    STACK_AXIS_LABELS,
    combine_compatibility,
)

_MODE_STACK = "stack"
_MODE_CONCATENATE = "concatenate"


class StackCombineDialog(QtWidgets.QDialog):
    """Choose how to combine the selected reconstruction-list results.

    Stack mode adds a new leading axis (two 2D images -> a 3D stack, two 3D
    stacks -> a 4D stack); concatenate mode appends along an existing shared
    axis. Incompatible inputs disable OK and show the reason instead of
    failing silently.
    """

    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stack/Combine")
        self.setMinimumWidth(480)
        self._results = list(results)

        # -- input order ---------------------------------------------------
        self.inputList = QtWidgets.QListWidget()
        self.inputList.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for result in self._results:
            self.inputList.addItem(self._describe(result))
        self.upButton = QtWidgets.QPushButton("Up")
        self.downButton = QtWidgets.QPushButton("Down")
        self.upButton.clicked.connect(lambda: self._move_selected(-1))
        self.downButton.clicked.connect(lambda: self._move_selected(+1))

        orderButtons = QtWidgets.QVBoxLayout()
        orderButtons.addWidget(self.upButton)
        orderButtons.addWidget(self.downButton)
        orderButtons.addStretch()

        inputRow = QtWidgets.QHBoxLayout()
        inputRow.addWidget(self.inputList)
        inputRow.addLayout(orderButtons)

        # -- mode + axis ----------------------------------------------------
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem("Stack along a new axis", _MODE_STACK)
        self.modeCombo.addItem("Concatenate along an existing axis", _MODE_CONCATENATE)

        self.axisLabelCombo = QtWidgets.QComboBox()
        self.axisLabelCombo.setEditable(True)
        self.axisLabelCombo.addItems(list(STACK_AXIS_LABELS))
        self.axisLabelCombo.setToolTip("Label for the new leading axis")

        self.joinAxisCombo = QtWidgets.QComboBox()
        try:
            labels = axis_labels_for_result(self._results[0])
        except Exception:
            labels = []
        for axis, label in enumerate(labels):
            self.joinAxisCombo.addItem(f"{label} (axis {axis})", axis)

        self.nameEdit = QtWidgets.QLineEdit()

        form = QtWidgets.QFormLayout()
        form.addRow("Mode:", self.modeCombo)
        self._axisLabelRowLabel = QtWidgets.QLabel("New axis:")
        form.addRow(self._axisLabelRowLabel, self.axisLabelCombo)
        self._joinAxisRowLabel = QtWidgets.QLabel("Join axis:")
        form.addRow(self._joinAxisRowLabel, self.joinAxisCombo)
        form.addRow("Output name:", self.nameEdit)

        # -- status + buttons -------------------------------------------------
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Inputs (output order):"))
        layout.addLayout(inputRow)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)

        self.modeCombo.currentIndexChanged.connect(self._refresh)
        self.joinAxisCombo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    # -- params ---------------------------------------------------------

    def selected_params(self) -> dict:
        mode = self.modeCombo.currentData()
        params = {
            "mode": mode,
            "name": self.nameEdit.text().strip() or self._default_name(),
            "results": list(self._results),
        }
        if mode == _MODE_CONCATENATE:
            params["join_axis"] = int(self.joinAxisCombo.currentData() or 0)
        else:
            params["axis_label"] = self.axisLabelCombo.currentText().strip() or "Z"
        return params

    @classmethod
    def get_params(cls, results, parent=None) -> dict | None:
        dialog = cls(results, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    # -- internals --------------------------------------------------------

    @staticmethod
    def _describe(result) -> str:
        name = getattr(result, "name", "result")
        try:
            shape = tuple(shape_for_result(result))
            labels = axis_labels_for_result(result)
            return f"{name} — {shape} {''.join(labels)}"
        except Exception:
            return str(name)

    def _default_name(self) -> str:
        if self.modeCombo.currentData() == _MODE_CONCATENATE:
            return "Concatenated"
        return "Stacked"

    def _move_selected(self, delta: int) -> None:
        row = self.inputList.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._results)):
            return
        self._results[row], self._results[target] = (
            self._results[target],
            self._results[row],
        )
        item = self.inputList.takeItem(row)
        self.inputList.insertItem(target, item)
        self.inputList.setCurrentRow(target)
        self._refresh()

    def _refresh(self) -> None:
        mode = self.modeCombo.currentData()
        concatenate = mode == _MODE_CONCATENATE
        self._axisLabelRowLabel.setVisible(not concatenate)
        self.axisLabelCombo.setVisible(not concatenate)
        self._joinAxisRowLabel.setVisible(concatenate)
        self.joinAxisCombo.setVisible(concatenate)
        if not self.nameEdit.isModified():
            self.nameEdit.setText(self._default_name())

        join_axis = int(self.joinAxisCombo.currentData() or 0)
        ok, reason = combine_compatibility(self._results, mode=mode, join_axis=join_axis)
        self.statusLabel.setText(reason if not ok else "")
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ok)


__all__ = ["StackCombineDialog"]
