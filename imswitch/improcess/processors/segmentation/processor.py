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
        method_combo.addItems(["otsu", "manual"])
        layout.addRow("Threshold:", method_combo)

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

        def get_values():
            return {
                "threshold_method": method_combo.currentText(),
                "threshold_value": threshold_spin.value(),
                "min_area": min_area_spin.value(),
                "smooth_sigma": smooth_spin.value(),
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
