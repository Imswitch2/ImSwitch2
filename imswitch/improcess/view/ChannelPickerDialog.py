"""Channel-selection dialog for the Make RGB toolbar action."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.improcess.processors._axis_split import axis_labels_for_result, shape_for_result


class ChannelPickerDialog(QtWidgets.QDialog):
    """Pick exactly which 3 channels along an axis map to R/G/B."""

    def __init__(self, result, axis: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select RGB Channels")

        shape = shape_for_result(result)
        labels = axis_labels_for_result(result)
        axis_label = labels[axis] if axis < len(labels) else str(axis)
        count = int(shape[axis])
        self._choices = [(index, f"{axis_label} {index}") for index in range(count)]

        layout = QtWidgets.QFormLayout(self)
        self._combos: list[QtWidgets.QComboBox] = []
        for row_label, default_index in (("Red:", 0), ("Green:", 1), ("Blue:", 2)):
            combo = QtWidgets.QComboBox()
            for index, text in self._choices:
                combo.addItem(text, index)
            combo.setCurrentIndex(min(default_index, len(self._choices) - 1))
            layout.addRow(row_label, combo)
            self._combos.append(combo)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def selected_channels(self) -> list[int]:
        return [int(combo.currentData()) for combo in self._combos]

    @classmethod
    def get_channels(cls, result, axis: int, parent=None) -> list[int] | None:
        dialog = cls(result, axis, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_channels()


__all__ = ["ChannelPickerDialog"]
