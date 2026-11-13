"""What a results-list or data-list entry holds, in one icon and one line.

The two lists in ImProcess (the results of the viewer, the files of the Data
dock) used to show names only. :func:`describe_result` and
:func:`describe_data_obj` give each entry a :class:`ContentSummary`: a
qtawesome icon name saying what kind of thing it is (an image, a stack, a
curve, a table, points, a number), a short label and a detail line with the
axes and sizes. No Qt here, so the rules are testable without a display;
:mod:`imswitch.improcess.view.ContentDelegate` draws them.

A result may override both: ``list_icon`` names its own icon (a PSF
measurement is more than "a table") and ``list_summary()`` its own line.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .result import result_kind

#: Icon per thing the list can hold (qtawesome names; Material Design Icons 6).
ICONS = {
    "image": "mdi6.image-outline",
    "stack": "mdi6.image-multiple-outline",
    "time_series": "mdi6.filmstrip",
    "channels": "mdi6.layers-triple-outline",
    "labels": "mdi6.shape-outline",
    "table": "mdi6.table",
    "curve": "mdi6.chart-line",
    "points": "mdi6.scatter-plot",
    "rgb": "mdi6.palette-outline",
    "composite": "mdi6.layers-triple-outline",
    "number": "mdi6.numeric",
    "file": "mdi6.file-image-outline",
    "folder": "mdi6.folder-multiple-image",
    "unknown": "mdi6.file-outline",
}

#: Axis labels that mean a plane series in time, and channels.
TIME_LABELS = ("T", "Frame", "Time")
CHANNEL_LABELS = ("C",)


@dataclass(frozen=True)
class ContentSummary:
    """One list entry's content: ``icon`` (a key of :data:`ICONS` or a
    qtawesome name), ``label`` (what it is) and ``detail`` (sizes, state)."""

    icon: str
    label: str
    detail: str = ""

    @property
    def icon_name(self) -> str:
        return ICONS.get(self.icon, self.icon)

    @property
    def line(self) -> str:
        return f"{self.label} · {self.detail}" if self.detail else self.label


def describe_result(result) -> ContentSummary:
    """The summary of a processing result (duck-typed: ``kind``, ``data``,
    ``axis_labels``, ``list_icon``, ``list_summary``)."""
    kind = result_kind(result)
    shape = _shape(getattr(result, "data", None))
    labels = [str(a) for a in (getattr(result, "axis_labels", None) or [])]
    labels = (labels + [""] * len(shape))[:len(shape)]
    dtype = _dtype(getattr(result, "data", None))

    if kind == "image" and shape is not None and _is_number(shape):
        summary = ContentSummary("number", "value", _value_text(result.data))
    elif kind == "image":
        summary = _image_summary(shape, labels, dtype)
    elif kind == "labels":
        summary = ContentSummary("labels", "labels", _dims(shape, labels, dtype))
    elif kind == "curve":
        n = shape[-1] if shape else 0
        summary = ContentSummary("curve", "curve", f"{n:,} points along {labels[-1] or 'x'}" if n else "")
    elif kind == "localization":
        summary = ContentSummary("points", "localizations", _count(result, "localizations"))
    elif kind == "table":
        summary = ContentSummary("table", "table", _count(result, "rows", shape))
    elif kind == "rgb":
        summary = ContentSummary("rgb", "RGB image", _dims(shape, labels, drop=("RGB", "C")))
    elif kind == "composite":
        summary = ContentSummary("composite", "composite", _dims(shape, labels))
    else:
        summary = ContentSummary("unknown", kind, _dims(shape, labels))

    icon = getattr(result, "list_icon", None)
    own = None
    summarize = getattr(result, "list_summary", None)
    if callable(summarize):
        try:
            own = summarize()
        except Exception:
            own = None
    if icon or own:
        label, _, detail = (own or summary.line).partition(" · ")
        summary = ContentSummary(str(icon) if icon else summary.icon, label, detail)
    return summary


def describe_data_obj(data_obj) -> ContentSummary:
    """The summary of a Data-dock entry (a :class:`DataObj`), from what it
    already knows: opening a file to describe it is the Load button's job,
    not the list's."""
    if getattr(data_obj, "sourceKind", "image") != "image":
        return ContentSummary("folder", str(getattr(data_obj, "sourceKind", "source")).replace("_", " "))
    handle = None
    if getattr(data_obj, "_dataSource", None) is not None or getattr(data_obj, "_data", None) is not None:
        try:
            handle = data_obj.data_handle
        except Exception:
            handle = None
    if handle is None:
        return ContentSummary("file", "file", "not opened")
    shape = _shape(handle)
    labels = [str(a) for a in (getattr(data_obj, "axis_labels", None) or [])]
    labels = (labels + [""] * len(shape))[:len(shape)]
    summary = _image_summary(shape, labels, _dtype(handle))
    state = "in memory" if getattr(data_obj, "dataMaterialized", False) else "on disk"
    return ContentSummary(summary.icon, summary.label, f"{summary.detail} · {state}" if summary.detail else state)


# --------------------------------------------------------------------------- #
def _image_summary(shape, labels, dtype: str) -> ContentSummary:
    if shape is None:
        return ContentSummary("image", "image", "")
    leading = labels[:-2] if len(shape) > 2 else []
    if len(shape) <= 2:
        icon, label = "image", "image"
    elif "Z" in leading:
        icon, label = "stack", "Z-stack"
    elif any(a in TIME_LABELS for a in leading):
        icon, label = "time_series", "time series"
    elif leading and all(a in CHANNEL_LABELS for a in leading):
        icon, label = "channels", "channels"
    else:
        icon, label = "stack", "stack"
    return ContentSummary(icon, label, _dims(shape, labels, dtype))


def _dims(shape, labels, dtype: str = "", drop=()) -> str:
    """``T 100 · Z 21 · 512 × 512 · uint16``: leading axes by name, the plane
    by size."""
    if shape is None:
        return ""
    if len(shape) == 0:
        return dtype
    if len(shape) == 1:
        text = f"{shape[0]:,}" + (f" {labels[0]}" if labels and labels[0] else "")
    else:
        parts = [f"{labels[k] or '?'} {shape[k]}" for k in range(len(shape) - 2) if labels[k] not in drop]
        parts.append(f"{shape[-2]} × {shape[-1]}")
        text = " · ".join(parts)
    return f"{text} · {dtype}" if dtype else text


def _count(result, noun: str, shape=None) -> str:
    n = None
    try:
        n = len(result)
    except TypeError:
        if shape:
            n = shape[0]
    return f"{n:,} {noun}" if n is not None else ""


def _shape(data):
    if data is None:
        return None
    shape = getattr(data, "shape", None)
    if shape is None:
        try:
            shape = np.shape(data)
        except Exception:
            return None
    try:
        return tuple(int(n) for n in shape)
    except (TypeError, ValueError):
        return None


def _dtype(data) -> str:
    dtype = getattr(data, "dtype", None)
    return str(dtype) if dtype is not None else ""


def _is_number(shape) -> bool:
    return len(shape) == 0 or (len(shape) == 1 and shape[0] == 1)


def _value_text(data) -> str:
    try:
        value = float(np.asarray(data).reshape(-1)[0])
    except Exception:
        return ""
    return f"{value:.4g}"


__all__ = ["ContentSummary", "ICONS", "describe_result", "describe_data_obj"]
