"""Interactive segmentation panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.segmentation import SegmentationAnalysis, segment_image


class SegmentationWidget(QtWidgets.QWidget):
    """Threshold + connected-component segmentation for the active image layer."""

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._last_analysis: SegmentationAnalysis | None = None

        self.methodCombo = QtWidgets.QComboBox()
        self.methodCombo.addItems(["otsu", "manual", "triangle", "yen", "local", "watershed"])

        self.thresholdSpin = QtWidgets.QDoubleSpinBox()
        self.thresholdSpin.setDecimals(6)
        self.thresholdSpin.setRange(-1e12, 1e12)
        self.thresholdSpin.setValue(0.0)

        self.minAreaSpin = QtWidgets.QSpinBox()
        self.minAreaSpin.setRange(1, 999999999)
        self.minAreaSpin.setValue(10)

        self.smoothSpin = QtWidgets.QDoubleSpinBox()
        self.smoothSpin.setDecimals(3)
        self.smoothSpin.setRange(0.0, 1000.0)
        self.smoothSpin.setValue(0.0)

        self.backgroundSpin = QtWidgets.QDoubleSpinBox()
        self.backgroundSpin.setDecimals(1)
        self.backgroundSpin.setRange(0.0, 10000.0)
        self.backgroundSpin.setValue(0.0)

        self.morphologySpin = QtWidgets.QSpinBox()
        self.morphologySpin.setRange(0, 9999)
        self.morphologySpin.setValue(0)

        self.fillHolesCheck = QtWidgets.QCheckBox("Fill holes")
        self.clearBorderCheck = QtWidgets.QCheckBox("Clear border")

        self.localBlockSpin = QtWidgets.QSpinBox()
        self.localBlockSpin.setRange(3, 9999)
        self.localBlockSpin.setSingleStep(2)
        self.localBlockSpin.setValue(51)

        self.localOffsetSpin = QtWidgets.QDoubleSpinBox()
        self.localOffsetSpin.setDecimals(6)
        self.localOffsetSpin.setRange(-1e12, 1e12)
        self.localOffsetSpin.setValue(0.0)

        self.watershedDistanceSpin = QtWidgets.QSpinBox()
        self.watershedDistanceSpin.setRange(1, 9999)
        self.watershedDistanceSpin.setValue(5)

        self.prefixEdit = QtWidgets.QLineEdit("Seg")

        self.runButton = QtWidgets.QPushButton("Segment")
        self.addRoisButton = QtWidgets.QPushButton("Add ROIs")
        self.addRoisButton.setToolTip("Add segmented component masks to ROI Manager")
        self.exportCsvButton = QtWidgets.QPushButton("Export CSV")
        self.exportCsvButton.setToolTip("Export the last segmentation region table as CSV")
        self.exportJsonButton = QtWidgets.QPushButton("Export JSON")
        self.exportJsonButton.setToolTip("Export the last segmentation labels and region table as JSON")
        self.summaryLabel = QtWidgets.QLabel("Run segmentation on the active image layer.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        form = QtWidgets.QFormLayout()
        form.addRow("Method", self.methodCombo)
        form.addRow("Manual value", self.thresholdSpin)
        form.addRow("Min area", self.minAreaSpin)
        form.addRow("Smooth sigma", self.smoothSpin)
        form.addRow("Top-hat radius", self.backgroundSpin)
        form.addRow("Morph radius", self.morphologySpin)
        form.addRow("", self.fillHolesCheck)
        form.addRow("", self.clearBorderCheck)
        form.addRow("Local block", self.localBlockSpin)
        form.addRow("Local offset", self.localOffsetSpin)
        form.addRow("Watershed distance", self.watershedDistanceSpin)
        form.addRow("ROI prefix", self.prefixEdit)

        controls = QtWidgets.QHBoxLayout()
        controls.addLayout(form)
        controls.addWidget(self.runButton)
        controls.addWidget(self.addRoisButton)
        controls.addWidget(self.exportCsvButton)
        controls.addWidget(self.exportJsonButton)
        controls.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.runButton.clicked.connect(self.run)
        self.addRoisButton.clicked.connect(self.add_rois_to_manager)
        self.exportCsvButton.clicked.connect(self.export_csv)
        self.exportJsonButton.clicked.connect(self.export_json)
        self.methodCombo.currentTextChanged.connect(self._update_manual_enabled)
        self._update_manual_enabled()

    def run(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        try:
            method = self.methodCombo.currentText()
            analysis = segment_image(
                image,
                threshold_method=method,
                threshold_value=self.thresholdSpin.value() if method == "manual" else None,
                min_area=self.minAreaSpin.value(),
                smooth_sigma=self.smoothSpin.value(),
                background_radius=self.backgroundSpin.value(),
                morphology_radius=self.morphologySpin.value(),
                fill_holes=self.fillHolesCheck.isChecked(),
                clear_border=self.clearBorderCheck.isChecked(),
                local_block_size=self.localBlockSpin.value(),
                local_offset=self.localOffsetSpin.value(),
                watershed_min_distance=self.watershedDistanceSpin.value(),
            )
            self._last_analysis = analysis
            self._viewer.add_labels(
                analysis.labels,
                name=f"Segmentation ({len(analysis.regions)} regions)",
                metadata={"segmentation": analysis.metadata},
            )
            self.summaryLabel.setText(
                f"Threshold {analysis.threshold:.6g}; {len(analysis.regions)} region(s)."
            )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime.

        Called by the main view after a runtime-loaded ROI manager dock comes
        up so 'Push to ROI manager' starts working without restarting the app.
        """
        self._roiManagerWidget = roiManagerWidget

    def add_rois_to_manager(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run segmentation first.")
            return
        if self._roiManagerWidget is None:
            self.summaryLabel.setText("ROI Manager panel is not enabled.")
            return
        prefix = self.prefixEdit.text().strip() or "Seg"
        count = self._roiManagerWidget.add_rois(self._last_analysis.rois(name_prefix=prefix))
        self.summaryLabel.setText(f"Added {count} ROI(s) to ROI Manager.")

    def export_csv(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run segmentation first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Segmentation Regions",
            "",
            "CSV files (*.csv)",
        )
        if path:
            self.write_csv(Path(path))
            self.summaryLabel.setText(f"Exported {len(self._last_analysis.regions)} region(s).")

    def export_json(self) -> None:
        if self._last_analysis is None:
            self.summaryLabel.setText("Run segmentation first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Segmentation",
            "",
            "JSON files (*.json)",
        )
        if path:
            self.write_json(Path(path))
            self.summaryLabel.setText(f"Exported {len(self._last_analysis.regions)} region(s).")

    def write_csv(self, path: Path) -> None:
        if self._last_analysis is None:
            raise ValueError("Run segmentation first")
        rows = self._last_analysis.region_rows()
        fieldnames = ["label", "area_pixels", "bounds", "mean_intensity", "max_intensity"]
        with Path(path).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def write_json(self, path: Path) -> None:
        if self._last_analysis is None:
            raise ValueError("Run segmentation first")
        payload = {
            "metadata": self._last_analysis.metadata,
            "regions": self._last_analysis.region_rows(),
            "labels": self._last_analysis.labels.tolist(),
        }
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def _update_manual_enabled(self) -> None:
        method = self.methodCombo.currentText()
        self.thresholdSpin.setEnabled(method == "manual")
        is_local = method == "local"
        self.localBlockSpin.setEnabled(is_local)
        self.localOffsetSpin.setEnabled(is_local)
        self.watershedDistanceSpin.setEnabled(method == "watershed")

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
