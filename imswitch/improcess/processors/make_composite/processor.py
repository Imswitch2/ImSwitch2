"""Create a composite display result from a channel-like axis."""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    resolve_axis,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

from .result import CompositeResult


_CHANNEL_LABELS = ("C", "Channel", "Channels", "Base")
_DEFAULT_COLORMAPS = ("red", "green", "blue", "magenta", "cyan", "yellow")


class MakeCompositeProcessor(Processor):
    """Represent channels as independently-scaled colored display layers."""

    name = "Make composite"
    id = "make-composite"
    category = "Visualization"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True

    @classmethod
    def default_params(cls) -> dict:
        return {'axis': 'Auto'}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return self._has_channel_axis

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", *_CHANNEL_LABELS])
        axis_combo.setToolTip("Channel axis to render as composite layers.")
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
        return CompositeResult(
            name=f"{result.name} (composite)",
            data=result.data,
            axis_labels=labels,
            channel_axis=axis,
            channel_colormaps=list(params.get("colormaps", _DEFAULT_COLORMAPS)),
            axis_scales=axis_scales_for_result(result),
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


__all__ = ["MakeCompositeProcessor"]
