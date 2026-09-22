"""Projection processing result."""


import numpy as np

from imswitch.improcess.analysis.projections import ProjectionAnalysis
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.model.result_io import save_image_result


class ProjectionResult(ProcessingResult):
    """Result wrapper for projected image data."""

    def __init__(
        self,
        name: str,
        analysis: ProjectionAnalysis,
        scale_unit: str = "px",
        params: dict | None = None,
    ):
        self.analysis = analysis
        self.params = params or {}
        data = np.asarray(analysis.data)
        display_levels = None
        finite = data[np.isfinite(data)]
        if finite.size:
            display_levels = (float(np.nanmin(finite)), float(np.nanmax(finite)))
        super().__init__(
            name=name,
            data=data,
            axis_labels=list(analysis.output_axis_labels),
            display_levels=display_levels,
            axis_scales=list(analysis.output_axis_scales),
            scale_unit=scale_unit,
        )

    def write_files(self, plan, document) -> None:
        save_image_result(self, plan.primary, plan.fmt, extra={
            "projection_mode": self.analysis.mode,
            "projection_axis": self.analysis.axis,
            "projection_axis_label": self.analysis.axis_label,
        }, document=document)

    def plot_payloads(self) -> list[PlotPayload]:
        data = np.asarray(self.data, dtype=np.float64)
        finite = data[np.isfinite(data)]
        if finite.size == 0:
            return []
        hist, edges = np.histogram(finite, bins=64)
        centers = 0.5 * (edges[:-1] + edges[1:])
        return [
            PlotPayload(
                title=f"{self.analysis.mode} {self.analysis.axis_label}-projection histogram",
                x_label="Intensity",
                y_label="Count",
                series=[
                    PlotSeries(
                        name="Pixels",
                        x=centers,
                        y=hist,
                        kind="bar",
                    )
                ],
                metadata=dict(self.analysis.metadata),
            )
        ]
