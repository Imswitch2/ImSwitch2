"""Conversion between an ImProcess result and an ImageJ1 hyperstack.

The Fiji host process only ever sees a canonical ``(T, Z, C, Y, X)`` array plus
a calibration dict. Every decision about axes, units and what cannot be carried
across is made here, in both directions, so it is testable without Java and
nothing is lost silently: whatever ImageJ cannot represent (or we cannot read
back) ends up in a ``notes`` list that travels with the result.

Facts this module is built on (verified against ImageJ1 in the P0 prototype):

* a hyperstack is X, Y, C, Z, T with stack index ``c + z*C + t*C*Z``; we use
  numpy order ``(T, Z, C, Y, X)``, which is the same memory order;
* pixel types are 8-bit, 16-bit and 32-bit float only;
* ImageJ normalises units (``um`` comes back as ``µm``, uncalibrated is
  ``pixel``, the default time unit is ``sec``) and a frame interval of 0 means
  "not set";
* ImageJ *copies* ImagePlus properties onto duplicates, crops and scaled
  copies, and drops them on Z Project, so ``imswitch.result_uid`` is a lineage
  **hint**, never an identity;
* a crop does not update the calibration origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from imswitch.improcess.fiji.protocol import IMAGEJ_DTYPE_NAMES, FijiImageDescription
from imswitch.improcess.model.napari_import import (
    ImportedLayer,
    LayerSnapshot,
    NotImportable,
    import_layer,
)
from imswitch.improcess.model.result import result_kind

#: Canonical axis order handed to the host: ImageJ's X, Y, C, Z, T reversed
#: into numpy order, so the array's memory order is ImageJ's stack order.
IMAGEJ_AXES = ("T", "Z", "C", "Y", "X")

#: ImageJ1 has 8-bit, 16-bit and 32-bit float processors only. Anything else
#: would need a cast, which would change the numbers, so it is refused.
IMAGEJ_DTYPES = tuple(np.dtype(name) for name in IMAGEJ_DTYPE_NAMES)

PROP_RESULT_UID = "imswitch.result_uid"
PROP_AXES = "imswitch.axes"

#: Spatial unit spellings (ImageJ's and ours) -> ImProcess spelling.
_SPATIAL_UNITS = {
    "µm": "um", "μm": "um", "um": "um", "micron": "um", "microns": "um",
    "nm": "nm", "mm": "mm",
    "pixel": "px", "pixels": "px", "px": "px", "": "px",
}
#: Size of each known length unit in nanometres, for converting one axis' scale
#: onto another's unit. Pixels are deliberately absent: they are not a length.
_LENGTH_NM = {"nm": 1.0, "um": 1000.0, "mm": 1e6}

#: Time unit spellings -> seconds per unit. Unlisted spellings are unknown and
#: reported, never guessed.
_TIME_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 1e-3, "msec": 1e-3,
    "min": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0,
    "µs": 1e-6, "μs": 1e-6, "us": 1e-6,
}

#: ImProcess (``um``) -> the spelling ImageJ shows and expects.
_IMAGEJ_UNIT = {"um": "µm", "px": "pixel"}

#: Lazy data is materialised when it cannot be viewed in canonical order
#: without a copy; the note reports the size so the cost is visible.
_BYTES_PER_MB = 2 ** 20


class NotSendable(ValueError):
    """The result has no honest ImageJ hyperstack form."""


# Import-side refusals reuse the napari importer's exception: callers that
# already handle "this layer has no honest result form" handle Fiji's too.
NotImportableFromFiji = NotImportable


# --------------------------------------------------------------------------
# ImProcess -> Fiji
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SendPlan:
    """Everything the host needs to open one result as an ImagePlus."""

    data: Any                      # (T, Z, C, Y, X); a view for numpy input
    source_axes: tuple[str, ...]   # the result's labels, upper-cased
    title: str
    calibration: dict
    channel_names: list
    properties: dict
    notes: list = field(default_factory=list)


def plan_send(result, *, title: str | None = None) -> SendPlan:
    kind = result_kind(result)
    if kind not in ("image", "composite"):
        raise NotSendable(
            f"a {kind} result cannot be sent to Fiji yet; only images and composites can"
        )
    data = result.data
    labels = [str(label).upper() for label in (result.axis_labels or [])]
    raw_labels = [str(label) for label in (result.axis_labels or [])]
    ndim = int(getattr(data, "ndim", np.ndim(data)))
    if len(labels) != ndim:
        raise NotSendable(f"the result has {ndim} data axes but {len(labels)} axis labels {raw_labels}")
    bad = [raw for raw, up in zip(raw_labels, labels) if up not in IMAGEJ_AXES]
    if bad:
        raise NotSendable(
            f"axes {bad} have no ImageJ equivalent (ImageJ has {', '.join(IMAGEJ_AXES)}); "
            "project or split them before sending"
        )
    duplicates = sorted({a for a in labels if labels.count(a) > 1})
    if duplicates:
        raise NotSendable(f"axes {duplicates} appear more than once; project or split them before sending")
    if "Y" not in labels or "X" not in labels:
        raise NotSendable(f"the result needs both Y and X axes, it has {raw_labels}")
    dtype = np.dtype(getattr(data, "dtype", None))
    if dtype not in IMAGEJ_DTYPES:
        raise NotSendable(
            f"dtype {dtype} cannot be sent: ImageJ1 holds uint8, uint16 and float32 only "
            "(convert the result first; nothing is cast silently)"
        )

    notes: list[str] = []
    order = [labels.index(a) for a in IMAGEJ_AXES if a in labels]
    missing = [i for i, a in enumerate(IMAGEJ_AXES) if a not in labels]
    if isinstance(data, np.ndarray):
        view = data.transpose(order)
        for index in missing:                      # ascending: each lands at its canonical slot
            view = np.expand_dims(view, index)
    elif hasattr(data, "transpose") and not missing:
        view = data.transpose(order)
    else:
        size_mb = int(np.prod(np.shape(data), dtype=np.int64)) * dtype.itemsize / _BYTES_PER_MB
        notes.append(f"materialised lazy data ({size_mb:.0f} MB)")
        view = np.asarray(data).transpose(order)
        for index in missing:
            view = np.expand_dims(view, index)

    scales = {a: float(s) for a, s in zip(labels, list(result.axis_scales or []))}
    unit = str(getattr(result, "scale_unit", "") or "px")
    unit = _SPATIAL_UNITS.get(unit.strip().lower(), unit)
    calibration = {
        "pixel_width": scales.get("X"),
        "pixel_height": scales.get("Y"),
        # Absent when the axis is absent: the host leaves ImageJ's default
        # rather than us inventing a value.
        "pixel_depth": scales.get("Z"),
        "unit": _IMAGEJ_UNIT.get(unit, unit),
        # ImProcess keeps the T axis scale in seconds.
        "frame_interval_s": scales.get("T"),
        "time_unit": "s",
    }
    for axis, key in (("X", "pixel_width"), ("Y", "pixel_height")):
        if calibration[key] is None:
            notes.append(f"the {axis} axis has no scale; ImageJ's default applies")

    channel_names = _channel_names(result, view.shape[IMAGEJ_AXES.index("C")], notes)
    return SendPlan(
        data=view,
        source_axes=tuple(labels),
        title=str(title or getattr(result, "name", "") or "ImProcess result"),
        calibration=calibration,
        channel_names=channel_names,
        properties={
            PROP_RESULT_UID: str(getattr(result, "result_uid", "") or ""),
            PROP_AXES: ",".join(labels),
        },
        notes=notes,
    )


def _channel_names(result, n_channels: int, notes: list[str]) -> list[str]:
    candidates = []
    try:
        candidates.append(result.serialization_view().channel_names)
    except Exception:
        pass
    candidates.append((getattr(result, "metadata", None) or {}).get("channel_names"))
    for names in candidates:
        if names and len(names) == n_channels:
            return [str(n) for n in names]
    if n_channels > 1:
        notes.append(f"no channel names for the {n_channels} channels")
    return []


# --------------------------------------------------------------------------
# what crosses the process boundary
# --------------------------------------------------------------------------

# FijiImageDescription lives in protocol.py (the host imports that module and
# must not load this one); it is imported above and re-exported from here.

@dataclass(frozen=True)
class SentRecord:
    """What the parent remembers about one send, keyed by ``image_id``."""

    image_id: int
    result_uid: str
    source_axes: tuple
    shape_tzcyx: tuple


# --------------------------------------------------------------------------
# Fiji -> ImProcess
# --------------------------------------------------------------------------

def to_snapshot(
    desc: FijiImageDescription,
    data_tzcyx: np.ndarray,
    *,
    sent: SentRecord | None,
) -> tuple[LayerSnapshot, list[str]]:
    """A layer snapshot for ``desc``'s pixels, and what could not be carried."""
    x, y, c, z, t = (int(v) for v in desc.dims_xyczt)
    expected = (t, z, c, y, x)
    data = data_tzcyx
    if tuple(np.shape(data)) != expected:
        raise NotImportableFromFiji(
            f"pixel array shape {tuple(np.shape(data))} does not match the image's (T,Z,C,Y,X) {expected}"
        )
    notes: list[str] = []

    keep, restore = _axes_to_keep(sent, expected)
    drop = tuple(i for i in range(len(IMAGEJ_AXES)) if i not in keep)
    data = np.squeeze(data, axis=drop) if drop else data
    kept_in_canonical = [IMAGEJ_AXES[i] for i in keep]
    if restore is not None:
        labels = list(restore)
        data = np.transpose(data, [kept_in_canonical.index(a) for a in labels])
    else:
        labels = kept_in_canonical
        notes.append("axis order restored canonically (T,Z,C,Y,X)")

    unit, spatial = _spatial_scales(desc, labels, notes)
    scale = []
    for label in labels:
        if label in spatial:
            scale.append(spatial[label])
        elif label == "T":
            scale.append(_frame_interval_s(desc, notes))
        else:
            scale.append(1.0)                      # C: an index, identity scale, no unit
    if any(v != 0.0 for v in (desc.x_origin, desc.y_origin, desc.z_origin)):
        notes.append("calibration origin (x,y,z) not represented")

    channel_names = None
    if "C" in labels:
        n_channels = data.shape[labels.index("C")]
        names = list(desc.channel_labels)
        if len(names) == n_channels and all(n is not None for n in names):
            channel_names = names

    metadata = {
        "axis_labels": labels,
        "scale_unit": unit,
        # A lineage HINT only: ImageJ copies properties onto duplicates, crops
        # and scaled copies, so a matching uid does not prove the pixels still
        # sit on the source's grid. grid_decision uses it to refuse a mismatch.
        "result_uid": desc.properties.get(PROP_RESULT_UID, ""),
        "fiji_image_id": desc.image_id,
    }
    if channel_names:
        metadata["channel_names"] = channel_names
    snapshot = LayerSnapshot(
        name=desc.title,
        layer_type="image",
        data=data,
        ndim=len(labels),
        scale=tuple(scale),
        translate=tuple(0.0 for _ in labels),
        rotate=None, shear=None, affine=None,
        units=None,
        metadata=metadata,
    )
    return snapshot, notes


