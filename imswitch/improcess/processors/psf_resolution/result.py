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


# --------------------------------------------------------------------------- #
# Bead analysis results (params_version 2)
# --------------------------------------------------------------------------- #
class _RowsTableResult(ProcessingResult):
    """A table of dict rows with a fixed column order, saved as CSV or HDF5.

    ``data`` is the numeric projection of the rows (non-numeric cells are
    NaN) so the shared table/kind machinery sees an ordinary 2-D array.
    """

    kind = "table"
    supported_formats = ("hdf5", "csv")
    _row_axis = "Row"

    def __init__(self, name: str, rows: list[dict], columns: list[str], attrs: dict | None = None):
        self._rows = rows
        self._columns = columns
        self.attrs = dict(attrs or {})
        data = np.array(
            [[_as_float(row.get(column)) for column in columns] for row in rows], dtype=np.float32
        ).reshape(len(rows), len(columns))
        super().__init__(
            name=name,
            data=data,
            axis_labels=[self._row_axis, "Metric"],
            view_modes=[ViewMode("Table", (0, 1))],
            display_levels=None,
        )

    def table_records(self) -> list[dict]:
        return [{column: row.get(column) for column in self._columns} for row in self._rows]

    def table_columns(self) -> list[str]:
        return list(self._columns)

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

    def _save_hdf5(self, path: Path) -> None:
        import json

        records = self.table_records()
        with h5py.File(str(path), "w") as h5:
            h5.attrs["row_count"] = len(records)
            for key, value in self.attrs.items():
                h5.attrs[str(key)] = value if isinstance(value, (int, float, str)) else json.dumps(
                    value, default=_json_default
                )
            table = h5.create_group("table")
            for column in self._columns:
                values = [row.get(column) for row in records]
                if all(isinstance(v, (int, float, np.integer, np.floating)) or v is None for v in values):
                    table.create_dataset(column, data=np.array([_as_float(v) for v in values]))
                else:
                    dtype = h5py.string_dtype(encoding="utf-8")
                    table.create_dataset(column, data=np.array([str(v) for v in values], dtype=dtype))

    def _save_text(self, path: Path) -> None:
        import csv

        with Path(path).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(self._columns)
            for row in self.table_records():
                writer.writerow(["" if row[c] is None else row[c] for c in self._columns])


def _as_float(value) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    return float("nan")


