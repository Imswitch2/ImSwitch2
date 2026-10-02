"""Re-select the beads of a ``psf-resolution`` bead table.

The cheap half of the bead analysis: given the fitted beads (the bead table
keeps every candidate in memory, rejected ones included), choose which enter
the statistics (R², ellipticity, FWHM range) and summarize them again.
No refitting, so it is what the PSF panel's range slider drives, and the
chosen range is recorded in provenance like any other parameter. The
averaged PSF and aberrations need the image and are not recomputed here;
re-run ``psf-resolution`` with the same selection for those.
"""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.bead_psf import field_trend, focal_surface, select_beads, summarize
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput
from imswitch.improcess.processors.psf_resolution._params import (
    SELECTION_FIELDS,
    build_form,
    selection_from_params,
)
from imswitch.improcess.processors.psf_resolution.result import BeadTableResult, PSFSummaryResult


class PSFBeadSelectProcessor(Processor):
    """Choose which fitted beads of a PSF bead table enter the statistics."""

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
        return OutputSpec(ports=("beads", "summary"))

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, BeadTableResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return build_form(parent, self.param_spec(), collapsed=())

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        if not isinstance(result, BeadTableResult):
            raise TypeError("psf-bead-select needs a bead table from psf-resolution")
        p = {**self.default_params(), **(params or {})}
        analysis = result.analysis
        selection = selection_from_params(p)
        mask = select_beads(analysis, selection)
        summary = summarize(analysis, mask, selection)
        recorded = {**result.params, **p}
        base = result.name.removesuffix(" (PSF beads)")
        return ProcessorOutput(
            [
                BeadTableResult(f"{base} (PSF beads, reselected)", analysis, mask, selection, recorded),
                PSFSummaryResult(
                    f"{base} (PSF summary, reselected)", summary, None,
                    focal_surface(analysis, mask, notes=summary["notes"]), field_trend(analysis, mask), recorded,
                    detection_fwhm=analysis.expected_fwhm(),
                ),
            ],
            keys=("beads", "summary"),
        )