def _axes_to_keep(sent: SentRecord | None, shape: tuple) -> tuple[list[int], list[str] | None]:
    """Canonical indices to keep, and the source order to restore (or ``None``)."""
    if sent is not None:
        source = [str(a).upper() for a in sent.source_axes]
        if all(a in IMAGEJ_AXES for a in source) and len(set(source)) == len(source):
            others = [i for i, a in enumerate(IMAGEJ_AXES) if a not in source]
            if all(shape[i] == 1 for i in others):
                return sorted(IMAGEJ_AXES.index(a) for a in source), source
    always = (IMAGEJ_AXES.index("Y"), IMAGEJ_AXES.index("X"))
    return [i for i in range(len(IMAGEJ_AXES)) if shape[i] != 1 or i in always], None


def _spatial_scales(desc: FijiImageDescription, labels: list[str], notes: list[str]) -> tuple[str, dict]:
    """One ImProcess unit for the kept spatial axes, with the scales moved onto it."""
    axes = [("X", desc.pixel_width, desc.x_unit), ("Y", desc.pixel_height, desc.y_unit)]
    if "Z" in labels:
        axes.append(("Z", desc.pixel_depth, desc.z_unit))
    mapped = [(a, float(s), _SPATIAL_UNITS.get(str(u).strip().lower())) for a, s, u in axes]
    raw = {a: str(u) for a, _s, u in axes}
    unknown = [a for a, _s, u in mapped if u is None]
    if unknown:
        spellings = {raw[a] for a in unknown}
        if len(unknown) == len(mapped) and len(spellings) == 1:
            notes.append(f"unknown spatial unit {next(iter(spellings))!r} kept as written")
            return next(iter(spellings)), {a: s for a, s, _u in mapped}
        raise NotImportableFromFiji(f"the axis units {raw} cannot be reconciled; set one unit in ImageJ")
    units = {u for _a, _s, u in mapped}
    if len(units) == 1:
        return units.pop(), {a: s for a, s, _u in mapped}
    if all(u in _LENGTH_NM for _a, _s, u in mapped):
        target = mapped[0][2]                      # onto the X unit
        return target, {a: s * _LENGTH_NM[u] / _LENGTH_NM[target] for a, s, u in mapped}
    raise NotImportableFromFiji(f"the axis units {raw} are not all lengths and not all the same")


