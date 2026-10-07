"""PSF / bead resolution processing result."""

from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.psf_resolution import PSFResolutionAnalysis
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult, ViewMode


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
# Bead analysis (params_version 2): one measurement, one result
# --------------------------------------------------------------------------- #
#: Marker colour per bead state (``""`` = selected), shared by the panel's
#: preview and the measurement's viewer overlay.
STATE_COLORS = {
    "": "#33dd55",
    "border": "#8c8c8c",
    "crowded": "#ff9f1c",
    "bright": "#d65bd6",
    "saturated": "#d65bd6",
    "fit_failed": "#ff4d4d",
    "fit_quality": "#ff4d4d",
    "ellipticity": "#ff4d4d",
    "fwhm_outlier": "#ff4d4d",
}
#: Short tag drawn next to a rejected bead.
STATE_TAGS = {
    "border": "edge",
    "crowded": "crowded",
    "bright": "bright",
    "saturated": "saturated",
    "fit_failed": "no fit",
    "fit_quality": "R²",
    "ellipticity": "elliptic",
    "fwhm_outlier": "FWHM",
}

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


class PSFMeasurementResult(ProcessingResult):
    """One bead-based PSF measurement: everything ``psf-resolution`` found.

    One results-list entry per measurement, whatever was asked for. Its parts
    are views of the same measurement, not separate data:

    * **viewer**: the analysed image with every bead candidate marked
      (green = used, other colours = why it was rejected);
    * **Results dock**: one headline row (:meth:`table_records`), so
      repeated measurements line up as a comparison table;
    * **Graph**: the FWHM histograms, the lateral FWHM across the field and,
      when fitted, the Zernike coefficients;
    * **PSF panel**: the report card (widths, averaged-PSF thumbnails,
      aberrations, the bead list);
    * **file**: one HDF5 with ``beads``, ``summary``, ``average_psf`` and
      ``aberrations`` (or a bead CSV with summary / Zernike CSV companions).

    The averaged PSF is *also* published on its own, as an ordinary image: it
    is the one part with a use beyond describing the measurement.

    ``run`` (the analysed data, every candidate and their rejection reasons)
    stays attached in memory, so ``psf-bead-select`` can re-select without
    refitting and recompute the average and the aberrations.
    """

    kind = "table"
    supported_formats = ("hdf5", "csv")

    def __init__(self, name: str, run, summary: dict, *, source_name: str = "", averaged=None,
                 aberrations=None, focal=None, trend=None, params: dict | None = None):
        self.run = run
        self.summary = summary
        self.averaged = averaged
        self.aberrations = aberrations
        self.focal = dict(focal or {})
        self.trend = dict(trend or {})
        self.params = dict(params or {})
        self.source_name = source_name
        self.warnings = list(summary.get("warnings", []))
        if aberrations is not None:
            self.warnings += [w for w in aberrations.warnings if w not in self.warnings]
        record = self.headline()
        super().__init__(
            name=name,
            data=np.array([[_as_float(v) for v in record.values()]], dtype=np.float32),
            axis_labels=["Measurement", "Metric"],
            view_modes=[ViewMode("Table", (0, 1))],
            display_levels=None,
        )

    # -- the pieces --------------------------------------------------------- #
    @property
    def analysis(self):
        return self.run.analysis

    @property
    def mask(self) -> np.ndarray:
        return self.run.mask

    @property
    def reasons(self) -> list[str]:
        return self.run.reasons

    @property
    def unit(self) -> str:
        return self.run.unit

    def headline(self) -> dict:
        """The measurement in one row: counts, half-maximum widths (median and
        MAD of the selected beads), the Gaussian lateral width, the ratio to
        the diffraction limit and, when fitted, the aberration totals."""
        unit, stats = self.unit, self.summary["stats"]
        row = {"source": self.source_name, "beads_used": self.summary["n_selected"],
               "beads_found": self.summary["n_candidates"]}
        for ax in self.analysis.axes[::-1]:  # x, y(, z)
            s = stats.get(f"fwhm_{ax}_hm", {})
            row[f"fwhm_{ax}_{unit}"] = s.get("median")
            row[f"fwhm_{ax}_mad_{unit}"] = s.get("mad")
        row[f"fwhm_lat_gauss_{unit}"] = stats.get("fwhm_lat", {}).get("median")
        if self.analysis.ndim == 3:
            row[f"fwhm_z_gauss_{unit}"] = stats.get("fwhm_z", {}).get("median")
        for axis, ratio in self.summary.get("ratio_to_theory", {}).items():
            row[f"ratio_{axis}_to_theory"] = ratio
        if self.aberrations is not None:
            row["aberration_rms_nm"] = self.aberrations.rms_nm
            row["strehl"] = self.aberrations.strehl
        return row

    def bead_rows(self) -> list[dict]:
        """Every candidate, with its state (``selected`` or why not)."""
        from imswitch.improcess.analysis.bead_psf import reason_label

        analysis, unit = self.analysis, self.unit
        width_keys = _width_keys(analysis)
        rows = []
        for bead, chosen, reason in zip(analysis.beads, self.mask, self.reasons):
            row = {"id": bead["id"], "selected": bool(chosen), "state": reason_label(reason)}
            if analysis.projected:
                row["plane"] = bead.get("plane")
            for ax in analysis.axes:
                row[f"{ax}_px"] = bead.get(ax, bead.get(f"{ax}_det"))
            for key in width_keys:
                row[f"{key}_{unit}"] = bead.get(key)
            row.update(ellipticity=bead.get("ellipticity"), r2=bead.get("r2"))
            if analysis.ndim == 3:
                row["r2_z"] = bead.get("r2_z")
            row.update(amplitude=bead.get("amp"), background=bead.get("offset"))
            rows.append(row)
        return rows

    def summary_rows(self) -> list[dict]:
        """Statistics of the selected beads, one row per width, plus scalars."""
        from imswitch.improcess.analysis.bead_psf import reason_label

        summary, averaged, unit = self.summary, self.averaged, self.unit
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
        detection = self.analysis.expected_fwhm()
        if detection:
            rows.append({"metric": "detection scale FWHM lateral", "unit": unit, "value": detection[-1]})
            if len(detection) == 3:
                rows.append({"metric": "detection scale FWHM axial (z)", "unit": unit, "value": detection[0]})
        for axis, ratio in summary.get("ratio_to_theory", {}).items():
            rows.append({"metric": f"ratio to theory ({_AXIS_NAMES.get(axis, axis)})", "value": ratio})
        for key, value in self.focal.items():
            rows.append({"metric": f"focal surface {key}", "unit": _focal_unit(key, unit), "value": value})
        for key, value in self.trend.items():
            rows.append({"metric": f"lateral FWHM trend {key}", "unit": unit, "value": value})
        if self.aberrations is not None:
            rows += _aberration_rows(self.aberrations)
        return [{column: row.get(column) for column in SUMMARY_COLUMNS} for row in rows]

    def report(self, headline: bool = True) -> str:
        """A short plain-text report of the measurement.

        ``headline=False`` leaves out what the panel's card already shows in
        large type (the bead count, the half-maximum widths and the
        diffraction limit): the details beneath it."""
        return psf_report(self.summary, self.averaged, self.aberrations, count=headline, widths=headline)

    # -- shared result contract --------------------------------------------- #
    def table_records(self) -> list[dict]:
        return [self.headline()]

    def table_columns(self) -> list[str]:
        return list(self.headline())

    def display_layers(self) -> list[DisplayLayerSpec]:
        """The analysed image behind a marker per bead candidate."""
        image, labels, scales, unit = self._display_image()
        layers = [DisplayLayerSpec(
            name=f"{self.name} image", data=image, axis_labels=labels, axis_scales=scales,
            scale_unit=unit, display_levels=finite_range(image), colormap="gray",
            kind="image", role="context", component="image",
        )]
        beads = self.analysis.beads
        if not beads:
            return layers
        axes = self.analysis.axes
        points = np.array([[float(b.get(ax, b.get(f"{ax}_det"))) for ax in axes] for b in beads])
        tags = [str(b["id"]) if not r else f"{b['id']} {STATE_TAGS.get(r, r)}"
                for b, r in zip(beads, self.reasons)]
        lateral_px = float(np.mean(self.analysis.expected_sigma_px[-2:])) * 2.3548
        layers.append(DisplayLayerSpec(
            name=f"{self.name} beads", data=points, axis_labels=labels, axis_scales=scales,
            scale_unit=unit, kind="points", role="primary", component="beads",
            layer_kwargs={
                "size": max(4.0, 4.0 * lateral_px),
                "face_color": "transparent",
                "border_color": [STATE_COLORS.get(r, "#ff4d4d") for r in self.reasons],
                "border_width": 0.12,
                "border_width_is_relative": True,
                "out_of_slice_display": True,
                "features": {"label": tags, "state": [r or "selected" for r in self.reasons]},
                "text": {"string": "{label}", "size": 8, "color": "white", "anchor": "upper_left"},
                "opacity": 0.9,
            },
        ))
        return layers

    def _display_image(self):
        """``(image, labels, scales, unit)`` the bead coordinates refer to:
        the analysed stack, or for beads found on a projection, the projection."""
        data = np.asarray(self.run.data)
        if self.analysis.projected and data.ndim == 3:
            data = data.max(axis=0)
        labels = ["Z", "Y", "X"] if data.ndim == 3 else ["Y", "X"]
        if self.unit == "nm":
            return data, labels, [float(v) * 1e-3 for v in self.run.pixel_size[-data.ndim:]], "um"
        return data, labels, [1.0] * data.ndim, "px"

    def plot_payloads(self) -> list[PlotPayload]:
        unit = self.unit
        beads = self.analysis.beads
        fitted = [b for b in beads if "fwhm_lat" in b]
        chosen = [b for b, m in zip(beads, self.mask) if m]
        payloads = []
        for key, label in (("fwhm_lat", "lateral"), ("fwhm_z", "axial")):
            if not fitted or (key == "fwhm_z" and self.analysis.ndim != 3):
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
        fit = self.aberrations
        if fit is not None:
            rows = fit.rows()
            payloads.append(PlotPayload(
                title=f"Zernike aberrations (RMS {fit.rms_nm:.1f} nm, Strehl {fit.strehl:.2f})",
                x_label="Noll index", y_label="Coefficient (nm RMS)",
                series=[PlotSeries(name="coefficient", x=np.array([r["noll"] for r in rows], dtype=np.float64),
                                   y=np.array([r["coefficient_nm_rms"] for r in rows]), kind="scatter")],
                metadata=aberration_scalars(fit),
            ))
        return payloads

    # -- saving ------------------------------------------------------------- #
    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan, companion_json_path

        path = Path(path)
        if fmt == "csv":
            return SavePlan(path, fmt, (*self._csv_companions(path).values(), companion_json_path(path)))
        return SavePlan(path, fmt)

    def _csv_companions(self, path: Path) -> dict[str, Path]:
        """The summary (and Zernike) tables written next to the bead CSV."""
        from imswitch.improcess.model.save_protocol import strip_format_suffix

        stem = strip_format_suffix(Path(path).name)
        names = {"summary": f"{stem}_summary.csv"}
        if self.aberrations is not None:
            names["zernike"] = f"{stem}_zernike.csv"
        return {key: Path(path).with_name(name) for key, name in names.items()}

    def write_files(self, plan, document) -> None:
        from imswitch.improcess.model.save_protocol import embed_hdf5_path, write_companion_json

        if plan.fmt == "hdf5":
            self._save_hdf5(plan.primary)
            embed_hdf5_path(plan.primary, document)
        elif plan.fmt == "csv":
            _write_csv(plan.primary, self.bead_rows())
            companions = self._csv_companions(plan.primary)
            _write_csv(companions["summary"], self.summary_rows(), SUMMARY_COLUMNS)
            if "zernike" in companions:
                _write_csv(companions["zernike"], self.aberrations.rows(), ZERNIKE_COLUMNS)
            write_companion_json(plan.primary, document)
        else:
            raise ValueError(f"{type(self).__name__} supports HDF5 or CSV/TXT, got {plan.fmt!r}")

    def _save_hdf5(self, path: Path) -> None:
        with h5py.File(str(path), "w") as h5:
            attrs = {
                "source": self.source_name, "unit": self.unit, "pixel_size": list(self.run.pixel_size),
                "n_candidates": self.summary["n_candidates"], "n_selected": self.summary["n_selected"],
                "warnings": self.warnings, "notes": list(self.summary.get("notes", [])),
                "selection": self.summary.get("selection", {}), "params": self.params,
                "report": self.report(),
            }
            if self.summary.get("aberrations_skipped"):
                attrs["aberrations_skipped"] = self.summary["aberrations_skipped"]
            _write_attrs(h5, attrs)
            _write_table(h5.create_group("beads"), self.bead_rows())
            _write_table(h5.create_group("summary"), self.summary_rows(), SUMMARY_COLUMNS)
            if self.averaged is not None:
                dataset = h5.create_dataset("average_psf", data=np.asarray(self.averaged.image, dtype=np.float32))
                _write_attrs(dataset, {"n": self.averaged.n, "bead_ids": list(self.averaged.bead_ids),
                                       "fwhm": self.averaged.fwhm, "pixel_size": list(self.run.pixel_size),
                                       "unit": self.unit})
            fit = self.aberrations
            if fit is not None:
                group = h5.create_group("aberrations")
                _write_attrs(group, {**aberration_scalars(fit), "warnings": fit.warnings})
                _write_table(group.create_group("zernike"), fit.rows(), ZERNIKE_COLUMNS)
                group.create_dataset("wavefront_nm", data=fit.wavefront().astype(np.float32))
                group.create_dataset("data", data=np.asarray(fit.data, dtype=np.float32))
                group.create_dataset("model", data=np.asarray(fit.model, dtype=np.float32))


