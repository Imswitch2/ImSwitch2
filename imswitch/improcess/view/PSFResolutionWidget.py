"""Interactive PSF / bead resolution panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.psf_resolution import PSFResolutionAnalysis, fit_psf_batch


class PSFResolutionWidget(QtWidgets.QWidget):
    """Fit 2D Gaussian PSFs on the active image or ROI Manager entries."""

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._last_analysis: PSFResolutionAnalysis | None = None

        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItems(["Full image", "ROI Manager"])

        self.pixelSizeSpin = QtWidgets.QDoubleSpinBox()
        self.pixelSizeSpin.setDecimals(6)
        self.pixelSizeSpin.setRange(1e-9, 1e12)
        self.pixelSizeSpin.setValue(1.0)

        self.unitCombo = QtWidgets.QComboBox()
        self.unitCombo.addItems(["px", "nm", "um"])

        self.fitButton = QtWidgets.QPushButton("Fit")
        self.exportCsvButton = QtWidgets.QPushButton("Export CSV")
        self.exportJsonButton = QtWidgets.QPushButton("Export JSON")

        self.table = QtWidgets.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Name", "FWHM X", "FWHM Y", "Sigma X", "Sigma Y", "Center X", "Center Y", "Amp", "Error"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        self.summaryLabel = QtWidgets.QLabel("Fit PSF resolution on the active image layer.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Source", self.sourceCombo)
        form.addRow("Pixel size", self.pixelSizeSpin)
        form.addRow("Unit", self.unitCombo)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.fitButton)
        controls.addWidget(self.exportCsvButton)
        controls.addWidget(self.exportJsonButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.fitButton.clicked.connect(self.fit)
        self.exportCsvButton.clicked.connect(self.export_csv)
        self.exportJsonButton.clicked.connect(self.export_json)

    def fit(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            rois = None
            if self.sourceCombo.currentText() == "ROI Manager":
                if self._roiManagerWidget is None:
                    self.summaryLabel.setText("ROI Manager panel is not enabled.")
                    return
                rois = self._roiManagerWidget.rois()
                if not rois:
                    self.summaryLabel.setText("ROI Manager has no ROIs.")
                    return
            analysis = fit_psf_batch(
                image,
                rois,
                pixel_size=self.pixelSizeSpin.value(),
                unit=self.unitCombo.currentText(),
            )
            self._last_analysis = analysis
            self._populate_table(analysis)
            self.summaryLabel.setText(f"Fit {len(analysis.fits)} PSF region(s).")
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def export_csv(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run PSF fitting first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export PSF Fits",
            "",
            "CSV files (*.csv)",
        )
        if path:
            self.write_csv(Path(path))

    def export_json(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run PSF fitting first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export PSF Fits",
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
            raise ValueError("Run PSF fitting first")
        payload = {
            "metadata": self._last_analysis.metadata or {},
            "pixel_size": self._last_analysis.pixel_size,
            "unit": self._last_analysis.unit,
            "fits": self._last_analysis.rows(),
        }
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def _rows_or_raise(self) -> list[dict[str, object]]:
        if self._last_analysis is None:
            raise ValueError("Run PSF fitting first")
        rows = self._last_analysis.rows()
        if not rows:
            raise ValueError("No PSF fits to export")
        return rows

    def _populate_table(self, analysis: PSFResolutionAnalysis) -> None:
        rows = analysis.rows()
        self.table.setRowCount(len(rows))
        unit = analysis.unit
        for row_index, row in enumerate(rows):
            values = [
                row["name"],
                row[f"fwhm_x_{unit}"],
                row[f"fwhm_y_{unit}"],
                row[f"sigma_x_{unit}"],
                row[f"sigma_y_{unit}"],
                row["center_x_px"],
                row["center_y_px"],
                row["amplitude"],
                row["fit_error"],
            ]
            for col, value in enumerate(values):
                self.table.setItem(row_index, col, QtWidgets.QTableWidgetItem(self._format_value(value)))

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
            indexer.append(slice(None) if axis >= data.ndim - 2 else min(max(step[axis], 0), size - 1))
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
