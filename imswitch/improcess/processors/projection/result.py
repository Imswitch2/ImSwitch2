"""Projection processing result."""

from pathlib import Path

import h5py
import numpy as np
import tifffile

from imswitch.improcess.analysis.projections import ProjectionAnalysis
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult


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

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt in ("tiff", "tif"):
            tifffile.imwrite(str(path), np.asarray(self.data).astype(np.float32))
        elif fmt in ("hdf5", "h5", "hdf"):
            with h5py.File(str(path), "w") as h5:
                h5.create_dataset("projection", data=np.asarray(self.data))
                h5.attrs["mode"] = self.analysis.mode
                h5.attrs["axis"] = self.analysis.axis
                h5.attrs["axis_label"] = self.analysis.axis_label
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
        else:
            raise ValueError(f"Projection result supports TIFF or HDF5, got {fmt!r}")

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