SUMMARY_COLUMNS = ["metric", "unit", "n", "mean", "std", "sem", "median", "mad", "averaged_psf", "theory", "value"]
ZERNIKE_COLUMNS = ["noll", "mode", "coefficient_nm_rms", "error_nm_rms", "milliwaves"]


def aberration_scalars(fit) -> dict:
    """The totals and fit quality of an aberration fit, flat."""
    scalars = {
        "rms_nm": fit.rms_nm, "strehl_marechal": fit.strehl, "r2": fit.r2,
        "n_beads": fit.n_beads, "wavelength_nm": fit.wavelength_nm,
        "illumination": getattr(fit, "illumination", "widefield"),
    }
    for name, pair in fit.pairs.items():
        scalars[f"{name}_magnitude_nm_rms"] = pair["magnitude_nm_rms"]
        scalars[f"{name}_angle_deg"] = pair["angle_deg"]
    if getattr(fit, "blur_nm", None) is not None:
        scalars["extra_blur_sigma_nm"] = fit.blur_nm
    for key, value in (getattr(fit, "sheet", None) or {}).items():
        scalars[f"light_sheet_{key}"] = value
    return scalars


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


def psf_report(summary: dict, averaged=None, aberrations=None, count: bool = True, widths: bool = True) -> str:
    """Plain-text report: counts, widths (both measures), aberrations, warnings.

    ``count`` / ``widths`` leave out the bead count and the half-maximum
    widths with the diffraction limit, for a panel that shows them already."""
    from imswitch.improcess.analysis.bead_psf import reason_label

    unit = summary["unit"]
    stats = summary["stats"]
    lines = []
    rejected = ", ".join(f"{n} {reason_label(r)}" for r, n in summary.get("rejected", {}).items())
    if count:
        lines.append(
            f"{summary['n_selected']} of {summary['n_candidates']} bead candidates selected"
            + (f" (rejected: {rejected})." if rejected else ".")
        )
    elif rejected:
        lines.append(f"Rejected: {rejected}.")

    def axes(suffix: str) -> str:
        parts = []
        for axis, name in (("x", "x"), ("y", "y"), ("z", "z")):
            s = stats.get(f"fwhm_{axis}{suffix}")
            if s and s.get("n"):
                spread = f" ± {_fmt_width(s['mad'], unit)}" if np.isfinite(s.get("mad", np.nan)) else ""
                parts.append(f"{name} {_fmt_width(s['median'], unit)}{spread}")
        return ", ".join(parts)

    if summary["n_selected"]:
        if widths:
            lines.append("FWHM, half maximum (median ± MAD): " + axes("_hm"))
        lines.append("FWHM, Gaussian fit: " + axes(""))
        if "fwhm_lat_corr" in stats:
            lines.append("FWHM, Gaussian, bead-corrected: " + axes("_corr"))
    if averaged is not None and averaged.fwhm:
        parts = [f"{ax} {_fmt_width(averaged.fwhm.get(f'fwhm_{ax}_hm'), unit)}"
                 for ax in ("x", "y", "z") if f"fwhm_{ax}_hm" in averaged.fwhm]
        lines.append(f"Averaged PSF ({averaged.n} beads), half maximum: " + ", ".join(parts))
    theory = summary.get("theory_fwhm_nm")
    if theory and widths:
        lines.append(
            f"Diffraction limit (widefield): lateral {_fmt_width(theory['lat'], 'nm')}"
            + (f", axial {_fmt_width(theory['z'], 'nm')}" if np.isfinite(theory.get("z", np.nan)) else "")
        )
    if aberrations is not None:
        lines.append(f"Aberrations: {aberrations.headline()}")
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


