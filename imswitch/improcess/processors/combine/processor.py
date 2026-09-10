"""Stack or concatenate compatible image results into one output.

Rank-general: N same-shaped inputs of any dimensionality gain one new leading
axis (two 2D images -> a 3D stack, two 3D stacks -> a 4D stack, ...), or are
appended along an existing shared axis (grow a T-series, add Z planes).
Unlike the one-click C-only `channel-merge`, the axis is user-chosen and
incompatibilities are reported as reasons instead of a silently dead button.
"""

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

#: Common labels offered for the new stack axis; any custom string is valid.
STACK_AXIS_LABELS = ("Z", "T", "C")


class StackCombineProcessor(Processor):
    """Stack or concatenate the selected reconstruction-list results."""

    name = "Stack/Combine"
    id = "stack-combine"
    category = "Dimensions and channels"
    min_inputs = 2
    max_inputs = None

    @classmethod
    def default_params(cls) -> dict:
        # Set by the Stack/Combine dialog in the GUI, by the step's params in
        # a workflow. ``name`` empty means "derive one from the mode".
        return {"mode": "stack", "join_axis": 0, "name": "", "axis_label": "Z"}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def check_inputs(self, results) -> tuple[bool, str]:
        # Mode/axis are chosen in the dialog; the panel-level check is the
        # stack case, which is the stricter of the two.
        return combine_compatibility(results, mode="stack")

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(
            QtWidgets.QLabel(
                "Stacks the checked inputs along a new axis, in the order "
                "listed;\nuse the Stack/Combine toolbar action to concatenate "
                "along an existing axis."
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
        mode = str(params.get("mode") or "stack")
        if mode not in ("stack", "concatenate"):
            raise ValueError(f"mode must be 'stack' or 'concatenate', got {mode!r}")
        name = params.get("name") or None
        if mode == "concatenate":
            return concatenate_results(
                results,
                join_axis=int(params.get("join_axis") or 0),
                name=name or "Concatenated",
            )
        return stack_results(
            results,
            axis_label=str(params.get("axis_label") or "Z"),
            name=name or "Stacked",
        )


def combine_compatibility(
    results: Sequence[ProcessingResult],
    mode: str = "stack",
    join_axis: int = 0,
    new_axis_label: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for combining ``results`` without reading pixels.

    The reason string is user-facing: the combine dialog shows it instead of
    presenting a silently disabled OK button. Metadata must match across ALL
    inputs — axis labels, axis scales (including the join axis) and
    ``scale_unit`` — so same-shaped but physically different arrays can never
    silently become one calibrated stack. This check must stay
    metadata-only: it uses the shape/labels/scales accessors and never
    materializes lazy pixel data.
    """
    results = list(results or [])
    if len(results) < 2:
        return False, "Select at least two image results to combine"

    try:
        shape = shape_for_result(results[0])
        labels = axis_labels_for_result(results[0])
        scales = axis_scales_for_result(results[0])
    except Exception:
        return False, "Could not read the shape of the first input"
    unit = getattr(results[0], "scale_unit", "px")

    if mode == "concatenate" and not (0 <= join_axis < len(shape)):
        return False, f"Join axis {join_axis} is out of range for {len(shape)}D data"

    # A duplicate axis label would break every label-driven axis lookup
    # downstream (resolve_axis takes the FIRST match), so [Z, Z, Y, X] is
    # blocked here rather than handled ad hoc by each consumer.
    if mode == "stack" and new_axis_label is not None and new_axis_label in labels:
        return False, (
            f"Axis label '{new_axis_label}' already exists in the inputs "
            f"({labels}); pick a label that is not already used"
        )

    first_name = getattr(results[0], "name", "input 1")
    for index, result in enumerate(results[1:], start=2):
        result_name = getattr(result, "name", f"input {index}")
        try:
            other_shape = shape_for_result(result)
            other_labels = axis_labels_for_result(result)
            other_scales = axis_scales_for_result(result)
        except Exception:
            return False, f"Could not read the shape of '{result_name}'"
        if mode == "concatenate":
            if len(other_shape) != len(shape):
                return False, (
                    f"'{result_name}' is {len(other_shape)}D but "
                    f"'{first_name}' is {len(shape)}D"
                )
            mismatched = [
                axis
                for axis in range(len(shape))
                if axis != join_axis and other_shape[axis] != shape[axis]
            ]
            if mismatched:
                return False, (
                    f"'{result_name}' shape {tuple(other_shape)} does not match "
                    f"'{first_name}' shape {tuple(shape)} outside the join axis"
                )
        elif other_shape != shape:
            return False, (
                f"'{result_name}' shape {tuple(other_shape)} does not match "
                f"'{first_name}' shape {tuple(shape)}"
            )
        if other_labels != labels:
            return False, (
                f"'{result_name}' axes {other_labels} do not match "
                f"'{first_name}' axes {labels}"
            )
        if not _scales_close(other_scales, scales):
            return False, (
                f"'{result_name}' pixel scales do not match '{first_name}'"
            )
        other_unit = getattr(result, "scale_unit", "px")
        if other_unit != unit:
            return False, (
                f"'{result_name}' scale unit '{other_unit}' does not match "
                f"'{first_name}' unit '{unit}'"
            )
    return True, ""


def _broadcast_alignment(shape, other_shape):
    """``(result_shape, offset, other_offset)`` for two right-aligned shapes.

    ``None`` when they cannot be lined up. Right-aligned because that is what
    numpy does, and the whole point is to agree with what the arithmetic will
    actually compute.
    """
    width = max(len(shape), len(other_shape))
    left = (1,) * (width - len(shape)) + tuple(shape)
    right = (1,) * (width - len(other_shape)) + tuple(other_shape)
    out = []
    for a, b in zip(left, right):
        if a == b or a == 1 or b == 1:
            out.append(max(a, b))
        else:
            return None
    result = tuple(out)
    # The answer has to be one of the inputs, not something larger than both.
    # Without this, a (233, 1) column and a (1, 233) row "broadcast" into a
    # 233x233 image neither of them contains — arithmetic numpy will happily
    # perform and nobody asked for.
    if result not in (tuple(shape), tuple(other_shape)):
        return None
    return result, width - len(shape), width - len(other_shape)


def elementwise_compatibility(
    results: Sequence[ProcessingResult],
) -> tuple[bool, str]:
    """``(ok, reason)`` for pixel-wise arithmetic between results.

    Deliberately looser than :func:`combine_compatibility`, because the two
    answer different questions. Stacking builds one array, so the shapes must
    genuinely agree; arithmetic only needs the operands to line up, and numpy
    lines up a ``(1, 233, 233)`` image with a ``(233, 233)`` mask without
    ambiguity. Refusing that pair — as this did, because the calculator reused
    the stacking check — turns "apply this mask to the image I drew it on"
    into an error about two shapes the user can see are the same picture.

    Length-1 axes may broadcast, and a plane may broadcast across a stack: one
    mask applied to every slice is what a mask on a stack means. What is still
    refused is anything where the answer is bigger than both inputs, and
    anything whose *shared* axes disagree in label, scale or unit — a nm result
    and a um one are not comparable however well their shapes line up.

    Metadata-only, like the stacking check: no pixel data is read.
    """
    results = list(results or [])
    if len(results) < 2:
        return False, "Select two image results"

    try:
        shape = tuple(shape_for_result(results[0]))
        labels = list(axis_labels_for_result(results[0]))
        scales = list(axis_scales_for_result(results[0]))
    except Exception:
        return False, "Could not read the shape of the first input"
    unit = getattr(results[0], "scale_unit", "px")
    first_name = getattr(results[0], "name", "input 1")

    for index, result in enumerate(results[1:], start=2):
        result_name = getattr(result, "name", f"input {index}")
        try:
            other_shape = tuple(shape_for_result(result))
            other_labels = list(axis_labels_for_result(result))
            other_scales = list(axis_scales_for_result(result))
        except Exception:
            return False, f"Could not read the shape of '{result_name}'"

        alignment = _broadcast_alignment(shape, other_shape)
        if alignment is None:
            return False, (
                f"'{result_name}' shape {other_shape} cannot be lined up with "
                f"'{first_name}' shape {shape}"
            )
        _result_shape, offset, other_offset = alignment

        # Only the axes both inputs actually have are compared. A broadcast
        # axis has no counterpart to disagree with, so demanding a matching
        # label there is demanding a label for an axis that is not there.
        shared = min(len(shape), len(other_shape))
        for position in range(1, shared + 1):
            mine, theirs = len(shape) - position, len(other_shape) - position
            if shape[mine] == 1 or other_shape[theirs] == 1:
                continue
            if labels[mine] != other_labels[theirs]:
                return False, (
                    f"'{result_name}' axis '{other_labels[theirs]}' does not "
                    f"match '{first_name}' axis '{labels[mine]}'"
                )
            if not _scales_close([other_scales[theirs]], [scales[mine]]):
                return False, (
                    f"'{result_name}' pixel scales along "
                    f"'{other_labels[theirs]}' do not match '{first_name}'"
                )

        other_unit = getattr(result, "scale_unit", "px")
        if other_unit != unit and "px" not in (other_unit, unit):
            return False, (
                f"'{result_name}' scale unit '{other_unit}' does not match "
                f"'{first_name}' unit '{unit}'"
            )
    return True, ""


def default_stack_axis_label(labels: Sequence[str]) -> str:
    """Return the first common stack label not colliding with ``labels``."""
    for candidate in (*STACK_AXIS_LABELS, "S"):
        if candidate not in labels:
            return candidate
    index = 0
    while f"S{index}" in labels:
        index += 1
    return f"S{index}"


def stack_results(
    results: Sequence[ProcessingResult],
    *,
    axis_label: str = "Z",
    name: str = "Stacked",
) -> ArrayProcessingResult:
    """Stack same-shaped results along a new leading ``axis_label`` axis."""
    results = list(results or [])
    ok, reason = combine_compatibility(results, mode="stack", new_axis_label=axis_label)
    if not ok:
        raise ValueError(reason)

    first = results[0]
    labels = axis_labels_for_result(first)
    scales = axis_scales_for_result(first)
    data = np.stack([np.asarray(result.data) for result in results], axis=0)

    # A C-labelled stack is a channel stack; render it like channel-merge does.
    view_modes = None
    if axis_label == "C":
        view_modes = [ViewMode("Channels", tuple(range(data.ndim)))]

    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=[axis_label, *labels],
        view_modes=view_modes,
        display_levels=finite_range(data),
        axis_scales=[1.0, *scales],
        scale_unit=getattr(first, "scale_unit", "px"),
        metadata={
            "operation": StackCombineProcessor.id,
            "mode": "stack",
            "stack_axis": axis_label,
            "source_results": [getattr(r, "name", "result") for r in results],
        },
    )


def concatenate_results(
    results: Sequence[ProcessingResult],
    *,
    join_axis: int = 0,
    name: str = "Concatenated",
) -> ArrayProcessingResult:
    """Append results along the existing shared axis ``join_axis``."""
    results = list(results or [])
    ok, reason = combine_compatibility(results, mode="concatenate", join_axis=join_axis)
    if not ok:
        raise ValueError(reason)

    first = results[0]
    labels = axis_labels_for_result(first)
    scales = axis_scales_for_result(first)
    data = np.concatenate([np.asarray(result.data) for result in results], axis=join_axis)

    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=list(labels),
        display_levels=finite_range(data),
        axis_scales=list(scales),
        scale_unit=getattr(first, "scale_unit", "px"),
        metadata={
            "operation": StackCombineProcessor.id,
            "mode": "concatenate",
            "join_axis": labels[join_axis],
            "source_results": [getattr(r, "name", "result") for r in results],
        },
    )


def _scales_close(scales: Sequence[float], other: Sequence[float]) -> bool:
    """Compare axis scales with float tolerance instead of exact equality."""
    if len(scales) != len(other):
        return False
    return bool(np.allclose(scales, other, rtol=1e-5, atol=1e-8))


__all__ = [
    "StackCombineProcessor",
    "STACK_AXIS_LABELS",
    "combine_compatibility",
    "concatenate_results",
    "default_stack_axis_label",
    "stack_results",
]
