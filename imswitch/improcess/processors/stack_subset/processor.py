"""Crop/substack a result by axis ranges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor


class StackSubsetProcessor(Processor):
    """Create a cropped/substack result while preserving axis dimensionality."""

    name = "Crop/Substack"
    id = "stack-subset"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        ranges_edit = QtWidgets.QLineEdit()
        ranges_edit.setPlaceholderText("Z=0:10,T=0:5:2")
        ranges_edit.setToolTip("Comma-separated zero-based ranges: label=start:stop[:step].")
        copy_check = QtWidgets.QCheckBox("Copy data")

        layout.addRow("Ranges", ranges_edit)
        layout.addRow("", copy_check)

        def get_values():
            return {
                "ranges": _parse_range_text(ranges_edit.text()),
                "copy": copy_check.isChecked(),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        ranges = normalize_subset_ranges(
            params.get("ranges", []),
            labels=axis_labels_for_result(result),
            shape=shape_for_result(result),
        )
        return subset_result(
            result,
            ranges,
            copy=bool(params.get("copy", False)),
        )


def subset_result(
    result: ProcessingResult,
    ranges: Sequence[dict],
    *,
    copy: bool = False,
) -> ArrayProcessingResult:
    """Return a sliced result, preserving rank and axis metadata."""
    data = result.data
    shape = shape_for_result(result)
    labels = axis_labels_for_result(result)
    scales = axis_scales_for_result(result)
    slices = [slice(None)] * len(shape)
    range_metadata = []

    for spec in ranges:
        axis = int(spec["axis"])
        step = int(spec.get("step", 1))
        slices[axis] = slice(int(spec["start"]), int(spec["stop"]), step)
        if step != 1:
            scales[axis] = float(scales[axis]) * step
        range_metadata.append(
            {
                "axis": axis,
                "label": labels[axis],
                "start": int(spec["start"]),
                "stop": int(spec["stop"]),
                "step": step,
            }
        )

    if copy:
        subset = np.array(data[tuple(slices)], copy=True)
    elif isinstance(data, np.ndarray):
        subset = data[tuple(slices)]
    elif hasattr(data, "__getitem__"):
        subset = LazySubsetArray(data, tuple(slices), source_shape=shape)
    else:
        subset = np.asarray(data)[tuple(slices)]

    return ArrayProcessingResult(
        name=f"{result.name} (subset)",
        data=subset,
        axis_labels=labels,
        view_modes=list(getattr(result, "view_modes", [])),
        display_levels=getattr(result, "display_levels", None),
        axis_scales=scales,
        scale_unit=getattr(result, "scale_unit", "px"),
        metadata={
            "source_result": result.name,
            "operation": StackSubsetProcessor.id,
            "ranges": range_metadata,
            "copied": bool(copy),
        },
    )


def normalize_subset_ranges(
    ranges,
    *,
    labels: Sequence[str],
    shape: Sequence[int],
) -> tuple[dict, ...]:
    """Normalize user/GUI range specs to validated axis/start/stop/step dicts."""
    normalized = []
    if isinstance(ranges, Mapping):
        iterable = [
            {"axis": axis, "start": value[0], "stop": value[1], "step": value[2] if len(value) > 2 else 1}
            for axis, value in ranges.items()
        ]
    else:
        iterable = list(ranges or [])

    seen_axes = set()
    for spec in iterable:
        axis = _resolve_axis(spec.get("axis", spec.get("label")), labels)
        if axis in seen_axes:
            raise ValueError(f"Duplicate subset range for axis {axis}")
        seen_axes.add(axis)
        size = int(shape[axis])
        start = 0 if spec.get("start") is None else int(spec.get("start"))
        stop = size if spec.get("stop") is None else int(spec.get("stop"))
        step = 1 if spec.get("step") is None else int(spec.get("step"))
        if step < 1:
            raise ValueError("Subset step must be at least 1")
        if start < 0 or stop > size or start >= stop:
            label = labels[axis] if axis < len(labels) else str(axis)
            raise ValueError(
                f"Invalid subset range for axis {label}: start={start}, stop={stop}, size={size}"
            )
        normalized.append({"axis": axis, "start": start, "stop": stop, "step": step})
    return tuple(normalized)


class LazySubsetArray:
    """Deferred sliced view over an array-like object."""

    supports_lazy_indexing = True

    def __init__(
        self,
        source,
        base_key: tuple[slice, ...],
        *,
        source_shape: Sequence[int],
    ):
        self._source = source
        self._base_key = base_key
        self._source_shape = tuple(int(size) for size in source_shape)
        self._shape = tuple(
            _slice_length(selection, size)
            for selection, size in zip(base_key, self._source_shape, strict=True)
        )
        self.backend = f"{getattr(source, 'backend', type(source).__name__)}-subset"

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def ndim(self) -> int:
        return len(self._shape)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(getattr(self._source, "dtype", np.asarray(self.asarray()).dtype))

    @property
    def chunks(self) -> tuple[int, ...] | None:
        return None

    def __getitem__(self, key: Any) -> np.ndarray:
        composed = self._compose_key(key)
        if composed is None:
            return np.asarray(self.asarray()[key])
        return np.asarray(self._source[composed])

    def asarray(self) -> np.ndarray:
        return np.asarray(self._source[self._base_key])

    def __array__(self, dtype=None) -> np.ndarray:
        array = self.asarray()
        if dtype is not None:
            return np.asarray(array, dtype=dtype)
        return array

    def close(self) -> None:
        pass

    def _compose_key(self, key: Any) -> tuple[Any, ...] | None:
        expanded = _expand_key(key, self.ndim)
        if expanded is None:
            return None
        composed = []
        for axis_key, base_slice, source_size, subset_size in zip(
            expanded,
            self._base_key,
            self._source_shape,
            self._shape,
            strict=True,
        ):
            composed_axis = _compose_axis_key(
                axis_key,
                base_slice,
                source_size=source_size,
                subset_size=subset_size,
            )
            if composed_axis is None:
                return None
            composed.append(composed_axis)
        return tuple(composed)


def _resolve_axis(axis, labels: Sequence[str]) -> int:
    if axis is None:
        raise ValueError("Subset range is missing an axis")
    if isinstance(axis, int):
        index = axis
    else:
        text = str(axis)
        if text.isdigit():
            index = int(text)
        elif text.lower().startswith("axis") and text[4:].isdigit():
            index = int(text[4:])
        else:
            lowered = [label.lower() for label in labels]
            try:
                index = lowered.index(text.lower())
            except ValueError as exc:
                raise ValueError(f"Unknown subset axis {axis!r}") from exc
    if index < 0 or index >= len(labels):
        raise ValueError(f"Subset axis index {index} outside result rank {len(labels)}")
    return index


def _slice_length(selection: slice, size: int) -> int:
    start, stop, step = selection.indices(size)
    if step > 0:
        return max(0, (stop - start + step - 1) // step)
    return max(0, (start - stop - step - 1) // (-step))


def _expand_key(key: Any, ndim: int) -> tuple[Any, ...] | None:
    if not isinstance(key, tuple):
        key = (key,)
    if any(part is None for part in key):
        return None
    if Ellipsis in key:
        ellipsis_index = key.index(Ellipsis)
        before = key[:ellipsis_index]
        after = key[ellipsis_index + 1:]
        fill = (slice(None),) * (ndim - len(before) - len(after))
        key = before + fill + after
    if len(key) > ndim:
        return None
    return key + (slice(None),) * (ndim - len(key))


def _compose_axis_key(
    axis_key: Any,
    base_slice: slice,
    *,
    source_size: int,
    subset_size: int,
):
    base_start, _base_stop, base_step = base_slice.indices(source_size)
    if isinstance(axis_key, int):
        index = axis_key + subset_size if axis_key < 0 else axis_key
        if index < 0 or index >= subset_size:
            raise IndexError("Lazy subset index out of range")
        return base_start + index * base_step
    if not isinstance(axis_key, slice):
        return None
    start, stop, step = axis_key.indices(subset_size)
    if step <= 0:
        return None
    return slice(
        base_start + start * base_step,
        base_start + stop * base_step,
        base_step * step,
    )


def _parse_range_text(text: str) -> list[dict]:
    specs = []
    for chunk in str(text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Invalid range {chunk!r}")
        axis, raw_range = [part.strip() for part in chunk.split("=", 1)]
        parts = [part.strip() for part in raw_range.split(":")]
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid range {chunk!r}")
        specs.append(
            {
                "axis": axis,
                "start": int(parts[0]) if parts[0] else None,
                "stop": int(parts[1]) if parts[1] else None,
                "step": int(parts[2]) if len(parts) == 3 and parts[2] else 1,
            }
        )
    return specs


__all__ = [
    "LazySubsetArray",
    "StackSubsetProcessor",
    "normalize_subset_ranges",
    "subset_result",
]
