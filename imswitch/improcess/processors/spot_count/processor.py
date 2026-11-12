"""Spot count: how many diffraction-limited spots each frame of a stack holds.

The detection half of the SMLM localizer (Picasso net gradient, see
:mod:`imswitch.improcess.analysis.spot_count`) run frame by frame, without
the Gaussian fit: an event-trigger experiment wants *how many* and *where*,
not sub-pixel positions. Two outputs:

* ``counts`` -- a curve, one sample per frame along the stack's leading axis
  (its label and scale are kept, so a calibrated T axis plots in seconds);
* ``spots`` -- every detection as a points result, ``(y, x)`` in pixels with
  the frame index as property ``t`` and the net gradient, smoothed intensity
  and local contrast as further properties.

The same detection settings drive the tiling processor (``tile-spots``), so
a threshold tuned on a time stack finds the same spots in an overview.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.spot_count import (
    THRESHOLD_MODES,
    SpotParams,
    find_spots_in_stack,
)
from imswitch.improcess.model.curve_result import CurveResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import ANY_KIND, ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput

#: Default threshold per mode: SMLM's net-gradient default, and a multiple of
#: the frame's robust net-gradient noise.
DEFAULT_THRESHOLDS = {"absolute": 500.0, "relative": 6.0}


def spot_default_params() -> dict:
    """The detection settings both spot processors start from."""
    return {
        "threshold": DEFAULT_THRESHOLDS["absolute"],
        "threshold_mode": "absolute",
        "sigma": 1.0,
        "roi": 7,
        "reject_edges": True,
    }


def spot_param_fields() -> tuple:
    """Field specs of :func:`spot_default_params`, as the panel shows them."""
    return (
        ParamField(
            "threshold", "float", DEFAULT_THRESHOLDS["absolute"], label="Threshold",
            help="absolute: minimum net gradient, as in the SMLM localizer. "
                 "relative: multiples of the frame's robust net-gradient noise "
                 "(median + k * 1.4826 * MAD); about 6 is a start.",
            min=0, group="Detection",
        ),
        ParamField(
            "threshold_mode", "select", "absolute", label="Threshold mode",
            options=THRESHOLD_MODES,
            help="absolute is comparable across frames; relative adapts to each "
                 "frame's noise (tiles of different brightness)",
            group="Detection",
        ),
        ParamField(
            "sigma", "float", 1.0, label="Smoothing sigma", min=0, max=20, suffix="px",
            help="Gaussian pre-smoothing width in pixels", group="Detection",
        ),
        ParamField(
            "roi", "int", 7, label="Box size", min=3, max=31, suffix="px",
            help="Net-gradient window (odd); also the closest two spots can be "
                 "and still count twice, and the margin kept from the frame edge",
            group="Detection",
        ),
        ParamField(
            "reject_edges", "bool", True, label="Reject edges",
            help="Keep a candidate only where the image is a round peak, not "
                 "an edge or ridge; drops the rim of large bright structures",
            group="Detection",
        ),
    )


def add_spot_param_rows(layout: QtWidgets.QFormLayout):
    """Add the detection rows to ``layout``; return a ``get_values`` callable."""
    defaults = spot_default_params()

    mode_combo = QtWidgets.QComboBox()
    mode_combo.addItems(list(THRESHOLD_MODES))
    mode_combo.setCurrentText(defaults["threshold_mode"])
    mode_combo.setToolTip(
        "absolute is comparable across frames; relative adapts to each frame's noise"
    )

    threshold_spin = QtWidgets.QDoubleSpinBox()
    threshold_spin.setDecimals(2)
    threshold_spin.setRange(0.0, 1e12)
    threshold_spin.setValue(defaults["threshold"])
    threshold_spin.setToolTip(
        "absolute: minimum net gradient (as in the SMLM localizer). "
        "relative: multiples of the frame's robust net-gradient noise."
    )

    def on_mode(mode: str) -> None:
        # Swap to the other mode's default when the value is still the old
        # mode's default: 500 noise-sigmas would find nothing.
        other = "relative" if mode == "absolute" else "absolute"
        if threshold_spin.value() == DEFAULT_THRESHOLDS[other]:
            threshold_spin.setValue(DEFAULT_THRESHOLDS[mode])

    mode_combo.currentTextChanged.connect(on_mode)

    sigma_spin = QtWidgets.QDoubleSpinBox()
    sigma_spin.setRange(0.0, 20.0)
    sigma_spin.setSingleStep(0.1)
    sigma_spin.setValue(defaults["sigma"])
    sigma_spin.setSuffix(" px")
    sigma_spin.setToolTip("Gaussian pre-smoothing width in pixels")

    roi_spin = QtWidgets.QSpinBox()
    roi_spin.setRange(3, 31)
    roi_spin.setSingleStep(2)
    roi_spin.setValue(defaults["roi"])
    roi_spin.setSuffix(" px")
    roi_spin.setToolTip("Net-gradient window (odd); also the minimum spot separation")

    edges_check = QtWidgets.QCheckBox("Reject edges")
    edges_check.setChecked(defaults["reject_edges"])
    edges_check.setToolTip(
        "Keep a candidate only where the image is a round peak, not an edge or ridge"
    )

    layout.addRow("Threshold:", threshold_spin)
    layout.addRow("Threshold mode:", mode_combo)
    layout.addRow("Smoothing sigma:", sigma_spin)
    layout.addRow("Box size:", roi_spin)
    layout.addRow("", edges_check)

    def get_values() -> dict:
        return {
            "threshold": float(threshold_spin.value()),
            "threshold_mode": mode_combo.currentText(),
            "sigma": float(sigma_spin.value()),
            "roi": int(roi_spin.value()),
            "reject_edges": bool(edges_check.isChecked()),
        }

    return get_values


def spots_points(name: str, spots: dict, *, frame_property: str, scales_yx=None,
                 scale_unit: str = "px", frame: str = UNKNOWN_FRAME,
                 metadata: dict | None = None) -> PointsResult:
    """A spot table as a points result: ``(y, x)`` plus the other columns."""
    properties = {
        frame_property: np.asarray(spots["frame"], dtype=np.int64),
        "net_gradient": spots["net_gradient"],
        "intensity": spots["intensity"],
        "contrast": spots["contrast"],
    }
    return PointsResult(
        name,
        np.column_stack([spots["y"], spots["x"]]).reshape(-1, 2),
        frame=frame,
        axis_names=["y", "x"],
        properties=properties,
        coordinate_scale=list(scales_yx) if scales_yx is not None else None,
        scale_unit=scale_unit,
        metadata=metadata,
    )


class SpotCountProcessor(Processor):
    """Count diffraction-limited spots in every frame of a stack."""

    name = "Spot count"
    id = "spot-count"
    category = "Measurement"
    run_label = "Count spots"
    kinds = ("image",)
    #: A curve and a points table: two kinds, so the declaration is ``any``.
    output_kind = ANY_KIND

    @classmethod
    def default_params(cls) -> dict:
        return spot_default_params()

    @classmethod
    def param_spec(cls) -> tuple:
        return spot_param_fields()

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        return OutputSpec(ports=("counts", "spots"), kind=self.output_kind)

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """A (Y, X) frame or an (N, Y, X) stack (T, tiles, ...)."""
        return lambda result: len(shape_for_result(result)) in (2, 3)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        widget.get_values = add_spot_param_rows(layout)
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        data = np.asarray(result.data)
        if data.ndim not in (2, 3):
            raise ValueError(
                f"Spot count needs a (Y, X) frame or an (N, Y, X) stack, got shape {data.shape}"
            )
        settings = SpotParams.from_params(params)
        spots, counts = find_spots_in_stack(data, settings)

        labels = list(axis_labels_for_result(result))
        scales = list(axis_scales_for_result(result))
        axis_label = labels[0] if data.ndim == 3 else "Frame"
        axis_scale = scales[0] if data.ndim == 3 else 1.0
        settings_meta = {
            "operation": self.id,
            "threshold": settings.threshold,
            "threshold_mode": settings.threshold_mode,
            "sigma": settings.sigma,
            "roi": settings.roi,
            "reject_edges": settings.reject_edges,
        }
        curve = CurveResult(
            f"{result.name} spot count",
            counts.astype(np.float64),
            [axis_label],
            axis_scales=[axis_scale],
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={**settings_meta, "total_spots": int(counts.sum())},
        )
        points = spots_points(
            f"{result.name} spots",
            spots,
            frame_property=str(axis_label).lower() if data.ndim == 3 else "t",
            scales_yx=scales[-2:],
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata=settings_meta,
        )
        return ProcessorOutput((curve, points), keys=("counts", "spots"))


__all__ = [
    "DEFAULT_THRESHOLDS",
    "SpotCountProcessor",
    "add_spot_param_rows",
    "spot_default_params",
    "spot_param_fields",
    "spots_points",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
