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
)


def _affine_from_layer(layer, ndim: int) -> tuple[float, ...]:
    """The layer's pixel→world transform for the displayed plane.

    Composed from napari's own transform rather than rebuilt from scale and
    translate by hand, so a rotation or shear cannot be silently dropped.
    """
    scale = tuple(float(v) for v in getattr(layer, "scale", ()) or ())
    translate = tuple(float(v) for v in getattr(layer, "translate", ()) or ())
    row_scale = scale[-2] if len(scale) >= 2 else 1.0
    col_scale = scale[-1] if len(scale) >= 1 else 1.0
    row_shift = translate[-2] if len(translate) >= 2 else 0.0
    col_shift = translate[-1] if len(translate) >= 1 else 0.0

    affine = getattr(layer, "affine", None)
    matrix = None
    for attr in ("affine_matrix", "matrix"):
        candidate = getattr(affine, attr, None)
        if candidate is not None:
            matrix = np.asarray(candidate, dtype=float)
            break
    if matrix is not None and matrix.ndim == 2 and matrix.shape[0] >= 3:
        # Take the 2x2 spatial block and its offset from the full transform.
        spatial = matrix[-3:-1, -3:-1]
        offset = matrix[-3:-1, -1]
        return (
            float(spatial[0, 0]) * row_scale, float(spatial[0, 1]), float(offset[0]) + row_shift,
            float(spatial[1, 0]), float(spatial[1, 1]) * col_scale, float(offset[1]) + col_shift,
            0.0, 0.0, 1.0,
        )

    if (row_scale, col_scale, row_shift, col_shift) == (1.0, 1.0, 0.0, 0.0):
        return IDENTITY_AFFINE
    return (row_scale, 0.0, row_shift, 0.0, col_scale, col_shift, 0.0, 0.0, 1.0)


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
        # No recorded provenance: derive something stable from what the layer
        # is, and mark it inferred so it can never claim certainty.
        layer_name = str(getattr(layer, "name", "layer"))
        space_uid = content_digest_uid("space", layer_name, shape[-2:])
        result_uid = result_uid or content_digest_uid("result", layer_name, shape)
        dataset_uid = dataset_uid or content_digest_uid("data", layer_name)
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


__all__ = ["frame_from_layer", "plane_position"]
