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


class StackSubsetRangesWidget(QtWidgets.QWidget):
    """The per-axis range table, and the ROI chooser that fills it.

    A widget rather than part of the dialog because there are two ways to
    crop -- the toolbar's dialog and the processor panel -- and when they were
    two implementations only one of them learned about ROIs. Anything that can
    host a widget now gets the same controls, and :meth:`setResult` retargets
    them when the panel's input changes.
    """

    #: The "type the numbers yourself" entry, and what the combo returns to
    #: whenever a spinbox is touched.
    MANUAL = "Manual"

    def __init__(self, result=None, parent=None, napari_viewer=None, rois=()):
        super().__init__(parent)
        self._updating = False
        self._shape = ()
        self._labels = []
        self._rows: list[_AxisRangeRow] = []
        self._rois = []
        self._appliedROI = None
        # Optional live X/Y crop-rectangle preview drawn into the reconstruction
        # viewer while the widget is up (removed on close).
        self._viewer = napari_viewer
        self._preview_layer = None

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Axis", "Size", "First", "Last", "Step"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)

        # Crop from an ROI (P-R adjacent): the ranges below are still what is
        # applied — an ROI *fills* them rather than replacing them — so the
        # numbers stay visible, adjustable, and the single source of truth.
        self.roiCombo = QtWidgets.QComboBox()
        self.roiCombo.currentIndexChanged.connect(self._roiChosen)

        self.copyCheck = QtWidgets.QCheckBox("Copy data")
        self.copyCheck.setChecked(False)

        self.resetButton = QtWidgets.QPushButton("Reset")
        self.resetButton.clicked.connect(self.resetRanges)

        source = QtWidgets.QHBoxLayout()
        source.addWidget(QtWidgets.QLabel("Crop from:"))
        source.addWidget(self.roiCombo, 1)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.copyCheck)
        bottom.addStretch()
        bottom.addWidget(self.resetButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(source)
        layout.addWidget(self.table)
        layout.addLayout(bottom)

        self.setResult(result, rois)

    # -- retargeting -------------------------------------------------------

    def setResult(self, result, rois=()) -> None:
        """Rebuild the rows for ``result`` and re-offer ``rois``.

        The panel's input changes under it -- a different reconstruction has
        different axes and sizes -- so the table is rebuilt rather than
        adjusted. Any ranges typed for the previous result are dropped with
        it: keeping numbers that referred to a different image is how a crop
        silently applies to the wrong extent.
        """
        self._appliedROI = None
        self._rows = []
        self._shape = shape_for_result(result) if result is not None else ()
        self._labels = axis_labels_for_result(result) if result is not None else []
        self.table.setRowCount(len(self._shape))
        for axis, (label, size) in enumerate(
            zip(self._labels, self._shape, strict=True)
        ):
            self._add_axis_row(axis, label, int(size))
        self.setROIs(rois)
        self._update_crop_preview()

    def setROIs(self, rois=()) -> None:
        """Re-offer the ROI manager's current set without touching the ranges."""
        self._rois = [roi for roi in (rois or []) if _croppable(roi)]
        current = self.roiCombo.currentData()
        self.roiCombo.blockSignals(True)
        self.roiCombo.clear()
        self.roiCombo.addItem(self.MANUAL, None)
        for roi in self._rois:
            self.roiCombo.addItem(f"{roi.name} ({roi.roi_type})", roi.uid)
        if current is not None:
            index = self.roiCombo.findData(current)
            self.roiCombo.setCurrentIndex(max(0, index))
        self.roiCombo.blockSignals(False)
        self.roiCombo.setEnabled(bool(self._rois))
        self.roiCombo.setToolTip(
            "Fill the Y and X ranges from an ROI in the ROI manager. The "
            "ranges stay editable; changing one returns this to Manual."
            if self._rois
            else "No ROIs with an extent in the ROI manager to crop from."
        )

    # -- cropping from an ROI ---------------------------------------------

    def _rowForLabel(self, label: str):
        """The range row for an axis, found **by label**.

        By label rather than by position: axis order is view-mode dependent,
        so taking "the last two rows" would fill a YZ view's ranges from an
        ROI drawn on YX.
        """
        for row, name in zip(self._rows, self._labels):
            if str(name).upper() == label:
                return row
        return None

    def _roiChosen(self, _index: int) -> None:
        uid = self.roiCombo.currentData()
        if uid is None:
            self._appliedROI = None
            return
        roi = next((item for item in self._rois if item.uid == uid), None)
        if roi is None:
            return
        self.applyROI(roi)

    def applyROI(self, roi) -> bool:
        """Fill the Y and X ranges from an ROI's bounding box.

        Its *box*, even for a polygon or a mask: a crop is rectangular, so the
        honest thing is to take the rectangle the ROI occupies rather than
        pretend a shape was applied. Use the ROI manager's own crop-mode
        restriction on a processor if the shape itself should matter.

        Bounds are clipped by the spinbox ranges, so an ROI drawn on a larger
        result gives the part of it that exists here.
        """
        from imswitch.imcommon.algorithms.roi_geometry import roi_bounds

        r0, r1, c0, c1 = (int(v) for v in roi_bounds(roi))
        pairs = (("Y", r0, r1), ("X", c0, c1))
        applied = False
        self._updating = True
        try:
            for label, start, stop in pairs:
                row = self._rowForLabel(label)
                if row is None:
                    continue
                # ROI bounds are half-open and 0-based; the dialog is
                # inclusive and 1-based.
                row.firstSpin.setValue(max(1, start + 1))
                row.lastSpin.setValue(max(1, min(row.size, stop)))
                applied = True
        finally:
            self._updating = False
        self._appliedROI = roi if applied else None
        self._update_crop_preview()
        return applied

    def _returnToManual(self) -> None:
        """Any hand edit means the ranges are no longer the ROI's."""
        if self._updating or self._appliedROI is None:
            return
        self._appliedROI = None
        self.roiCombo.blockSignals(True)
        self.roiCombo.setCurrentIndex(0)
        self.roiCombo.blockSignals(False)

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
        params = {
            "ranges": ranges,
            "copy": self.copyCheck.isChecked(),
        }
        if self._appliedROI is not None:
            # Recorded so the crop can say where its rectangle came from,
            # rather than the ROI being a UI convenience that leaves no trace.
            params["roi_uid"] = str(getattr(self._appliedROI, "uid", ""))
            params["roi_name"] = str(getattr(self._appliedROI, "name", ""))
        return params

    def get_values(self) -> dict:
        """What the generic processor panel asks its parameter widget for."""
        return self.selected_params()

    def resetRanges(self) -> None:
        self._appliedROI = None
        self.roiCombo.blockSignals(True)
        self.roiCombo.setCurrentIndex(0)
        self.roiCombo.blockSignals(False)
        self._updating = True
        try:
            for row in self._rows:
                row.firstSpin.setValue(1)
                row.lastSpin.setValue(row.size)
                row.stepSpin.setValue(1)
        finally:
            self._updating = False

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
        first_spin.valueChanged.connect(lambda _value: self._returnToManual())
        last_spin.valueChanged.connect(lambda _value: self._returnToManual())

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


def _croppable(roi) -> bool:
    """Only ROIs with an extent: a crop is a rectangle, and a line or a point
    has no rectangle to crop to."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

    try:
        return bool(roi_capabilities(roi.roi_type).is_area)
    except Exception:
        return False


class StackSubsetDialog(QtWidgets.QDialog):
    """The toolbar's Crop/Substack, wrapping the shared range controls."""

    MANUAL = StackSubsetRangesWidget.MANUAL

    def __init__(self, result, parent=None, napari_viewer=None, rois=()):
        super().__init__(parent)
        self.setWindowTitle("Crop/Substack")
        self.setMinimumWidth(460)
        self.ranges = StackSubsetRangesWidget(
            result, parent=self, napari_viewer=napari_viewer, rois=rois
        )

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.ranges)
        layout.addWidget(self.buttons)

    # The dialog is a thin shell; everything it is asked about lives on the
    # widget, so the two cannot answer differently.
    def __getattr__(self, name):
        # Only reached for attributes the dialog itself does not define, and
        # only after __init__ has run -- guarded so a lookup during
        # construction raises normally instead of recursing.
        ranges = self.__dict__.get("ranges")
        if ranges is None:
            raise AttributeError(name)
        return getattr(ranges, name)

    def accept(self) -> None:
        self.ranges._remove_crop_preview()
        super().accept()

    def reject(self) -> None:
        self.ranges._remove_crop_preview()
        super().reject()

    @classmethod
    def get_params(
        cls, result, parent=None, napari_viewer=None, rois=()
    ) -> dict | None:
        dialog = cls(result, parent=parent, napari_viewer=napari_viewer, rois=rois)
        try:
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return None
            return dialog.selected_params()
        finally:
            dialog.ranges._remove_crop_preview()


__all__ = ["StackSubsetDialog", "StackSubsetRangesWidget"]
