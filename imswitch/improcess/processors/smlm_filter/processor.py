"""Filter a localization table by photons, lateral sigma and frame range."""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_tables import filter_localizations
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor


def _bound(value, minimum=None) -> float | None:
    """Spin-box convention: 0 (or below) means "no bound"."""
    number = float(value or 0.0)
    if number <= 0.0:
        return None
    if minimum is not None and number < minimum:
        return None
    return number


class SmlmFilterProcessor(Processor):
    """Keep only localizations inside photon/sigma/frame ranges.

    The results-table plotting tools (photon and sigma histograms on the
    selected LocalizationResult) are the intended way to choose cutoffs.
    """

    name = "SMLM filter"
    id = "smlm-filter"
    category = "Localization"
    kinds = ("localization",)

    @classmethod
    def default_params(cls) -> dict:
        return {   'min_photons': 0.0,
        'max_photons': 0.0,
        'min_sigma_nm': 0.0,
        'max_sigma_nm': 0.0,
        'min_frame': 0,
        'max_frame': 0}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        def _spin(maximum, suffix="", decimals=1):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.0, maximum)
            spin.setDecimals(decimals)
            spin.setValue(0.0)
            spin.setSpecialValueText("off")
            if suffix:
                spin.setSuffix(suffix)
            return spin

        min_photons = _spin(1e9)
        max_photons = _spin(1e9)
        min_sigma = _spin(1e6, " nm")
        max_sigma = _spin(1e6, " nm")
        min_frame = _spin(1e9, decimals=0)
        max_frame = _spin(1e9, decimals=0)
        layout.addRow("Min photons:", min_photons)
        layout.addRow("Max photons:", max_photons)
        layout.addRow("Min sigma:", min_sigma)
        layout.addRow("Max sigma:", max_sigma)
        layout.addRow("First frame:", min_frame)
        layout.addRow("Last frame:", max_frame)

        def get_values():
            return {
                "min_photons": float(min_photons.value()),
                "max_photons": float(max_photons.value()),
                "min_sigma_nm": float(min_sigma.value()),
                "max_sigma_nm": float(max_sigma.value()),
                "min_frame": int(min_frame.value()),
                "max_frame": int(max_frame.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("SMLM filter requires a LocalizationResult input")

        min_frame = _bound(params.get("min_frame"))
        max_frame = _bound(params.get("max_frame"))
        filtered, mask = filter_localizations(
            result.locs,
            min_photons=_bound(params.get("min_photons")),
            max_photons=_bound(params.get("max_photons")),
            min_sigma_nm=_bound(params.get("min_sigma_nm")),
            max_sigma_nm=_bound(params.get("max_sigma_nm")),
            min_frame=int(min_frame) if min_frame is not None else None,
            max_frame=int(max_frame) if max_frame is not None else None,
        )
        return LocalizationResult(
            name=f"{result.name} (filtered {int(mask.sum())}/{len(mask)})",
            locs=filtered,
            pixel_size_nm=result.pixel_size_nm,
            z_step_nm=result.z_step_nm,
            dims=result.dims,
            source_name=result.source_name,
            source_shape=result.source_shape,
            metadata={
                **result.metadata,
                "filter_params": dict(params),
                "filter_kept": int(mask.sum()),
                "filter_total": int(len(mask)),
            },
        )


__all__ = ["SmlmFilterProcessor"]
