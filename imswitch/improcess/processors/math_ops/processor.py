"""Single-image math (ImageJ's Process > Math).

Constant arithmetic plus the classic unary transforms. Runs in the generic
result-processor panel: one operation combo and one value spinbox.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

#: Operations taking the constant ``value`` parameter.
_VALUE_OPERATIONS = ("add", "subtract", "multiply", "divide", "gamma")
#: Operations ignoring ``value``.
_UNARY_OPERATIONS = ("invert", "log", "exp", "square-root")

MATH_OPERATIONS = _VALUE_OPERATIONS + _UNARY_OPERATIONS


class MathProcessor(Processor):
    """Apply constant arithmetic or a unary transform to the image."""

    name = "Math"
    id = "math"
    category = "Math"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True
    # Restricting to a region is meaningful here (P-R): the operation is
    # per-pixel or local, so running it over one cell answers the same
    # question as running it over the frame, only about that cell.
    accepts_roi = True
    roi_modes = ('mask', 'crop')

    @classmethod
    def default_params(cls) -> dict:
        return {'operation': 'add', 'value': 1.0}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        operation_combo = QtWidgets.QComboBox()
        operation_combo.addItems(list(MATH_OPERATIONS))
        layout.addRow("Operation:", operation_combo)

        value_spin = QtWidgets.QDoubleSpinBox()
        value_spin.setRange(-1e9, 1e9)
        value_spin.setDecimals(4)
        value_spin.setValue(1.0)
        value_spin.setToolTip(
            "Constant for add/subtract/multiply/divide; exponent for gamma"
        )
        layout.addRow("Value:", value_spin)

        def sync_value_enabled():
            value_spin.setEnabled(operation_combo.currentText() in _VALUE_OPERATIONS)

        operation_combo.currentIndexChanged.connect(lambda _index: sync_value_enabled())
        sync_value_enabled()

        def get_values():
            return {
                "operation": operation_combo.currentText(),
                "value": float(value_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        operation = str(params.get("operation", "add"))
        value = float(params.get("value", 1.0))
        data = apply_math(np.asarray(result.data), operation, value)
        suffix = (
            f"{operation} {value:g}" if operation in _VALUE_OPERATIONS else operation
        )
        return ArrayProcessingResult(
            name=f"{result.name} ({suffix})",
            data=data,
            axis_labels=list(axis_labels_for_result(result)),
            display_levels=finite_range(data),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={
                "operation": self.id,
                "math_operation": operation,
                "value": value,
            },
        )


def apply_math(data: np.ndarray, operation: str, value: float = 1.0) -> np.ndarray:
    """Apply one math operation to an array; float32 output for safety."""
    data = np.asarray(data, dtype=np.float32)
    if operation == "add":
        return data + value
    if operation == "subtract":
        return data - value
    if operation == "multiply":
        return data * value
    if operation == "divide":
        if value == 0:
            raise ValueError("Cannot divide by zero")
        return data / value
    if operation == "invert":
        # Range-preserving inversion (ImageJ inverts within the value range).
        low, high = float(np.min(data)), float(np.max(data))
        return (high + low) - data
    if operation == "log":
        # ln(x) with non-positive pixels mapped to 0, like ImageJ's Log.
        result = np.zeros_like(data)
        positive = data > 0
        result[positive] = np.log(data[positive])
        return result
    if operation == "exp":
        return np.exp(data)
    if operation == "gamma":
        # Gamma on the normalized range so the value span is preserved.
        low, high = float(np.min(data)), float(np.max(data))
        span = high - low
        if span == 0:
            return data
        normalized = (data - low) / span
        return np.power(normalized, value, dtype=np.float32) * span + low
    if operation == "square-root":
        return np.sqrt(np.maximum(data, 0))
    raise ValueError(
        f"Unknown math operation {operation!r}; expected one of {list(MATH_OPERATIONS)}"
    )


__all__ = ["MATH_OPERATIONS", "MathProcessor", "apply_math"]
