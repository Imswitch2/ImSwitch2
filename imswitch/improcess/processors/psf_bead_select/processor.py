"""Re-select the beads of a ``psf-resolution`` measurement.

The cheap half of the bead analysis: given the fitted beads (a measurement
keeps every candidate in memory, rejected ones included, and the data they
came from), choose which enter the statistics (R², ellipticity, FWHM range)
and measure again. No refitting; the averaged PSF and, when the measurement
had them, the aberrations are recomputed from the new selection. The chosen
range is recorded in provenance like any other parameter.
"""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput
from imswitch.improcess.processors.psf_resolution._params import SELECTION_FIELDS, build_form
from imswitch.improcess.processors.psf_resolution.processor import measure, reselect
from imswitch.improcess.processors.psf_resolution.result import PSFMeasurementResult


class PSFBeadSelectProcessor(Processor):
    """Choose which fitted beads of a PSF measurement enter the statistics."""

    name = "PSF Bead Selection"
    id = "psf-bead-select"
    category = "Measurement"
    kinds = ("table",)

    @classmethod
    def param_spec(cls) -> tuple:
        return SELECTION_FIELDS

    @classmethod
    def default_params(cls) -> dict:
        return {f.key: f.default for f in cls.param_spec()}

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        # ``average_psf`` only when the measurement was averaged.
        return OutputSpec(ports=("psf", "average_psf"))

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, PSFMeasurementResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return build_form(parent, self.param_spec(), collapsed=())

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        if not isinstance(result, PSFMeasurementResult):
            raise TypeError("psf-bead-select needs a PSF measurement from psf-resolution")
        p = {**self.default_params(), **result.params, **(params or {})}
        return measure(reselect(result.run, p), p, result.source_name, suffix=" (re-selected)")
