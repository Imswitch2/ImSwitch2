"""Taking a layer back from a napari plugin, on ImProcess's terms.

The endpoint lanes are one-way. This is the explicit, opt-in way back: the
user (or a verified adapter's output mapping) picks *one* layer a plugin
created and *the result it was derived from*, and the layer becomes a typed
result. Nothing is inferred from layer-added events, because a plugin can
add anything and the session owns only what ImProcess itself added.

Two rules are worth stating plainly, because getting them wrong corrupts
measurements later rather than failing now:

* **The imported result gets a fresh coordinate space** unless the adapter
  says the plugin preserves the grid *and* the layer's transform is the
  identity. ``ProcessingResult`` stores a scale and nothing else -- no
  translate, rotation, shear or affine -- so any other transform cannot be
  represented and must not be claimed. Two arrays of the same shape are not
  the same grid.
* **Points never become localizations here.** A Points layer is a table;
  emitter semantics need columns nobody has checked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.points_table_result import PointsTableResult
from imswitch.improcess.model.provenance import record_import

IMPORTABLE_LAYER_TYPES = ("image", "labels", "points", "shapes")

#: Absolute tolerance for "this transform is the identity".
_ATOL = 1e-9


class NotImportable(ValueError):
    """The layer has no honest result form."""


@dataclass(frozen=True)
class LayerSnapshot:
    """What matters about a napari layer, copied out so the model never holds one."""

    name: str
    layer_type: str
    data: Any
    ndim: int
    scale: tuple[float, ...]
    translate: tuple[float, ...]
    rotate: np.ndarray | None = None
    shear: np.ndarray | None = None
    affine: np.ndarray | None = None
    metadata: dict = field(default_factory=dict)
    properties: dict = field(default_factory=dict)
    shape_types: tuple[str, ...] = ()
    units: tuple[str, ...] | None = None


def snapshot_layer(layer) -> LayerSnapshot:
    """Copy a napari layer's transform, data and metadata into a snapshot."""
    layer_type = type(layer).__name__.lower()
    ndim = int(getattr(layer, "ndim", np.ndim(getattr(layer, "data", None))))
    affine = getattr(layer, "affine", None)
    matrix = getattr(affine, "affine_matrix", affine)
    properties = {}
    props = getattr(layer, "properties", None)
    if isinstance(props, dict):
        properties = {str(k): np.asarray(v) for k, v in props.items()}
    shape_types = tuple(str(s) for s in (getattr(layer, "shape_type", None) or ())) if layer_type == "shapes" else ()
    units = getattr(layer, "units", None)
    return LayerSnapshot(
        name=str(getattr(layer, "name", "") or layer_type),
        layer_type=layer_type,
        data=getattr(layer, "data", None),
        ndim=ndim,
        scale=tuple(float(v) for v in np.atleast_1d(getattr(layer, "scale", np.ones(ndim)))),
        translate=tuple(float(v) for v in np.atleast_1d(getattr(layer, "translate", np.zeros(ndim)))),
        rotate=_matrix(getattr(layer, "rotate", None)),
        shear=_matrix(getattr(layer, "shear", None)),
        affine=_matrix(matrix),
        metadata=dict(getattr(layer, "metadata", {}) or {}),
        properties=properties,
        shape_types=shape_types,
        units=tuple(str(u) for u in units) if units else None,
    )


def _matrix(value) -> np.ndarray | None:
    if value is None:
        return None
    try:
        return np.asarray(value, dtype=float)
    except Exception:
        return None


def identity_transform(snapshot: LayerSnapshot) -> tuple[bool, str]:
    """Whether the layer sits on its data's own index grid, scale aside."""
    if any(abs(t) > _ATOL for t in snapshot.translate):
        return False, f"layer is translated by {snapshot.translate}"
    if snapshot.rotate is not None and snapshot.rotate.size:
        if not np.allclose(snapshot.rotate, np.eye(snapshot.rotate.shape[0]), atol=_ATOL):
            return False, "layer is rotated"
    if snapshot.shear is not None and snapshot.shear.size:
        if not np.allclose(snapshot.shear, 0.0, atol=_ATOL):
            return False, "layer is sheared"
    if snapshot.affine is not None and snapshot.affine.size:
        if not np.allclose(snapshot.affine, np.eye(snapshot.affine.shape[0]), atol=_ATOL):
            return False, "layer has a non-identity affine"
    return True, ""


def grid_decision(
    snapshot: LayerSnapshot,
    source_result,
    *,
    preserves_grid: bool,
    source_scale: tuple[float, ...] | None = None,
    source_axis_labels: list[str] | None = None,
) -> tuple[str, str]:
    """``("inherit" | "fresh", reason)`` for an Image/Labels import."""
    if not preserves_grid:
        return "fresh", "the endpoint does not declare that the plugin preserves the grid"
    if source_result is None:
        return "fresh", "no source result was selected"
    same, reason = identity_transform(snapshot)
    if not same:
        return "fresh", reason
    expected_scale = tuple(float(v) for v in (source_scale or getattr(source_result, "axis_scales", []) or []))
    if expected_scale and len(expected_scale) != len(snapshot.scale):
        return "fresh", "layer and source have different dimensionality"
    if expected_scale and not np.allclose(snapshot.scale, expected_scale, rtol=1e-9, atol=_ATOL):
        return "fresh", f"layer scale {snapshot.scale} differs from the source's {expected_scale}"
    source_shape = tuple(np.shape(getattr(source_result, "data", None)) or ())
    data_shape = tuple(np.shape(snapshot.data) or ())
    if source_shape and data_shape and source_shape != data_shape:
        return "fresh", f"layer shape {data_shape} differs from the source's {source_shape}"
    layer_uid = str(snapshot.metadata.get("result_uid") or snapshot.metadata.get("source_result_uid") or "")
    source_uid = str(getattr(source_result, "result_uid", "") or "")
    if layer_uid and source_uid and layer_uid != source_uid:
        return "fresh", "the layer says it came from a different result"
    axis_labels = list(source_axis_labels or getattr(source_result, "axis_labels", []) or [])
    layer_labels = list(snapshot.metadata.get("axis_labels") or [])
    if layer_labels and axis_labels and layer_labels != axis_labels:
        return "fresh", "axis order differs from the source"
    return "inherit", "identity transform on the source's grid"


