"""One result for a bead FWHM run: the bead table, the image with the beads marked, two graphs.

The table (and ``coordinates``) has one row per kept bead -- its position in
the image's own axes, FWHM in x, y and their mean, SNR and fit quality. The
viewer shows the searched image with a square on every kept bead's fit window
and a second colour on the candidates that were rejected
(``display_layers``). For a stack, the Graph panel offers the mean FWHM and
the mean SNR of each plane's beads (``plot_payloads``). Empty points layers
are never handed to the viewer.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from imswitch.improcess.analysis.bead_fwhm import BeadAnalysis
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.points_table_result import PointsTableResult
from imswitch.improcess.model.result import DisplayLayerSpec

BEAD_COLOR = "#00e5ff"
REJECTED_COLOR = "#ff6a00"

_MICROMETRES = {"um", "µm", "μm", "micron", "microns"}
_PIXELS = {"", "px", "pixel", "pixels"}


def fwhm_unit(scale_unit: str | None, scale_y: float, scale_x: float) -> tuple[str, float, float]:
    """``(unit, factor_y, factor_x)`` turning pixel FWHM into the reported unit.

    Micrometres are reported in nm, where bead widths read naturally; an
    uncalibrated image stays in px.
    """
    unit = str(scale_unit or "").strip()
    if unit.lower() in _PIXELS:
        return "px", 1.0, 1.0
    factor = 1.0
    if unit.lower() in _MICROMETRES:
        unit, factor = "nm", 1000.0
    return unit, float(scale_y) * factor, float(scale_x) * factor


def plane_label(leading_labels: list[str]) -> str:
    """What the x axis of the per-plane graphs counts."""
    if not leading_labels:
        return "Plane"
    if len(leading_labels) == 1:
        return f"{leading_labels[0]} slice"
    return f"Plane ({' × '.join(leading_labels)})"


class BeadFwhmResult(PointsTableResult):
    """Kept beads as a points table, with the image and per-plane graphs."""

    def __init__(
        self,
        name: str,
        analysis: BeadAnalysis,
        *,
        image,
        image_axis_labels: list[str],
        image_axis_scales: list[float] | None = None,
        image_scale_unit: str = "px",
        params: dict[str, Any] | None = None,
    ):
        self.analysis = analysis
        self.image = image
        self.image_axis_labels = list(image_axis_labels)
        ndim = len(self.image_axis_labels)
        self.image_axis_scales = [float(v) for v in (image_axis_scales or [1.0] * ndim)]
        self.image_scale_unit = str(image_scale_unit or "px")
        self.leading_shape = tuple(int(n) for n in np.shape(image)[:-2])
        self.plane_label = plane_label(self.image_axis_labels[:-2])
        self.params = dict(params or {})
        self.fwhm_unit, factor_y, factor_x = fwhm_unit(
            self.image_scale_unit, self.image_axis_scales[-2], self.image_axis_scales[-1],
        )
        self._factor_y, self._factor_x = factor_y, factor_x

        fits = analysis.fits
        unit = self.fwhm_unit
        fwhm_x = np.array([fit.fwhm_x_px * factor_x for fit in fits], dtype=np.float64)
        fwhm_y = np.array([fit.fwhm_y_px * factor_y for fit in fits], dtype=np.float64)
        properties = {
            "plane": np.array([fit.plane for fit in fits], dtype=np.int64),
            f"fwhm_{unit}": 0.5 * (fwhm_x + fwhm_y),
            f"fwhm_x_{unit}": fwhm_x,
            f"fwhm_y_{unit}": fwhm_y,
            "snr": np.array([fit.snr for fit in fits], dtype=np.float64),
            "amplitude": np.array([fit.amplitude for fit in fits], dtype=np.float64),
            "background": np.array([fit.background for fit in fits], dtype=np.float64),
            "residual_rms": np.array([fit.residual_rms for fit in fits], dtype=np.float64),
            "r2": np.array([fit.r2 for fit in fits], dtype=np.float64),
        }
        super().__init__(
            name,
            self._coordinates([(fit.plane, fit.y, fit.x) for fit in fits]),
            axis_names=self.image_axis_labels,
            properties=properties,
            coordinate_scale=self.image_axis_scales,
            scale_unit=self.image_scale_unit,
            metadata=self.summary(),
        )

    # -- numbers ---------------------------------------------------------------------------
    def _coordinates(self, points) -> np.ndarray:
        """``(plane, y, x)`` triples as rows in the image's own axes."""
        ndim = len(self.leading_shape) + 2
        rows = []
        for plane, y, x in points:
            leading = np.unravel_index(int(plane), self.leading_shape) if self.leading_shape else ()
            rows.append([*(float(i) for i in leading), float(y), float(x)])
        return np.asarray(rows, dtype=np.float64).reshape(len(rows), ndim)

    def mean_fwhm(self) -> np.ndarray:
        """Each plane's mean bead FWHM in :attr:`fwhm_unit`; NaN where no bead was kept."""
        fy, fx = self._factor_y, self._factor_x
        return self.analysis.per_plane(lambda fit: 0.5 * (fit.fwhm_x_px * fx + fit.fwhm_y_px * fy))

    def mean_snr(self) -> np.ndarray:
        return self.analysis.per_plane(lambda fit: fit.snr)

    def summary(self) -> dict[str, Any]:
        fits = self.analysis.fits
        fwhm = [0.5 * (f.fwhm_x_px * self._factor_x + f.fwhm_y_px * self._factor_y) for f in fits]
        snr = [f.snr for f in fits if math.isfinite(f.snr)]
        return {
            "planes": self.analysis.n_planes,
            "beads": len(fits),
            "planes_with_beads": int(np.count_nonzero(self.analysis.counts())),
            "fwhm_unit": self.fwhm_unit,
            "mean_fwhm": float(np.mean(fwhm)) if fwhm else math.nan,
            "mean_snr": float(np.mean(snr)) if snr else math.nan,
            "rejected": self.analysis.rejection_counts(),
            "params": dict(self.params),
        }

    # -- viewer ----------------------------------------------------------------------------
    def display_layers(self) -> list[DisplayLayerSpec]:
        image = self.image
        if len(np.shape(image)) < 2 or np.size(image) == 0:
            return []
        labels, scales, unit = self.image_axis_labels, self.image_axis_scales, self.image_scale_unit
        layers = [DisplayLayerSpec(
            name=f"{self.name} image", data=image, axis_labels=labels, axis_scales=scales,
            scale_unit=unit, display_levels=finite_range(np.asarray(image)), colormap="gray",
            kind="image", role="context", component="image",
        )]
        rejected = self._coordinates([(r.plane, r.y, r.x) for r in self.analysis.rejected])
        for data, component, color, role in (
            (self.coordinates, "beads", BEAD_COLOR, "primary"),
            (rejected, "rejected", REJECTED_COLOR, "overlay"),
        ):
            if len(data) == 0:
                continue  # napari cannot draw an empty, styled points layer
            kwargs: dict[str, Any] = {
                "symbol": "square",
                "size": float(self.analysis.params.window),
                "face_color": "transparent",
                "border_color": color,
                "border_width": 0.08,
                "border_width_is_relative": True,
                "opacity": 0.9,
            }
            if component == "rejected":
                kwargs["features"] = {"reason": [r.reason for r in self.analysis.rejected]}
            layers.append(DisplayLayerSpec(
                name=f"{self.name} {component}", data=data, axis_labels=labels,
                axis_scales=scales, scale_unit=unit, kind="points", role=role,
                component=component, layer_kwargs=kwargs,
            ))
        return layers

    def display_layer_data(self) -> list[np.ndarray]:
        return [np.asarray(layer.data) for layer in self.display_layers()]

    # -- graphs ----------------------------------------------------------------------------
    def plot_payloads(self) -> list[PlotPayload]:
        if self.analysis.n_planes < 2:
            return []
        x = np.arange(self.analysis.n_planes, dtype=np.float64)
        counts = self.analysis.counts()
        payloads = []
        for title, y_label, series, values in (
            ("Mean bead FWHM", f"FWHM ({self.fwhm_unit})", "mean FWHM", self.mean_fwhm()),
            ("Mean bead SNR", "SNR (amplitude / residual RMS)", "mean SNR", self.mean_snr()),
        ):
            finite = np.isfinite(values)
            if not finite.any():
                continue
            payloads.append(PlotPayload(
                title=f"{title} vs {self.plane_label}",
                x_label=self.plane_label,
                y_label=y_label,
                series=[PlotSeries(name=series, x=x, y=values, kind="line")],
                metadata={
                    "beads": int(counts.sum()),
                    "planes_with_beads": int(np.count_nonzero(counts)),
                    "mean": float(np.mean(values[finite])),
                },
            ))
        return payloads


__all__ = ["BEAD_COLOR", "REJECTED_COLOR", "BeadFwhmResult", "fwhm_unit", "plane_label"]
