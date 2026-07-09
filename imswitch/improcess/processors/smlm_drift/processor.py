"""Estimate and subtract lateral drift from a localization table.

Segment cross-correlation: the acquisition is split into temporal segments,
each segment is rendered as a super-resolved histogram on a shared grid, and
every segment is cross-correlated against the first to recover its lateral
shift. Per-frame drift is interpolated between segment centers and
subtracted from the positions. CPU-only, pure numpy; a GPU/RCC upgrade can
swap in behind the same params.
"""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_tables import apply_drift, estimate_drift
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .result import DriftCorrectedLocalizationResult


class SmlmDriftProcessor(Processor):
    """Segment cross-correlation drift correction for localization tables."""

    name = "SMLM drift correction"
    id = "smlm-drift"
    category = "Localization"
    kinds = ("localization",)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        segments_spin = QtWidgets.QSpinBox()
        segments_spin.setRange(2, 10000)
        segments_spin.setValue(10)
        layout.addRow("Temporal segments:", segments_spin)

        pixel_spin = QtWidgets.QDoubleSpinBox()
        pixel_spin.setRange(1.0, 1000.0)
        pixel_spin.setValue(30.0)
        pixel_spin.setSuffix(" nm/px")
        layout.addRow("Render pixel:", pixel_spin)

        def get_values():
            return {
                "segments": int(segments_spin.value()),
                "render_pixel_size_nm": float(pixel_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("SMLM drift correction requires a LocalizationResult input")

        estimate = estimate_drift(
            result.locs,
            segments=int(params.get("segments", 10)),
            render_pixel_size_nm=float(params.get("render_pixel_size_nm", 30.0)),
        )
        corrected = apply_drift(result.locs, estimate)
        return DriftCorrectedLocalizationResult(
            name=f"{result.name} (drift-corrected)",
            locs=corrected,
            drift_frames=estimate.frames,
            drift_x_nm=estimate.drift_x_nm,
            drift_y_nm=estimate.drift_y_nm,
            pixel_size_nm=result.pixel_size_nm,
            z_step_nm=result.z_step_nm,
            dims=result.dims,
            source_name=result.source_name,
            source_shape=result.source_shape,
            metadata={
                **result.metadata,
                "drift_params": dict(params),
                "drift_segments_used": int(len(estimate.segment_centers)),
                "drift_max_nm": float(
                    max(
                        abs(estimate.drift_x_nm).max(),
                        abs(estimate.drift_y_nm).max(),
                    )
                ),
            },
        )


__all__ = ["SmlmDriftProcessor"]
