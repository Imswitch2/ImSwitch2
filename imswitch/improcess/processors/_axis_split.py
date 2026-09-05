"""Shared axis-splitting helpers for ImProcess processors."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import ProcessingResult


def axis_labels_for_result(result: ProcessingResult) -> list[str]:
    shape = shape_for_result(result)
    labels = list(getattr(result, "axis_labels", []) or [])
    if len(labels) == len(shape):
        return [str(label) for label in labels]
    defaults = ["T", "Z", "C", "Y", "X"]
    if len(shape) <= len(defaults):
        return defaults[-len(shape):]
    return [f"D{i}" for i in range(len(shape) - len(defaults))] + defaults


def axis_scales_for_result(result: ProcessingResult) -> list[float]:
    shape = shape_for_result(result)
    scales = list(getattr(result, "axis_scales", []) or [])
    if len(scales) == len(shape):
        return [float(scale) for scale in scales]
    return [1.0] * len(shape)


def resolve_axis(
    result: ProcessingResult,
    requested: str | None,
    *,
    preferred_labels: Iterable[str],
    require_label_match: bool = False,
) -> int:
    shape = shape_for_result(result)
    labels = axis_labels_for_result(result)
    if len(shape) < 3:
        raise ValueError(f"Cannot split a 2D result with shape {shape}")

    requested = str(requested or "Auto")
    if requested != "Auto":
        return _explicit_axis_index(requested, labels, shape)

    lowered = {label.lower(): index for index, label in enumerate(labels)}
    for candidate in preferred_labels:
        index = lowered.get(str(candidate).lower())
        if index is not None and shape[index] > 1:
            return index
    if require_label_match:
        raise ValueError(f"No matching split axis found in labels {labels}")

    for index, size in enumerate(shape[:-2]):
        if size > 1:
            return index
    raise ValueError(f"No non-spatial axis with more than one plane in shape {shape}")


def split_result(result: ProcessingResult, axis: int, *, operation: str) -> tuple[ArrayProcessingResult, ...]:
    data = np.asarray(result.data)
    labels = axis_labels_for_result(result)
    scales = axis_scales_for_result(result)
    axis_label = labels[axis]
    output_labels = [label for index, label in enumerate(labels) if index != axis]
    output_scales = [scale for index, scale in enumerate(scales) if index != axis]

    outputs = []
    for index in range(data.shape[axis]):
        slice_data = np.array(np.take(data, index, axis=axis), copy=True)
        outputs.append(
            ArrayProcessingResult(
                name=f"{result.name} ({axis_label} {index})",
                data=slice_data,
                axis_labels=output_labels,
                display_levels=finite_range(slice_data),
                axis_scales=output_scales,
                scale_unit=getattr(result, "scale_unit", "px"),
                metadata={
                    "source_result": result.name,
                    "operation": operation,
                    "split_axis": axis_label,
                    "split_axis_index": int(axis),
                    "split_index": int(index),
                },
            )
        )
    return tuple(outputs)


def split_port_keys(outputs) -> tuple[str, ...]:
    """Provenance output ports for the results of :func:`split_result`.

    ``C0, C1, …`` for a channel split, ``Z3`` for the fourth slice of a Z
    split: the axis label plus the index along it, which is what a later step
    needs to name "the third channel" in a way that survives renaming.
    """
    return tuple(
        f"{result.metadata.get('split_axis', 'out')}{result.metadata.get('split_index', index)}"
        for index, result in enumerate(outputs)
    )


def _explicit_axis_index(requested: str, labels: list[str], shape: tuple[int, ...]) -> int:
    if requested in labels:
        index = labels.index(requested)
    else:
        lowered = [label.lower() for label in labels]
        try:
            index = lowered.index(requested.lower())
        except ValueError as exc:
            raise ValueError(f"Requested split axis {requested!r} not in {labels}") from exc
    if shape[index] <= 1:
        raise ValueError(f"Requested split axis {requested!r} has only one plane")
    return index


def shape_for_result(result: ProcessingResult) -> tuple[int, ...]:
    shape = getattr(getattr(result, "data", None), "shape", None)
    if shape is not None:
        return tuple(int(size) for size in shape)
    return tuple(int(size) for size in np.asarray(result.data).shape)


__all__ = [
    "axis_labels_for_result",
    "resolve_axis",
    "shape_for_result",
    "split_result",
]
