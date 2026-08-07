"""Stack/Combine dialog for merging selected results into one output."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors._axis_split import axis_labels_for_result
from imswitch.improcess.processors.combine import (
    STACK_AXIS_LABELS,
    combine_compatibility,
    default_stack_axis_label,
)
from imswitch.improcess.view.ResultInputList import (  # re-exported
    ResultInputListWidget,
    expand_input_choices,
)

_MODE_STACK = "stack"
_MODE_CONCATENATE = "concatenate"


class StackCombineDialog(QtWidgets.QDialog):
    """Choose how to combine the selected reconstruction-list results.

    Stack mode adds a new leading axis (two 2D images -> a 3D stack, two 3D
    stacks -> a 4D stack); concatenate mode appends along an existing shared
    axis. Incompatible inputs disable OK and show the reason instead of
    failing silently. Multi-layer results are expanded into their components
    so individual layers can be combined.
    """

    def __init__(self, results, parent=None, *, preselected=None):
        super().__init__(parent)
        self.setWindowTitle("Stack/Combine")
        self.setMinimumWidth(500)

        # -- input order + selection ------------------------------------
        self.inputWidget = ResultInputListWidget(
            list(results), self, preselected=preselected
        )

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

        # One-click escape hatch for the most common dead end: stacking along
        # a label the inputs already carry (e.g. Z on results with a singleton
        # Z axis), where appending along that existing axis is what the user
        # almost certainly wants.
        self.switchToConcatenateButton = QtWidgets.QPushButton()
        self.switchToConcatenateButton.setVisible(False)
        self.switchToConcatenateButton.clicked.connect(
            self._switch_to_concatenate_along_existing
        )

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Inputs (checked, in output order):"))
        layout.addWidget(self.inputWidget)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.switchToConcatenateButton)
        layout.addWidget(self.buttons)

        self.modeCombo.currentIndexChanged.connect(self._refresh)
        self.joinAxisCombo.currentIndexChanged.connect(self._refresh)
        self.axisLabelCombo.editTextChanged.connect(lambda _text: self._refresh())
        self.inputWidget.sigInputsChanged.connect(self._refresh)
        self._refresh()

    # -- params -----------------------------------------------------------

    def checked_results(self) -> list:
        """Return the checked input results in list order."""
        return self.inputWidget.checked_results()

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
    def get_params(cls, results, parent=None, preselected=None) -> dict | None:
        dialog = cls(results, parent=parent, preselected=preselected)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    # -- internals ----------------------------------------------------------

    def _default_name(self) -> str:
        if self.modeCombo.currentData() == _MODE_CONCATENATE:
            return "Concatenated"
        return "Stacked"

    def _default_axis_label(self) -> str:
        checked = self.checked_results()
        first = checked[0] if checked else None
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

        redirect = self._existing_axis_redirect(ok, mode, new_axis_label, checked)
        if redirect is None:
            self.switchToConcatenateButton.setVisible(False)
        else:
            self.switchToConcatenateButton.setText(
                f"Concatenate along the existing {redirect[0]} axis instead"
            )
            self.switchToConcatenateButton.setVisible(True)

    @staticmethod
    def _existing_axis_redirect(
        ok: bool, mode: str, new_axis_label: str | None, checked: list
    ) -> tuple[str, int] | None:
        """Return ``(label, axis)`` when stack mode is blocked by a duplicate
        axis label and concatenate along that existing axis is valid."""
        if ok or mode != _MODE_STACK or not new_axis_label or len(checked) < 2:
            return None
        try:
            labels = axis_labels_for_result(checked[0])
        except Exception:
            return None
        if new_axis_label not in labels:
            return None
        axis = labels.index(new_axis_label)
        concat_ok, _reason = combine_compatibility(
            checked,
            mode=_MODE_CONCATENATE,
            join_axis=axis,
        )
        if not concat_ok:
            return None
        return new_axis_label, axis

    def _switch_to_concatenate_along_existing(self) -> None:
        redirect = self._existing_axis_redirect(
            False,
            _MODE_STACK,
            self.axisLabelCombo.currentText().strip() or None,
            self.checked_results(),
        )
        if redirect is None:
            return
        _label, axis = redirect
        mode_index = self.modeCombo.findData(_MODE_CONCATENATE)
        if mode_index >= 0:
            self.modeCombo.setCurrentIndex(mode_index)
        join_index = self.joinAxisCombo.findData(axis)
        if join_index >= 0:
            self.joinAxisCombo.setCurrentIndex(join_index)


__all__ = ["ResultInputListWidget", "StackCombineDialog", "expand_input_choices"]
