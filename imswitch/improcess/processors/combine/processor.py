"""Stack or concatenate compatible image results into one output.

Rank-general: N same-shaped inputs of any dimensionality gain one new leading
axis (two 2D images -> a 3D stack, two 3D stacks -> a 4D stack, ...), or are
appended along an existing shared axis (grow a T-series, add Z planes).
Unlike the one-click C-only `channel-merge`, the axis is user-chosen and
incompatibilities are reported as reasons instead of a silently dead button.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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


#: Length units a scale can be in, in nanometres per unit. Scales in two of
#: these are comparable once one is converted to the other: a MoNaLISA
#: result in nm and a lattice result in um are the same picture.
_LENGTH_NM = {"nm": 1.0, "um": 1000.0, "\u00b5m": 1000.0, "micron": 1000.0, "mm": 1e6}


def _unit_factor(unit: str, other_unit: str) -> float | None:
    """Multiply scales in ``other_unit`` by this to state them in ``unit``.

    ``None`` when the two cannot be reconciled: pixels against a length, or
    a unit this does not know.
    """
    if other_unit == unit:
        return 1.0
    mine = _LENGTH_NM.get(str(unit).lower())
    theirs = _LENGTH_NM.get(str(other_unit).lower())
    if mine is None or theirs is None:
        return None
    return theirs / mine


@dataclass(frozen=True)
class CombineAlignment:
    """How the inputs of a combine line up, decided from metadata alone.

    ``shapes`` is the shape each input is read in. When the inputs agree
    axis for axis it is their own shape. When they differ only by singleton
    axes — the ``(1, 1, 1, 1, Y, X)`` result of one reconstructor and the
    ``(Y, X)`` result of another — it is the shape without those axes, which
    carry no pixels and nothing to disagree about. ``labels``, ``scales``
    and ``unit`` are the output's: the first input's, in its unit.
    """

    shapes: tuple[tuple[int, ...], ...]
    labels: tuple[str, ...]
    scales: tuple[float, ...]
    unit: str

    def arrays(self, results: Sequence[ProcessingResult]) -> list[np.ndarray]:
        """The inputs' pixel data, each in the shape it is combined in."""
        return [
            np.asarray(result.data).reshape(shape)
            for result, shape in zip(results, self.shapes)
        ]


