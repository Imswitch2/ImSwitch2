"""Generic projection helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np


ProjectionMode = Literal["max", "mean", "sum", "median", "std"]


@dataclass(frozen=True)
class ProjectionAnalysis:
    """Output of an axis projection."""

    data: np.ndarray
    axis: int
    axis_label: str
    mode: ProjectionMode
    input_shape: tuple[int, ...]
    output_axis_labels: list[str]
    output_axis_scales: list[float]
    metadata: dict[str, object]


def project_array(
    data: np.ndarray,
    *,
    axis: int,
    mode: ProjectionMode = "max",
    axis_labels: list[str] | None = None,
    axis_scales: list[float] | None = None,
) -> ProjectionAnalysis:
    """Project an N-dimensional array along one axis."""
    arr = np.asarray(data)
    if arr.ndim < 2:
        raise ValueError(f"Projection expects at least 2D data, got shape {arr.shape}")
    axis = _normalize_axis(axis, arr.ndim)
    labels = _normalize_axis_labels(axis_labels, arr.ndim)
    scales = _normalize_axis_scales(axis_scales, arr.ndim)

    projected = _project(arr, axis=axis, mode=mode)
    out_labels = [label for i, label in enumerate(labels) if i != axis]
    out_scales = [scale for i, scale in enumerate(scales) if i != axis]

    return ProjectionAnalysis(
        data=np.asarray(projected),
        axis=axis,
        axis_label=labels[axis],
        mode=mode,
        input_shape=tuple(arr.shape),
        output_axis_labels=out_labels,
        output_axis_scales=out_scales,
        metadata={
            "mode": mode,
            "axis": axis,
            "axis_label": labels[axis],
            "input_shape": tuple(arr.shape),
            "output_shape": tuple(projected.shape),
        },
    )


def axis_labels_for_shape(data: np.ndarray, axis_labels: list[str] | None = None) -> list[str]:
    """Return axis labels matching ``data.ndim``."""
    return _normalize_axis_labels(axis_labels, np.asarray(data).ndim)


def axis_index_from_label(label: str, axis_labels: list[str], ndim: int) -> int:
    """Resolve a user-facing axis label/index string to an axis index."""
    if label in axis_labels:
        return axis_labels.index(label)
    if label.startswith("D") and label[1:].isdigit():
        return _normalize_axis(int(label[1:]), ndim)
    try:
        return _normalize_axis(int(label), ndim)
    except ValueError as exc:
        raise ValueError(f"Axis {label!r} is not available for axes {axis_labels}") from exc


def _project(data: np.ndarray, *, axis: int, mode: ProjectionMode) -> np.ndarray:
    finite_data = np.asarray(data, dtype=np.float64)
    if mode == "max":
        return np.nanmax(finite_data, axis=axis)
    if mode == "mean":
        return np.nanmean(finite_data, axis=axis)
    if mode == "sum":
        return np.nansum(finite_data, axis=axis)
    if mode == "median":
        return np.nanmedian(finite_data, axis=axis)
    if mode == "std":
        return np.nanstd(finite_data, axis=axis)
    raise ValueError(f"Unsupported projection mode: {mode!r}")


def _normalize_axis(axis: int, ndim: int) -> int:
    if axis < 0:
        axis += ndim
    if axis < 0 or axis >= ndim:
        raise ValueError(f"Axis {axis} out of range for ndim={ndim}")
    return axis


def _normalize_axis_labels(axis_labels: list[str] | None, ndim: int) -> list[str]:
    if axis_labels and len(axis_labels) == ndim:
        return [str(label) for label in axis_labels]
    defaults = ["T", "Z", "C", "Y", "X"]
    if ndim <= len(defaults):
        return defaults[-ndim:]
    extra = [f"D{i}" for i in range(ndim - len(defaults))]
    return [*extra, *defaults]


def _normalize_axis_scales(axis_scales: list[float] | None, ndim: int) -> list[float]:
    if axis_scales and len(axis_scales) == ndim:
        return [float(scale) for scale in axis_scales]
    return [1.0 for _ in range(ndim)]
