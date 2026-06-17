"""Colocalization processor."""

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.colocalization import colocalization_batch
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._extraction import extract_2d_plane, resolve_axis
from imswitch.improcess.processors.base import Processor

from .result import ColocalizationResult


class ColocalizationProcessor(Processor):
    """Compute colocalization metrics between two planes of an image stack."""

    name = "Colocalization"
    id = "colocalization"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """Require a stack: the last two axes are the (Y, X) plane, so at least
        one further non-spatial axis is needed to provide two planes to
        compare. Hence ``ndim >= 3``."""
        return lambda result: result.data.ndim >= 3

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItems(["Auto", "C", "T", "Z", "D0", "D1", "D2"])
        layout.addRow("Compare axis:", axis_combo)

        index_a_spin = QtWidgets.QSpinBox()
        index_a_spin.setRange(0, 999999)
        index_a_spin.setValue(0)
        layout.addRow("Index A:", index_a_spin)

        index_b_spin = QtWidgets.QSpinBox()
        index_b_spin.setRange(0, 999999)
        index_b_spin.setValue(1)
        layout.addRow("Index B:", index_b_spin)

        threshold_a_spin = QtWidgets.QDoubleSpinBox()
        threshold_a_spin.setDecimals(6)
        threshold_a_spin.setRange(-1e12, 1e12)
        threshold_a_spin.setValue(0.0)
        layout.addRow("Threshold A:", threshold_a_spin)

        threshold_b_spin = QtWidgets.QDoubleSpinBox()
        threshold_b_spin.setDecimals(6)
        threshold_b_spin.setRange(-1e12, 1e12)
        threshold_b_spin.setValue(0.0)
        layout.addRow("Threshold B:", threshold_b_spin)

        def get_values():
            return {
                "compare_axis": axis_combo.currentText(),
                "index_a": index_a_spin.value(),
                "index_b": index_b_spin.value(),
                "threshold_a": threshold_a_spin.value(),
                "threshold_b": threshold_b_spin.value(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if np.asarray(result.data).ndim < 3:
            raise ValueError(
                f"Colocalization needs a stack with at least one compare axis, "
                f"got {np.asarray(result.data).shape}"
            )
        axis_label = resolve_axis(
            result,
            ("C", "T", "Z"),
            requested=str(params.get("compare_axis", "Auto")),
            error_message="Colocalization needs an axis with at least two planes",
        )
        image_a = extract_2d_plane(
            result, compare_axis=axis_label, index=int(params.get("index_a", 0))
        )
        image_b = extract_2d_plane(
            result, compare_axis=axis_label, index=int(params.get("index_b", 1))
        )
        analysis = colocalization_batch(
            image_a,
            image_b,
            threshold_a=float(params.get("threshold_a", 0.0)),
            threshold_b=float(params.get("threshold_b", 0.0)),
        )
        return ColocalizationResult(
            name=f"{result.name} (colocalization {axis_label})",
            analysis=analysis,
            params=dict(params),
        )
