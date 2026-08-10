"""ImageJ-like ROI manager panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService
from imswitch.improcess.layer_selection import active_image_layer
from imswitch.improcess.analysis.roi_manager import (
    ROIManagerModel,
    ROIRecord,
    rectangle_roi_from_vertices,
)
from .ResultsTableWidget import format_table_value

#: Role holding the ROI's key on column 0 of every row. Rows are resolved
#: through this, never through their index, so sorting and filtering cannot
#: make an action target a different ROI than the one that was clicked.
ROI_KEY_ROLE = QtCore.Qt.UserRole

#: Role holding a value the table sorts on, so numeric columns sort
#: numerically rather than by their formatted text.
SORT_KEY_ROLE = QtCore.Qt.UserRole + 1


class _TableItem(QtWidgets.QTableWidgetItem):
    """Table item that sorts on a stored key rather than its display text."""

    def __lt__(self, other):
        mine = self.data(SORT_KEY_ROLE)
        theirs = other.data(SORT_KEY_ROLE) if isinstance(other, QtWidgets.QTableWidgetItem) else None
        if mine is None or theirs is None:
            return super().__lt__(other)
        try:
            return mine < theirs
        except TypeError:
            return super().__lt__(other)


class ROIManagerWidget(QtWidgets.QWidget):
    """Manage multiple rectangular ROIs and per-ROI statistics."""

    #: Stable owner key for the shared drawing tool (see ViewerToolService).
    TOOL_OWNER = "improcess.roi-manager"

    _COLUMNS = [
        "Visible",
        "Name",
        "Type",
        "Bounds",
        "Area",
        "Mean",
        "Median",
        "Std",
        "Min",
        "Max",
        "Sum",
        "Note",
    ]

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolService = ViewerToolService.for_viewer(napariViewer)
        # Register only; the tool is acquired when Draw Rectangle is used.
        self._toolToken = self._toolService.register(self.TOOL_OWNER)
        self._model = ROIManagerModel()
        self._stats_rows: list[dict[str, object]] = []

        self.addRectangleButton = QtWidgets.QPushButton("Draw Rectangle")
        self.addRectangleButton.setToolTip("Switch the viewer tool to rectangle drawing")
        self.captureButton = QtWidgets.QPushButton("Add Shape")
        self.captureButton.setToolTip("Add the first rectangle from the current shapes layer")
        self.renameButton = QtWidgets.QPushButton("Rename")
        self.duplicateButton = QtWidgets.QPushButton("Duplicate")
        self.deleteButton = QtWidgets.QPushButton("Delete")
        self.clearButton = QtWidgets.QPushButton("Clear")
        self.refreshButton = QtWidgets.QPushButton("Refresh Stats")
        self.exportCsvButton = QtWidgets.QPushButton("Export CSV")
        self.exportJsonButton = QtWidgets.QPushButton("Export JSON")

        controls = QtWidgets.QHBoxLayout()
        for btn in (
            self.addRectangleButton,
            self.captureButton,
            self.renameButton,
            self.duplicateButton,
            self.deleteButton,
            self.clearButton,
            self.refreshButton,
            self.exportCsvButton,
            self.exportJsonButton,
        ):
            controls.addWidget(btn)
        controls.addStretch()

        self.table = QtWidgets.QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)

        self.summaryLabel = QtWidgets.QLabel("No ROIs.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.addRectangleButton.clicked.connect(self._startRectangleDrawing)
        self.captureButton.clicked.connect(self.add_current_rectangle)
        self.renameButton.clicked.connect(self.rename_selected)
        self.duplicateButton.clicked.connect(self.duplicate_selected)
        self.deleteButton.clicked.connect(self.delete_selected)
        self.clearButton.clicked.connect(self.clear_rois)
        self.refreshButton.clicked.connect(self.refresh_stats)
        self.exportCsvButton.clicked.connect(self.export_csv)
        self.exportJsonButton.clicked.connect(self.export_json)
        self.table.itemChanged.connect(self._item_changed)
        try:
            self._viewer.dims.events.current_step.connect(lambda _event: self.refresh_stats())
        except Exception:
            pass

        self.refresh_stats()

    def _startRectangleDrawing(self) -> None:
        # Re-acquire so the rectangle drawn next is attributed to this panel.
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        self._toolService.set_mode(self._toolToken, "rectangle")

    def add_current_rectangle(self) -> None:
        try:
            # add() uniquifies on collision, so naming here as well would only
            # be a second chance to get it wrong.
            self._model.add(self._first_rectangle_roi())
            # Clears only this panel's scratch shape, leaving the Profile and
            # ROI statistics panels' shapes alone.
            self._toolService.clear(self._toolToken)
            self.refresh_stats()
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Release the drawing tool and our viewer callbacks on close."""
        try:
            self._toolService.release(self._toolToken)
        except Exception:
            pass
        super().closeEvent(event)

    def add_rois(self, rois: list[ROIRecord]) -> int:
        """Add externally generated ROIs, preserving unique names."""
        count = 0
        for roi in rois:
            self._model.add(roi)
            count += 1
        self.refresh_stats()
        return count

    def rois(self, *, visible_only: bool = False) -> list[ROIRecord]:
        """Return a copy of currently managed ROIs for analysis widgets.

        Defaults to every ROI so existing consumers (PSF resolution,
        colocalization, segmentation) keep the behaviour they were written
        against; pass ``visible_only=True`` to honour the panel's checkboxes.
        """
        rois = self._model.rois
        return [roi for roi in rois if roi.visible] if visible_only else rois

    def rename_selected(self) -> None:
        roi = self._selected_roi()
        if roi is None:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Rename ROI",
            "ROI name:",
            text=roi.name,
        )
        if ok and new_name.strip():
            try:
                self._model.rename(roi.name, new_name.strip())
                self.refresh_stats()
            except Exception as exc:
                self.summaryLabel.setText(str(exc))

    def duplicate_selected(self) -> None:
        roi = self._selected_roi()
        if roi is None:
            return
        self._model.duplicate(roi.name)
        self.refresh_stats()

    def delete_selected(self) -> None:
        roi = self._selected_roi()
        if roi is None:
            return
        self._model.remove(roi.name)
        self.refresh_stats()

    def clear_rois(self) -> None:
        self._model.clear()
        self.refresh_stats()

    def export_csv(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export ROI Statistics",
            "",
            "CSV files (*.csv)",
        )
        if path:
            self.write_csv(Path(path))

    def export_json(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export ROIs",
            "",
            "JSON files (*.json)",
        )
        if path:
            self.write_json(Path(path))

    def write_csv(self, path: Path) -> None:
        rows = self._stats_rows
        if not rows:
            rows = [record.to_dict() for record in self._model.rois]
        fieldnames = list(rows[0].keys()) if rows else ["name"]
        with Path(path).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def write_json(self, path: Path) -> None:
        payload = {
            "rois": self._model.to_dicts(),
            "statistics": self._stats_rows,
        }
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def setCurrentResult(self, result) -> None:
        """Re-measure the managed ROIs against the newly selected result.

        The ROI set is deliberately kept across results — measuring the same
        regions on several reconstructions is the point — but the numbers
        beside them have to follow the result they are measured on.
        """
        self.refresh_stats()

    def refresh_stats(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self._stats_rows = []
            self._populate_table(None)
            self.summaryLabel.setText("No image layer selected.")
            return
        # compute_stats reports per-ROI failures in the record rather than
        # raising, so one unmeasurable ROI can no longer blank the table.
        records = self._model.compute_stats(image)
        self._stats_rows = [record.to_row() for record in records]
        self._populate_table(records)
        measured = sum(1 for record in records if record.measured)
        failed = sum(1 for record in records if record.error)
        summary = f"{len(records)} ROI(s), {measured} measured, image shape {image.shape}."
        if failed:
            summary += f" {failed} could not be measured (see Note)."
        self.summaryLabel.setText(summary)

    def _populate_table(self, records) -> None:
        """Render one row per ROI in the model.

        Rows always come from the model, never from the measured subset: an
        unchecked ROI still needs the row that holds the checkbox to switch it
        back on. Statistics cells stay blank for anything not measured.
        """
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        try:
            by_name = {record.roi.name: record for record in (records or [])}
            rois = self._model.rois
            self.table.setRowCount(len(rois))
            for row, roi in enumerate(rois):
                record = by_name.get(roi.name)
                stats = record.stats if record is not None and record.measured else None

                visible_item = _TableItem("")
                visible_item.setFlags(visible_item.flags() | QtCore.Qt.ItemIsUserCheckable)
                visible_item.setCheckState(QtCore.Qt.Checked if roi.visible else QtCore.Qt.Unchecked)
                visible_item.setData(ROI_KEY_ROLE, roi.name)
                visible_item.setData(SORT_KEY_ROLE, bool(roi.visible))
                self.table.setItem(row, 0, visible_item)

                values = [
                    roi.name,
                    roi.roi_type,
                    f"{roi.bounds}",
                    None if stats is None else stats.area_pixels,
                    None if stats is None else stats.mean,
                    None if stats is None else stats.median,
                    None if stats is None else stats.std,
                    None if stats is None else stats.minimum,
                    None if stats is None else stats.maximum,
                    None if stats is None else stats.total,
                    "" if record is None else record.note,
                ]
                for col, value in enumerate(values, start=1):
                    item = _TableItem(self._format_value(value))
                    item.setData(SORT_KEY_ROLE, value)
                    self.table.setItem(row, col, item)
        finally:
            self.table.blockSignals(False)
            self.table.setSortingEnabled(sorting)

    def _item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        name = item.data(ROI_KEY_ROLE)
        if not name:
            return
        self._model.set_visible(str(name), item.checkState() == QtCore.Qt.Checked)
        self.refresh_stats()

    def _first_rectangle_roi(self) -> ROIRecord:
        # Only shapes this panel owns: a rectangle drawn for the Profile panel
        # is not ours to capture.
        for _index, shape_type, shape in self._toolService.shapes(self._toolToken):
            if shape_type != "rectangle":
                continue
            # Shape vertices come from the Shapes layer in world coordinates;
            # divide by the image scale so the ROI bounds are stored in image
            # row/col (pixel) coordinates, as ROIRecord expects. No-op at scale
            # 1, but required for scaled reconstructions.
            row_scale, col_scale = self._visible_pixel_scales()
            vertices = np.asarray(shape, dtype=np.float64).copy()
            vertices[:, 0] /= row_scale
            vertices[:, 1] /= col_scale
            return rectangle_roi_from_vertices(
                vertices,
                name=self._model.unique_name("ROI"),
            )
        raise ValueError("Draw a rectangle first.")

    def _visible_pixel_scales(self) -> tuple[float, float]:
        """Return the active image layer's (row, col) scale (1.0 fallback)."""
        layer = self._active_image_layer()
        if layer is None:
            return 1.0, 1.0
        try:
            scale = tuple(float(v) for v in layer.scale)
        except Exception:
            return 1.0, 1.0
        if len(scale) < 2:
            return 1.0, 1.0
        return scale[-2], scale[-1]

    def _selected_roi(self) -> ROIRecord | None:
        """The ROI behind the selected row, resolved by key rather than index.

        Row order stops matching model order the moment the table is sorted, so
        indexing the model by row number would rename or delete a different ROI
        than the one the user clicked.
        """
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        name = item.data(ROI_KEY_ROLE) if item is not None else None
        roi = self._model.get(str(name)) if name else None
        if roi is None:
            self.summaryLabel.setText("Select an ROI first.")
            return None
        return roi

    def _current_image_2d(self):
        layer = self._active_image_layer()
        if layer is None:
            return None
        data = np.asarray(layer.data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data
        step = self._current_step(data.ndim)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            else:
                indexer.append(min(max(step[axis], 0), size - 1))
        return np.asarray(data[tuple(indexer)])

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _active_image_layer(self):
        return active_image_layer(self._viewer)

    @staticmethod
    def _format_value(value) -> str:
        """Format a cell the same way the shared Results table does.

        Kept in step with ``ResultsTableWidget.format_table_value`` so a number
        does not render one way in this panel and another way once it is
        exported or pushed.
        """
        return format_table_value(value)