def _json_default(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


class BeadTableResult(_RowsTableResult):
    """Every candidate bead: status, selection flag, positions and widths.

    Carries the full :class:`~imswitch.improcess.analysis.bead_psf.BeadAnalysis`
    so ``psf-bead-select`` can re-select and re-summarize without refitting.
    """

    _row_axis = "Bead"

    def __init__(self, name: str, analysis, mask, selection, params: dict | None = None):
        self.analysis = analysis
        self.mask = np.asarray(mask, dtype=bool)
        self.selection = selection
        self.params = dict(params or {})
        unit = analysis.unit
        axes = analysis.axes
        columns = ["id", "status", "selected", *[f"{ax}_det_px" for ax in axes if ax != "z"]]
        if analysis.ndim == 3:
            columns.append("z_det_px")
        columns += [f"{ax}_px" for ax in axes]
        width_keys = [f"fwhm_{ax}" for ax in ("lat", *axes)]
        columns += [f"{k}_{unit}" for k in width_keys]
        columns += [f"fwhm_{ax}_err_{unit}" for ax in axes]
        if analysis.params.physical and analysis.params.bead_diameter_nm:
            columns += [f"{k}_corr_{unit}" for k in width_keys]
        columns += ["ellipticity", "r2"] + (["r2_z"] if analysis.ndim == 3 else []) + ["amplitude", "offset"]
        rows = []
        for bead, chosen in zip(analysis.beads, self.mask):
            row = {
                "id": bead["id"], "status": bead["status"], "selected": bool(chosen),
                "y_det_px": bead["y_det"], "x_det_px": bead["x_det"], "z_det_px": bead.get("z_det"),
                "ellipticity": bead.get("ellipticity"), "r2": bead.get("r2"), "r2_z": bead.get("r2_z"),
                "amplitude": bead.get("amp"), "offset": bead.get("offset"),
            }
            for ax in axes:
                row[f"{ax}_px"] = bead.get(ax)
                row[f"fwhm_{ax}_err_{unit}"] = bead.get(f"fwhm_{ax}_err")
            for key in width_keys:
                row[f"{key}_{unit}"] = bead.get(key)
                row[f"{key}_corr_{unit}"] = bead.get(f"{key}_corr")
            rows.append(row)
        super().__init__(name, rows, columns, attrs={"unit": unit, "source": analysis.source})

    def plot_payloads(self) -> list[PlotPayload]:
        unit = self.analysis.unit
        fitted = [b for b in self.analysis.beads if "fwhm_lat" in b]
        if not fitted:
            return []
        chosen = [b for b, m in zip(self.analysis.beads, self.mask) if m]
        payloads = []
        for key, label in (("fwhm_lat", "lateral"), ("fwhm_z", "axial")):
            if key == "fwhm_z" and self.analysis.ndim != 3:
                continue
            series = [PlotSeries(name="all fitted", y=np.array([b[key] for b in fitted]), kind="histogram",
                                 style={"bins": 30})]
            if chosen:
                series.append(PlotSeries(name="selected", y=np.array([b[key] for b in chosen]),
                                         kind="histogram", style={"bins": 30}))
            payloads.append(PlotPayload(
                title=f"Bead FWHM, {label} ({len(chosen)}/{len(fitted)} selected)",
                x_label=f"FWHM {label} ({unit})", y_label="Beads", series=series,
                metadata={"unit": unit, "n_fitted": len(fitted), "n_selected": len(chosen)},
            ))
        if chosen:
            payloads.append(PlotPayload(
                title="Lateral FWHM across the field",
                x_label="x (px)", y_label=f"FWHM lateral ({unit})",
                series=[PlotSeries(name="selected", x=np.array([b["x"] for b in chosen]),
                                   y=np.array([b["fwhm_lat"] for b in chosen]), kind="scatter")],
                metadata={"unit": unit},
            ))
        return payloads


class PSFSummaryResult(_RowsTableResult):
    """Statistics of the selected beads, one row per width, plus scalar rows.

    Stat rows fill ``n``..``mad`` (and the averaged-bead and diffraction-limit
    values when available); scalar rows (bead counts, tilt, field trend) put
    their number in ``value``.
    """

    _row_axis = "Metric"
    COLUMNS = ["metric", "unit", "n", "mean", "std", "sem", "median", "mad", "averaged_psf", "theory", "value"]

    def __init__(self, name: str, summary: dict, averaged=None, focal=None, trend=None,
                 params: dict | None = None):
        self.summary = summary
        self.params = dict(params or {})
        unit = summary["unit"]
        theory = summary.get("theory_fwhm_nm", {})
        rows = []
        for key, stats in summary["stats"].items():
            axis = key.split("_")[1]
            rows.append({
                "metric": key, "unit": unit, **stats,
                "averaged_psf": (averaged.fwhm.get(key) if averaged is not None else None),
                "theory": theory.get(axis) if axis in ("lat", "z") else None,
            })
        rows.append({"metric": "beads selected", "value": summary["n_selected"]})
        rows.append({"metric": "bead candidates", "value": summary["n_candidates"]})
        for status, count in summary["status_counts"].items():
            rows.append({"metric": f"beads {status}", "value": count})
        if averaged is not None:
            rows.append({"metric": "beads averaged", "value": averaged.n})
        for axis, ratio in summary.get("ratio_to_theory", {}).items():
            rows.append({"metric": f"ratio to theory ({axis})", "value": ratio})
        for key, value in (focal or {}).items():
            rows.append({"metric": f"focal surface {key}", "unit": _focal_unit(key, unit), "value": value})
        for key, value in (trend or {}).items():
            rows.append({"metric": f"lateral FWHM trend {key}", "unit": unit, "value": value})
        self.warnings = list(summary.get("warnings", []))
        super().__init__(name, rows, list(self.COLUMNS), attrs={
            "unit": unit, "warnings": self.warnings, "selection": summary.get("selection", {}),
        })

    def plot_payloads(self) -> list[PlotPayload]:
        stats = {k: v for k, v in self.summary["stats"].items() if v.get("n")}
        if not stats:
            return []
        keys = list(stats)
        return [PlotPayload(
            title="PSF FWHM summary (median, selected beads)",
            x_label="metric #", y_label=f"FWHM ({self.summary['unit']})",
            series=[PlotSeries(name="median", x=np.arange(len(keys), dtype=np.float64),
                               y=np.array([stats[k]["median"] for k in keys]), kind="scatter")],
            metadata={"metrics": ", ".join(keys), "warnings": " | ".join(self.warnings)},
        )]


def _focal_unit(key: str, unit: str) -> str:
    if key.endswith("_mrad"):
        return "mrad"
    if key == "n":
        return ""
    return unit


class AberrationsResult(_RowsTableResult):
    """Fitted Zernike modes (nm RMS); totals and fit quality in ``attrs`` and
    the plot metadata, which the results dock lists alongside the curve."""

    _row_axis = "Mode"
    COLUMNS = ["noll", "mode", "coefficient_nm_rms", "error_nm_rms", "milliwaves"]

    def __init__(self, name: str, fit, params: dict | None = None):
        self.fit = fit
        self.params = dict(params or {})
        scalars = {
            "rms_nm": fit.rms_nm, "strehl_marechal": fit.strehl, "r2": fit.r2,
            "n_beads": fit.n_beads, "wavelength_nm": fit.wavelength_nm,
        }
        for name_, pair in fit.pairs.items():
            scalars[f"{name_}_magnitude_nm_rms"] = pair["magnitude_nm_rms"]
            scalars[f"{name_}_angle_deg"] = pair["angle_deg"]
        self.scalars = scalars
        super().__init__(name, fit.rows(), list(self.COLUMNS), attrs={**scalars, "warnings": fit.warnings})

    def plot_payloads(self) -> list[PlotPayload]:
        rows = self.fit.rows()
        return [PlotPayload(
            title=f"Zernike aberrations (RMS {self.fit.rms_nm:.1f} nm, Strehl {self.fit.strehl:.2f})",
            x_label="Noll index", y_label="Coefficient (nm RMS)",
            series=[PlotSeries(name="coefficient", x=np.array([r["noll"] for r in rows], dtype=np.float64),
                               y=np.array([r["coefficient_nm_rms"] for r in rows]), kind="scatter")],
            metadata=dict(self.scalars),
        )]
