"""Labels -> points: one coordinate per labelled region, with its properties.

The measuring step of a smart pipeline. Every region of a label mask becomes
one row of a :class:`~imswitch.improcess.model.PointsResult`: its centroid
``(y, x)`` as the coordinate, ``label`` and ``area`` as properties, and
``mean_intensity`` / ``max_intensity`` when the intensity image is wired in
as the second input. The result carries the ``frame`` tag the user typed, so
the point can later be transformed into a stage frame and drive a move.

The numbers come from the ROI measurement registry
(:mod:`imswitch.improcess.analysis.roi_measurements`), one
:class:`~imswitch.improcess.analysis.roi_measurements.MeasurementContext`
per region, so a region's centroid and mean here are the same centroid and
mean the ROI table reports for the same pixels.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import ProcessingResult, result_kind
from imswitch.improcess.processors._axis_split import (
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

#: Measurement ids (``roi_measurements``) read for every region, and the
#: property column each one becomes.
_GEOMETRY = (("centroid_r_px", "y"), ("centroid_c_px", "x"), ("area_px", "area"))
_INTENSITY = (("mean", "mean_intensity"), ("max", "max_intensity"))


class RegionPropsProcessor(Processor):
    """Measure every region of a label mask into a points result."""

    name = "Region properties"
    id = "regionprops"
    category = "Measurement"
    #: The label mask first; an intensity image on the same grid, optionally,
    #: second. ``image`` is listed so a workflow may wire the image in.
    kinds = ("labels", "image")
    output_kind = "points"
    min_inputs = 1
    max_inputs = 2

    @classmethod
    def default_params(cls) -> dict:
        return {"frame": UNKNOWN_FRAME, "min_area": 1}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "frame", "text", UNKNOWN_FRAME, label="Frame",
                help="Frame of reference written on the points, e.g. detector:Camera",
            ),
            ParamField(
                "min_area", "int", 1, label="Min area", min=1, suffix="px",
                help="Regions with fewer pixels are left out",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        """A (Y, X) label plane or a (T, Y, X) stack, measured plane by plane."""
        return lambda result: len(shape_for_result(result)) in (2, 3)

    def check_inputs(self, results) -> tuple[bool, str]:
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        results = list(results or [])
        if result_kind(results[0]) != "labels":
            return False, (
                f"{self.name} measures a labels result first; "
                f"'{getattr(results[0], 'name', 'input 1')}' is {result_kind(results[0])}"
            )
        if len(results) == 2 and shape_for_result(results[0]) != shape_for_result(results[1]):
            return False, (
                f"{self.name}: the intensity image must have the shape of the labels, "
                f"{shape_for_result(results[0])} vs {shape_for_result(results[1])}"
            )
        return True, ""

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        frame_edit = QtWidgets.QLineEdit()
        frame_edit.setText(UNKNOWN_FRAME)
        frame_edit.setPlaceholderText("e.g. detector:Camera")
        frame_edit.setToolTip("Frame of reference written on the points, e.g. detector:Camera")
        layout.addRow("Frame:", frame_edit)

        min_area_spin = QtWidgets.QSpinBox()
        min_area_spin.setRange(1, 999999999)
        min_area_spin.setValue(1)
        min_area_spin.setSuffix(" px")
        min_area_spin.setToolTip("Regions with fewer pixels are left out")
        layout.addRow("Min area:", min_area_spin)

        def get_values():
            return {
                "frame": frame_edit.text().strip() or UNKNOWN_FRAME,
                "min_area": int(min_area_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or []) or [result]
        labels_result = results[0]
        if result_kind(labels_result) != "labels":
            raise ValueError(
                f"Region properties measures a labels result first, got "
                f"{result_kind(labels_result)} '{getattr(labels_result, 'name', '')}'"
            )
        image_result = results[1] if len(results) > 1 else None
        labels = np.asarray(labels_result.data)
        image = None if image_result is None else np.asarray(image_result.data)
        if labels.ndim not in (2, 3):
            raise ValueError(
                f"Region properties needs (Y, X) or (T, Y, X) labels, got shape {labels.shape}"
            )
        if image is not None and image.shape != labels.shape:
            raise ValueError(
                f"The intensity image must have the shape of the labels, "
                f"{image.shape} vs {labels.shape}"
            )
        min_area = int(params.get("min_area", 1))
        frame = str(params.get("frame") or UNKNOWN_FRAME)

        if labels.ndim == 2:
            columns = measure_labelled_regions(labels, image, min_area=min_area)
        else:
            # Plane by plane, each region tagged with the plane it was found on.
            per_plane = [
                measure_labelled_regions(
                    labels[t], None if image is None else image[t], min_area=min_area
                )
                for t in range(labels.shape[0])
            ]
            if not per_plane:
                per_plane = [measure_labelled_regions(labels[0:0, 0, :], None)]
            columns = {
                key: np.concatenate([plane[key] for plane in per_plane])
                for key in per_plane[0]
            }
            columns["t"] = np.concatenate([
                np.full(len(plane["label"]), t, dtype=np.int64)
                for t, plane in enumerate(per_plane)
            ])

        coordinates = np.column_stack([columns.pop("y"), columns.pop("x")])
        frame_index = _frame_index(labels_result, image_result)
        scales = list(axis_scales_for_result(labels_result))
        return PointsResult(
            f"{labels_result.name} (regions)",
            coordinates.reshape(-1, 2),
            frame=frame,
            frame_index=frame_index,
            axis_names=["y", "x"],
            properties=columns,
            coordinate_scale=[float(scales[-2]), float(scales[-1])] if len(scales) >= 2 else None,
            scale_unit=getattr(labels_result, "scale_unit", "px"),
            metadata={"operation": self.id, "min_area": min_area},
        )


def _frame_index(*results) -> int | None:
    """The source frame index the first of ``results`` records, if any."""
    for item in results:
        meta = getattr(item, "metadata", None) or {}
        value = meta.get("frame_index") if isinstance(meta, dict) else None
        if value is not None:
            return int(value)
    return None


def measure_labelled_regions(
    labels: np.ndarray,
    image: np.ndarray | None = None,
    *,
    min_area: int = 1,
) -> dict[str, np.ndarray]:
    """Per-region columns of a 2-D label plane, in increasing label order.

    Always ``y``, ``x`` (centroid, pixel units), ``label`` and ``area``;
    ``mean_intensity`` and ``max_intensity`` too when ``image`` is given.
    Each region is measured through
    :func:`~imswitch.improcess.analysis.roi_measurements.measure` on a
    context over the region's bounding box, as the ROI table would measure
    a mask ROI of the same pixels.
    """
    from scipy import ndimage

    from imswitch.imcommon.algorithms.roi import ROIRecord
    from imswitch.improcess.analysis.roi_measurements import MeasurementContext, measure

    labels = np.asarray(labels)
    if labels.ndim != 2:
        raise ValueError(f"Region properties measures a 2-D label plane, got shape {labels.shape}")
    if image is not None:
        image = np.asarray(image)
        if image.shape != labels.shape:
            raise ValueError("The intensity image must have the shape of the labels")
    wanted = _GEOMETRY + (_INTENSITY if image is not None else ())
    selection = tuple(measurement_id for measurement_id, _column in wanted)
    rows: dict[str, list] = {column: [] for _id, column in wanted}
    rows["label"] = []

    if labels.size and labels.max() > 0:
        slices = ndimage.find_objects(labels.astype(np.int64))
        for index, box in enumerate(slices):
            if box is None:
                continue
            label = index + 1
            local_mask = labels[box] == label
            area = int(local_mask.sum())
            if area < max(1, int(min_area)):
                continue
            local_image = (
                image[box].astype(np.float64) if image is not None
                else np.zeros(local_mask.shape, dtype=np.float64)
            )
            bounds = (box[0].start, box[0].stop, box[1].start, box[1].stop)
            roi = ROIRecord(name=f"region {label}", roi_type="rectangle", bounds=bounds)
            context = MeasurementContext(local_mask=local_mask, local_image=local_image, roi=roi)
            measured = measure(context, selection)
            for measurement_id, column in wanted:
                rows[column].append(measured[measurement_id])
            rows["label"].append(label)

    columns: dict[str, np.ndarray] = {
        "y": np.asarray(rows["y"], dtype=np.float64),
        "x": np.asarray(rows["x"], dtype=np.float64),
        "label": np.asarray(rows["label"], dtype=np.int64),
        "area": np.asarray(rows["area"], dtype=np.int64),
    }
    if image is not None:
        columns["mean_intensity"] = np.asarray(rows["mean_intensity"], dtype=np.float64)
        columns["max_intensity"] = np.asarray(rows["max_intensity"], dtype=np.float64)
    return columns


__all__ = ["RegionPropsProcessor", "measure_labelled_regions"]


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