@dataclass(frozen=True)
class ImportedLayer:
    """What an import produced: a result, or ROIs, plus the grid decision."""

    result: Any | None
    rois: tuple = ()
    grid: str = "fresh"
    reason: str = ""


def import_layer(
    snapshot: LayerSnapshot,
    source_result,
    *,
    plugin_name: str,
    widget_name: str | None = None,
    preserves_grid: bool = False,
    name: str | None = None,
) -> ImportedLayer:
    """Turn one plugin-created layer into a typed result or a set of ROIs."""
    layer_type = str(snapshot.layer_type).lower()
    if layer_type not in IMPORTABLE_LAYER_TYPES:
        raise NotImportable(f"a {snapshot.layer_type} layer cannot be imported as a result")
    label = name or snapshot.name
    unit = _unit_from(snapshot, source_result)

    if layer_type == "shapes":
        rois = _rois_from_shapes(snapshot)
        return ImportedLayer(result=None, rois=tuple(rois), grid="n/a", reason="ROIs are not results")

    if layer_type == "points":
        coords = np.asarray(snapshot.data, dtype=float)
        if coords.ndim != 2:
            raise NotImportable("points data must be (N, D)")
        result = PointsTableResult(
            name=label,
            coordinates=coords,
            properties=snapshot.properties,
            coordinate_scale=list(snapshot.scale),
            scale_unit=unit,
            metadata={"layer_name": snapshot.name, "layer_type": "points"},
        )
        record_import(
            result, plugin_name=plugin_name, widget_name=widget_name,
            layer_name=snapshot.name, layer_type="points", grid="n/a",
            source_result=source_result,
        )
        return ImportedLayer(result=result, grid="n/a", reason="points carry no pixel grid")

    grid, reason = grid_decision(snapshot, source_result, preserves_grid=preserves_grid)
    axis_labels = list(snapshot.metadata.get("axis_labels") or [])
    if len(axis_labels) != snapshot.ndim:
        source_labels = list(getattr(source_result, "axis_labels", []) or [])
        axis_labels = source_labels if len(source_labels) == snapshot.ndim else _default_labels(snapshot.ndim)
    common = {
        "axis_labels": axis_labels,
        "axis_scales": list(snapshot.scale),
        "scale_unit": unit,
        "metadata": {"layer_name": snapshot.name, "layer_type": layer_type, "grid": grid},
    }
    if layer_type == "labels":
        result = LabelsResult(label, np.asarray(snapshot.data), **common)
    else:
        result = ArrayProcessingResult(label, np.asarray(snapshot.data), **common)

    if grid == "inherit":
        result.adopt_identity_from(source_result, same_grid=True)
    elif source_result is not None:
        result.adopt_identity_from(source_result, same_grid=False)
    record_import(
        result, plugin_name=plugin_name, widget_name=widget_name,
        layer_name=snapshot.name, layer_type=layer_type, grid=grid,
        source_result=source_result,
    )
    return ImportedLayer(result=result, grid=grid, reason=reason)


def _rois_from_shapes(snapshot: LayerSnapshot):
    from imswitch.improcess.analysis.roi_manager import roi_from_shape

    shapes = list(snapshot.data or [])
    types = list(snapshot.shape_types) or ["polygon"] * len(shapes)
    rois = []
    for index, (vertices, shape_type) in enumerate(zip(shapes, types)):
        try:
            rois.append(roi_from_shape(
                np.asarray(vertices, dtype=float),
                shape_type=str(shape_type),
                name=f"{snapshot.name} {index + 1}",
                source=f"napari:{snapshot.name}",
            ))
        except Exception as exc:
            raise NotImportable(f"shape {index + 1} ({shape_type}) could not become an ROI: {exc}") from exc
    return rois


def _unit_from(snapshot: LayerSnapshot, source_result) -> str:
    if snapshot.units:
        first = str(snapshot.units[-1]).lower()
        if first.startswith("micro"):
            return "um"
        if first.startswith("nano"):
            return "nm"
        if first.startswith("milli"):
            return "mm"
    unit = str(snapshot.metadata.get("scale_unit") or "")
    if unit:
        return unit
    return str(getattr(source_result, "scale_unit", "px") or "px")


def _default_labels(ndim: int) -> list[str]:
    base = ["Z", "Y", "X"]
    return base[-ndim:] if ndim <= 3 else [f"D{i}" for i in range(ndim - 2)] + ["Y", "X"]


__all__ = [
    "IMPORTABLE_LAYER_TYPES",
    "ImportedLayer",
    "LayerSnapshot",
    "NotImportable",
    "grid_decision",
    "identity_transform",
    "import_layer",
    "snapshot_layer",
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
