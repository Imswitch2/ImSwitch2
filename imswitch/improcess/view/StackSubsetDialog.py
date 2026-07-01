"""Crop/substack range dialog for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass

from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors._axis_split import axis_labels_for_result, shape_for_result


@dataclass
class _AxisRangeRow:
    axis: int
    size: int
    firstSpin: QtWidgets.QSpinBox
    lastSpin: QtWidgets.QSpinBox
    stepSpin: QtWidgets.QSpinBox


class StackSubsetDialog(QtWidgets.QDialog):
    """Select first/last/step ranges for a stack subset."""

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop/Substack")
        self.setMinimumWidth(460)
        self._updating = False
        self._shape = shape_for_result(result)
        self._labels = axis_labels_for_result(result)
        self._rows: list[_AxisRangeRow] = []

        self.table = QtWidgets.QTableWidget(len(self._shape), 5)
        self.table.setHorizontalHeaderLabels(["Axis", "Size", "First", "Last", "Step"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        for axis, (label, size) in enumerate(zip(self._labels, self._shape, strict=True)):
            self._add_axis_row(axis, label, int(size))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)

        self.copyCheck = QtWidgets.QCheckBox("Copy data")
        self.copyCheck.setChecked(False)

        self.resetButton = QtWidgets.QPushButton("Reset")
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.resetButton.clicked.connect(self.resetRanges)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.copyCheck)
        bottom.addStretch()
        bottom.addWidget(self.resetButton)
        bottom.addWidget(self.buttons)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(bottom)

    def selected_params(self) -> dict:
        ranges = []
        for row in self._rows:
            first = int(row.firstSpin.value())
            last = int(row.lastSpin.value())
            step = int(row.stepSpin.value())
            if first != 1 or last != row.size or step != 1:
                ranges.append(
                    {
                        "axis": row.axis,
                        "start": first - 1,
                        "stop": last,
                        "step": step,
                    }
                )
        return {
            "ranges": ranges,
            "copy": self.copyCheck.isChecked(),
        }

    def resetRanges(self) -> None:
        self._updating = True
        try:
            for row in self._rows:
                row.firstSpin.setValue(1)
                row.lastSpin.setValue(row.size)
                row.stepSpin.setValue(1)
        finally:
            self._updating = False

    @classmethod
    def get_params(cls, result, parent=None) -> dict | None:
        dialog = cls(result, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_params()

    def _add_axis_row(self, axis: int, label: str, size: int) -> None:
        label_item = QtWidgets.QTableWidgetItem(str(label))
        label_item.setData(QtCore.Qt.UserRole, axis)
        size_item = QtWidgets.QTableWidgetItem(str(size))
        size_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.table.setItem(axis, 0, label_item)
        self.table.setItem(axis, 1, size_item)

        first_spin = QtWidgets.QSpinBox()
        last_spin = QtWidgets.QSpinBox()
        step_spin = QtWidgets.QSpinBox()
        for spin in (first_spin, last_spin):
            spin.setRange(1, max(1, size))
        first_spin.setValue(1)
        last_spin.setValue(max(1, size))
        step_spin.setRange(1, max(1, size))
        step_spin.setValue(1)

        row = _AxisRangeRow(
            axis=axis,
            size=max(1, size),
            firstSpin=first_spin,
            lastSpin=last_spin,
            stepSpin=step_spin,
        )
        self._rows.append(row)

        first_spin.valueChanged.connect(lambda _value, range_row=row: self._sync_row(range_row))
        last_spin.valueChanged.connect(lambda _value, range_row=row: self._sync_row(range_row))

        self.table.setCellWidget(axis, 2, first_spin)
        self.table.setCellWidget(axis, 3, last_spin)
        self.table.setCellWidget(axis, 4, step_spin)

    def _sync_row(self, row: _AxisRangeRow) -> None:
        if self._updating:
            return
        first = row.firstSpin.value()
        last = row.lastSpin.value()
        if first <= last:
            return
        self._updating = True
        try:
            sender = self.sender()
            if sender is row.firstSpin:
                row.lastSpin.setValue(first)
            else:
                row.firstSpin.setValue(last)
        finally:
            self._updating = False


__all__ = ["StackSubsetDialog"]
