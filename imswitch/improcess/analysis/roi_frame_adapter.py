"""Read a :class:`SpatialFrame` off a napari layer.

The frame type itself is pure and lives in ``imcommon`` — it is shared, and
``imcommon`` must never import an app package or a viewer.  This module is the
one napari-aware side of that contract, and it lives here because knowing what
a napari layer is, is an ImProcess concern.

Everything is read from **one layer, in one call**: scale, transform, unit,
axis labels and provenance are written onto a layer together by the render
path, and reading half of them from a layer and the other half from whatever
result the panel thinks is current is how a measurement ends up calibrated
with one image's pixel size and another's units.
"""

from __future__ import annotations

import numpy as np

from imswitch.imcommon.algorithms.spatial_frame import (
    IDENTITY_AFFINE,
    AxisDescriptor,
    SpatialFrame,
    content_digest_uid,
    mint_uid,
)


def data_to_world(layer, point) -> np.ndarray:
    """A (row, col) data coordinate mapped into world space.

    Uses napari's own ``data_to_world``, which composes scale, translate,
    rotate, shear and affine in the right order. Reconstructing that by hand
    from ``scale`` and ``translate`` — as this module first did — silently
    drops rotation and shear, and a rotated layer is exactly the case where an
    ROI lands somewhere other than where it was drawn.
    """
    point = np.asarray(point, dtype=float)
    ndim = int(getattr(layer, "ndim", len(point)))
    full = np.zeros(ndim, dtype=float)
    full[-len(point):] = point
    return np.asarray(layer.data_to_world(full), dtype=float)[-len(point):]


def world_to_data(layer, point) -> np.ndarray:
    """The inverse of :func:`data_to_world`, for turning drawn shapes back
    into pixel coordinates."""
    point = np.asarray(point, dtype=float)
    ndim = int(getattr(layer, "ndim", len(point)))
    full = np.zeros(ndim, dtype=float)
    full[-len(point):] = point
    return np.asarray(layer.world_to_data(full), dtype=float)[-len(point):]


def _affine_from_layer(layer, ndim: int) -> tuple[float, ...]:
    """The layer's pixel→world transform for the displayed plane, as a 3x3.

    Derived by probing napari's own mapping at three points rather than
    reaching into transform internals: whatever napari composes — scale,
    translate, rotate, shear, affine — the probe sees the result, so nothing
    can be dropped by rebuilding the composition incorrectly.
    """
    try:
        origin = data_to_world(layer, (0.0, 0.0))
        along_row = data_to_world(layer, (1.0, 0.0)) - origin
        along_col = data_to_world(layer, (0.0, 1.0)) - origin
    except Exception:
        return IDENTITY_AFFINE

    affine = (
        float(along_row[0]), float(along_col[0]), float(origin[0]),
        float(along_row[1]), float(along_col[1]), float(origin[1]),
        0.0, 0.0, 1.0,
    )
    if np.allclose(affine, IDENTITY_AFFINE, atol=1e-12):
        return IDENTITY_AFFINE
    return affine


#: Metadata key under which a layer's fallback identity is cached.
_UNKNOWN_IDENTITY_KEY = "_roi_unknown_identity"


def _unknown_identity(layer) -> tuple[str, str, str]:
    """Identity for a layer that carries no recorded provenance.

    Deliberately **minted per layer, not derived from what the layer looks
    like**. Hashing the name and shape seemed reasonable and was actively
    dangerous: two unrelated results both called "Reconstruction" at 512x512 —
    the overwhelmingly common case — would hash alike and be declared
    ``pixel-compatible``, so an ROI drawn on one would happily measure the
    other. Rejecting that is the entire purpose of the compatibility ladder.

    Minting instead means two unknown layers never match (they are
    ``incompatible``, which is the honest answer), while the *same* layer keeps
    its identity for as long as it exists, because the uid is cached on it. It
    is still marked ``derived``, so it can never claim an exact match.
    """
    metadata = getattr(layer, "metadata", None)
    if isinstance(metadata, dict):
        cached = metadata.get(_UNKNOWN_IDENTITY_KEY)
        if cached:
            return tuple(cached)  # type: ignore[return-value]

    identity = (mint_uid("space"), mint_uid("result"), mint_uid("data"))
    if isinstance(metadata, dict):
        metadata[_UNKNOWN_IDENTITY_KEY] = identity
    return identity


