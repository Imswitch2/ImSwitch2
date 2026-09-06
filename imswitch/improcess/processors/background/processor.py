"""Background subtraction (ImageJ's Process > Subtract Background).

Rolling-ball background removal per Y/X plane; stacks are processed plane by
plane over the leading axes. The estimated background can be returned as a
second result for inspection.
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
from imswitch.improcess.processors.base import Processor, ProcessorOutput


class SubtractBackgroundProcessor(Processor):
    """Remove smooth background with a rolling ball."""

    name = "Subtract background"
    id = "subtract-background"
    category = "Restoration"
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
        return {'radius': 50.0, 'output_background': False}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        radius_spin = QtWidgets.QDoubleSpinBox()
        radius_spin.setRange(1.0, 10000.0)
        radius_spin.setDecimals(1)
        radius_spin.setValue(50.0)
        radius_spin.setToolTip(
            "Rolling-ball radius in pixels; should exceed the largest "
            "foreground feature"
        )
        layout.addRow("Ball radius:", radius_spin)

        background_check = QtWidgets.QCheckBox("Also output the background")
        background_check.setChecked(False)
        layout.addRow("", background_check)

        def get_values():
            return {
                "radius": float(radius_spin.value()),
                "output_background": background_check.isChecked(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict):
        radius = float(params.get("radius", 50.0))
        labels = axis_labels_for_result(result)
        subtracted, background = subtract_background(
            np.asarray(result.data), radius, spatial_axes=_spatial_axes(labels)
        )

        def _result(name, data):
            return ArrayProcessingResult(
                name=name,
                data=data,
                axis_labels=list(labels),
                display_levels=finite_range(data),
                axis_scales=list(axis_scales_for_result(result)),
                scale_unit=getattr(result, "scale_unit", "px"),
                metadata={"operation": self.id, "radius": radius},
            )

        output = _result(f"{result.name} (bg-subtracted r={radius:g})", subtracted)
        if not params.get("output_background", False):
            return output
        return ProcessorOutput(
            [output, _result(f"{result.name} (background r={radius:g})", background)],
            keys=("signal", "background"),
        )


def _spatial_axes(labels) -> tuple[int, int]:
    """Return the (Y, X) axis indices; fall back to the last two axes."""
    labels = list(labels)
    try:
        return labels.index("Y"), labels.index("X")
    except ValueError:
        return len(labels) - 2, len(labels) - 1


def subtract_background(
    data: np.ndarray,
    radius: float = 50.0,
    *,
    spatial_axes: tuple[int, int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(subtracted, background)`` for a rolling-ball estimate."""
    from skimage.restoration import rolling_ball

    if radius <= 0:
        raise ValueError("Rolling-ball radius must be positive")
    data = np.asarray(data, dtype=np.float32)
    if data.ndim < 2:
        raise ValueError("Background subtraction needs at least a 2D image")
    if spatial_axes is None:
        spatial_axes = (data.ndim - 2, data.ndim - 1)
    y_axis, x_axis = spatial_axes

    # Bring the spatial plane to the back and roll the ball per leading index.
    moved = np.moveaxis(data, (y_axis, x_axis), (-2, -1))
    flat = moved.reshape(-1, moved.shape[-2], moved.shape[-1])
    background_flat = np.empty_like(flat)
    for index, plane in enumerate(flat):
        background_flat[index] = rolling_ball(plane, radius=radius)
    background = np.moveaxis(
        background_flat.reshape(moved.shape), (-2, -1), (y_axis, x_axis)
    )
    return data - background, background


__all__ = ["SubtractBackgroundProcessor", "subtract_background"]
