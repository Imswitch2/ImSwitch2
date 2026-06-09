"""Processor that extracts three-color strip registration from a bead volume."""

from pathlib import Path
from typing import Callable

from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.analysis.multicolor import (
    apply_alignment,
    extract_alignment,
    extract_calibration_volume,
    output_axis_scales,
    parse_x_bounds,
    save_alignment,
)
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import MulticolorRegistrationResult


class MulticolorRegistrationProcessor(Processor):
    """Extract a reusable multicolor alignment from a deskewed bead sample."""

    name = "Multicolor Registration"
    id = "multicolor-registration"

    def __init__(self):
        self._logger = initLogger(self, tryInheritParent=False)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: result.axis_labels in (["Z", "Y", "X"], ["T", "Z", "Y", "X"])

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        x_bounds_edit = QtWidgets.QLineEdit()
        x_bounds_edit.setPlaceholderText("blank = equal thirds, or x0,x1,x2,x3")
        layout.addRow("X bounds:", x_bounds_edit)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(["maxproj", "volume", "descriptor_3d"])
        layout.addRow("Mode:", mode_combo)

        reference_spin = QtWidgets.QSpinBox()
        reference_spin.setRange(0, 2)
        reference_spin.setValue(0)
        layout.addRow("Reference channel:", reference_spin)

        time_spin = QtWidgets.QSpinBox()
        time_spin.setRange(0, 999999)
        time_spin.setValue(0)
        layout.addRow("Timepoint:", time_spin)

        save_path_edit = QtWidgets.QLineEdit()
        save_path_edit.setPlaceholderText("optional .h5 alignment path")
        layout.addRow("Save alignment:", save_path_edit)

        bead_sigma_spin = QtWidgets.QDoubleSpinBox()
        bead_sigma_spin.setRange(0.01, 100.0)
        bead_sigma_spin.setDecimals(3)
        bead_sigma_spin.setValue(1.5)
        layout.addRow("Bead sigma:", bead_sigma_spin)

        bead_min_dist_spin = QtWidgets.QSpinBox()
        bead_min_dist_spin.setRange(1, 9999)
        bead_min_dist_spin.setValue(6)
        layout.addRow("Bead min dist:", bead_min_dist_spin)

        bead_threshold_spin = QtWidgets.QDoubleSpinBox()
        bead_threshold_spin.setRange(0.0, 1.0)
        bead_threshold_spin.setDecimals(3)
        bead_threshold_spin.setSingleStep(0.05)
        bead_threshold_spin.setValue(0.5)
        layout.addRow("Bead threshold:", bead_threshold_spin)

        match_dist_spin = QtWidgets.QDoubleSpinBox()
        match_dist_spin.setRange(0.1, 10000.0)
        match_dist_spin.setDecimals(2)
        match_dist_spin.setValue(25.0)
        layout.addRow("Match max dist:", match_dist_spin)

        ransac_iter_spin = QtWidgets.QSpinBox()
        ransac_iter_spin.setRange(1, 1000000)
        ransac_iter_spin.setValue(2000)
        layout.addRow("RANSAC iter:", ransac_iter_spin)

        ransac_inlier_spin = QtWidgets.QDoubleSpinBox()
        ransac_inlier_spin.setRange(0.01, 1000.0)
        ransac_inlier_spin.setDecimals(2)
        ransac_inlier_spin.setValue(3.0)
        layout.addRow("RANSAC inlier px:", ransac_inlier_spin)

        def get_values():
            return {
                "x_bounds": x_bounds_edit.text(),
                "mode": mode_combo.currentText(),
                "reference_channel": reference_spin.value(),
                "time_index": time_spin.value(),
                "save_path": save_path_edit.text().strip(),
                "bead_sigma": bead_sigma_spin.value(),
                "bead_min_dist": bead_min_dist_spin.value(),
                "bead_thr_rel": bead_threshold_spin.value(),
                "match_max_dist": match_dist_spin.value(),
                "ransac_n_iter": ransac_iter_spin.value(),
                "ransac_inlier_px": ransac_inlier_spin.value(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        volume = extract_calibration_volume(
            result.data,
            result.axis_labels,
            time_index=int(params.get("time_index", 0)),
        )
        x_bounds = parse_x_bounds(params.get("x_bounds"), volume.shape[-1])
        alignment = extract_alignment(
            volume,
            x_bounds=x_bounds,
            mode=str(params.get("mode", "maxproj")),
            reference_channel=int(params.get("reference_channel", 0)),
            bead_sigma=float(params.get("bead_sigma", 1.5)),
            bead_min_dist=int(params.get("bead_min_dist", 6)),
            bead_thr_rel=float(params.get("bead_thr_rel", 0.5)),
            match_max_dist=float(params.get("match_max_dist", 25.0)),
            ransac_n_iter=int(params.get("ransac_n_iter", 2000)),
            ransac_inlier_px=float(params.get("ransac_inlier_px", 3.0)),
        )
        preview = apply_alignment(volume, alignment)
        save_path = str(params.get("save_path", "")).strip()
        if save_path:
            save_alignment(alignment, Path(save_path))
            self._logger.info("Saved multicolor alignment: %s", save_path)

        axis_scales = output_axis_scales(["Z", "Y", "X"], result.axis_scales[-3:])
        return MulticolorRegistrationResult(
            name=f"{result.name} (multicolor registration)",
            data=preview,
            alignment=alignment,
            params=dict(params),
            axis_scales=axis_scales,
            scale_unit=result.scale_unit,
        )
