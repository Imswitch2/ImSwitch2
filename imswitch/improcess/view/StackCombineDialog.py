"""Stack/Combine dialog for merging selected results into one output."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.combine import (
    STACK_AXIS_LABELS,
    combine_compatibility,
    default_stack_axis_label,
)

_MODE_STACK = "stack"
_MODE_CONCATENATE = "concatenate"


def expand_input_choices(results):
    """Expand results into checkable combine inputs, one per component.

    A multi-layer result (``display_layers``) contributes its whole-result
    entry plus one entry per non-context component via
    ``processor_input_choices()``, so heterogeneous results are listed layer
    by layer instead of hiding their components. Returns
    ``(label, result, checked_by_default)`` tuples; whole results start
    checked, layer components start unchecked.
    """
    inputs = []
    for result in results:
        choices_fn = getattr(result, "processor_input_choices", None)
        choices = choices_fn() if callable(choices_fn) else None
        if not choices:
            inputs.append((getattr(result, "name", "result"), result, True))
            continue
        for choice in choices:
            whole = choice.id == "result"
            label = (
                getattr(result, "name", "result")
                if whole
                else f"{getattr(result, 'name', 'result')} › {choice.label}"
            )
            inputs.append((label, choice.result, whole))
    return inputs


class StackCombineDialog(QtWidgets.QDialog):
    """Choose how to combine the selected reconstruction-list results.

    Stack mode adds a new leading axis (two 2D images -> a 3D stack, two 3D
    stacks -> a 4D stack); concatenate mode appends along an existing shared
    axis. Incompatible inputs disable OK and show the reason instead of
    failing silently. Multi-layer results are expanded into their components
    so individual layers can be combined.
    """

    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stack/Combine")
        self.setMinimumWidth(500)
        self._inputs = expand_input_choices(list(results))

        # -- input order + selection ------------------------------------
        self.inputList = QtWidgets.QListWidget()
        self.inputList.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for label, result, checked in self._inputs:
            item = QtWidgets.QListWidgetItem(self._describe(label, result))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)
            self.inputList.addItem(item)
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

        # -- mode + axis --------------------------------------------------
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem("Stack along a new axis", _MODE_STACK)
        self.modeCombo.addItem("Concatenate along an existing axis", _MODE_CONCATENATE)

        self.axisLabelCombo = QtWidgets.QComboBox()
        self.axisLabelCombo.setEditable(True)
        self.axisLabelCombo.addItems(list(STACK_AXIS_LABELS))
        self.axisLabelCombo.setToolTip("Label for the new leading axis")
        self.axisLabelCombo.setCurrentText(self._default_axis_label())

        self.joinAxisCombo = QtWidgets.QComboBox()
        self._rebuild_join_axes()

        self.nameEdit = QtWidgets.QLineEdit()

        form = QtWidgets.QFormLayout()
        form.addRow("Mode:", self.modeCombo)
        self._axisLabelRowLabel = QtWidgets.QLabel("New axis:")
        form.addRow(self._axisLabelRowLabel, self.axisLabelCombo)
        self._joinAxisRowLabel = QtWidgets.QLabel("Join axis:")
        form.addRow(self._joinAxisRowLabel, self.joinAxisCombo)
        form.addRow("Output name:", self.nameEdit)

        # -- status + buttons ----------------------------------------------
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Inputs (checked, in output order):"))
        layout.addLayout(inputRow)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)

        self.modeCombo.currentIndexChanged.connect(self._refresh)
        self.joinAxisCombo.currentIndexChanged.connect(self._refresh)
        self.axisLabelCombo.editTextChanged.connect(lambda _text: self._refresh())
        self.inputList.itemChanged.connect(lambda _item: self._refresh())
        self._refresh()

    # -- params -----------------------------------------------------------

    def checked_results(self) -> list:
        """Return the checked input results in list order."""
        return [
            self._inputs[row][1]
            for row in range(self.inputList.count())
            if self.inputList.item(row).checkState() == QtCore.Qt.Checked
        ]

    def selected_params(self) -> dict:
        mode = self.modeCombo.currentData()
        params = {
            "mode": mode,
            "name": self.nameEdit.text().strip() or self._default_name(),
            "results": self.checked_results(),
        }
        if mode == _MODE_CONCATENATE:
            params["join_axis"] = int(self.joinAxisCombo.currentData() or 0)
        else:
            params["axis_label"] = (
                self.axisLabelCombo.currentText().strip() or self._default_axis_label()
            )
        return params

    @classmethod
    def get_params(cls, results, parent=None) -> dict | None:
        dialog = cls(results, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _describe(label, result) -> str:
        try:
            shape = tuple(shape_for_result(result))
            labels = axis_labels_for_result(result)
            return f"{label} — {shape} {''.join(labels)}"
        except Exception:
            return str(label)

    def _default_name(self) -> str:
        if self.modeCombo.currentData() == _MODE_CONCATENATE:
            return "Concatenated"
        return "Stacked"

    def _default_axis_label(self) -> str:
        checked = self.checked_results() if self.inputList.count() else []
        first = checked[0] if checked else (self._inputs[0][1] if self._inputs else None)
        if first is None:
            return "Z"
        try:
            return default_stack_axis_label(axis_labels_for_result(first))
        except Exception:
            return "Z"

    def _rebuild_join_axes(self) -> None:
        checked = self.checked_results()
        first = checked[0] if checked else None
        try:
            labels = axis_labels_for_result(first) if first is not None else []
        except Exception:
            labels = []
        current = self.joinAxisCombo.currentData()
        self.joinAxisCombo.blockSignals(True)
        self.joinAxisCombo.clear()
        for axis, label in enumerate(labels):
            self.joinAxisCombo.addItem(f"{label} (axis {axis})", axis)
        if current is not None and 0 <= int(current) < len(labels):
            self.joinAxisCombo.setCurrentIndex(int(current))
        self.joinAxisCombo.blockSignals(False)

    def _move_selected(self, delta: int) -> None:
        row = self.inputList.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._inputs)):
            return
        self._inputs[row], self._inputs[target] = (
            self._inputs[target],
            self._inputs[row],
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
        self._rebuild_join_axes()

        checked = self.checked_results()
        join_axis = int(self.joinAxisCombo.currentData() or 0)
        new_axis_label = self.axisLabelCombo.currentText().strip() or None
        ok, reason = combine_compatibility(
            checked,
            mode=mode,
            join_axis=join_axis,
            new_axis_label=new_axis_label if not concatenate else None,
        )
        self.statusLabel.setText(reason if not ok else "")
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ok)


__all__ = ["StackCombineDialog", "expand_input_choices"]
