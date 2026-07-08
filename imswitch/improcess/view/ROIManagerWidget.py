"""ImageJ-like ROI manager panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager
from imswitch.improcess.analysis.roi_manager import (
    ROIManagerModel,
    ROIRecord,
    rectangle_roi_from_vertices,
)


class ROIManagerWidget(QtWidgets.QWidget):
    """Manage multiple rectangular ROIs and per-ROI statistics."""

    _COLUMNS = [
        "Visible",
        "Name",
        "Bounds",
        "Area",
        "Mean",
        "Median",
        "Std",
        "Min",
        "Max",
        "Sum",
    ]

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolManager = ViewerToolManager(napariViewer)
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

        self.summaryLabel = QtWidgets.QLabel("No ROIs.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.addRectangleButton.clicked.connect(lambda: self._toolManager.set_mode("rectangle"))
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

    def add_current_rectangle(self) -> None:
        try:
            roi = self._first_rectangle_roi()
            roi = ROIRecord(
                name=self._model.unique_name(roi.name),
                roi_type=roi.roi_type,
                bounds=roi.bounds,
                visible=roi.visible,
                source=roi.source,
            )
            self._model.add(roi)
            self._toolManager.clear_shapes()
            self.refresh_stats()
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def add_rois(self, rois: list[ROIRecord]) -> int:
        """Add externally generated ROIs, preserving unique names."""
        count = 0
        for roi in rois:
            self._model.add(roi)
            count += 1
        self.refresh_stats()
        return count

    def rois(self) -> list[ROIRecord]:
        """Return a copy of currently managed ROIs for analysis widgets."""
        return self._model.rois

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

    def refresh_stats(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self._stats_rows = []
            self._populate_table(None)
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            records = self._model.compute_stats(image)
            self._stats_rows = [record.to_row() for record in records]
            self._populate_table(records)
            self.summaryLabel.setText(f"{len(records)} ROI(s), image shape {image.shape}.")
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            self._populate_table(None)

    def _populate_table(self, records) -> None:
        self.table.blockSignals(True)
        try:
            rois = self._model.rois if records is None else [record.roi for record in records]
            self.table.setRowCount(len(rois))
            for row, roi in enumerate(rois):
                visible_item = QtWidgets.QTableWidgetItem("")
                visible_item.setFlags(visible_item.flags() | QtCore.Qt.ItemIsUserCheckable)
                visible_item.setCheckState(QtCore.Qt.Checked if roi.visible else QtCore.Qt.Unchecked)
                visible_item.setData(QtCore.Qt.UserRole, roi.name)
                self.table.setItem(row, 0, visible_item)

                stats = records[row].stats if records is not None else None
                values = [
                    roi.name,
                    f"{roi.bounds}",
                    "" if stats is None else stats.area_pixels,
                    "" if stats is None else stats.mean,
                    "" if stats is None else stats.median,
                    "" if stats is None else stats.std,
                    "" if stats is None else stats.minimum,
                    "" if stats is None else stats.maximum,
                    "" if stats is None else stats.total,
                ]
                for col, value in enumerate(values, start=1):
                    self.table.setItem(row, col, QtWidgets.QTableWidgetItem(self._format_value(value)))
        finally:
            self.table.blockSignals(False)

    def _item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        name = item.data(QtCore.Qt.UserRole)
        if not name:
            return
        self._model.set_visible(str(name), item.checkState() == QtCore.Qt.Checked)
        self.refresh_stats()

    def _first_rectangle_roi(self) -> ROIRecord:
        for index, shape_type in enumerate(self._toolManager.get_shape_types()):
            if shape_type != "rectangle":
                continue
            shapes = self._toolManager.get_shapes_data()
            if index >= len(shapes):
                break
            # Shape vertices come from the Shapes layer in world coordinates;
            # divide by the image scale so the ROI bounds are stored in image
            # row/col (pixel) coordinates, as ROIRecord expects. No-op at scale
            # 1, but required for scaled reconstructions.
            row_scale, col_scale = self._visible_pixel_scales()
            vertices = np.asarray(shapes[index], dtype=np.float64).copy()
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
        row = self.table.currentRow()
        if row < 0 or row >= len(self._model.rois):
            self.summaryLabel.setText("Select an ROI first.")
            return None
        return self._model.rois[row]

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
        """Return the active image-like Napari layer, falling back to the first valid one.
        
        Aligned with ReconstructionView.getActiveImageLayer() semantics: prefer the active
        layer when it's image-like, otherwise scan for a valid layer.
        """
        try:
            active = self._viewer.layers.selection.active
        except Exception:
            active = None
        if self._is_image_layer(active):
            return active
        for layer in self._viewer.layers:
            if self._is_image_layer(layer):
                return layer
        return None

    @staticmethod
    def _is_image_layer(layer) -> bool:
        return (
            layer is not None
            and hasattr(layer, "data")
            and isinstance(layer.data, np.ndarray)
            and layer.data.ndim >= 2
            and getattr(layer, "visible", True)
            and not str(getattr(layer, "name", "")).startswith("_")
            and getattr(layer, "name", "") != "Viewer Tools"
        )

    @staticmethod
    def _format_value(value) -> str:
        if isinstance(value, float):
            return f"{value:.6g}" if np.isfinite(value) else "nan"
        return str(value)
