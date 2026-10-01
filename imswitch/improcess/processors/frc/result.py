"""FRC processing result."""

from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.frc import FRCAnalysis
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class FRCResult(ProcessingResult):
    """Result wrapper for Fourier ring correlation curves."""

    kind = "curve"
    #: The resolution is the answer FRC is run for; the curve is how it was
    #: obtained. It belongs in the Results table, not only in the plot title.
    publishes_table_rows = True

    def __init__(self, name: str, analysis: FRCAnalysis, params: dict):
        self.analysis = analysis
        self.params = params

        cutoff = np.full_like(analysis.frequency, np.nan, dtype=np.float64)
        if np.isfinite(analysis.cutoff_frequency):
            cutoff[:] = analysis.cutoff_frequency
        data = np.stack([analysis.frc, analysis.threshold, cutoff], axis=0).astype(np.float32)

        super().__init__(
            name=name,
            data=data,
            axis_labels=["C", "Frequency"],
            view_modes=[ViewMode("Curves", (0, 1))],
            display_levels=None,
        )

    def table_columns(self) -> list[str]:
        return [
            "source",
            "kind",
            "mode",
            "resolution",
            "resolution_unit",
            "cutoff_frequency",
            "frequency_unit",
            "window",
        ]

    def table_records(self) -> list[dict]:
        """One row: the measured resolution and how it was measured."""
        analysis = self.analysis
        return [
            {
                "source": self.name,
                "kind": "frc",
                "mode": str(self.params.get("mode", "")),
                "resolution": float(analysis.resolution),
                "resolution_unit": str(analysis.resolution_unit),
                "cutoff_frequency": float(analysis.cutoff_frequency),
                "frequency_unit": str(analysis.frequency_unit),
                "window": str(self.params.get("window", "")),
            }
        ]


    supported_formats = ("hdf5", "csv")

    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan, companion_json_path

        path = Path(path)
        if fmt == "csv":
            return SavePlan(path, fmt, (companion_json_path(path),))
        return SavePlan(path, fmt)

    def write_files(self, plan, document) -> None:
        from imswitch.improcess.model.save_protocol import embed_hdf5_path, write_companion_json

        if plan.fmt == "hdf5":
            self._save_hdf5(plan.primary)
            embed_hdf5_path(plan.primary, document)
        elif plan.fmt == "csv":
            self._save_text(plan.primary)
            write_companion_json(plan.primary, document)
        else:
            raise ValueError(f"{type(self).__name__} supports HDF5 or CSV/TXT, got {plan.fmt!r}")

    def plot_payloads(self) -> list[PlotPayload]:
        series = [
            PlotSeries(
                name="FRC",
                x=self.analysis.frequency,
                y=self.analysis.frc,
                kind="line",
            ),
            PlotSeries(
                name="1/7 threshold",
                x=self.analysis.frequency,
                y=self.analysis.threshold,
                kind="line",
                style={"pen": "y"},
            ),
        ]
        if np.isfinite(self.analysis.cutoff_frequency):
            series.append(
                PlotSeries(
                    name="Cutoff",
                    x=np.array([self.analysis.cutoff_frequency, self.analysis.cutoff_frequency]),
                    y=np.array([0.0, 1.0]),
                    kind="line",
                    style={"pen": "r"},
                )
            )

        title = "FRC"
        if np.isfinite(self.analysis.resolution):
            title = f"FRC resolution: {self.analysis.resolution:.4g} {self.analysis.resolution_unit}"

        return [
            PlotPayload(
                title=title,
                x_label=f"Spatial frequency ({self.analysis.frequency_unit})",
                y_label="Correlation",
                series=series,
                metadata={
                    "resolution": self.analysis.resolution,
                    "resolution_unit": self.analysis.resolution_unit,
                    "cutoff_frequency": self.analysis.cutoff_frequency,
                    **self.analysis.metadata,
                },
            )
        ]

    def _save_hdf5(self, path: Path) -> None:
        with h5py.File(str(path), "w") as h5:
            h5.create_dataset("frequency", data=self.analysis.frequency)
            h5.create_dataset("frc", data=self.analysis.frc)
            h5.create_dataset("threshold", data=self.analysis.threshold)
            h5.attrs["cutoff_frequency"] = self.analysis.cutoff_frequency
            h5.attrs["resolution"] = self.analysis.resolution
            h5.attrs["frequency_unit"] = self.analysis.frequency_unit
            h5.attrs["resolution_unit"] = self.analysis.resolution_unit
            for key, value in self.params.items():
                if value is None:
                    continue
                h5.attrs[str(key)] = value
            for key, value in self.analysis.metadata.items():
                if value is None:
                    continue
                attr_key = str(key)
                if attr_key in h5.attrs:
                    attr_key = f"frc_{attr_key}"
                h5.attrs[attr_key] = value

    def _save_text(self, path: Path) -> None:
        table = np.column_stack([self.analysis.frequency, self.analysis.frc, self.analysis.threshold])
        np.savetxt(
            str(path),
            table,
            delimiter=",",
            header=f"frequency,frc,threshold; resolution={self.analysis.resolution} {self.analysis.resolution_unit}",
        )
