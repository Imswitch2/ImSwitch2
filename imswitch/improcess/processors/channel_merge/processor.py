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
from imswitch.improcess.processors.combine import combine_compatibility

#: Axis label of the channel axis this processor creates.
CHANNEL_AXIS_LABEL = "C"
#: Labels that already mean "channel"; a result carrying one wants Make
#: composite, not a merge.
_CHANNEL_LIKE_LABELS = ("C", "Channel", "Channels", "Base")


class ChannelMergeProcessor(Processor):
    """Merge selected compatible image results along a new C axis."""

    name = "Merge channels"
    id = "channel-merge"
    category = "Dimensions and channels"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True
    min_inputs = 2
    max_inputs = None

    @classmethod
    def default_params(cls) -> dict:
        # ``name`` and ``axis_label`` are set by the toolbar dialog rather
        # than the parameter widget, but they are parameters all the same:
        # a workflow must be able to set them, and a typo must be caught.
        return {"name": "Merged channels", "axis_label": CHANNEL_AXIS_LABEL}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def check_inputs(self, results) -> tuple[bool, str]:
        return merge_compatibility(results)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(
            QtWidgets.QLabel(
                "Merges the checked inputs into one C-axis channel stack, in "
                "the order listed."
            )
        )
        layout.addStretch()

        def get_values():
            return dict(self.default_params())

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or [])
        if not results:
            results = [result, *list(params.get("additional_results", []) or [])]
        return merge_results(
            results,
            name=params.get("name") or "Merged channels",
            axis_label=str(params.get("axis_label") or CHANNEL_AXIS_LABEL),
        )


def merge_results(
    results: Sequence[ProcessingResult],
    *,
    name: str = "Merged channels",
    axis_label: str = "C",
) -> ArrayProcessingResult:
    """Merge same-shaped image results into one leading-channel stack."""
    results = list(results or [])
    ok, reason = merge_compatibility(results, axis_label=axis_label)
    if not ok:
        raise ValueError(reason)

    first = results[0]
    labels = axis_labels_for_result(first)
    scales = axis_scales_for_result(first)
    arrays = [np.asarray(result.data) for result in results]
    source_names = [getattr(result, "name", "result") for result in results]

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


def merge_compatibility(
    results: Sequence[ProcessingResult],
    *,
    axis_label: str = CHANNEL_AXIS_LABEL,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for merging ``results`` into a channel stack.

    A channel merge is a stack along a new ``C`` axis, so the metadata rules
    are exactly Stack/Combine's and are shared with it rather than restated:
    same shape, same axis labels, same pixel scales and the same scale unit.
    Inputs that already carry a ``C`` axis are rejected here — a second one
    would give the output two identically-labelled axes, and every
    label-driven axis lookup downstream takes the first match. Metadata-only:
    lazy inputs are never materialized.
    """
    results = list(results or [])
    if len(results) < 2:
        return False, _too_few_inputs_reason(results)
    return combine_compatibility(results, mode="stack", new_axis_label=axis_label)


def _too_few_inputs_reason(results: Sequence[ProcessingResult]) -> str:
    """Explain a one-input merge, and where the second channel comes from.

    One selected stack is the common dead end: its planes are images too, but
    a merge takes whole results, and expanding every plane of a long time
    series into the picker would make it unusable. Name the operation that
    does turn those planes into inputs instead of leaving the user at
    "select two".
    """
    base = "Select at least two results to merge into channels"
    if len(results) != 1:
        return base
    try:
        labels = axis_labels_for_result(results[0])
    except Exception:
        return base
    if len(labels) < 3:
        return base
    name = getattr(results[0], "name", "the selected result")
    if any(label in _CHANNEL_LIKE_LABELS for label in labels[:-2]):
        return (
            f"{base} — '{name}' already has a channel axis; use Make composite "
            f"to colour its channels"
        )
    return (
        f"{base} — '{name}' is one stack; use Split stack first to get one "
        f"result per plane"
    )


def can_merge_results(results: Sequence[ProcessingResult]) -> bool:
    """Return True when results can be merged without reading pixel data."""
    return merge_compatibility(results)[0]


__all__ = [
    "CHANNEL_AXIS_LABEL",
    "ChannelMergeProcessor",
    "can_merge_results",
    "merge_compatibility",
    "merge_results",
]
