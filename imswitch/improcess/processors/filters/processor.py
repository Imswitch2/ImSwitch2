"""Classic spatial filters (ImageJ's Process > Filters).

Gaussian blur, median, mean and unsharp mask. Filtering acts on the spatial
Y/X plane only: for stacks the filter footprint is 1 along every leading axis
(Z/T/C), so each plane is filtered independently without an explicit loop.
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

FILTER_METHODS = ("gaussian", "median", "mean", "unsharp")


class FilterProcessor(Processor):
    """Apply a classic spatial filter to the image's Y/X plane."""

    name = "Filter"
    id = "filter"
    category = "Filters"
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
        return {'method': 'gaussian', 'radius': 2.0, 'amount': 0.6}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        method_combo = QtWidgets.QComboBox()
        method_combo.addItems(list(FILTER_METHODS))
        layout.addRow("Filter:", method_combo)

        radius_spin = QtWidgets.QDoubleSpinBox()
        radius_spin.setRange(0.1, 1000.0)
        radius_spin.setDecimals(2)
        radius_spin.setValue(2.0)
        radius_spin.setToolTip(
            "Gaussian/unsharp sigma in pixels; median/mean kernel radius"
        )
        layout.addRow("Radius/sigma:", radius_spin)

        amount_spin = QtWidgets.QDoubleSpinBox()
        amount_spin.setRange(0.05, 10.0)
        amount_spin.setDecimals(2)
        amount_spin.setValue(0.6)
        amount_spin.setToolTip("Unsharp mask weight (like ImageJ's 0.1-0.9)")
        layout.addRow("Unsharp amount:", amount_spin)

        def sync_amount_enabled():
            amount_spin.setEnabled(method_combo.currentText() == "unsharp")

        method_combo.currentIndexChanged.connect(lambda _index: sync_amount_enabled())
        sync_amount_enabled()

        def get_values():
            return {
                "method": method_combo.currentText(),
                "radius": float(radius_spin.value()),
                "amount": float(amount_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        method = str(params.get("method", "gaussian"))
        radius = float(params.get("radius", 2.0))
        amount = float(params.get("amount", 0.6))
        labels = axis_labels_for_result(result)
        data = apply_filter(
            np.asarray(result.data),
            method,
            radius=radius,
            amount=amount,
            spatial_axes=_spatial_axes(labels),
        )
        return ArrayProcessingResult(
            name=f"{result.name} ({method} {radius:g})",
            data=data,
            axis_labels=list(labels),
            display_levels=finite_range(data),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={
                "operation": self.id,
                "filter_method": method,
                "radius": radius,
                "amount": amount if method == "unsharp" else None,
            },
        )


def _spatial_axes(labels) -> tuple[int, int]:
    """Return the (Y, X) axis indices; fall back to the last two axes."""
    labels = list(labels)
    try:
        return labels.index("Y"), labels.index("X")
    except ValueError:
        return len(labels) - 2, len(labels) - 1


def apply_filter(
    data: np.ndarray,
    method: str,
    *,
    radius: float = 2.0,
    amount: float = 0.6,
    spatial_axes: tuple[int, int] | None = None,
) -> np.ndarray:
    """Filter ``data`` in its spatial plane, leaving other axes untouched."""
    from scipy import ndimage

    data = np.asarray(data, dtype=np.float32)
    if data.ndim < 2:
        raise ValueError("Filtering needs at least a 2D image")
    if spatial_axes is None:
        spatial_axes = (data.ndim - 2, data.ndim - 1)
    y_axis, x_axis = spatial_axes

    if method in ("gaussian", "unsharp"):
        sigma = [0.0] * data.ndim
        sigma[y_axis] = radius
        sigma[x_axis] = radius
        blurred = ndimage.gaussian_filter(data, sigma=sigma)
        if method == "gaussian":
            return blurred
        # Unsharp mask: original + amount * (original - blurred), the same
        # weighting ImageJ applies before renormalizing.
        return (data - amount * blurred) / (1.0 - amount)
    if method in ("median", "mean"):
        size = [1] * data.ndim
        kernel = max(1, int(round(2 * radius + 1)))
        size[y_axis] = kernel
        size[x_axis] = kernel
        if method == "median":
            return ndimage.median_filter(data, size=size)
        return ndimage.uniform_filter(data, size=size)
    raise ValueError(
        f"Unknown filter method {method!r}; expected one of {list(FILTER_METHODS)}"
    )


__all__ = ["FILTER_METHODS", "FilterProcessor", "apply_filter"]
