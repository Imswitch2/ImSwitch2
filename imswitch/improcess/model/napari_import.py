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


def transform_problems(snapshot: LayerSnapshot) -> list[str]:
    """Every way the layer's transform departs from a plain per-axis scale.

    Each component is checked on its own: a translated *and* rotated layer
    reports both, so a caller that can fold one in (translation) cannot be
    fooled into ignoring the other.
    """
    problems = []
    if any(abs(t) > _ATOL for t in snapshot.translate):
        problems.append(f"layer is translated by {snapshot.translate}")
    if snapshot.rotate is not None and snapshot.rotate.size:
        if not np.allclose(snapshot.rotate, np.eye(snapshot.rotate.shape[0]), atol=_ATOL):
            problems.append("layer is rotated")
    if snapshot.shear is not None and snapshot.shear.size:
        if not np.allclose(snapshot.shear, 0.0, atol=_ATOL):
            problems.append("layer is sheared")
    if snapshot.affine is not None and snapshot.affine.size:
        if not np.allclose(snapshot.affine, np.eye(snapshot.affine.shape[0]), atol=_ATOL):
            problems.append("layer has a non-identity affine")
    return problems


def identity_transform(snapshot: LayerSnapshot) -> tuple[bool, str]:
    """Whether the layer sits on its data's own index grid, scale aside."""
    problems = transform_problems(snapshot)
    if problems:
        return False, "; ".join(problems)
    return True, ""


#: Length units napari may put on a layer axis, in nanometres.
_LENGTH_NM = {
    "nm": 1.0, "nanometer": 1.0, "nanometre": 1.0, "nanometers": 1.0,
    "um": 1000.0, "µm": 1000.0, "micrometer": 1000.0, "micrometre": 1000.0, "micrometers": 1000.0,
    "micron": 1000.0, "microns": 1000.0,
    "mm": 1e6, "millimeter": 1e6, "millimetre": 1e6, "millimeters": 1e6,
    "m": 1e9, "meter": 1e9, "metre": 1e9, "meters": 1e9,
}
_UNIT_NAME = {1.0: "nm", 1000.0: "um", 1e6: "mm", 1e9: "m"}


def normalized_units(snapshot: LayerSnapshot, source_result) -> tuple[str, list[float]]:
    """One unit for every axis, with the scales converted to it.

    napari carries a unit *per axis*; ImProcess results carry one
    ``scale_unit`` for all of them. Length units that differ between axes
    (micrometre Y, nanometre X) are converted onto one of them, so the
    per-axis scales keep meaning what they meant. Axes whose units are not
    all lengths, or not all the same non-length unit, cannot be represented
    and are refused rather than silently relabelled.
    """
    scales = [float(v) for v in snapshot.scale]
    units = [str(u).strip().lower() for u in (snapshot.units or ())]
    if not units or all(not u or u == "none" for u in units):
        return _fallback_unit(snapshot, source_result), scales
    if len(units) != len(scales):
        raise NotImportable(f"the layer names {len(units)} axis units for {len(scales)} axes")
    factors = [_LENGTH_NM.get(u) for u in units]
    if all(f is not None for f in factors):
        target = factors[-1]
        unit = _UNIT_NAME.get(target)
        if unit is None:                      # a length we have no short name for
            target, unit = 1000.0, "um"
        return unit, [scale * factor / target for scale, factor in zip(scales, factors)]
    if len(set(units)) == 1:
        unit = units[0]
        return ("px" if unit in ("pixel", "pixels") else unit), scales
    raise NotImportable(
        f"the layer's axis units {tuple(snapshot.units)} are not all lengths and not all the same; "
        "set one unit per layer in napari before importing"
    )


def _fallback_unit(snapshot: LayerSnapshot, source_result) -> str:
    unit = str(snapshot.metadata.get("scale_unit") or "")
    if unit:
        return unit
    return str(getattr(source_result, "scale_unit", "px") or "px")


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
    unit, scales = normalized_units(snapshot, source_result)

    if layer_type == "shapes":
        rois = _rois_from_shapes(snapshot)
        return ImportedLayer(result=None, rois=tuple(rois), grid="n/a", reason="ROIs are not results")

    if layer_type == "points":
        coords = np.asarray(snapshot.data, dtype=float)
        if coords.ndim != 2:
            raise NotImportable("points data must be (N, D)")
        coords, baked = _bake_points_transform(snapshot, coords)
        result = PointsTableResult(
            name=label,
            coordinates=coords,
            properties=snapshot.properties,
            coordinate_scale=list(scales),
            scale_unit=unit,
            metadata={"layer_name": snapshot.name, "layer_type": "points", "transform_baked": baked},
        )
        record_import(
            result, plugin_name=plugin_name, widget_name=widget_name,
            layer_name=snapshot.name, layer_type="points", grid="n/a",
            source_result=source_result,
        )
        return ImportedLayer(result=result, grid="n/a", reason="points carry no pixel grid")

    grid, reason = grid_decision(snapshot, source_result, preserves_grid=preserves_grid)
    source_unit = str(getattr(source_result, "scale_unit", "") or "")
    if grid == "inherit" and source_unit and unit != source_unit:
        # Same numbers in a different unit is a different grid.
        grid, reason = "fresh", f"layer unit {unit!r} differs from the source's {source_unit!r}"
    axis_labels = list(snapshot.metadata.get("axis_labels") or [])
    if len(axis_labels) != snapshot.ndim:
        source_labels = list(getattr(source_result, "axis_labels", []) or [])
        axis_labels = source_labels if len(source_labels) == snapshot.ndim else _default_labels(snapshot.ndim)
    common = {
        "axis_labels": axis_labels,
        "axis_scales": list(scales),
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


def _bake_points_transform(snapshot: LayerSnapshot, coords: np.ndarray) -> tuple[np.ndarray, dict]:
    """Points in the layer's *data* frame with its translation folded in.

    A Points layer's coordinates are index-space numbers; the layer's
    ``scale`` and ``translate`` place them in the world. The table keeps the
    per-axis scale (``coordinate_scale``), so the only thing that would be
    lost is the translation: it is added here, in data units
    (``translate / scale``), so ``coords * scale`` is again the world
    position. A rotation, shear or extra affine cannot be folded into a
    per-axis scale; such a layer is refused rather than imported wrong.
    """
    scale = np.asarray(snapshot.scale, dtype=float)
    translate = np.asarray(snapshot.translate, dtype=float)
    if coords.shape[1] != len(scale) or len(translate) != len(scale):
        raise NotImportable(
            f"points are {coords.shape[1]}-D but the layer transform is {len(scale)}-D"
        )
    # Every component on its own: a translation (which can be folded in)
    # must never hide a rotation, shear or affine (which cannot).
    unfoldable = [p for p in transform_problems(snapshot) if "translated" not in p]
    if unfoldable:
        raise NotImportable(
            f"the points layer {'; '.join(unfoldable)}; that transform cannot be represented as a "
            "per-axis scale -- bake it into the coordinates in napari before importing"
        )
    if np.any(scale == 0):
        raise NotImportable("the points layer has a zero scale on some axis")
    baked = {"translate": translate.tolist(), "scale": scale.tolist()}
    if np.any(np.abs(translate) > _ATOL):
        coords = coords + translate / scale
    return coords, baked


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
    "normalized_units",
    "transform_problems",
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
