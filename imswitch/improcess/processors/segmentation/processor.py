"""Threshold + connected-component segmentation processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.segmentation import segment_image
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import SegmentationResult


class SegmentationProcessor(Processor):
    """Segment a 2D plane from an ImProcess result."""

    name = "Segmentation"
    id = "segmentation"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.data.ndim >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        method_combo = QtWidgets.QComboBox()
        method_combo.addItems(["otsu", "manual", "triangle", "yen", "local", "watershed"])
        layout.addRow("Method:", method_combo)

        threshold_spin = QtWidgets.QDoubleSpinBox()
        threshold_spin.setDecimals(6)
        threshold_spin.setRange(-1e12, 1e12)
        threshold_spin.setValue(0.0)
        layout.addRow("Manual value:", threshold_spin)

        min_area_spin = QtWidgets.QSpinBox()
        min_area_spin.setRange(1, 999999999)
        min_area_spin.setValue(10)
        layout.addRow("Min area:", min_area_spin)

        smooth_spin = QtWidgets.QDoubleSpinBox()
        smooth_spin.setDecimals(3)
        smooth_spin.setRange(0.0, 1000.0)
        smooth_spin.setValue(0.0)
        layout.addRow("Smooth sigma:", smooth_spin)

        background_spin = QtWidgets.QDoubleSpinBox()
        background_spin.setDecimals(1)
        background_spin.setRange(0.0, 10000.0)
        background_spin.setValue(0.0)
        layout.addRow("Top-hat radius:", background_spin)

        morphology_spin = QtWidgets.QSpinBox()
        morphology_spin.setRange(0, 9999)
        morphology_spin.setValue(0)
        layout.addRow("Morph radius:", morphology_spin)

        fill_holes_check = QtWidgets.QCheckBox("Fill holes")
        layout.addRow("", fill_holes_check)

        clear_border_check = QtWidgets.QCheckBox("Clear border")
        layout.addRow("", clear_border_check)

        local_block_spin = QtWidgets.QSpinBox()
        local_block_spin.setRange(3, 9999)
        local_block_spin.setSingleStep(2)
        local_block_spin.setValue(51)
        layout.addRow("Local block:", local_block_spin)

        local_offset_spin = QtWidgets.QDoubleSpinBox()
        local_offset_spin.setDecimals(6)
        local_offset_spin.setRange(-1e12, 1e12)
        local_offset_spin.setValue(0.0)
        layout.addRow("Local offset:", local_offset_spin)

        watershed_distance_spin = QtWidgets.QSpinBox()
        watershed_distance_spin.setRange(1, 9999)
        watershed_distance_spin.setValue(5)
        layout.addRow("Watershed distance:", watershed_distance_spin)

        def get_values():
            return {
                "threshold_method": method_combo.currentText(),
                "threshold_value": threshold_spin.value(),
                "min_area": min_area_spin.value(),
                "smooth_sigma": smooth_spin.value(),
                "background_radius": background_spin.value(),
                "morphology_radius": morphology_spin.value(),
                "fill_holes": fill_holes_check.isChecked(),
                "clear_border": clear_border_check.isChecked(),
                "local_block_size": local_block_spin.value(),
                "local_offset": local_offset_spin.value(),
                "watershed_min_distance": watershed_distance_spin.value(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        image = self._extract_2d(result)
        threshold_method = params.get("threshold_method", "otsu")
        analysis = segment_image(
            image,
            threshold_method=threshold_method,
            threshold_value=(
                float(params.get("threshold_value", 0.0))
                if threshold_method == "manual"
                else None
            ),
            min_area=int(params.get("min_area", 10)),
            smooth_sigma=float(params.get("smooth_sigma", 0.0)),
            background_radius=float(params.get("background_radius", 0.0)),
            morphology_radius=int(params.get("morphology_radius", 0)),
            fill_holes=bool(params.get("fill_holes", False)),
            clear_border=bool(params.get("clear_border", False)),
            local_block_size=int(params.get("local_block_size", 51)),
            local_offset=float(params.get("local_offset", 0.0)),
            watershed_min_distance=int(params.get("watershed_min_distance", 5)),
        )
        return SegmentationResult(
            name=f"{result.name} (segmentation)",
            analysis=analysis,
            params=dict(params),
        )

    @staticmethod
    def _extract_2d(result: ProcessingResult) -> np.ndarray:
        data = np.asarray(result.data)
        if data.ndim == 2:
            return data
        if data.ndim < 2:
            raise ValueError(f"Segmentation needs at least 2D data, got shape {data.shape}")
        indexer = []
        for axis in range(data.ndim):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            else:
                indexer.append(0)
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D segmentation image from shape {data.shape}")
        return image
