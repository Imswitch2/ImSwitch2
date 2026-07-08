"""Interactive segmentation panel for ImProcess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.improcess.analysis.segmentation import SegmentationAnalysis, segment_image
from imswitch.improcess.layer_selection import active_image_layer
from imswitch.improcess.processors import SegmentationProcessor


class SegmentationWidget(QtWidgets.QWidget):
    """Threshold + connected-component segmentation for the active image layer."""

    sigRunRequested = QtCore.Signal(object, dict)

    # Class attribute so layer-resolution helpers work even on partially
    # constructed instances (unit tests build the widget via __new__).
    _preview_layer_name = "Segmentation preview"
    _binary_preview_layer_name = "Binarization preview"

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._last_analysis: SegmentationAnalysis | None = None
        self._currentResult = None
        self.processor = SegmentationProcessor()
        self._preview_timer = QtCore.QTimer()
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(350)
        self._preview_timer.timeout.connect(self._update_preview)
        self._dims_connection = None
        self._layer_selection_connection = None

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
        self.previewCheck = QtWidgets.QCheckBox("Preview")
        self.previewModeCombo = QtWidgets.QComboBox()
        self.previewModeCombo.addItems(["Segmentation labels", "Binarization mask"])

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
        form.addRow("", self.previewCheck)
        form.addRow("Preview mode", self.previewModeCombo)

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
        self.previewCheck.toggled.connect(self._on_preview_toggled)
        
        self.methodCombo.currentTextChanged.connect(self._schedule_preview)
        self.thresholdSpin.valueChanged.connect(self._schedule_preview)
        self.minAreaSpin.valueChanged.connect(self._schedule_preview)
        self.smoothSpin.valueChanged.connect(self._schedule_preview)
        self.backgroundSpin.valueChanged.connect(self._schedule_preview)
        self.morphologySpin.valueChanged.connect(self._schedule_preview)
        self.fillHolesCheck.toggled.connect(self._schedule_preview)
        self.clearBorderCheck.toggled.connect(self._schedule_preview)
        self.localBlockSpin.valueChanged.connect(self._schedule_preview)
        self.localOffsetSpin.valueChanged.connect(self._schedule_preview)
        self.watershedDistanceSpin.valueChanged.connect(self._schedule_preview)
        self.previewModeCombo.currentTextChanged.connect(self._schedule_preview)
        
        self._update_manual_enabled()

    def run(self) -> None:
        """Commit button: segment the current result and emit sigRunRequested.
        
        The processor extracts the 2D plane itself (_extract_2d), so we segment
        the whole current result. The preview operates on the active layer (visual
        tuning); commit segments the current result.
        """
        if self._currentResult is None:
            self.summaryLabel.setText("No result selected. Load or create a result first.")
            return
        try:
            self.sigRunRequested.emit(self._currentResult, self.parameterValues())
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def setCurrentResult(self, result):
        """Conform to result-processor widget contract: store the current result."""
        from imswitch.improcess.processors.segmentation.result import SegmentationResult
        
        self._currentResult = result
        # Enable/disable Segment button based on whether result has image data
        has_image = result is not None and hasattr(result, 'data') and result.data is not None
        self.runButton.setEnabled(has_image)
        
        # Capture analysis from SegmentationResult for ROI export
        if isinstance(result, SegmentationResult):
            self._last_analysis = getattr(result, "analysis", None)
        else:
            # Non-segmentation result: clear cached analysis
            # (ROI export will show "Run segmentation first.")
            self._last_analysis = None

    def setStatusText(self, text: str):
        """Conform to result-processor widget contract: forward to summaryLabel."""
        self.summaryLabel.setText(text)

    def parameterValues(self) -> dict:
        """Conform to result-processor widget contract: return current params.

        Keys MUST match SegmentationProcessor.apply()'s param contract
        (threshold_method/threshold_value/smooth_sigma/background_radius/
        morphology_radius/...), otherwise the processor silently falls back to
        its defaults and the panel's controls are ignored on commit.
        """
        return {
            "threshold_method": self.methodCombo.currentText(),
            "threshold_value": self.thresholdSpin.value(),
            "min_area": self.minAreaSpin.value(),
            "smooth_sigma": self.smoothSpin.value(),
            "background_radius": self.backgroundSpin.value(),
            "morphology_radius": self.morphologySpin.value(),
            "fill_holes": self.fillHolesCheck.isChecked(),
            "clear_border": self.clearBorderCheck.isChecked(),
            "local_block_size": self.localBlockSpin.value(),
            "local_offset": self.localOffsetSpin.value(),
            "watershed_min_distance": self.watershedDistanceSpin.value(),
        }

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

    def _schedule_preview(self) -> None:
        if self.previewCheck.isChecked():
            self._preview_timer.start()

    def _on_preview_toggled(self, checked: bool) -> None:
        if checked:
            self._connect_viewer_events()
            self._update_preview()
        else:
            self._disconnect_viewer_events()
            self._preview_timer.stop()
            self._remove_preview_layer()

    def _update_preview(self) -> None:
        layer = self._active_image_layer()
        image = self._image_2d_from_layer(layer)
        if image is None:
            self.summaryLabel.setText("Preview: no image layer selected.")
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
            preview_mode = self._preview_mode()
            preview_name = self._current_preview_layer_name(preview_mode)
            self._remove_inactive_preview_layers(preview_name)
            preview_layer = self._get_preview_layer(preview_name)
            preview_data = self._preview_data_for_analysis(analysis, preview_mode)
            preview_metadata = {
                "axis_labels": ["Y", "X"],
                "scale_unit": self._layer_scale_unit(layer),
                "segmentation": analysis.metadata,
                "preview_mode": preview_mode,
            }
            if preview_layer is None:
                # napari activates freshly added layers; restore the previous
                # active layer so the preview never becomes its own source.
                try:
                    prev_active = self._viewer.layers.selection.active
                except Exception:
                    prev_active = None
                if preview_mode == "mask":
                    self._viewer.add_image(
                        preview_data,
                        name=preview_name,
                        scale=self._spatial_layer_scale(layer),
                        opacity=0.45,
                        colormap="green",
                        blending="translucent",
                        metadata=preview_metadata,
                    )
                else:
                    self._viewer.add_labels(
                        preview_data,
                        name=preview_name,
                        scale=self._spatial_layer_scale(layer),
                        opacity=0.5,
                        metadata=preview_metadata,
                    )
                if prev_active is not None:
                    try:
                        self._viewer.layers.selection.active = prev_active
                    except Exception:
                        pass
            else:
                preview_layer.data = preview_data
                preview_layer.scale = self._spatial_layer_scale(layer)
                try:
                    preview_layer.metadata = preview_metadata
                except Exception:
                    pass
            if preview_mode == "mask":
                foreground = int(np.count_nonzero(preview_data))
                self.summaryLabel.setText(
                    f"Preview mask: threshold {analysis.threshold:.6g}; {foreground} foreground pixel(s)."
                )
            else:
                self.summaryLabel.setText(
                    f"Preview: threshold {analysis.threshold:.6g}; {len(analysis.regions)} region(s)."
                )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def _connect_viewer_events(self) -> None:
        try:
            if self._dims_connection is None:
                self._dims_connection = self._viewer.dims.events.current_step.connect(
                    lambda _: self._schedule_preview()
                )
        except Exception:
            pass
        try:
            if self._layer_selection_connection is None:
                self._layer_selection_connection = self._viewer.layers.selection.events.active.connect(
                    lambda _: self._schedule_preview()
                )
        except Exception:
            pass

    def _disconnect_viewer_events(self) -> None:
        try:
            if self._dims_connection is not None:
                self._viewer.dims.events.current_step.disconnect(self._dims_connection)
                self._dims_connection = None
        except Exception:
            pass
        try:
            if self._layer_selection_connection is not None:
                self._viewer.layers.selection.events.active.disconnect(self._layer_selection_connection)
                self._layer_selection_connection = None
        except Exception:
            pass

    def _preview_layer_names(self) -> tuple[str, str]:
        return (self._preview_layer_name, self._binary_preview_layer_name)

    def _preview_mode(self) -> str:
        combo = getattr(self, "previewModeCombo", None)
        try:
            text = str(combo.currentText()).strip().lower()
        except Exception:
            text = ""
        return "mask" if "mask" in text or "binar" in text else "labels"

    def _current_preview_layer_name(self, preview_mode: str | None = None) -> str:
        mode = preview_mode or self._preview_mode()
        return self._binary_preview_layer_name if mode == "mask" else self._preview_layer_name

    @staticmethod
    def _preview_data_for_analysis(analysis: SegmentationAnalysis, preview_mode: str) -> np.ndarray:
        if preview_mode == "mask":
            mask = analysis.binary_mask if analysis.binary_mask is not None else analysis.mask
            return np.asarray(mask, dtype=np.uint8)
        return np.asarray(analysis.labels)

    def _get_preview_layer(self, name: str | None = None):
        names = (name,) if name is not None else self._preview_layer_names()
        try:
            for layer in self._viewer.layers:
                if getattr(layer, "name", "") in names:
                    return layer
        except Exception:
            pass
        return None

    def _remove_preview_layer(self) -> None:
        try:
            for preview_name in self._preview_layer_names():
                preview_layer = self._get_preview_layer(preview_name)
                if preview_layer is not None:
                    self._viewer.layers.remove(preview_layer)
        except Exception:
            pass

    def _remove_inactive_preview_layers(self, active_name: str) -> None:
        try:
            for preview_name in self._preview_layer_names():
                if preview_name == active_name:
                    continue
                preview_layer = self._get_preview_layer(preview_name)
                if preview_layer is not None:
                    self._viewer.layers.remove(preview_layer)
        except Exception:
            pass

    def _current_image_2d(self):
        layer = self._active_image_layer()
        return self._image_2d_from_layer(layer)

    def _image_2d_from_layer(self, layer):
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

    @staticmethod
    def _spatial_layer_scale(layer) -> list[float]:
        try:
            scale = [float(value) for value in layer.scale]
        except Exception:
            scale = []
        if len(scale) >= 2:
            return scale[-2:]
        return [1.0, 1.0]

    @staticmethod
    def _layer_scale_unit(layer) -> str:
        try:
            unit = layer.metadata.get("scale_unit", None)
        except Exception:
            unit = None
        return str(unit or "px")

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _active_image_layer(self):
        # Preview layers must never be picked as a segmentation source.
        return active_image_layer(
            self._viewer, exclude_names=self._preview_layer_names()
        )