def frame_from_layer(layer, viewer=None) -> SpatialFrame | None:
    """The plane ``layer`` currently shows, or None when it is not an image.

    Provenance comes from the layer's metadata when the render path put it
    there. A layer without it still yields a usable frame — measurements work,
    they just cannot claim an exact match against anything else, which is
    exactly what an unknown provenance should mean.
    """
    data = getattr(layer, "data", None)
    if data is None:
        return None
    shape = tuple(int(v) for v in np.shape(data))
    if len(shape) < 2:
        return None

    metadata = dict(getattr(layer, "metadata", {}) or {})
    labels = [str(v) for v in (metadata.get("axis_labels") or [])]
    if len(labels) != len(shape):
        # Fall back to the viewer's labels, then to positional names.
        try:
            labels = [str(v) for v in viewer.dims.axis_labels][-len(shape):]
        except Exception:
            labels = []
        if len(labels) != len(shape):
            labels = [f"D{i}" for i in range(len(shape) - 2)] + ["Y", "X"]

    scale = tuple(float(v) for v in getattr(layer, "scale", ()) or ())
    unit = str(metadata.get("scale_unit", "px") or "px")

    recorded_axes = metadata.get("axes")
    if recorded_axes:
        axes = tuple(
            AxisDescriptor(
                label=str(item.get("label", labels[index] if index < len(labels) else index)),
                size=int(item.get("size", shape[index] if index < len(shape) else 1)),
                scale=float(item.get("scale", 1.0)),
                unit=str(item.get("unit", unit)),
            )
            for index, item in enumerate(recorded_axes)
        )
    else:
        axes = tuple(
            AxisDescriptor(
                label=labels[index],
                size=int(shape[index]),
                scale=float(scale[index]) if index < len(scale) else 1.0,
                unit=unit,
            )
            for index in range(len(shape))
        )

    plane_axes = tuple(metadata.get("plane_axes") or labels[-2:])
    identity_kind = str(metadata.get("identity_kind", "") or "")
    result_uid = str(metadata.get("result_uid", "") or "")
    dataset_uid = str(metadata.get("dataset_uid", "") or "")
    space_uid = str(metadata.get("coordinate_space_uid", "") or "")

    if not space_uid:
        space_uid, result_uid, dataset_uid = _unknown_identity(layer)
        identity_kind = "derived"

    return SpatialFrame(
        coordinate_space_uid=space_uid,
        result_uid=result_uid,
        dataset_uid=dataset_uid,
        plane_axes=(str(plane_axes[0]), str(plane_axes[1])),
        axes=axes,
        shape=(shape[-2], shape[-1]),
        affine=_affine_from_layer(layer, len(shape)),
        unit=unit,
        component=metadata.get("component"),
        view_mode=metadata.get("view_mode"),
        lineage=tuple(str(v) for v in (metadata.get("lineage") or ())),
        identity_kind=identity_kind or "minted",  # type: ignore[arg-type]
    )


def plane_position(viewer, frame: SpatialFrame) -> tuple[tuple[str, int], ...]:
    """The axis-labelled slice position currently shown, non-displayed axes only.

    Labelled rather than positional because axis order is view-mode dependent:
    matching ``Z=12`` by index would silently follow a transposition onto a
    different axis.
    """
    try:
        step = tuple(int(v) for v in viewer.dims.current_step)
    except Exception:
        return ()
    displayed = set(frame.plane_axes)
    position = []
    for index, axis in enumerate(frame.axes):
        if axis.label in displayed:
            continue
        if index < len(step):
            position.append((axis.label, int(step[index])))
    return tuple(position)


def frame_from_result(result) -> SpatialFrame | None:
    """The plane a `ProcessingResult` would show, without rendering it.

    Measuring one ROI set across several results has to compare frames for
    results that were never on screen, so the frame has to come from the result
    itself. Built from the same fields the render path writes into a layer's
    metadata, so a result compared here and the same result compared after
    being displayed give the same verdict.
    """
    data = getattr(result, "data", None)
    if data is None:
        return None
    shape = tuple(int(v) for v in np.shape(data))
    if len(shape) < 2:
        return None
    labels = [str(v) for v in (getattr(result, "axis_labels", None) or [])]
    if len(labels) != len(shape):
        labels = [f"D{i}" for i in range(len(shape) - 2)] + ["Y", "X"]
    scales = list(getattr(result, "axis_scales", None) or [1.0] * len(shape))
    unit = str(getattr(result, "scale_unit", "px") or "px")

    axes = tuple(
        AxisDescriptor(
            label=labels[index],
            size=int(shape[index]),
            scale=float(scales[index]) if index < len(scales) else 1.0,
            unit=unit,
        )
        for index in range(len(shape))
    )
    space_uid = str(getattr(result, "coordinate_space_uid", "") or "")
    result_uid = str(getattr(result, "result_uid", "") or "")
    dataset_uid = str(getattr(result, "dataset_uid", "") or "")
    if not space_uid:
        # A result with no recorded identity gets one derived from its
        # content, never a fresh one: two views of the same result must not
        # look like two different pixel grids.
        space_uid = content_digest_uid("space", result_uid, labels, shape, scales, unit)
        return SpatialFrame(
            coordinate_space_uid=space_uid,
            result_uid=result_uid or space_uid,
            dataset_uid=dataset_uid or space_uid,
            plane_axes=(labels[-2], labels[-1]),
            axes=axes,
            shape=(shape[-2], shape[-1]),
            unit=unit,
            identity_kind="derived",
        )
    return SpatialFrame(
        coordinate_space_uid=space_uid,
        result_uid=result_uid,
        dataset_uid=dataset_uid,
        plane_axes=(labels[-2], labels[-1]),
        axes=axes,
        shape=(shape[-2], shape[-1]),
        unit=unit,
        lineage=tuple(str(v) for v in (getattr(result, "lineage", ()) or ())),
        identity_kind=str(getattr(result, "identity_kind", "minted") or "minted"),
    )


def plane_scales(frame: SpatialFrame | None) -> tuple[float, float, str]:
    """``(row_scale, col_scale, unit)`` for the plane a frame describes.

    Read off the two displayed axes by *label*, not by position: the displayed
    pair is view-mode dependent, so taking the last two entries would calibrate
    a YZ view with the Y and X scales.

    A frame that carries no calibration — or an axis with a non-positive scale,
    which no real calibration has — reports the pixel domain, so calibrated
    columns stay honestly equal to the pixel ones instead of being scaled by a
    number nobody supplied.
    """
    if frame is None:
        return 1.0, 1.0, "px"
    scales = []
    for label in frame.plane_axes:
        axis = frame.axis(str(label))
        scale = float(axis.scale) if axis is not None else 1.0
        scales.append(scale if scale > 0 else 1.0)
    unit = frame.unit or "px"
    if scales == [1.0, 1.0]:
        # An uncalibrated frame: saying "px" here is what keeps a calibrated
        # column from claiming micrometres it never had.
        unit = "px"
    return scales[0], scales[1], unit


__all__ = [
    "frame_from_layer",
    "frame_from_result",
    "plane_position",
    "plane_scales",
]
