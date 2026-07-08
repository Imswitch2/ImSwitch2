"""Colocalization processing result."""

from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.colocalization import ColocalizationAnalysis
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class ColocalizationResult(ProcessingResult):
    """Result wrapper for colocalization tables."""

    kind = "table"

    _METRICS = [
        "pearson",
        "manders_m1",
        "manders_m2",
        "overlap_coefficient",
        "pixel_count",
        "mean_a",
        "mean_b",
    ]

    def __init__(self, name: str, analysis: ColocalizationAnalysis, params: dict | None = None):
        self.analysis = analysis
        self.params = params or {}
        data = np.array(
            [[float(row[metric]) for metric in self._METRICS] for row in analysis.rows()],
            dtype=np.float32,
        )
        if data.size == 0:
            data = np.empty((0, len(self._METRICS)), dtype=np.float32)
        super().__init__(
            name=name,
            data=data,
            axis_labels=["ROI", "Metric"],
            view_modes=[ViewMode("Table", (0, 1))],
            display_levels=None,
        )

    def save(self, path: Path, fmt: str = "hdf5") -> None:
        path = Path(path)
        if fmt in ("hdf5", "h5", "hdf"):
            self._save_hdf5(path)
        elif fmt in ("csv", "txt"):
            self._save_text(path)
        else:
            raise ValueError(f"Colocalization result supports HDF5 or CSV/TXT, got {fmt!r}")

    def plot_payloads(self) -> list[PlotPayload]:
        if self.analysis.scatter_a.size == 0:
            return []
        title = "Colocalization intensity scatter"
        if self.analysis.records:
            title = f"Colocalization: Pearson {self.analysis.records[0].pearson:.4g}"
        return [
            PlotPayload(
                title=title,
                x_label="Channel A intensity",
                y_label="Channel B intensity",
                series=[
                    PlotSeries(
                        name="Pixels",
                        x=self.analysis.scatter_a,
                        y=self.analysis.scatter_b,
                        kind="scatter",
                    )
                ],
                metadata=dict(self.analysis.metadata),
            )
        ]

    def _save_hdf5(self, path: Path) -> None:
        rows = self.analysis.rows()
        with h5py.File(str(path), "w") as h5:
            h5.attrs["region_count"] = len(rows)
            for key, value in {**self.analysis.metadata, **self.params}.items():
                if value is not None:
                    h5.attrs[str(key)] = value
            table = h5.create_group("records")
            for key in rows[0].keys() if rows else []:
                values = [row[key] for row in rows]
                if key in ("name", "bounds"):
                    dtype = h5py.string_dtype(encoding="utf-8")
                    table.create_dataset(key, data=np.array([str(v) for v in values], dtype=dtype))
                else:
                    table.create_dataset(key, data=np.asarray(values, dtype=np.float64))
            scatter = h5.create_group("scatter")
            scatter.create_dataset("a", data=self.analysis.scatter_a)
            scatter.create_dataset("b", data=self.analysis.scatter_b)

    def _save_text(self, path: Path) -> None:
        rows = self.analysis.rows()
        if not rows:
            Path(path).write_text("", encoding="utf-8")
            return
        fieldnames = list(rows[0].keys())
        with Path(path).open("w", encoding="utf-8") as fh:
            fh.write(",".join(fieldnames) + "\n")
            for row in rows:
                fh.write(",".join(str(row[key]) for key in fieldnames) + "\n")
