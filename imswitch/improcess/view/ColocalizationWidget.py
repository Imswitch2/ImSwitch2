"""Interactive colocalization panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.colocalization import (
    ColocalizationAnalysis,
    colocalization_batch,
)


class ColocalizationWidget(QtWidgets.QWidget):
    """Compute channel colocalization from the active image layer."""

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._last_analysis: ColocalizationAnalysis | None = None

        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItems(["Full image", "ROI Manager"])

        self.axisCombo = QtWidgets.QComboBox()
        self.axisCombo.addItems(["Auto", "C", "T", "Z", "D0", "D1", "D2"])

        self.indexASpin = QtWidgets.QSpinBox()
        self.indexASpin.setRange(0, 999999)
        self.indexASpin.setValue(0)

        self.indexBSpin = QtWidgets.QSpinBox()
        self.indexBSpin.setRange(0, 999999)
        self.indexBSpin.setValue(1)

        self.thresholdASpin = QtWidgets.QDoubleSpinBox()
        self.thresholdASpin.setDecimals(6)
        self.thresholdASpin.setRange(-1e12, 1e12)
        self.thresholdASpin.setValue(0.0)

        self.thresholdBSpin = QtWidgets.QDoubleSpinBox()
        self.thresholdBSpin.setDecimals(6)
        self.thresholdBSpin.setRange(-1e12, 1e12)
        self.thresholdBSpin.setValue(0.0)

        self.runButton = QtWidgets.QPushButton("Run")
        self.exportCsvButton = QtWidgets.QPushButton("Export CSV")
        self.exportJsonButton = QtWidgets.QPushButton("Export JSON")

        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Pearson", "Manders M1", "Manders M2", "Overlap", "Pixels", "Mean A", "Mean B"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        self.summaryLabel = QtWidgets.QLabel("Run colocalization on the active image stack.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Source", self.sourceCombo)
        form.addRow("Compare axis", self.axisCombo)
        form.addRow("Index A", self.indexASpin)
        form.addRow("Index B", self.indexBSpin)
        form.addRow("Threshold A", self.thresholdASpin)
        form.addRow("Threshold B", self.thresholdBSpin)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.runButton)
        controls.addWidget(self.exportCsvButton)
        controls.addWidget(self.exportJsonButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.runButton.clicked.connect(self.run)
        self.exportCsvButton.clicked.connect(self.export_csv)
        self.exportJsonButton.clicked.connect(self.export_json)

    def run(self) -> None:
        layer = self._active_image_layer()
        if layer is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            data = np.asarray(layer.data)
            axis = self._resolve_compare_axis(layer, data)
            image_a = self._extract_2d(layer, data, axis, self.indexASpin.value())
            image_b = self._extract_2d(layer, data, axis, self.indexBSpin.value())
            rois = None
            if self.sourceCombo.currentText() == "ROI Manager":
                if self._roiManagerWidget is None:
                    self.summaryLabel.setText("ROI Manager panel is not enabled.")
                    return
                rois = self._roiManagerWidget.rois()
                if not rois:
                    self.summaryLabel.setText("ROI Manager has no ROIs.")
                    return
            analysis = colocalization_batch(
                image_a,
                image_b,
                rois,
                threshold_a=self.thresholdASpin.value(),
                threshold_b=self.thresholdBSpin.value(),
            )
            self._last_analysis = analysis
            self._populate_table(analysis)
            self.summaryLabel.setText(f"Computed {len(analysis.records)} colocalization row(s).")
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget

    def export_csv(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run colocalization first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Colocalization",
            "",
            "CSV files (*.csv)",
        )
        if path:
            self.write_csv(Path(path))

    def export_json(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run colocalization first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Colocalization",
            "",
            "JSON files (*.json)",
        )
        if path:
            self.write_json(Path(path))

    def write_csv(self, path: Path) -> None:
        rows = self._rows_or_raise()
        with Path(path).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def write_json(self, path: Path) -> None:
        if self._last_analysis is None:
            raise ValueError("Run colocalization first")
        payload = {
            "metadata": self._last_analysis.metadata,
            "records": self._last_analysis.rows(),
        }
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def _rows_or_raise(self) -> list[dict[str, object]]:
        if self._last_analysis is None:
            raise ValueError("Run colocalization first")
        rows = self._last_analysis.rows()
        if not rows:
            raise ValueError("No colocalization rows to export")
        return rows

    def _populate_table(self, analysis: ColocalizationAnalysis) -> None:
        rows = analysis.rows()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row["name"],
                row["pearson"],
                row["manders_m1"],
                row["manders_m2"],
                row["overlap_coefficient"],
                row["pixel_count"],
                row["mean_a"],
                row["mean_b"],
            ]
            for col, value in enumerate(values):
                self.table.setItem(row_index, col, QtWidgets.QTableWidgetItem(self._format_value(value)))

    def _resolve_compare_axis(self, layer, data: np.ndarray) -> int:
        requested = self.axisCombo.currentText()
        labels = self._axis_labels(layer, data)
        if requested != "Auto":
            if requested not in labels:
                raise ValueError(f"Axis {requested!r} is not available for this layer")
            axis = labels.index(requested)
            if data.shape[axis] < 2:
                raise ValueError(f"Axis {requested!r} needs at least two planes")
            return axis
        for candidate in ("C", "T", "Z"):
            if candidate in labels and data.shape[labels.index(candidate)] >= 2:
                return labels.index(candidate)
        for axis, size in enumerate(data.shape[:-2]):
            if size >= 2:
                return axis
        raise ValueError("Colocalization needs an axis with at least two planes")

    def _extract_2d(self, layer, data: np.ndarray, compare_axis: int, index: int):
        if data.ndim < 3:
            raise ValueError("Colocalization needs a stack or channel axis")
        step = self._current_step(data.ndim)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            elif axis == compare_axis:
                if index < 0 or index >= size:
                    raise ValueError(f"Index {index} is out of range for axis size {size}")
                indexer.append(index)
            else:
                indexer.append(min(max(step[axis], 0), size - 1))
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D colocalization image from shape {data.shape}")
        return image

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _axis_labels(self, layer, data: np.ndarray) -> list[str]:
        try:
            labels = list(layer.metadata.get("axis_labels", []))
        except Exception:
            labels = []
        if len(labels) == data.ndim:
            return [str(label) for label in labels]
        defaults = ["T", "Z", "C", "Y", "X"]
        if data.ndim <= len(defaults):
            return defaults[-data.ndim:]
        extra = [f"D{i}" for i in range(data.ndim - len(defaults))]
        return [*extra, *defaults]

    def _active_image_layer(self):
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
