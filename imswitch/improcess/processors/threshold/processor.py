"""Plain threshold + connected components: an image becomes labels.

The first step of a smart pipeline (detector frame -> threshold ->
regionprops -> pick -> move). Deliberately smaller than ``segmentation``:
one threshold rule, one minimum area, no smoothing or morphology, and it
runs over a ``(T, Y, X)`` stack plane by plane so a time series of detector
frames labels every frame.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.analysis.segmentation import otsu_threshold
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.processors.label_morphology import LabelsResult

THRESHOLD_METHODS = ("otsu", "absolute")


class ThresholdProcessor(Processor):
    """Threshold an image (or each plane of a stack) into labelled components."""

    name = "Threshold"
    id = "threshold"
    category = "Segmentation"
    kinds = ("image",)
    output_kind = "labels"
    # The mask is pixel-for-pixel aligned with the image it came from.
    preserves_grid = True
    accepts_roi = True
    roi_modes = ("mask", "crop")

    @classmethod
    def default_params(cls) -> dict:
        return {"method": "otsu", "value": 0.0, "min_area": 1, "invert": False}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "method", "select", "otsu", label="Method", options=THRESHOLD_METHODS,
                help="otsu picks the threshold per plane; absolute uses the value below",
            ),
            ParamField(
                "value", "float", 0.0, label="Value", decimals=6,
                help="Threshold for the absolute method, in image units",
            ),
            ParamField(
                "min_area", "int", 1, label="Min area", min=1, suffix="px",
                help="Connected components smaller than this are dropped",
            ),
            ParamField(
                "invert", "bool", False, label="Invert",
                help="Keep pixels below the threshold instead of above it",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """A (Y, X) plane or a (T, Y, X) stack, thresholded plane by plane."""
        return lambda result: len(shape_for_result(result)) in (2, 3)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        method_combo = QtWidgets.QComboBox()
        method_combo.addItems(list(THRESHOLD_METHODS))
        method_combo.setToolTip("otsu picks the threshold per plane; absolute uses the value below")
        layout.addRow("Method:", method_combo)

        value_spin = QtWidgets.QDoubleSpinBox()
        value_spin.setDecimals(6)
        value_spin.setRange(-1e12, 1e12)
        value_spin.setValue(0.0)
        value_spin.setToolTip("Threshold for the absolute method, in image units")
        layout.addRow("Value:", value_spin)

        min_area_spin = QtWidgets.QSpinBox()
        min_area_spin.setRange(1, 999999999)
        min_area_spin.setValue(1)
        min_area_spin.setSuffix(" px")
        min_area_spin.setToolTip("Connected components smaller than this are dropped")
        layout.addRow("Min area:", min_area_spin)

        invert_check = QtWidgets.QCheckBox("Invert")
        invert_check.setToolTip("Keep pixels below the threshold instead of above it")
        layout.addRow("", invert_check)

        def get_values():
            return {
                "method": method_combo.currentText(),
                "value": float(value_spin.value()),
                "min_area": int(min_area_spin.value()),
                "invert": bool(invert_check.isChecked()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        data = np.asarray(result.data)
        if data.ndim not in (2, 3):
            raise ValueError(
                f"Threshold needs a (Y, X) image or a (T, Y, X) stack, got shape {data.shape}"
            )
        method = str(params.get("method", "otsu"))
        value = float(params.get("value", 0.0))
        min_area = int(params.get("min_area", 1))
        invert = bool(params.get("invert", False))

        if data.ndim == 2:
            labels, threshold = threshold_labels(
                data, method=method, value=value, min_area=min_area, invert=invert
            )
            thresholds = [threshold]
        else:
            planes = []
            thresholds = []
            for plane in data:
                plane_labels, threshold = threshold_labels(
                    plane, method=method, value=value, min_area=min_area, invert=invert
                )
                planes.append(plane_labels)
                thresholds.append(threshold)
            labels = (
                np.stack(planes, axis=0) if planes else np.zeros(data.shape, dtype=np.int32)
            )

        metadata = {
            "operation": self.id,
            "method": method,
            "threshold": thresholds[0] if len(thresholds) == 1 else list(thresholds),
            "min_area": min_area,
            "invert": invert,
        }
        # A detector frame's index rides along so a point measured on the
        # labels can still say which frame it came from.
        source_meta = getattr(result, "metadata", None) or {}
        if source_meta.get("frame_index") is not None:
            metadata["frame_index"] = int(source_meta["frame_index"])

        return LabelsResult(
            name=f"{result.name} (threshold)",
            data=labels,
            axis_labels=list(axis_labels_for_result(result)),
            display_levels=(0.0, float(max(1, int(labels.max()) if labels.size else 1))),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata=metadata,
        ).adopt_identity_from(result, same_grid=True)


def threshold_labels(
    image: np.ndarray,
    *,
    method: str = "otsu",
    value: float = 0.0,
    min_area: int = 1,
    invert: bool = False,
) -> tuple[np.ndarray, float]:
    """``(labels, threshold)`` for one 2-D plane.

    Pixels above the threshold (or at/below it, inverted) form the
    foreground; 8-connected components are numbered 1..N in scan order after
    components with fewer than ``min_area`` pixels are dropped. Non-finite
    pixels are never foreground.
    """
    from scipy import ndimage

    plane = np.asarray(image, dtype=np.float64)
    if plane.ndim != 2:
        raise ValueError(f"Threshold works on a 2-D plane, got shape {plane.shape}")
    if method == "otsu":
        threshold = float(otsu_threshold(plane))
    elif method == "absolute":
        threshold = float(value)
    else:
        raise ValueError(
            f"Unknown threshold method {method!r}; expected one of {list(THRESHOLD_METHODS)}"
        )
    finite = np.isfinite(plane)
    mask = (plane <= threshold) if invert else (plane > threshold)
    mask &= finite

    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    labels = np.asarray(labels, dtype=np.int32)
    if count and min_area > 1:
        areas = np.bincount(labels.ravel(), minlength=count + 1)
        keep = areas >= min_area
        keep[0] = False
        remap = np.zeros(count + 1, dtype=np.int32)
        remap[keep] = np.arange(1, int(keep.sum()) + 1, dtype=np.int32)
        labels = remap[labels]
    return labels, threshold


__all__ = ["THRESHOLD_METHODS", "ThresholdProcessor", "threshold_labels"]


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
