"""Two-image arithmetic (ImageJ's Process > Image Calculator).

Combines two same-shaped results pixel-wise. Output is float32 by default so
subtract/divide never clip or wrap; compatibility reuses the Stack/Combine
metadata rules (shape, axis labels, axis scales and scale unit must match).
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
from imswitch.improcess.processors.combine import combine_compatibility

_OPERATIONS: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
    "add": np.add,
    "subtract": np.subtract,
    "multiply": np.multiply,
    "divide": lambda a, b: np.divide(
        a, b, out=np.zeros_like(a, dtype=np.result_type(a, b, np.float32)),
        where=b != 0,
    ),
    "min": np.minimum,
    "max": np.maximum,
    "average": lambda a, b: (a + b) / 2.0,
    "difference": lambda a, b: np.abs(a - b),
}

CALCULATOR_OPERATIONS = tuple(_OPERATIONS)


class ImageCalculatorProcessor(Processor):
    """Pixel-wise arithmetic between two selected results."""

    name = "Image calculator"
    id = "image-calculator"
    category = "Math"
    min_inputs = 2
    max_inputs = 2

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def check_inputs(self, results) -> tuple[bool, str]:
        results = list(results or [])
        if len(results) == 2 and results[0] is results[1]:
            # Same pick twice is meaningful (image minus itself) and is always
            # compatible with itself.
            return True, ""
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        return combine_compatibility(results, mode="stack")

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        operation_combo = QtWidgets.QComboBox()
        operation_combo.addItems(list(CALCULATOR_OPERATIONS))
        operation_combo.setToolTip(
            "Applied as A <operation> B, in the order the inputs are listed"
        )
        layout.addRow("Operation:", operation_combo)

        float_check = QtWidgets.QCheckBox("32-bit float result")
        float_check.setChecked(True)
        float_check.setToolTip(
            "Compute in float32 so subtraction and division never clip or wrap"
        )
        layout.addRow("", float_check)

        def get_values():
            return {
                "operation": operation_combo.currentText(),
                "float32": float_check.isChecked(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or [])
        if not results:
            results = [result, *list(params.get("additional_results", []) or [])]
        if len(results) != 2:
            raise ValueError("Image calculator needs exactly two inputs")
        return calculate_results(
            results[0],
            results[1],
            operation=str(params.get("operation", "add")),
            float_output=bool(params.get("float32", True)),
            name=params.get("name") or None,
        )


def calculate(a: np.ndarray, b: np.ndarray, operation: str) -> np.ndarray:
    """Apply one pixel-wise calculator operation to two arrays."""
    try:
        function = _OPERATIONS[operation]
    except KeyError:
        raise ValueError(
            f"Unknown calculator operation {operation!r}; "
            f"expected one of {sorted(_OPERATIONS)}"
        ) from None
    with np.errstate(divide="ignore", invalid="ignore"):
        return function(a, b)


def calculate_results(
    first: ProcessingResult,
    second: ProcessingResult,
    *,
    operation: str = "add",
    float_output: bool = True,
    name: str | None = None,
) -> ArrayProcessingResult:
    """Combine two compatible results pixel-wise into a new result."""
    ok, reason = combine_compatibility([first, second], mode="stack")
    if not ok:
        raise ValueError(reason)
    if operation not in _OPERATIONS:
        raise ValueError(
            f"Unknown calculator operation {operation!r}; "
            f"expected one of {sorted(_OPERATIONS)}"
        )

    a = np.asarray(first.data)
    b = np.asarray(second.data)
    if float_output:
        a = a.astype(np.float32, copy=False)
        b = b.astype(np.float32, copy=False)
    data = calculate(a, b, operation)

    first_name = getattr(first, "name", "A")
    second_name = getattr(second, "name", "B")
    return ArrayProcessingResult(
        name=name or f"{first_name} {operation} {second_name}",
        data=data,
        axis_labels=list(axis_labels_for_result(first)),
        display_levels=finite_range(data),
        axis_scales=list(axis_scales_for_result(first)),
        scale_unit=getattr(first, "scale_unit", "px"),
        metadata={
            "operation": ImageCalculatorProcessor.id,
            "calculator_operation": operation,
            "source_results": [first_name, second_name],
        },
    )


__all__ = [
    "CALCULATOR_OPERATIONS",
    "ImageCalculatorProcessor",
    "calculate",
    "calculate_results",
]
