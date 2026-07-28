"""PSF / bead resolution processing result."""

from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.psf_resolution import PSFResolutionAnalysis
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class PSFResolutionResult(ProcessingResult):
    """Result wrapper for PSF fit tables."""

    kind = "table"

    _METRICS = [
        "center_y_px",
        "center_x_px",
        "sigma_y_px",
        "sigma_x_px",
        "fwhm_y_px",
        "fwhm_x_px",
        "amplitude",
        "background",
        "fit_error",
    ]

    def __init__(self, name: str, analysis: PSFResolutionAnalysis, params: dict | None = None):
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
            raise ValueError(f"PSF result supports HDF5 or CSV/TXT, got {fmt!r}")

    def table_records(self) -> list[dict]:
        """One row per fit, including the unit-scaled sigma/FWHM columns.

        ``analysis.rows()`` is already the row-per-fit projection used for CSV
        and HDF5 export; the shared Results dock renders the same rows.
        """
        return self.analysis.rows()

    def table_columns(self) -> list[str]:
        # Column names depend on the analysis unit (e.g. fwhm_x_nm), so they
        # are read off an actual row rather than hard-coded.
        rows = self.analysis.rows()
        return list(rows[0].keys()) if rows else []

    def plot_payloads(self) -> list[PlotPayload]:
        if not self.analysis.fits:
            return []
        x = np.arange(1, len(self.analysis.fits) + 1, dtype=np.float64)
        fwhm_x = np.array([fit.fwhm_x * self.analysis.pixel_size for fit in self.analysis.fits])
        fwhm_y = np.array([fit.fwhm_y * self.analysis.pixel_size for fit in self.analysis.fits])
        return [
            PlotPayload(
                title=f"PSF FWHM ({len(self.analysis.fits)} fit(s))",
                x_label="Fit",
                y_label=f"FWHM ({self.analysis.unit})",
                series=[
                    PlotSeries(name="FWHM X", x=x, y=fwhm_x, kind="scatter"),
                    PlotSeries(name="FWHM Y", x=x, y=fwhm_y, kind="scatter"),
                ],
                metadata={
                    "fit_count": len(self.analysis.fits),
                    "pixel_size": self.analysis.pixel_size,
                    "unit": self.analysis.unit,
                },
            )
        ]

    def _save_hdf5(self, path: Path) -> None:
        rows = self.analysis.rows()
        with h5py.File(str(path), "w") as h5:
            h5.attrs["fit_count"] = len(rows)
            h5.attrs["pixel_size"] = self.analysis.pixel_size
            h5.attrs["unit"] = self.analysis.unit
            for key, value in self.params.items():
                if value is not None:
                    h5.attrs[str(key)] = value
            table = h5.create_group("fits")
            for key in rows[0].keys() if rows else []:
                values = [row[key] for row in rows]
                if key in ("name", "bounds"):
                    dtype = h5py.string_dtype(encoding="utf-8")
                    table.create_dataset(key, data=np.array([str(v) for v in values], dtype=dtype))
                else:
                    table.create_dataset(key, data=np.asarray(values, dtype=np.float64))

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
