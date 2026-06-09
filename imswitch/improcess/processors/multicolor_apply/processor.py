"""Processor that applies a saved three-color strip registration."""

from pathlib import Path
from typing import Callable

from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.multicolor import (
    apply_alignment_to_result_data,
    load_alignment,
    output_axis_labels,
    output_axis_scales,
)
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import MulticolorApplyResult


class MulticolorApplyProcessor(Processor):
    """Apply a saved multicolor alignment to a deskewed sample volume."""

    name = "Multicolor Apply"
    id = "multicolor-apply"

    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.axis_labels in (["Z", "Y", "X"], ["T", "Z", "Y", "X"])

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        alignment_path_edit = QtWidgets.QLineEdit()
        alignment_path_edit.setPlaceholderText("alignment .h5 from Multicolor Registration")
        layout.addRow("Alignment file:", alignment_path_edit)

        def get_values():
            return {"alignment_path": alignment_path_edit.text().strip()}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        alignment_path = str(params.get("alignment_path", "")).strip()
        if not alignment_path:
            raise ValueError("multicolor-apply requires an alignment_path")
        alignment = load_alignment(Path(alignment_path))
        aligned = apply_alignment_to_result_data(result.data, result.axis_labels, alignment)
        labels = output_axis_labels(result.axis_labels)
        scales = output_axis_scales(result.axis_labels, result.axis_scales)
        self._logger.info("Applied multicolor alignment: %s", alignment_path)
        return MulticolorApplyResult(
            name=f"{result.name} (multicolor aligned)",
            data=aligned,
            axis_labels=labels,
            alignment=alignment,
            params=dict(params),
            axis_scales=scales,
            scale_unit=result.scale_unit,
        )
