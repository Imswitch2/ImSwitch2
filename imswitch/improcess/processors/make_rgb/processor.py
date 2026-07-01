"""Create an explicit RGB visualization result."""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    resolve_axis,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

from .result import RGBResult


_CHANNEL_LABELS = ("C", "Channel", "Channels", "Base")


class MakeRGBProcessor(Processor):
    """Bake a channel-like axis into a channel-last uint8 RGB image."""

    name = "Make RGB"
    id = "make-rgb"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return self._has_channel_axis

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", *_CHANNEL_LABELS])
        axis_combo.setToolTip("Channel axis to convert to RGB.")
        layout.addRow("Axis:", axis_combo)

        def get_values():
            return {"axis": axis_combo.currentText()}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        axis = resolve_axis(
            result,
            params.get("axis", "Auto"),
            preferred_labels=_CHANNEL_LABELS,
            require_label_match=True,
        )
        labels = axis_labels_for_result(result)
        scales = axis_scales_for_result(result)
        rgb = make_rgb_array(result.data, axis=axis)
        output_labels = [
            label for index, label in enumerate(labels) if index != axis
        ] + ["RGB"]
        output_scales = [
            scale for index, scale in enumerate(scales) if index != axis
        ] + [1.0]
        return RGBResult(
            name=f"{result.name} (RGB)",
            data=rgb,
            axis_labels=output_labels,
            source_result=result.name,
            source_channel_axis=labels[axis],
            axis_scales=output_scales,
            scale_unit=getattr(result, "scale_unit", "px"),
            params=dict(params),
        )

    @staticmethod
    def _has_channel_axis(result: ProcessingResult) -> bool:
        shape = shape_for_result(result)
        if len(shape) < 3:
            return False
        labels = axis_labels_for_result(result)
        lowered = {label.lower(): index for index, label in enumerate(labels)}
        for label in _CHANNEL_LABELS:
            index = lowered.get(label.lower())
            if index is not None and shape[index] > 1:
                return True
        return False


def make_rgb_array(data, *, axis: int) -> np.ndarray:
    """Scale up to three channel planes into a channel-last uint8 RGB array."""
    array = np.asarray(data)
    moved = np.moveaxis(array, axis, -1)
    rgb = np.zeros((*moved.shape[:-1], 3), dtype=np.uint8)
    channel_count = min(int(moved.shape[-1]), 3)
    for channel in range(channel_count):
        rgb[..., channel] = _scale_channel_to_uint8(moved[..., channel])
    return rgb


def _scale_channel_to_uint8(channel: np.ndarray) -> np.ndarray:
    finite = channel[np.isfinite(channel)]
    if finite.size == 0:
        return np.zeros(channel.shape, dtype=np.uint8)
    minimum = float(np.nanmin(finite))
    maximum = float(np.nanmax(finite))
    if maximum <= minimum:
        return np.zeros(channel.shape, dtype=np.uint8)
    scaled = (np.asarray(channel, dtype=np.float32) - minimum) / (maximum - minimum)
    return np.clip(scaled * 255.0, 0.0, 255.0).astype(np.uint8)


__all__ = ["MakeRGBProcessor", "make_rgb_array"]
