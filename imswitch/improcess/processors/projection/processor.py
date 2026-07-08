"""Generic projection processor."""

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.projections import axis_index_from_label, project_array
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import ProjectionResult


class ProjectionProcessor(Processor):
    """Project an ImProcess result along one axis."""

    name = "Projection"
    id = "projection"
    category = "Dimensions and channels"
    kinds = ("image", "composite")

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.data.ndim >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "T", "Z", "C", "D0", "D1", "D2"])
        layout.addRow("Axis:", axis_combo)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["max", "mean", "sum", "median", "std"])
        layout.addRow("Mode:", mode_combo)

        def get_values():
            return {
                "axis": axis_combo.currentText(),
                "mode": mode_combo.currentText(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        labels = list(result.axis_labels)
        requested_axis = params.get("axis", "Auto")
        if requested_axis == "Auto":
            axis = self._default_axis(result)
        else:
            axis = axis_index_from_label(str(requested_axis), labels, result.data.ndim)
        analysis = project_array(
            result.data,
            axis=axis,
            mode=params.get("mode", "max"),
            axis_labels=labels,
            axis_scales=result.axis_scales,
        )
        name = f"{result.name} ({analysis.mode} {analysis.axis_label}-projection)"
        return ProjectionResult(
            name=name,
            analysis=analysis,
            scale_unit=result.scale_unit,
            params=dict(params),
        )

    @staticmethod
    def _default_axis(result: ProcessingResult) -> int:
        for label in ("Z", "T", "C"):
            if label in result.axis_labels and result.data.shape[result.axis_labels.index(label)] > 1:
                return result.axis_labels.index(label)
        if result.data.ndim > 2:
            return 0
        return result.data.ndim - 1
