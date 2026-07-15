"""Image calculator dialog: pick two results and a pixel-wise operation."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.combine import combine_compatibility
from imswitch.improcess.processors.image_calculator import CALCULATOR_OPERATIONS
from imswitch.improcess.view.StackCombineDialog import expand_input_choices


class ImageCalculatorDialog(QtWidgets.QDialog):
    """ImageJ-style calculator: ``A <operation> B`` over any two loaded results.

    Both combos list every loaded image result (plus the components of
    multi-layer results); incompatible pairs disable OK and show the reason.
    """

    def __init__(self, results, parent=None, active_result=None):
        super().__init__(parent)
        self.setWindowTitle("Image calculator")
        self.setMinimumWidth(420)
        self._inputs = [
            (label, result)
            for label, result, _checked in expand_input_choices(list(results))
        ]

        self.firstCombo = QtWidgets.QComboBox()
        self.secondCombo = QtWidgets.QComboBox()
        for label, result in self._inputs:
            description = self._describe(label, result)
            self.firstCombo.addItem(description)
            self.secondCombo.addItem(description)
        if len(self._inputs) > 1:
            self.secondCombo.setCurrentIndex(1)
        if active_result is not None:
            for index, (_label, result) in enumerate(self._inputs):
                if result is active_result:
                    self.firstCombo.setCurrentIndex(index)
                    break

        self.operationCombo = QtWidgets.QComboBox()
        self.operationCombo.addItems(list(CALCULATOR_OPERATIONS))

        self.floatCheck = QtWidgets.QCheckBox("32-bit float result")
        self.floatCheck.setChecked(True)
        self.floatCheck.setToolTip(
            "Compute in float32 so subtraction and division never clip or wrap"
        )

        self.nameEdit = QtWidgets.QLineEdit()

        form = QtWidgets.QFormLayout()
        form.addRow("Image A:", self.firstCombo)
        form.addRow("Operation:", self.operationCombo)
        form.addRow("Image B:", self.secondCombo)
        form.addRow("", self.floatCheck)
        form.addRow("Output name:", self.nameEdit)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)

        self.firstCombo.currentIndexChanged.connect(self._refresh)
        self.secondCombo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    # -- params ----------------------------------------------------------

    def selected_results(self) -> list:
        picks = []
        for combo in (self.firstCombo, self.secondCombo):
            index = combo.currentIndex()
            picks.append(self._inputs[index][1] if 0 <= index < len(self._inputs) else None)
        return picks

    def selected_params(self) -> dict:
        first, second = self.selected_results()
        return {
            "results": [first, second],
            "operation": self.operationCombo.currentText(),
            "float32": self.floatCheck.isChecked(),
            "name": self.nameEdit.text().strip() or None,
        }

    @classmethod
    def get_params(cls, results, parent=None, active_result=None) -> dict | None:
        dialog = cls(results, parent=parent, active_result=active_result)
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

    def _refresh(self) -> None:
        first, second = self.selected_results()
        if first is None or second is None:
            ok, reason = False, "Load at least two image results first"
        elif first is second:
            # Same pick twice is allowed (e.g. image minus itself) and always
            # compatible with itself.
            ok, reason = True, ""
        else:
            ok, reason = combine_compatibility([first, second], mode="stack")
        self.statusLabel.setText(reason if not ok else "")
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ok)


__all__ = ["ImageCalculatorDialog"]
