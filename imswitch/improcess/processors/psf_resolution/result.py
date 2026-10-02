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


#: How each width key reads in tables and reports.
_AXIS_NAMES = {"lat": "lateral", "x": "x", "y": "y", "z": "axial (z)"}
_METHOD_NAMES = {"": "Gaussian fit", "_corr": "Gaussian fit, bead-corrected", "_hm": "half maximum"}


def width_label(key: str) -> str:
    """``fwhm_x_hm`` -> ``FWHM x (half maximum)``."""
    rest = key.removeprefix("fwhm_")
    axis, _, method = rest.partition("_")
    return f"FWHM {_AXIS_NAMES.get(axis, axis)} ({_METHOD_NAMES.get('_' + method if method else '', method)})"


def _width_keys(analysis) -> list[str]:
    """The per-bead width columns, Gaussian then half maximum."""
    keys = [f"fwhm_{ax}" for ax in ("lat", *analysis.axes)]
    if analysis.params.physical and analysis.params.bead_diameter_nm:
        keys += [f"fwhm_{ax}_corr" for ax in ("lat", *analysis.axes)]
    return keys + [f"fwhm_{ax}_hm" for ax in ("lat", *analysis.axes)]


class BeadTableResult(_RowsTableResult):
    """The selected beads: positions and widths, one row each.

    Only the beads that entered the statistics are listed (rejected ones are
    counted in the summary and shown by the panel's preview). The full
    :class:`~imswitch.improcess.analysis.bead_psf.BeadAnalysis`, rejected
    candidates included, stays attached in memory so ``psf-bead-select`` can
    re-select and re-summarize without refitting.
    """

    _row_axis = "Bead"

    def __init__(self, name: str, analysis, mask, selection, params: dict | None = None):
        self.analysis = analysis
        self.mask = np.asarray(mask, dtype=bool)
        self.selection = selection
        self.params = dict(params or {})
        unit = analysis.unit
        axes = analysis.axes
        width_keys = _width_keys(analysis)
        plane = ["plane"] if getattr(analysis, "projected", False) else []
        columns = ["id", *plane, *[f"{ax}_px" for ax in axes], *[f"{k}_{unit}" for k in width_keys],
                   "ellipticity", "r2", *(["r2_z"] if analysis.ndim == 3 else []), "amplitude", "background"]
        rows = []
        for bead, chosen in zip(analysis.beads, self.mask):
            if not chosen:
                continue
            row = {
                "id": bead["id"], "plane": bead.get("plane"),
                "ellipticity": bead.get("ellipticity"), "r2": bead.get("r2"),
                "r2_z": bead.get("r2_z"), "amplitude": bead.get("amp"), "background": bead.get("offset"),
            }
            for ax in axes:
                row[f"{ax}_px"] = bead.get(ax)
            for key in width_keys:
                row[f"{key}_{unit}"] = bead.get(key)
            rows.append(row)
        super().__init__(name, rows, columns, attrs={
            "unit": unit, "source": analysis.source, "n_candidates": len(analysis.beads),
        })

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
                x_label=f"FWHM {label}, Gaussian fit ({unit})", y_label="Beads", series=series,
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
    values when available); scalar rows (bead counts, rejections, tilt, field
    trend, aberrations) put their number in ``value``.
    """

    _row_axis = "Metric"
    COLUMNS = ["metric", "unit", "n", "mean", "std", "sem", "median", "mad", "averaged_psf", "theory", "value"]

    def __init__(self, name: str, summary: dict, averaged=None, focal=None, trend=None,
                 params: dict | None = None, aberrations=None, detection_fwhm=None):
        from imswitch.improcess.analysis.bead_psf import reason_label

        self.summary = summary
        self.averaged = averaged
        self.aberrations = aberrations
        self.params = dict(params or {})
        unit = summary["unit"]
        theory = summary.get("theory_fwhm_nm", {})
        rows = []
        for key, stats in summary["stats"].items():
            axis = key.split("_")[1]
            plain = key.count("_") == 1
            rows.append({
                "metric": width_label(key), "unit": unit, **stats,
                "averaged_psf": (averaged.fwhm.get(key) if averaged is not None else None),
                "theory": theory.get(axis) if plain and axis in ("lat", "z") else None,
            })
        rows.append({"metric": "beads selected", "value": summary["n_selected"]})
        rows.append({"metric": "bead candidates", "value": summary["n_candidates"]})
        for reason, count in summary.get("rejected", {}).items():
            rows.append({"metric": f"rejected: {reason_label(reason)}", "value": count})
        if averaged is not None:
            rows.append({"metric": "beads averaged", "value": averaged.n})
        if detection_fwhm:
            rows.append({"metric": "detection scale FWHM lateral", "unit": unit, "value": detection_fwhm[-1]})
            if len(detection_fwhm) == 3:
                rows.append({"metric": "detection scale FWHM axial (z)", "unit": unit, "value": detection_fwhm[0]})
        for axis, ratio in summary.get("ratio_to_theory", {}).items():
            rows.append({"metric": f"ratio to theory ({_AXIS_NAMES.get(axis, axis)})", "value": ratio})
        for key, value in (focal or {}).items():
            rows.append({"metric": f"focal surface {key}", "unit": _focal_unit(key, unit), "value": value})
        for key, value in (trend or {}).items():
            rows.append({"metric": f"lateral FWHM trend {key}", "unit": unit, "value": value})
        if aberrations is not None:
            rows += _aberration_rows(aberrations)
        self.warnings = list(summary.get("warnings", []))
        if aberrations is not None:
            self.warnings += [w for w in aberrations.warnings if w not in self.warnings]
        self.notes = list(summary.get("notes", []))
        attrs = {"unit": unit, "warnings": self.warnings, "notes": self.notes,
                 "selection": summary.get("selection", {})}
        if summary.get("aberrations_skipped"):
            attrs["aberrations_skipped"] = summary["aberrations_skipped"]
        super().__init__(name, rows, list(self.COLUMNS), attrs=attrs)

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
            metadata={"metrics": ", ".join(width_label(k) for k in keys), "warnings": " | ".join(self.warnings)},
        )]

    def report(self) -> str:
        """A short plain-text report of the run, for the panel."""
        return psf_report(self.summary, self.averaged, self.aberrations)


def _aberration_rows(fit) -> list[dict]:
    rows = [
        {"metric": "aberrations RMS", "unit": "nm", "value": fit.rms_nm},
        {"metric": "Strehl ratio (Maréchal)", "value": fit.strehl},
        {"metric": "aberration model fit R²", "value": fit.r2},
    ]
    for name, pair in fit.pairs.items():
        rows.append({"metric": f"{name} magnitude", "unit": "nm RMS", "value": pair["magnitude_nm_rms"]})
        rows.append({"metric": f"{name} angle", "unit": "deg", "value": pair["angle_deg"]})
    if 11 in fit.coeffs_nm:
        rows.append({"metric": "spherical (primary)", "unit": "nm RMS", "value": fit.coeffs_nm[11]})
    if fit.sheet:
        rows.append({"metric": "light sheet thickness (FWHM)", "unit": "nm", "value": fit.sheet["fwhm_nm"]})
        rows.append({"metric": "light sheet tilt", "unit": "deg", "value": fit.sheet["tilt_deg"]})
        rows.append({"metric": "light sheet tilt azimuth", "unit": "deg", "value": fit.sheet["azimuth_deg"]})
    if fit.blur_nm is not None:
        rows.append({"metric": "extra blur (sigma)", "unit": "nm", "value": fit.blur_nm})
    return rows


def _fmt_width(value: float, unit: str) -> str:
    if value is None or not np.isfinite(value):
        return "–"
    if unit == "nm" and value >= 1000:
        return f"{value / 1000:.2f} µm"
    return f"{value:.0f} {unit}" if unit == "nm" else f"{value:.2f} {unit}"


def psf_report(summary: dict, averaged=None, aberrations=None) -> str:
    """Plain-text report: counts, widths (both measures), aberrations, warnings."""
    from imswitch.improcess.analysis.bead_psf import reason_label

    unit = summary["unit"]
    stats = summary["stats"]
    lines = []
    rejected = ", ".join(f"{n} {reason_label(r)}" for r, n in summary.get("rejected", {}).items())
    lines.append(
        f"{summary['n_selected']} of {summary['n_candidates']} bead candidates selected"
        + (f" (rejected: {rejected})." if rejected else ".")
    )

    def widths(suffix: str) -> str:
        parts = []
        for axis, name in (("x", "x"), ("y", "y"), ("z", "z")):
            s = stats.get(f"fwhm_{axis}{suffix}")
            if s and s.get("n"):
                spread = f" ± {_fmt_width(s['mad'], unit)}" if np.isfinite(s.get("mad", np.nan)) else ""
                parts.append(f"{name} {_fmt_width(s['median'], unit)}{spread}")
        return ", ".join(parts)

    if summary["n_selected"]:
        lines.append("FWHM, half maximum (median ± MAD): " + widths("_hm"))
        lines.append("FWHM, Gaussian fit: " + widths(""))
        if "fwhm_lat_corr" in stats:
            lines.append("FWHM, Gaussian, bead-corrected: " + widths("_corr"))
    if averaged is not None and averaged.fwhm:
        parts = [f"{ax} {_fmt_width(averaged.fwhm.get(f'fwhm_{ax}_hm'), unit)}"
                 for ax in ("x", "y", "z") if f"fwhm_{ax}_hm" in averaged.fwhm]
        lines.append(f"Averaged PSF ({averaged.n} beads), half maximum: " + ", ".join(parts))
    theory = summary.get("theory_fwhm_nm")
    if theory:
        lines.append(
            f"Diffraction limit (widefield): lateral {_fmt_width(theory['lat'], 'nm')}"
            + (f", axial {_fmt_width(theory['z'], 'nm')}" if np.isfinite(theory.get("z", np.nan)) else "")
        )
    if aberrations is not None:
        lines.append(f"Aberrations: {aberrations.headline()}")
        lines.append("  The Zernike table, the wavefront map and the data | model comparison are in the "
                     "results list ('… (aberrations)', '… (wavefront …)', '… (aberration fit …)').")
    elif summary.get("aberrations_skipped"):
        lines.append(f"Aberrations: not estimated — {summary['aberrations_skipped']}.")
    warnings = list(summary.get("warnings", []))
    if aberrations is not None:
        warnings += [w for w in aberrations.warnings if w not in warnings]
    lines += [f"⚠ {w}" for w in warnings]
    return "\n".join(lines)


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
            "illumination": getattr(fit, "illumination", "widefield"),
        }
        for name_, pair in fit.pairs.items():
            scalars[f"{name_}_magnitude_nm_rms"] = pair["magnitude_nm_rms"]
            scalars[f"{name_}_angle_deg"] = pair["angle_deg"]
        if getattr(fit, "blur_nm", None) is not None:
            scalars["extra_blur_sigma_nm"] = fit.blur_nm
        for key, value in (getattr(fit, "sheet", None) or {}).items():
            scalars[f"light_sheet_{key}"] = value
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


def aberration_fit_image(name: str, fit) -> "ArrayProcessingResult":
    """Data and model side by side (``[data | model]`` along x) as one stack,
    so scrolling z or switching to the XZ / YZ view compares them directly."""
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.result import ViewMode

    data = np.asarray(fit.data, dtype=np.float32)
    model = np.asarray(fit.model, dtype=np.float32)
    gap = np.full(data.shape[:2] + (1,), float(np.nanmin(data)), dtype=np.float32)
    stack = np.concatenate([data, gap, model], axis=2)
    px = fit.pixel_size_nm or (1.0, 1.0, 1.0)
    return ArrayProcessingResult(
        name=f"{name} (aberration fit: data | model, R² {fit.r2:.2f})",
        data=stack,
        axis_labels=["Z", "Y", "X"],
        view_modes=[ViewMode("XY", (0, 1, 2)), ViewMode("XZ", (1, 0, 2)), ViewMode("YZ", (2, 0, 1))],
        axis_scales=[float(v) * 1e-3 for v in px],
        scale_unit="um",
        display_levels=(float(np.nanmin(stack)), float(np.nanmax(stack))),
        metadata={"layout": "data | model along x", "r2": fit.r2},
    )


def wavefront_image(name: str, fit, size: int = 129) -> "ArrayProcessingResult":
    """The fitted pupil phase (nm) over the unit pupil, 0 outside it."""
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    phase = np.nan_to_num(fit.wavefront(size), nan=0.0).astype(np.float32)
    bound = float(np.max(np.abs(phase))) or 1.0
    result = ArrayProcessingResult(
        name=f"{name} (wavefront, {fit.rms_nm:.0f} nm RMS)",
        data=phase,
        axis_labels=["Y", "X"],
        axis_scales=[2.0 / (size - 1)] * 2,
        scale_unit="px",
        display_levels=(-bound, bound),
        metadata={"unit": "nm", "description": "pupil phase over the unit pupil (y down, x right)"},
    )
    result.setDisplayColormap("bwr")
    return result