def averaged_psf_image(source_name: str, averaged, pixel_size, unit: str, suffix: str = "") -> ArrayProcessingResult:
    """The averaged bead as an ordinary image result (``Z, Y, X`` or ``Y, X``)."""
    image = np.asarray(averaged.image, dtype=np.float32)
    labels = ["Z", "Y", "X"] if image.ndim == 3 else ["Y", "X"]
    scale = {"nm": 1e-3, "px": 1.0}[unit]  # published in um when calibrated, like the inputs
    view_modes = (
        [ViewMode("XY", (0, 1, 2)), ViewMode("XZ", (1, 0, 2)), ViewMode("YZ", (2, 0, 1))]
        if image.ndim == 3 else None
    )
    return ArrayProcessingResult(
        name=f"{source_name} — averaged PSF{suffix}",
        data=image,
        axis_labels=labels,
        view_modes=view_modes,
        axis_scales=[float(v) * scale for v in pixel_size],
        scale_unit="um" if unit == "nm" else "px",
        display_levels=(float(np.nanmin(image)), float(np.nanmax(image))),
        metadata={"beads_averaged": averaged.n, "bead_ids": list(averaged.bead_ids),
                  "fwhm": {k: float(v) for k, v in averaged.fwhm.items()}, "unit": unit},
    )


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
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


def _write_attrs(node, attrs: dict) -> None:
    """Scalars as attributes, anything else as JSON text."""
    import json

    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str, np.integer, np.floating)):
            node.attrs[str(key)] = value
        else:
            node.attrs[str(key)] = json.dumps(value, default=_json_default, ensure_ascii=False)


def _columns(rows: list[dict], columns: list[str] | None) -> list[str]:
    if columns is not None:
        return list(columns)
    seen: dict[str, None] = {}
    for row in rows:
        seen.update(dict.fromkeys(row))
    return list(seen)


def _write_table(group, rows: list[dict], columns: list[str] | None = None) -> None:
    """One dataset per column: numbers as float64 (None -> NaN), else text."""
    group.attrs["row_count"] = len(rows)
    for column in _columns(rows, columns):
        values = [row.get(column) for row in rows]
        if all(isinstance(v, (int, float, np.integer, np.floating)) or v is None for v in values):
            group.create_dataset(column, data=np.array([_as_float(v) for v in values], dtype=np.float64))
        else:
            dtype = h5py.string_dtype(encoding="utf-8")
            group.create_dataset(column, data=np.array(["" if v is None else str(v) for v in values], dtype=dtype))


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    import csv

    columns = _columns(rows, columns)
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
