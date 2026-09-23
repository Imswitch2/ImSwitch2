"""Spatial transforms (ImageJ's Image > Transform).

Rotate 90 degrees and flips, acting on the Y/X plane wherever it sits in the
axis order, so stacks transform every plane consistently. Rotation swaps the
Y and X extents and therefore also swaps their pixel scales.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

TRANSFORM_OPERATIONS = (
    "rotate-90-cw",
    "rotate-90-ccw",
    "flip-horizontal",
    "flip-vertical",
)


class TransformProcessor(Processor):
    """Rotate or flip the image in its Y/X plane."""

    name = "Transform"
    id = "transform"
    category = "Transform"

    @classmethod
    def default_params(cls) -> dict:
        return {'operation': 'rotate-90-cw'}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        operation_combo = QtWidgets.QComboBox()
        operation_combo.addItems(list(TRANSFORM_OPERATIONS))
        layout.addRow("Transform:", operation_combo)

        def get_values():
            return {"operation": operation_combo.currentText()}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        operation = str(params.get("operation", "rotate-90-cw"))
        labels = list(axis_labels_for_result(result))
        scales = list(axis_scales_for_result(result))
        y_axis, x_axis = _spatial_axes(labels)
        data = apply_transform(
            np.asarray(result.data), operation, spatial_axes=(y_axis, x_axis)
        )
        if operation.startswith("rotate"):
            # The Y extent becomes the X extent and vice versa; the per-axis
            # pixel pitches travel with them.
            scales[y_axis], scales[x_axis] = scales[x_axis], scales[y_axis]
        return ArrayProcessingResult(
            name=f"{result.name} ({operation})",
            data=data,
            axis_labels=labels,
            display_levels=getattr(result, "display_levels", None),
            axis_scales=scales,
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={"operation": self.id, "transform": operation},
        )


def _spatial_axes(labels) -> tuple[int, int]:
    """Return the (Y, X) axis indices; fall back to the last two axes."""
    labels = list(labels)
    try:
        return labels.index("Y"), labels.index("X")
    except ValueError:
        return len(labels) - 2, len(labels) - 1


def apply_transform(
    data: np.ndarray,
    operation: str,
    *,
    spatial_axes: tuple[int, int] | None = None,
) -> np.ndarray:
    """Rotate/flip ``data`` in its spatial plane, other axes untouched."""
    data = np.asarray(data)
    if data.ndim < 2:
        raise ValueError("Transforming needs at least a 2D image")
    if spatial_axes is None:
        spatial_axes = (data.ndim - 2, data.ndim - 1)
    y_axis, x_axis = spatial_axes

    if operation == "rotate-90-cw":
        # np.rot90 with axes=(y, x) rotates counter-clockwise in that plane;
        # k=-1 gives the clockwise rotation users expect from ImageJ.
        return np.rot90(data, k=-1, axes=(y_axis, x_axis))
    if operation == "rotate-90-ccw":
        return np.rot90(data, k=1, axes=(y_axis, x_axis))
    if operation == "flip-horizontal":
        return np.flip(data, axis=x_axis)
    if operation == "flip-vertical":
        return np.flip(data, axis=y_axis)
    raise ValueError(
        f"Unknown transform {operation!r}; expected one of {list(TRANSFORM_OPERATIONS)}"
    )


__all__ = ["TRANSFORM_OPERATIONS", "TransformProcessor", "apply_transform"]
