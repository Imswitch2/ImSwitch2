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
    category = "Visualization"
    kinds = ("image", "composite")

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
        channels = params.get("channels")
        rgb, used_channels = make_rgb_array(
            result.data,
            axis=axis,
            channels=channels,
            channel_levels=params.get("channel_levels"),
        )
        output_labels = [
            label for index, label in enumerate(labels) if index != axis
        ] + ["RGB"]
        output_scales = [
            scale for index, scale in enumerate(scales) if index != axis
        ] + [1.0]
        output_params = dict(params)
        output_params["channels"] = list(used_channels)
        return RGBResult(
            name=f"{result.name} (RGB)",
            data=rgb,
            axis_labels=output_labels,
            source_result=result.name,
            source_channel_axis=labels[axis],
            axis_scales=output_scales,
            scale_unit=getattr(result, "scale_unit", "px"),
            params=output_params,
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


def make_rgb_array(
    data,
    *,
    axis: int,
    channels: list[int] | None = None,
    channel_levels: list[tuple[float, float] | None] | None = None,
) -> tuple[np.ndarray, list[int]]:
    """Scale up to three selected channel planes into a channel-last uint8 RGB array.

    Returns the RGB array and the resolved list of source-axis channel
    indices that were used (R, G, B order).
    """
    array = np.asarray(data)
    moved = np.moveaxis(array, axis, -1)
    available = int(moved.shape[-1])

    if channels is None:
        if available > 3:
            raise ValueError(
                f"Channel axis has {available} channels; pass channels=[r, g, b] "
                "to select exactly which 3 to use for RGB."
            )
        channels = list(range(available))
    else:
        channels = [int(index) for index in channels]
        if not 1 <= len(channels) <= 3:
            raise ValueError("RGB channel selection must have between 1 and 3 channels")
        for index in channels:
            if index < 0 or index >= available:
                raise ValueError(
                    f"RGB channel index {index} outside channel axis range {available}"
                )

    rgb = np.zeros((*moved.shape[:-1], 3), dtype=np.uint8)
    for output_index, channel_index in enumerate(channels):
        levels = channel_levels[output_index] if channel_levels else None
        rgb[..., output_index] = _scale_channel_to_uint8(
            moved[..., channel_index], levels=levels
        )
    return rgb, channels


def _scale_channel_to_uint8(
    channel: np.ndarray, *, levels: tuple[float, float] | None = None
) -> np.ndarray:
    if levels is not None:
        minimum, maximum = float(levels[0]), float(levels[1])
    else:
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