def combine_compatibility(
    results: Sequence[ProcessingResult],
    mode: str = "stack",
    join_axis: int = 0,
    new_axis_label: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for combining ``results`` without reading pixels.

    The reason string is user-facing: the combine dialog shows it instead of
    presenting a silently disabled OK button. Metadata must match across ALL
    inputs — axis labels, axis scales (including the join axis) and the
    scale unit — so same-shaped but physically different arrays can never
    silently become one calibrated stack. Two things are forgiven because
    they are not differences in the pixels: singleton axes one input has and
    another lacks, and a length unit stated differently (nm against um).
    This check must stay metadata-only: it uses the shape/labels/scales
    accessors and never materializes lazy pixel data.
    """
    try:
        align_results(results, mode=mode, join_axis=join_axis, new_axis_label=new_axis_label)
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def align_results(
    results: Sequence[ProcessingResult],
    mode: str = "stack",
    join_axis: int = 0,
    new_axis_label: str | None = None,
) -> CombineAlignment:
    """Line the inputs up for a combine, or raise ``ValueError`` with the reason.

    See :func:`combine_compatibility` for the rules. Metadata-only.
    """
    results = list(results or [])
    if len(results) < 2:
        raise ValueError("Select at least two image results to combine")

    try:
        shape = tuple(shape_for_result(results[0]))
        labels = list(axis_labels_for_result(results[0]))
        scales = list(axis_scales_for_result(results[0]))
    except Exception:
        raise ValueError("Could not read the shape of the first input") from None
    unit = getattr(results[0], "scale_unit", "px")

    if mode == "concatenate" and not (0 <= join_axis < len(shape)):
        raise ValueError(f"Join axis {join_axis} is out of range for {len(shape)}D data")

    first_name = getattr(results[0], "name", "input 1")
    views = [(shape, labels, scales)]
    units = [unit]
    for index, result in enumerate(results[1:], start=2):
        result_name = getattr(result, "name", f"input {index}")
        try:
            other_shape = tuple(shape_for_result(result))
            other_labels = list(axis_labels_for_result(result))
            other_scales = list(axis_scales_for_result(result))
        except Exception:
            raise ValueError(f"Could not read the shape of '{result_name}'") from None
        # A unit that cannot be converted is reported after the shape and
        # the labels, which are the coarser differences and the ones the
        # user sees first.
        other_unit = getattr(result, "scale_unit", "px")
        factor = _unit_factor(unit, other_unit)
        if factor is not None:
            other_scales = [s * factor for s in other_scales]
        views.append((other_shape, other_labels, other_scales))
        units.append(other_unit)

    # Inputs that agree axis for axis are combined as they are. Inputs that
    # do not are read without their singleton axes, so that the
    # (1, 1, 1, 1, Y, X) of one reconstructor and the (Y, X) of another
    # count as the same picture; the join axis is kept, singleton or not.
    same_axes = all(v[0] == shape and v[1] == labels for v in views)
    join_label = labels[join_axis] if mode == "concatenate" else None
    if same_axes:
        reduced = views
        join = join_axis
    else:
        reduced = [_without_singletons(*view, keep=join_label) for view in views]
        join = None
        if join_label is not None:
            join = reduced[0][1].index(join_label)
            reduced = [reduced[0]] + [
                _with_join_axis(view, join_label, join, reduced[0][2][join])
                for view in reduced[1:]
            ]
    out_shape, out_labels, out_scales = reduced[0]

    # A duplicate axis label would break every label-driven axis lookup
    # downstream (resolve_axis takes the FIRST match), so [Z, Z, Y, X] is
    # blocked here rather than handled ad hoc by each consumer.
    if mode == "stack" and new_axis_label is not None and new_axis_label in out_labels:
        raise ValueError(
            f"Axis label '{new_axis_label}' already exists in the inputs "
            f"({out_labels}); pick a label that is not already used"
        )

    for index, (view, (other_shape, other_labels, other_scales)) in enumerate(
        zip(views[1:], reduced[1:]), start=2
    ):
        result_name = getattr(results[index - 1], "name", f"input {index}")
        if mode == "concatenate":
            if len(other_shape) != len(out_shape):
                raise ValueError(
                    f"'{result_name}' is {len(view[0])}D but "
                    f"'{first_name}' is {len(shape)}D"
                )
            mismatched = [
                axis
                for axis in range(len(out_shape))
                if axis != join and other_shape[axis] != out_shape[axis]
            ]
            if mismatched:
                raise ValueError(
                    f"'{result_name}' shape {tuple(view[0])} does not match "
                    f"'{first_name}' shape {tuple(shape)} outside the join axis"
                )
        elif other_shape != out_shape:
            raise ValueError(
                f"'{result_name}' shape {tuple(view[0])} does not match "
                f"'{first_name}' shape {tuple(shape)}"
            )
        if other_labels != out_labels:
            raise ValueError(
                f"'{result_name}' axes {view[1]} do not match "
                f"'{first_name}' axes {labels}"
            )
        if _unit_factor(unit, units[index - 1]) is None:
            raise ValueError(
                f"'{result_name}' scale unit '{units[index - 1]}' does not match "
                f"'{first_name}' unit '{unit}'"
            )
        if not _scales_close(other_scales, out_scales):
            across = units[index - 1] != unit
            raise ValueError(
                f"'{result_name}' pixel scales do not match '{first_name}'"
                + (f" (its unit {units[index - 1]} converted to {unit})" if across else "")
            )
    return CombineAlignment(
        shapes=tuple(tuple(view[0]) for view in reduced),
        labels=tuple(out_labels),
        scales=tuple(float(s) for s in out_scales),
        unit=str(unit),
    )


def _without_singletons(shape, labels, scales, keep=None):
    """The view without its size-1 axes, except one labelled ``keep``."""
    kept = [
        axis for axis, size in enumerate(shape)
        if size != 1 or (keep is not None and labels[axis] == keep)
    ]
    return (
        tuple(shape[axis] for axis in kept),
        [labels[axis] for axis in kept],
        [scales[axis] for axis in kept],
    )


def _with_join_axis(view, label, position, scale):
    """``view`` with a singleton axis ``label`` at ``position`` if it lacks one."""
    shape, labels, scales = view
    if label in labels:
        return view
    shape, labels, scales = list(shape), list(labels), list(scales)
    shape.insert(position, 1)
    labels.insert(position, label)
    scales.insert(position, scale)
    return tuple(shape), labels, scales


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

        other_unit = getattr(result, "scale_unit", "px")
        factor = _unit_factor(unit, other_unit)
        if factor is None and "px" not in (other_unit, unit):
            return False, (
                f"'{result_name}' scale unit '{other_unit}' does not match "
                f"'{first_name}' unit '{unit}'"
            )
        if factor is not None:
            other_scales = [scale * factor for scale in other_scales]

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
                    + (f" (its unit {other_unit} converted to {unit})"
                       if other_unit != unit else "")
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
    alignment = align_results(results, mode="stack", new_axis_label=axis_label)
    labels = list(alignment.labels)
    scales = list(alignment.scales)
    data = np.stack(alignment.arrays(results), axis=0)

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
        scale_unit=alignment.unit,
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
    alignment = align_results(results, mode="concatenate", join_axis=join_axis)
    labels = list(alignment.labels)
    scales = list(alignment.scales)
    # The join axis of the first input, where it is once singleton axes the
    # inputs disagree on are dropped.
    join_label = axis_labels_for_result(results[0])[join_axis]
    join = labels.index(join_label)
    data = np.concatenate(alignment.arrays(results), axis=join)

    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=list(labels),
        display_levels=finite_range(data),
        axis_scales=list(scales),
        scale_unit=alignment.unit,
        metadata={
            "operation": StackCombineProcessor.id,
            "mode": "concatenate",
            "join_axis": join_label,
            "source_results": [getattr(r, "name", "result") for r in results],
        },
    )


def _scales_close(scales: Sequence[float], other: Sequence[float]) -> bool:
    """Compare axis scales with float tolerance instead of exact equality."""
    if len(scales) != len(other):
        return False
    return bool(np.allclose(scales, other, rtol=1e-5, atol=1e-8))


__all__ = [
    "CombineAlignment",
    "StackCombineProcessor",
    "STACK_AXIS_LABELS",
    "align_results",
    "combine_compatibility",
    "concatenate_results",
    "default_stack_axis_label",
    "stack_results",
]
