"""Link localizations of the same emitter across consecutive frames."""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_tables import link_localizations
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor


class SmlmGroupProcessor(Processor):
    """Merge blinking repeats: photon-weighted position, summed photons."""

    name = "SMLM group/link"
    id = "smlm-group"
    category = "Localization"
    kinds = ("localization",)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        radius_spin = QtWidgets.QDoubleSpinBox()
        radius_spin.setRange(0.1, 100000.0)
        radius_spin.setValue(50.0)
        radius_spin.setSuffix(" nm")
        layout.addRow("Link radius:", radius_spin)

        dark_spin = QtWidgets.QSpinBox()
        dark_spin.setRange(0, 1000)
        dark_spin.setValue(0)
        layout.addRow("Max dark frames:", dark_spin)

        def get_values():
            return {
                "radius_nm": float(radius_spin.value()),
                "max_dark_frames": int(dark_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("SMLM group/link requires a LocalizationResult input")

        merged = link_localizations(
            result.locs,
            radius_nm=float(params.get("radius_nm", 50.0)),
            max_dark_frames=int(params.get("max_dark_frames", 0)),
        )
        return LocalizationResult(
            name=f"{result.name} (grouped {len(merged)}/{result.count})",
            locs=merged,
            pixel_size_nm=result.pixel_size_nm,
            z_step_nm=result.z_step_nm,
            dims=result.dims,
            source_name=result.source_name,
            source_shape=result.source_shape,
            metadata={
                **result.metadata,
                "group_params": dict(params),
                "group_input": int(result.count),
                "group_output": int(len(merged)),
            },
        )


__all__ = ["SmlmGroupProcessor"]
