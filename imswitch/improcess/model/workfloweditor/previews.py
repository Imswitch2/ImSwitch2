"""Small pictures of results, made where the result is made. Qt-free.

The workflow editor shows each step's output in its box and a larger one in
the inspector. Both are computed on the worker thread that ran the step --
never on the GUI thread, which would contend with the worker for the file a
lazy result reads from -- and handed over as plain data: an RGB ``uint8``
array, a curve's samples, a table's first rows. Only turning that into a
pixmap happens on the GUI thread.

A stack is shown as its middle plane, or for a small in-memory stack as a
maximum projection; a large or lazily read stack is projected over a sample
of its planes and says so (``sampled``), so a 2 GB recording is never read
whole for a thumbnail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: Planes a stack is projected over at most; beyond it the projection samples.
_MAX_PLANES = 16
#: Elements of an in-memory stack small enough to project whole.
_SMALL = 8_000_000
#: Points a curve keeps for a sparkline.
_CURVE_POINTS = 400
_LABEL_SEED = 7


@dataclass(frozen=True)
class Preview:
    """What a box or the inspector shows for one output."""

    kind: str
    rgb: np.ndarray | None = None
    curve: np.ndarray | None = None
    columns: tuple = ()
    rows: tuple = ()
    row_count: int = 0
    shape: tuple = ()
    axes: tuple = ()
    dtype: str = ""
    #: A projection over some planes, not all of them.
    sampled: bool = False
    #: Planes along the stack axes (a slider's range), and the one shown (-1: a projection).
    planes: int = 1
    plane: int = -1
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        """``2 × 96 × 96 · C Y X · float32`` (or the row count of a table)."""
        if self.kind in ("table", "localization") and self.row_count:
            text = f"{self.row_count} row{'s' if self.row_count != 1 else ''}"
            return text + (f" · {', '.join(self.columns[:4])}" if self.columns else "")
        parts = []
        if self.shape:
            parts.append(" × ".join(str(n) for n in self.shape))
        if self.axes and len(self.axes) == len(self.shape):
            parts.append(" ".join(str(a) for a in self.axes))
        if self.dtype:
            parts.append(self.dtype)
        return " · ".join(parts)


def result_kind_of(result) -> str:
    if type(result).__name__ == "DataObj":
        return "recording"
    return str(getattr(result, "kind", "image") or "image")


def _array(result):
    """What to read pixels from, without reading the whole of it.

    A recording's ``data`` would decode the whole dataset (and keep it, so the
    reconstruction after it would get an in-memory copy instead of a lazy
    view): use its lazy handle, which reads one plane at a time, or nothing
    when even a plane read would decode the whole series.
    """
    if type(result).__name__ == "DataObj":
        if getattr(result, "dataLoaded", False):
            return result.data
        bounded = getattr(result, "planeReadIsBounded", None)
        if callable(bounded) and not bounded():
            return None
        return getattr(result, "data_handle", None)
    return getattr(result, "data", None)


def _axes(result, ndim: int) -> tuple:
    labels = [str(label) for label in (getattr(result, "axis_labels", None) or [])]
    return tuple(labels) if len(labels) == ndim else ()


def _normalise(plane) -> np.ndarray:
    plane = np.asarray(plane, dtype=np.float32)
    finite = plane[np.isfinite(plane)]
    if finite.size == 0:
        return np.zeros(plane.shape, np.uint8)
    if finite.size > 200_000:
        finite = finite[:: finite.size // 200_000 + 1]
    lo, hi = np.percentile(finite, [1.0, 99.7])
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((np.nan_to_num(plane, nan=lo) - lo) / (hi - lo), 0, 1)
    return (out * 255).astype(np.uint8)


def _shrink(plane: np.ndarray, size: int) -> np.ndarray:
    step = max(1, int(np.ceil(max(plane.shape[:2]) / float(size))))
    return plane[::step, ::step]


def _grey(plane: np.ndarray) -> np.ndarray:
    g = _normalise(plane)
    return np.stack([g, g, g], -1)


def _labels_rgb(plane: np.ndarray) -> np.ndarray:
    labels = np.asarray(plane).astype(np.int64)
    top = int(labels.max()) if labels.size else 0
    rng = np.random.default_rng(_LABEL_SEED)
    lut = rng.integers(60, 255, (max(top, 0) + 1, 3)).astype(np.uint8)
    lut[0] = (12, 16, 22)
    return lut[np.clip(labels, 0, top)]


def _plane_count(shape, keep: int) -> int:
    lead = shape[:-keep] if keep else shape
    return int(np.prod(lead)) if lead else 1


def _plane(data, index: int, keep: int = 2) -> np.ndarray:
    shape = tuple(data.shape)
    lead = shape[: len(shape) - keep]
    if not lead:
        return np.asarray(data[(slice(None),) * len(shape)])
    where = np.unravel_index(int(index), lead)
    return np.asarray(data[tuple(int(i) for i in where)])


def _stack_view(data, *, small: bool, keep: int = 2) -> tuple[np.ndarray, bool]:
    """A projection of every plane of a small stack, or of a sample of a big one."""
    count = _plane_count(tuple(data.shape), keep)
    if count == 1:
        return _plane(data, 0, keep), False
    if small and count <= 64:
        flat = np.asarray(data).reshape(-1, *tuple(data.shape)[-keep:])
        return flat.max(axis=0), False
    picks = np.unique(np.linspace(0, count - 1, min(count, _MAX_PLANES)).round().astype(int))
    planes = [_plane(data, int(i), keep) for i in picks]
    return np.max(np.stack(planes), axis=0), len(picks) < count


def _channel_axis(result, data) -> int | None:
    axes = _axes(result, data.ndim)
    for index, label in enumerate(axes):
        if label.upper().startswith("C") and data.shape[index] in (2, 3) and index < data.ndim - 2:
            return index
    return None


def _composite(planes: list[np.ndarray]) -> np.ndarray:
    channels = [_normalise(p) for p in planes]
    if len(channels) == 2:              # green / magenta
        return np.stack([channels[1], channels[0], channels[1]], -1)
    return np.stack(channels[:3], -1)


def make_preview(result, size: int = 192, plane: int | None = None) -> Preview:
    """A :class:`Preview` of ``result``; ``plane`` picks one plane of a stack
    (``None``: the default view). Never raises: what cannot be shown becomes
    a preview with its shape and nothing to draw."""
    kind = result_kind_of(result)
    try:
        return _make(result, kind, size, plane)
    except Exception as exc:  # noqa: BLE001 - a preview must not fail a run
        return Preview(kind=kind, extra={"error": str(exc)})


def _make(result, kind: str, size: int, plane: int | None) -> Preview:
    if kind == "curve":
        y = np.asarray(_array(result), dtype=float).ravel()
        if y.size > _CURVE_POINTS:
            edges = np.linspace(0, y.size, _CURVE_POINTS // 2 + 1).astype(int)
            pairs = [(y[a:b].min(), y[a:b].max()) for a, b in zip(edges[:-1], edges[1:]) if b > a]
            y = np.asarray([v for pair in pairs for v in pair], dtype=float)
        return Preview(kind=kind, curve=y, shape=(int(np.asarray(_array(result)).size),), dtype="float64")
    if kind == "table":
        columns = tuple(str(c) for c in (result.table_columns() or ()))
        records = list(result.table_records() or [])
        rows = tuple(tuple(_cell(record.get(column)) for column in columns) for record in records[:8])
        return Preview(kind=kind, columns=columns, rows=rows, row_count=len(records))
    data = _array(result)
    if data is None or not hasattr(data, "shape"):
        return Preview(kind=kind)
    shape = tuple(int(n) for n in data.shape)
    axes = _axes(result, len(shape))
    dtype = str(getattr(data, "dtype", ""))
    extra = {}
    if kind == "localization":
        count = getattr(result, "count", None)
        extra["count"] = int(count() if callable(count) else len(result))
    small = isinstance(data, np.ndarray) and data.size <= _SMALL
    if kind == "rgb" and len(shape) >= 3 and shape[-1] in (3, 4):
        planes = _plane_count(shape, 3)
        index = planes // 2 if plane is None else min(max(int(plane), 0), planes - 1)
        rgb = _plane(data, index, keep=3)[..., :3]
        if rgb.dtype != np.uint8:
            rgb = np.stack([_normalise(rgb[..., c]) for c in range(3)], -1)
        return Preview(kind=kind, rgb=_shrink(np.ascontiguousarray(rgb), size), shape=shape, axes=axes, dtype=dtype,
                       planes=planes, plane=index, extra=extra)
    if len(shape) < 2:
        return Preview(kind=kind, shape=shape, axes=axes, dtype=dtype, extra=extra)
    channel = _channel_axis(result, data) if kind in ("image", "composite") else None
    if channel is not None:
        moved = np.moveaxis(np.asarray(data), channel, 0) if small else None
        count = shape[channel]
        if moved is None:
            # A lazy composite: one plane per channel, from the middle of the
            # other stack axes -- never more than those planes are read.
            planes = []
            for c in range(count):
                index = [c if axis == channel else shape[axis] // 2 for axis in range(len(shape) - 2)]
                planes.append(np.asarray(data[tuple(index) + (slice(None), slice(None))]))
            rgb = _composite(planes)
            return Preview(kind=kind, rgb=_shrink(rgb, size), shape=shape, axes=axes, dtype=dtype, sampled=True,
                           extra=extra)
        rest = moved.shape[1:]
        planes_count = _plane_count(rest, 2)
        if plane is None:
            views = [moved[c].reshape(-1, *rest[-2:]).max(axis=0) for c in range(count)]
            index = -1
        else:
            index = min(max(int(plane), 0), planes_count - 1)
            views = [_plane(moved[c], index) for c in range(count)]
        return Preview(kind=kind, rgb=_shrink(_composite(views), size), shape=shape, axes=axes, dtype=dtype,
                       planes=planes_count, plane=index, extra=extra)
    planes = _plane_count(shape, 2)
    sampled = False
    if plane is not None:
        index = min(max(int(plane), 0), planes - 1)
        view = _plane(data, index)
    elif kind == "labels" or kind == "recording":
        index = planes // 2
        view = _plane(data, index)
    else:
        view, sampled = _stack_view(data, small=small)
        index = -1 if planes > 1 else 0
    view = _shrink(np.asarray(view), size)
    rgb = _labels_rgb(view) if kind == "labels" else _grey(view)
    return Preview(kind=kind, rgb=np.ascontiguousarray(rgb), shape=shape, axes=axes, dtype=dtype, sampled=sampled,
                   planes=planes, plane=index, extra=extra)


def _cell(value) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value) if value is not None else ""


def nbytes_in_memory(result) -> int:
    """Bytes a result holds in memory (what the cache limit counts): an
    in-memory array or table, not the file a lazy result reads from."""
    total = 0
    data = getattr(result, "data", None)
    if isinstance(data, np.ndarray):
        total += int(data.nbytes)
    if result_kind_of(result) == "localization":
        try:
            locs = result.locs
        except Exception:  # noqa: BLE001 - a size estimate must not fail
            locs = None
        if isinstance(locs, np.ndarray):
            total += int(locs.nbytes)
    return total


__all__ = ["Preview", "make_preview", "nbytes_in_memory", "result_kind_of"]
