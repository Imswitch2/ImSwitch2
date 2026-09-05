"""Split a stack-like axis into individual image results."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import resolve_axis, split_port_keys, split_result
from imswitch.improcess.processors.base import Processor, ProcessorOutput


class StackSplitProcessor(Processor):
    """Split one stack axis into separate results."""

    name = "Split stack"
    id = "stack-split"
    category = "Dimensions and channels"
    kinds = ("image", "composite")

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: getattr(result.data, "ndim", 0) > 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "Z", "T", "C", "Base", "Dataset", "projection"])
        axis_combo.setToolTip("Axis to split. Auto prefers Z, then T, then any stack axis.")
        layout.addRow("Axis:", axis_combo)

        def get_values():
            return {"axis": axis_combo.currentText()}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        axis = resolve_axis(
            result,
            params.get("axis", "Auto"),
            preferred_labels=("Z", "T", "C", "Base", "Dataset", "projection"),
        )
        outputs = split_result(result, axis, operation=self.id)
        return ProcessorOutput(outputs, keys=split_port_keys(outputs))