def _frame_interval_s(desc: FijiImageDescription, notes: list[str]) -> float:
    factor = _TIME_UNIT_SECONDS.get(str(desc.time_unit).strip().lower())
    if desc.frame_interval > 0 and factor is not None:
        return float(desc.frame_interval) * factor
    # The identity scale: T then counts frames, which is what ImageJ's own
    # "not set" means. Said out loud so nobody reads it as 1 s.
    notes.append("frame interval unknown; T scale is the frame index")
    if factor is None and desc.frame_interval > 0:
        notes.append(f"unknown time unit {desc.time_unit!r}")
    return 1.0


def import_fiji_image(
    desc: FijiImageDescription,
    data_tzcyx: np.ndarray,
    source_result,
    *,
    sent: SentRecord | None = None,
    # Shape and scale equality cannot prove alignment (ImageJ's Translate and
    # Rotate keep both), so a grid is inherited only when the caller, which
    # knows every operation recorded on the image, says all of them preserve it.
    preserves_grid: bool = False,
    details: dict | None = None,
    history_details: dict | None = None,
    name: str | None = None,
) -> ImportedLayer:
    """Turn one Fiji image back into a result with honest provenance."""
    snapshot, notes = to_snapshot(desc, data_tzcyx, sent=sent)
    merged = dict(details or {})
    merged.update({"image_id": desc.image_id, "title": desc.title, "notes": list(notes)})
    imported = import_layer(
        snapshot, source_result,
        plugin_name="Fiji", widget_name=None,
        preserves_grid=preserves_grid, name=name,
        tool="fiji", details=merged,
    )
    result = imported.result
    if result is not None:
        result.metadata["fiji"] = {"image_id": desc.image_id, "title": desc.title, "dropped": list(notes)}
        if history_details is not None:
            result.metadata["fiji"]["history"] = history_details
        if snapshot.metadata.get("channel_names"):
            result.metadata["channel_names"] = list(snapshot.metadata["channel_names"])
    return imported
