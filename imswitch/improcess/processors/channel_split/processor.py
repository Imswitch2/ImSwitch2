"""Split a channel-like axis into individual image results."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    resolve_axis,
    shape_for_result,
    split_result,
)
from imswitch.improcess.processors.base import Processor, ProcessorOutput


_CHANNEL_LABELS = ("C", "Channel", "Channels", "Base")


class ChannelSplitProcessor(Processor):
    """Split C/Channel/Base axes into separate results."""

    name = "Split channels"
    id = "channel-split"
    category = "Dimensions and channels"
    kinds = ("image", "composite")

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return self._has_channel_axis

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", *_CHANNEL_LABELS])
        axis_combo.setToolTip("Channel axis to split. Auto uses C, Channel or Base.")
        layout.addRow("Axis:", axis_combo)

        def get_values():
            return {"axis": axis_combo.currentText()}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        axis = resolve_axis(
            result,
            params.get("axis", "Auto"),
            preferred_labels=_CHANNEL_LABELS,
            require_label_match=True,
        )
        return ProcessorOutput(split_result(result, axis, operation=self.id))

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
