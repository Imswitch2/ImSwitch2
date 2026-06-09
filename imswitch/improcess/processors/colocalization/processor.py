"""Colocalization processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.colocalization import colocalization_batch
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import ColocalizationResult


class ColocalizationProcessor(Processor):
    """Compute colocalization metrics between two planes of an image stack."""

    name = "Colocalization"
    id = "colocalization"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.data.ndim >= 3 or (
            result.data.ndim >= 2 and len(result.axis_labels) >= 3
        )

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "C", "T", "Z", "D0", "D1", "D2"])
        layout.addRow("Compare axis:", axis_combo)

        index_a_spin = QtWidgets.QSpinBox()
        index_a_spin.setRange(0, 999999)
        index_a_spin.setValue(0)
        layout.addRow("Index A:", index_a_spin)

        index_b_spin = QtWidgets.QSpinBox()
        index_b_spin.setRange(0, 999999)
        index_b_spin.setValue(1)
        layout.addRow("Index B:", index_b_spin)

        threshold_a_spin = QtWidgets.QDoubleSpinBox()
        threshold_a_spin.setDecimals(6)
        threshold_a_spin.setRange(-1e12, 1e12)
        threshold_a_spin.setValue(0.0)
        layout.addRow("Threshold A:", threshold_a_spin)

        threshold_b_spin = QtWidgets.QDoubleSpinBox()
        threshold_b_spin.setDecimals(6)
        threshold_b_spin.setRange(-1e12, 1e12)
        threshold_b_spin.setValue(0.0)
        layout.addRow("Threshold B:", threshold_b_spin)

        def get_values():
            return {
                "compare_axis": axis_combo.currentText(),
                "index_a": index_a_spin.value(),
                "index_b": index_b_spin.value(),
                "threshold_a": threshold_a_spin.value(),
                "threshold_b": threshold_b_spin.value(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        axis_label = self._resolve_compare_axis(result, str(params.get("compare_axis", "Auto")))
        image_a = self._extract_2d(result, axis_label, int(params.get("index_a", 0)))
        image_b = self._extract_2d(result, axis_label, int(params.get("index_b", 1)))
        analysis = colocalization_batch(
            image_a,
            image_b,
            threshold_a=float(params.get("threshold_a", 0.0)),
            threshold_b=float(params.get("threshold_b", 0.0)),
        )
        return ColocalizationResult(
            name=f"{result.name} (colocalization {axis_label})",
            analysis=analysis,
            params=dict(params),
        )

    def _resolve_compare_axis(self, result: ProcessingResult, requested: str) -> str:
        if requested != "Auto":
            if requested not in result.axis_labels:
                raise ValueError(f"Requested compare axis {requested!r} not in {result.axis_labels}")
            if result.data.shape[result.axis_labels.index(requested)] < 2:
                raise ValueError(f"Compare axis {requested!r} needs at least two planes")
            return requested
        for candidate in ("C", "T", "Z"):
            if candidate in result.axis_labels:
                axis = result.axis_labels.index(candidate)
                if result.data.shape[axis] >= 2:
                    return candidate
        for axis, size in enumerate(result.data.shape[:-2]):
            if size >= 2:
                return result.axis_labels[axis]
        raise ValueError("Colocalization needs an axis with at least two planes")

    def _extract_2d(self, result: ProcessingResult, compare_axis_label: str, compare_index: int):
        data = np.asarray(result.data)
        if data.ndim < 3:
            raise ValueError(f"Colocalization needs a stack with at least one compare axis, got {data.shape}")
        compare_axis = result.axis_labels.index(compare_axis_label)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            elif axis == compare_axis:
                if compare_index < 0 or compare_index >= size:
                    raise ValueError(
                        f"Colocalization index {compare_index} out of range for axis "
                        f"{compare_axis_label!r} with size {size}"
                    )
                indexer.append(compare_index)
            else:
                indexer.append(0)
        image = np.asarray(data[tuple(indexer)])
        if image.ndim != 2:
            raise ValueError(f"Could not extract a 2D colocalization image from shape {data.shape}")
        return image
