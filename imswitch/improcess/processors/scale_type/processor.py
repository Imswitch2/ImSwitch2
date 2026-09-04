"""Scale/resize and bit-depth conversion (ImageJ's Image > Scale / Type).

Resize interpolates the Y/X plane by a zoom factor (leading stack axes are
untouched) and updates the physical pixel scales to match. Convert type
changes the stored dtype, optionally rescaling the finite data range onto the
integer range like ImageJ's scaled conversions.
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

_INTERPOLATIONS = {"nearest": 0, "bilinear": 1, "cubic": 3}
RESIZE_INTERPOLATIONS = tuple(_INTERPOLATIONS)

_TARGET_DTYPES = {
    "8-bit": np.uint8,
    "16-bit": np.uint16,
    "32-bit float": np.float32,
}
CONVERT_TYPES = tuple(_TARGET_DTYPES)


class ResizeProcessor(Processor):
    """Resize the Y/X plane by a zoom factor."""

    name = "Scale/Resize"
    id = "resize"
    category = "Transform"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        factor_spin = QtWidgets.QDoubleSpinBox()
        factor_spin.setRange(0.01, 100.0)
        factor_spin.setDecimals(3)
        factor_spin.setValue(0.5)
        factor_spin.setToolTip("Zoom factor for X and Y (0.5 halves, 2 doubles)")
        layout.addRow("Factor:", factor_spin)

        interpolation_combo = QtWidgets.QComboBox()
        interpolation_combo.addItems(list(RESIZE_INTERPOLATIONS))
        interpolation_combo.setCurrentText("bilinear")
        layout.addRow("Interpolation:", interpolation_combo)

        def get_values():
            return {
                "factor": float(factor_spin.value()),
                "interpolation": interpolation_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        factor = float(params.get("factor", 0.5))
        interpolation = str(params.get("interpolation", "bilinear"))
        labels = list(axis_labels_for_result(result))
        scales = list(axis_scales_for_result(result))
        y_axis, x_axis = _spatial_axes(labels)
        data = resize_image(
            np.asarray(result.data),
            factor,
            interpolation=interpolation,
            spatial_axes=(y_axis, x_axis),
        )
        # Fewer, larger pixels: the physical pitch grows as the pixel count
        # shrinks (and vice versa), keeping the imaged extent constant.
        scales[y_axis] = scales[y_axis] / factor
        scales[x_axis] = scales[x_axis] / factor
        return ArrayProcessingResult(
            name=f"{result.name} (x{factor:g})",
            data=data,
            axis_labels=labels,
            display_levels=finite_range(data),
            axis_scales=scales,
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={
                "operation": self.id,
                "factor": factor,
                "interpolation": interpolation,
            },
        )


class ConvertTypeProcessor(Processor):
    """Convert the stored dtype, optionally rescaling into the target range."""

    name = "Convert type"
    id = "convert-type"
    category = "Transform"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        type_combo = QtWidgets.QComboBox()
        type_combo.addItems(list(CONVERT_TYPES))
        layout.addRow("Type:", type_combo)

        rescale_check = QtWidgets.QCheckBox("Scale data range to type range")
        rescale_check.setChecked(True)
        rescale_check.setToolTip(
            "Map the finite data range onto the full integer range "
            "(like ImageJ's scaled conversions)"
        )
        layout.addRow("", rescale_check)

        def get_values():
            return {
                "type": type_combo.currentText(),
                "rescale": rescale_check.isChecked(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        target = str(params.get("type", "32-bit float"))
        rescale = bool(params.get("rescale", True))
        data = convert_type(np.asarray(result.data), target, rescale=rescale)
        return ArrayProcessingResult(
            name=f"{result.name} ({target})",
            data=data,
            axis_labels=list(axis_labels_for_result(result)),
            display_levels=finite_range(data),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={"operation": self.id, "type": target, "rescale": rescale},
        )


def _spatial_axes(labels) -> tuple[int, int]:
    """Return the (Y, X) axis indices; fall back to the last two axes."""
    labels = list(labels)
    try:
        return labels.index("Y"), labels.index("X")
    except ValueError:
        return len(labels) - 2, len(labels) - 1


def resize_image(
    data: np.ndarray,
    factor: float,
    *,
    interpolation: str = "bilinear",
    spatial_axes: tuple[int, int] | None = None,
) -> np.ndarray:
    """Zoom the spatial plane of ``data`` by ``factor``."""
    from scipy import ndimage

    if factor <= 0:
        raise ValueError("Resize factor must be positive")
    try:
        order = _INTERPOLATIONS[interpolation]
    except KeyError:
        raise ValueError(
            f"Unknown interpolation {interpolation!r}; "
            f"expected one of {list(RESIZE_INTERPOLATIONS)}"
        ) from None
    data = np.asarray(data)
    if data.ndim < 2:
        raise ValueError("Resizing needs at least a 2D image")
    if spatial_axes is None:
        spatial_axes = (data.ndim - 2, data.ndim - 1)
    zoom = [1.0] * data.ndim
    zoom[spatial_axes[0]] = factor
    zoom[spatial_axes[1]] = factor
    return ndimage.zoom(data, zoom, order=order)


def convert_type(data: np.ndarray, target: str, *, rescale: bool = True) -> np.ndarray:
    """Convert ``data`` to the named target dtype."""
    try:
        dtype = _TARGET_DTYPES[target]
    except KeyError:
        raise ValueError(
            f"Unknown target type {target!r}; expected one of {list(CONVERT_TYPES)}"
        ) from None
    data = np.asarray(data)
    if dtype == np.float32:
        return data.astype(np.float32)
    info = np.iinfo(dtype)
    if rescale:
        low, high = finite_range(data)
        span = high - low
        if span == 0:
            return np.zeros(data.shape, dtype=dtype)
        normalized = (data.astype(np.float64) - low) / span
        return np.clip(normalized * info.max, info.min, info.max).astype(dtype)
    return np.clip(data, info.min, info.max).astype(dtype)


__all__ = [
    "CONVERT_TYPES",
    "ConvertTypeProcessor",
    "RESIZE_INTERPOLATIONS",
    "ResizeProcessor",
    "convert_type",
    "resize_image",
]
