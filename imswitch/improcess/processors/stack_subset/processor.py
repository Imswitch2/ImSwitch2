"""Crop/substack a result by axis ranges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.lazy_array import LazySubsetArray
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
