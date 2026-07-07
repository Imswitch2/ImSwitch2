"""Merge compatible grayscale results into a channel stack."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor


class ChannelMergeProcessor(Processor):
    """Merge selected compatible image results along a new C axis."""

    name = "Merge channels"
    id = "channel-merge"

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(
            QtWidgets.QLabel(
                "Merge channels uses the selected reconstruction-list results."
            )
        )
        layout.addStretch()

        def get_values():
            return {}

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or [])
        if not results:
            results = [result, *list(params.get("additional_results", []) or [])]
        return merge_results(
            results,
            name=params.get("name") or "Merged channels",
            axis_label=str(params.get("axis_label", "C")),
        )


def merge_results(
    results: Sequence[ProcessingResult],
    *,
    name: str = "Merged channels",
    axis_label: str = "C",
) -> ArrayProcessingResult:
    """Merge same-shaped image results into one leading-channel stack."""
    results = list(results or [])
    if len(results) < 2:
        raise ValueError("Merging channels requires at least two results")

    first = results[0]
    shape = shape_for_result(first)
    labels = axis_labels_for_result(first)
    scales = axis_scales_for_result(first)
    arrays = []
    source_names = []
    for result in results:
        if shape_for_result(result) != shape:
            raise ValueError("All channel-merge inputs must have the same shape")
        if axis_labels_for_result(result) != labels:
            raise ValueError("All channel-merge inputs must have the same axis labels")
        if not _scales_close(axis_scales_for_result(result), scales):
            raise ValueError("All channel-merge inputs must have the same axis scales")
        arrays.append(np.asarray(result.data))
        source_names.append(getattr(result, "name", "result"))

    data = np.stack(arrays, axis=0)
    output_labels = [axis_label, *labels]
    output_scales = [1.0, *scales]
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=output_labels,
        view_modes=[ViewMode("Channels", tuple(range(data.ndim)))],
        display_levels=finite_range(data),
        axis_scales=output_scales,
        scale_unit=getattr(first, "scale_unit", "px"),
        metadata={
            "operation": ChannelMergeProcessor.id,
            "source_results": source_names,
            "channel_axis": axis_label,
        },
    )


def can_merge_results(results: Sequence[ProcessingResult]) -> bool:
    """Return True when results can be merged without reading pixel data."""
    results = list(results or [])
    if len(results) < 2:
        return False
    try:
        shape = shape_for_result(results[0])
        labels = axis_labels_for_result(results[0])
        scales = axis_scales_for_result(results[0])
        return all(
            shape_for_result(result) == shape
            and axis_labels_for_result(result) == labels
            and _scales_close(axis_scales_for_result(result), scales)
            for result in results[1:]
        )
    except Exception:
        return False


def _scales_close(scales: Sequence[float], other: Sequence[float]) -> bool:
    """Compare axis scales with float tolerance instead of exact equality.

    Two independently-produced results with the same nominal pixel size can
    differ by floating-point noise; that shouldn't block a merge.
    """
    if len(scales) != len(other):
        return False
    return bool(np.allclose(scales, other, rtol=1e-5, atol=1e-8))


__all__ = ["ChannelMergeProcessor", "can_merge_results", "merge_results"]
