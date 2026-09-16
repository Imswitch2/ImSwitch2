"""Results as napari layers, by semantic kind.

A napari plugin sees layers, not results. This is the one place that says
which layers a result becomes, so the endpoint code, the viewer and any
future writer agree on it. The mapping is by ``kind`` and explicit: an image
is an image, labels are labels, localizations are **points built from the
real table**, and a table or a curve is not a layer at all. The old
temptation -- "fall back to ``result.data`` whatever it is" -- would have
sent the low-resolution histogram *preview* of a localization result to a
SMLM plugin as if it were data, and a metrics table as if it were an image.

The output is napari's own ``LayerData`` contract: ``(data, kwargs,
layer_type)`` tuples, which ``viewer.add_layer`` / ``viewer._add_layer_from_data``
accept directly and readers return. Nothing here imports napari.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.improcess.model.result import result_kind

#: Kinds that have a napari layer representation at all.
LAYERABLE_KINDS = ("image", "composite", "rgb", "labels", "localization")

#: ``scale_unit`` values -> pint-style unit names napari understands.
_UNITS = {"um": "micrometer", "µm": "micrometer", "nm": "nanometer", "mm": "millimeter", "m": "meter"}

#: ImProcess registers a few colormaps of its own on its viewer ("grayclip"
#: clips saturated pixels red). A plugin, a reader, or a detached viewer has
#: never heard of them, so layers leaving ImProcess use the portable name.
_PORTABLE_COLORMAPS = {"grayclip": "gray"}


def portable_colormap(name) -> str:
    name = str(name or "gray")
    return _PORTABLE_COLORMAPS.get(name, name)

LayerData = tuple[Any, dict, str]


class NotLayerable(ValueError):
    """The result has no honest napari representation."""


def layer_units(scale_unit: str | None, ndim: int) -> tuple[str, ...] | None:
    unit = _UNITS.get(str(scale_unit or "").strip())
    return (unit,) * ndim if unit else None


def _base_metadata(result, *, session_uid: str | None) -> dict:
    from imswitch.improcess.model.provenance import graph_of

    metadata = {
        "result_uid": str(getattr(result, "result_uid", "") or ""),
        "result_name": str(getattr(result, "name", "") or ""),
        "result_kind": result_kind(result),
        "coordinate_space_uid": str(getattr(result, "coordinate_space_uid", "") or ""),
    }
    if session_uid:
        metadata["endpoint_session_uid"] = str(session_uid)
    graph = graph_of(result)
    if graph is not None:
        metadata["provenance"] = graph
    return metadata


def result_to_layer_data(
    result, *, session_uid: str | None = None, include_preview: bool = False
) -> list[LayerData]:
    """Every layer ``result`` becomes, in display order.

    Raises :class:`NotLayerable` for kinds that have no layer form. The
    ``session_uid`` is stamped into every layer's metadata so an endpoint
    session can tell its layers from the viewer's own.
    """
    kind = result_kind(result)
    if kind not in LAYERABLE_KINDS:
        raise NotLayerable(f"a {kind!r} result has no napari layer representation")
    if kind == "localization":
        layers = [localization_points(result, session_uid=session_uid)]
        if include_preview:
            layers.insert(0, localization_preview(result, session_uid=session_uid))
        return layers
    return _image_like_layers(result, kind, session_uid=session_uid)


def _image_like_layers(result, kind, *, session_uid) -> list[LayerData]:
    specs = result.display_layers() if hasattr(result, "display_layers") else []
    if specs and hasattr(result, "applyDisplayLayerSettings"):
        specs = result.applyDisplayLayerSettings(specs)
    if specs:
        return [_layer_from_spec(result, spec, session_uid=session_uid) for spec in specs]

    data = result.data
    ndim = int(getattr(data, "ndim", np.ndim(data)))
    if ndim < 2:
        raise NotLayerable(
            f"{getattr(result, 'name', 'result')!r} has {ndim} axis/axes "
            f"(shape {tuple(getattr(data, 'shape', ()) or ())}); an image layer needs at least two"
        )
    scales = list(getattr(result, "axis_scales", None) or [1.0] * ndim)
    unit = getattr(result, "scale_unit", "px")
    kwargs: dict[str, Any] = {
        "name": str(getattr(result, "name", "") or "result"),
        "scale": tuple(float(value) for value in scales[:ndim]) if len(scales) >= ndim else None,
        "metadata": {
            **_base_metadata(result, session_uid=session_uid),
            "axis_labels": list(getattr(result, "axis_labels", []) or []),
            "scale_unit": str(unit),
        },
    }
    units = layer_units(unit, ndim)
    if units:
        kwargs["units"] = units
    if kwargs["scale"] is None:
        del kwargs["scale"]
    if kind == "labels":
        return [(np.asarray(data).astype(np.int32, copy=False), kwargs, "labels")]
    if kind == "rgb":
        kwargs["rgb"] = True
    else:
        kwargs["colormap"] = portable_colormap(getattr(result, "display_colormap", "gray"))
        levels = getattr(result, "display_levels", None)
        if levels is not None:
            kwargs["contrast_limits"] = (float(levels[0]), float(levels[1]))
    return [(data, kwargs, "image")]


def _layer_from_spec(result, spec, *, session_uid) -> LayerData:
    kind = str(getattr(spec, "kind", "image") or "image")
    data = spec.data
    ndim = int(getattr(data, "ndim", np.ndim(data)))
    if kind in ("image", "labels") and ndim < 2:
        raise NotLayerable(
            f"layer {spec.name!r} of {getattr(result, 'name', 'result')!r} has {ndim} axis/axes "
            f"(shape {tuple(getattr(data, 'shape', ()) or ())}); an image layer needs at least two"
        )
    scales = spec.axis_scales if spec.axis_scales is not None else [1.0] * ndim
    metadata = {
        **_base_metadata(result, session_uid=session_uid),
        **dict(spec.metadata or {}),
        "axis_labels": list(spec.axis_labels),
        "scale_unit": str(spec.scale_unit),
        "component": str(getattr(spec, "component", None) or spec.name),
        "role": str(getattr(spec, "role", "primary")),
    }
    if getattr(spec, "coordinate_space_uid", None):
        metadata["coordinate_space_uid"] = str(spec.coordinate_space_uid)
    kwargs: dict[str, Any] = {
        "name": str(spec.name),
        "metadata": metadata,
        "visible": bool(spec.visible),
        **dict(spec.layer_kwargs or {}),
    }
    units = layer_units(spec.scale_unit, ndim)
    if kind == "points":
        if ndim == 2 and data.shape[1] == len(scales):
            kwargs["scale"] = tuple(float(v) for v in scales)
            if units:
                kwargs["units"] = layer_units(spec.scale_unit, int(data.shape[1]))
        return (data, kwargs, "points")
    kwargs["scale"] = tuple(float(v) for v in scales)
    if units:
        kwargs["units"] = units
    if kind == "labels":
        return (np.asarray(data).astype(np.int32, copy=False), kwargs, "labels")
    if kind == "shapes":
        return (data, kwargs, "shapes")
    kwargs["colormap"] = portable_colormap(spec.colormap)
    if spec.rgb:
        kwargs["rgb"] = True
    if spec.display_levels is not None:
        kwargs["contrast_limits"] = (float(spec.display_levels[0]), float(spec.display_levels[1]))
    return (data, kwargs, "image")


# --------------------------------------------------------------------------
# localizations
# --------------------------------------------------------------------------

#: Columns that are coordinates, not properties.
_COORDINATE_COLUMNS = ("x_nm", "y_nm", "z_nm")


def localization_transform(result) -> dict:
    """How the point coordinates relate to the table, for the layer metadata.

    Points are in **index units of the source pixel grid** (``nm / pixel_nm``),
    with the layer ``scale`` restoring world nanometres, so they land on the
    same world coordinates as a rendered image of the same result. napari
    orders axes ``(z, y, x)``, so a 3D table becomes ``(z/z_scale, y/px, x/px)``.
    """
    pixel_nm = float(result.pixel_size_nm)
    three_d = str(getattr(result, "dims", "2D")) == "3D"
    z_step = getattr(result, "z_step_nm", None)
    z_scale = float(z_step) if z_step else pixel_nm
    transform = {
        "axes": ["z", "y", "x"] if three_d else ["y", "x"],
        "index_from_nm": "coordinate_nm / scale_nm per axis",
        "pixel_size_nm": pixel_nm,
        "scale_nm": [z_scale, pixel_nm, pixel_nm] if three_d else [pixel_nm, pixel_nm],
        "world_unit": "nanometer",
    }
    if three_d:
        transform["z_scale_nm"] = z_scale
        transform["z_scale_assumed"] = not bool(z_step)
    return transform


def localization_points(result, *, session_uid: str | None = None) -> LayerData:
    """The localization table as a Points layer, built from ``locs``."""
    locs = result.locs
    transform = localization_transform(result)
    scale = transform["scale_nm"]
    three_d = len(scale) == 3
    y = np.asarray(locs["y_nm"], dtype=np.float64) / scale[-2]
    x = np.asarray(locs["x_nm"], dtype=np.float64) / scale[-1]
    if three_d:
        z = np.asarray(locs["z_nm"], dtype=np.float64) / scale[0]
        coords = np.column_stack([z, y, x])
    else:
        coords = np.column_stack([y, x])

    names = list(getattr(locs.dtype, "names", None) or [])
    properties = {
        name: np.asarray(locs[name])
        for name in names
        if name not in _COORDINATE_COLUMNS
    }
    metadata = {
        **_base_metadata(result, session_uid=session_uid),
        "coordinate_transform": transform,
        "count": int(len(locs)),
    }
    if transform.get("z_scale_assumed"):
        metadata["z_scale_assumed"] = True
    kwargs: dict[str, Any] = {
        "name": str(getattr(result, "name", "") or "localizations"),
        "scale": tuple(float(v) for v in scale),
        "units": ("nanometer",) * len(scale),
        "metadata": metadata,
        "size": 1.0,
    }
    if properties:
        kwargs["properties"] = properties
    return (coords, kwargs, "points")


def localization_preview(result, *, session_uid: str | None = None) -> LayerData:
    """The histogram preview as a context image, placed where the points are.

    The preview is binned from the table's *minimum* coordinate, not from the
    origin, so without a ``translate`` it would sit at the wrong place under
    the points. Both are in world nanometres.
    """
    extent = getattr(result, "preview_extent_nm", None) or (0.0, 0.0, 0.0, 0.0)
    x_min, _x_max, y_min, _y_max = (float(v) for v in extent)
    bin_nm = float(getattr(result, "preview_pixel_size_nm", None) or result.pixel_size_nm)
    kwargs = {
        "name": f"{getattr(result, 'name', 'localizations')} (preview)",
        "scale": (bin_nm, bin_nm),
        "translate": (y_min, x_min),
        "units": ("nanometer", "nanometer"),
        "colormap": "gray",
        "metadata": {
            **_base_metadata(result, session_uid=session_uid),
            "role": "context",
            "preview_extent_nm": [x_min, _x_max, y_min, _y_max],
        },
    }
    return (np.asarray(result.data), kwargs, "image")


__all__ = [
    "LAYERABLE_KINDS",
    "LayerData",
    "NotLayerable",
    "layer_units",
    "portable_colormap",
    "localization_points",
    "localization_preview",
    "localization_transform",
    "result_to_layer_data",
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
