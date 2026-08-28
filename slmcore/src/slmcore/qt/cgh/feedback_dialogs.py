"""Reusable inspection dialog for transient CGH feedback state."""

from __future__ import annotations



import numpy as np
from qtpy import QtWidgets

from ...core.cgh.feedback.model import FeedbackInspection


class FeedbackInspectionDialog(QtWidgets.QDialog):
    """Initial inspection surface backed by complete immutable feedback records."""

    def __init__(
        self,
        inspection: FeedbackInspection,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Feedback inspection")
        self.resize(700,520)
        layout = QtWidgets.QVBoxLayout(self)

        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(_inspection_text(inspection))
        layout.addWidget(text,1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @classmethod
    def show_inspection(
        cls,
        inspection: FeedbackInspection,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        dialog = cls(inspection,parent=parent)
        if hasattr(dialog,"exec_"):
            dialog.exec_()
        else:
            dialog.exec()


def _inspection_text(inspection: FeedbackInspection) -> str:
    lines = []
    measurement = inspection.measurement
    if measurement is None:
        lines.append("Current measurement: none")
    else:
        acquisition = measurement.acquisition
        lines.extend([
            "Current measurement",
            f"  acquired: {acquisition.created_at}",
            f"  source: {acquisition.source}",
            f"  image shape: {tuple(acquisition.image.shape)}",
            f"  image range: {_array_range(acquisition.image)}",
        ])
        localization = measurement.localization
        if localization is None:
            lines.append("  localization: none")
        else:
            errors = localization.expected_positions_px - localization.measured_positions_px
            norms = np.sqrt(np.sum(errors * errors,axis=0))
            lines.extend([
                "  localization:",
                f"    target: {localization.target_type}",
                f"    spots: {localization.measured_positions_px.shape[1]}",
                f"    crop: {localization.crop_coord}",
                f"    period px: ({localization.period_x_px:.6g}, "
                f"{localization.period_y_px:.6g})",
                f"    offset px: ({localization.offset_x_px:.6g}, "
                f"{localization.offset_y_px:.6g})",
                f"    reused previous: {localization.reused_previous}",
                f"    mean local displacement px: "
                f"{float(np.mean(norms)) if norms.size else 0.0:.6g}",
                "    parameters:",
            ])
            lines.extend(
                f"      {key}: {value}"
                for key,value in localization.parameters.items()
            )

    lines.append("")
    lines.append(f"Intensity feedback rounds: {len(inspection.intensity_rounds)}")
    for record in inspection.intensity_rounds:
        analysis = record.analysis
        lines.extend([
            f"  Round {record.index} ({record.created_at})",
            f"    uniformity: {analysis.uniformity:.6g}",
            f"    normalized std: {analysis.normalized_std:.6g}",
            f"    efficiency: {analysis.efficiency:.6g}",
            f"    measured power range: {_array_range(analysis.spot_powers)}",
        ])

    lines.append("")
    correction = inspection.position_correction
    if correction is None:
        lines.append("Position correction: none")
    else:
        displacement = correction.displacement_kxy
        magnitude = np.sqrt(np.sum(displacement * displacement,axis=0))
        lines.extend([
            "Position correction",
            f"  created: {correction.created_at}",
            f"  active: {inspection.position_active}",
            f"  spots: {displacement.shape[1]}",
            f"  mean |correction| kxy: "
            f"{float(np.mean(magnitude)) if magnitude.size else 0.0:.6g}",
            f"  max |correction| kxy: "
            f"{float(np.max(magnitude)) if magnitude.size else 0.0:.6g}",
        ])
    return "\n".join(lines)


def _array_range(array: np.ndarray) -> str:
    values = np.asarray(array)
    if values.size == 0:
        return "empty"
    return f"{float(np.min(values)):.6g} .. {float(np.max(values)):.6g}"
