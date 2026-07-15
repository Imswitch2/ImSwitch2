"""Crop/substack range dialog for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors._axis_split import axis_labels_for_result, shape_for_result


def crop_preview_rectangle(labels, first_last_by_axis):
    """Return napari rectangle corners for the X/Y crop, or ``None``.

    Only the spatial X/Y ranges map to a rectangle overlay (Z/T/C ranges have no
    spatial extent to preview). ``labels`` are the axis labels in data order;
    ``first_last_by_axis`` maps an axis index to its ``(first, last)`` 1-based
    spinbox values. Returns the four corner ``[row(Y), col(X)]`` points (0-based,
    inclusive edges) that napari's ``add_shapes(shape_type="rectangle")`` expects,
    or ``None`` when the result has no X/Y axes.
    """
    try:
        x_axis = list(labels).index("X")
        y_axis = list(labels).index("Y")
    except ValueError:
        return None
    if x_axis not in first_last_by_axis or y_axis not in first_last_by_axis:
        return None
    x_first, x_last = first_last_by_axis[x_axis]
    y_first, y_last = first_last_by_axis[y_axis]
    x0, x1 = int(x_first) - 1, int(x_last)
    y0, y1 = int(y_first) - 1, int(y_last)
    return [[y0, x0], [y0, x1], [y1, x1], [y1, x0]]


@dataclass
class _AxisRangeRow:
    axis: int
    size: int
    firstSpin: QtWidgets.QSpinBox
    lastSpin: QtWidgets.QSpinBox
    stepSpin: QtWidgets.QSpinBox


class StackSubsetDialog(QtWidgets.QDialog):
    """Select first/last/step ranges for a stack subset."""

    def __init__(self, result, parent=None, napari_viewer=None):
        super().__init__(parent)
        self.setWindowTitle("Crop/Substack")
        self.setMinimumWidth(460)
        self._updating = False
        self._shape = shape_for_result(result)
        self._labels = axis_labels_for_result(result)
        self._rows: list[_AxisRangeRow] = []
        # Optional live X/Y crop-rectangle preview drawn into the reconstruction
        # viewer while the dialog is open (removed on close).
        self._viewer = napari_viewer
        self._preview_layer = None

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

        self._update_crop_preview()

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
    def get_params(cls, result, parent=None, napari_viewer=None) -> dict | None:
        dialog = cls(result, parent=parent, napari_viewer=napari_viewer)
        try:
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return None
            return dialog.selected_params()
        finally:
            dialog._remove_crop_preview()

    # -- live crop-rectangle preview -------------------------------------

    def _update_crop_preview(self) -> None:
        """Draw/update the X/Y crop rectangle in the viewer (no-op without one)."""
        if self._viewer is None:
            return
        rect = crop_preview_rectangle(
            self._labels,
            {row.axis: (row.firstSpin.value(), row.lastSpin.value()) for row in self._rows},
        )
        if rect is None:
            return
        data = [np.array(rect, dtype=float)]
        try:
            if self._preview_layer is not None and self._preview_layer in self._viewer.layers:
                self._preview_layer.data = data
            else:
                self._preview_layer = self._viewer.add_shapes(
                    data,
                    shape_type="rectangle",
                    name="Crop preview",
                    edge_color="yellow",
                    face_color=[1.0, 1.0, 0.0, 0.10],
                    edge_width=2,
                )
        except Exception:
            # Never let a preview-drawing hiccup block the crop dialog itself.
            self._preview_layer = None

    def _remove_crop_preview(self) -> None:
        if self._preview_layer is not None and self._viewer is not None:
            try:
                self._viewer.layers.remove(self._preview_layer)
            except Exception:
                pass
        self._preview_layer = None

    def accept(self) -> None:
        self._remove_crop_preview()
        super().accept()

    def reject(self) -> None:
        self._remove_crop_preview()
        super().reject()

    def closeEvent(self, event) -> None:
        self._remove_crop_preview()
        super().closeEvent(event)

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
        first_spin.valueChanged.connect(lambda _value: self._update_crop_preview())
        last_spin.valueChanged.connect(lambda _value: self._update_crop_preview())

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
