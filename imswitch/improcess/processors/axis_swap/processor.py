"""Swap or reorder the axes of a result, and rename them.

Two separate things, either or both:

* **Order** moves the array's axes. Each axis keeps its label and its scale:
  the data is the same, stored in another order (``T, Z, Y, X`` ->
  ``Z, T, Y, X``).
* **Labels** renames axes without touching the array, for data whose labels
  are wrong (a stack read as ``Z, Y, X`` that is a time series: ``T, Y, X``).
* **Scales** sets the spacing of axes, for data whose pixel size is wrong in
  the metadata (``Z=0.25``). Metadata only; it is applied last, so it names
  axes by their labels after the reordering and relabelling.

Swapping the *meaning* of two axes -- what was along X is now along Y -- is
the first with *Keep labels in place*: the array is transposed and the labels
and scales stay where they were.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors import base as _base
from imswitch.improcess.processors.base import Processor

# "The input or the parameters do not fit": the processors' own error where
# this ImSwitch has one (it is reported without a traceback), else ValueError.
ProcessorInputError = getattr(_base, "ProcessorInputError", ValueError)

_ORDER_HELP = ("Two axes to swap (Z,T or X,Y), or every axis in its new order (Z,X,Y). "
               "By label or 0-based index. Empty: the array is left as it is.")
_KEEP_HELP = ("The labels and scales stay where they are while the array is reordered: "
              "the data changes meaning (what was along X is now along Y).")
_LABELS_HELP = ("Rename axes after reordering: every label in order (X,Y,T), or pairs (Z=T). "
                "The array is not touched. Empty: labels unchanged.")
_SCALES_HELP = ("Set the spacing of axes, in the result's unit, after reordering and relabelling: "
                "pairs (Z=0.25, T=2) or one value per axis (2,0.5,0.25). "
                "The array is not touched. Empty: scales unchanged.")


def _items(text) -> list[str]:
    if isinstance(text, (list, tuple)):
        return [str(item).strip() for item in text if str(item).strip()]
    return [item.strip() for item in str(text or "").replace(";", ",").split(",") if item.strip()]


def _axis_index(item: str, labels: Sequence[str]) -> int:
    lowered = [label.lower() for label in labels]
    if item.lower() in lowered:
        if lowered.count(item.lower()) > 1:
            raise ProcessorInputError(f"axis label {item!r} is not unique in {list(labels)}; use its index")
        return lowered.index(item.lower())
    try:
        index = int(item)
    except ValueError:
        raise ProcessorInputError(f"no axis {item!r}: the axes are {list(labels)}") from None
    if not -len(labels) <= index < len(labels):
        raise ProcessorInputError(f"axis index {index} is outside the {len(labels)} axes {list(labels)}")
    return index % len(labels)


def parse_order(order, labels: Sequence[str]) -> tuple[int, ...]:
    """The permutation ``order`` asks for: ``result_axis[i] = source_axis[perm[i]]``.

    Two items swap those two axes; as many items as there are axes give the
    whole new order; nothing gives the identity."""
    count = len(labels)
    items = _items(order)
    if not items:
        return tuple(range(count))
    indices = [_axis_index(item, labels) for item in items]
    if len(set(indices)) != len(indices):
        raise ProcessorInputError(f"order {items} names an axis twice")
    if len(indices) == count:
        return tuple(indices)
    if len(indices) == 2:
        permutation = list(range(count))
        first, second = indices
        permutation[first], permutation[second] = permutation[second], permutation[first]
        return tuple(permutation)
    raise ProcessorInputError(
        f"order {items}: give two axes to swap, or all {count} axes {list(labels)} in their new order")


def parse_labels(labels_text, labels: Sequence[str]) -> list[str]:
    """``labels`` renamed as ``labels_text`` asks: a full list, or ``old=new`` pairs."""
    items = _items(labels_text)
    current = [str(label) for label in labels]
    if not items:
        return current
    if any("=" in item for item in items):
        if not all("=" in item for item in items):
            raise ProcessorInputError(f"labels {items}: use either pairs (Z=T) or the full list, not both")
        renamed = list(current)
        for item in items:
            old, new = (part.strip() for part in item.split("=", 1))
            if not new:
                raise ProcessorInputError(f"labels: {item!r} gives no new label")
            renamed[_axis_index(old, current)] = new      # all pairs read the labels before renaming
    else:
        if len(items) != len(current):
            raise ProcessorInputError(
                f"labels {items}: give one label per axis ({len(current)}: {current}), or pairs like Z=T")
        renamed = items
    lowered = [label.lower() for label in renamed]
    if len(set(lowered)) != len(lowered):
        raise ProcessorInputError(f"labels {renamed}: two axes would have the same label")
    return renamed


def parse_scales(scales_text, labels: Sequence[str], scales: Sequence[float]) -> list[float]:
    """``scales`` as ``scales_text`` sets them: a full list, or ``axis=value`` pairs.

    ``labels`` are the axis labels as they stand after reordering and relabelling.
    Values must be finite and greater than zero."""
    items = _items(scales_text)
    current = [float(value) for value in scales]
    if not items:
        return current

    def number(text: str, what: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise ProcessorInputError(f"scales: {what} is not a number") from None
        if not np.isfinite(value) or value <= 0:
            raise ProcessorInputError(f"scales: {what} must be a finite number greater than zero")
        return value

    if any("=" in item for item in items):
        if not all("=" in item for item in items):
            raise ProcessorInputError(f"scales {items}: use either pairs (Z=0.25) or the full list, not both")
        result = list(current)
        seen: set[int] = set()
        for item in items:
            axis, value = (part.strip() for part in item.split("=", 1))
            index = _axis_index(axis, labels)
            if index in seen:
                raise ProcessorInputError(f"scales {items}: axis {labels[index]!r} is set twice")
            seen.add(index)
            result[index] = number(value, f"{item!r}")
        return result
    if len(items) != len(current):
        raise ProcessorInputError(
            f"scales {items}: give one value per axis ({len(current)}: {list(labels)}), or pairs like Z=0.25")
    return [number(item, repr(item)) for item in items]


class AxisSwapProcessor(Processor):
    """Swap or reorder the axes of an image, and rename them."""

    name = "Swap / Relabel Axes"
    id = "axis-swap"
    category = "Dimensions and channels"
    kinds = ("image",)
    output_kind = "image"

    @classmethod
    def default_params(cls) -> dict:
        return {"order": "", "keep_labels": False, "labels": "", "scales": ""}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("order", "text", "", label="Order", help=_ORDER_HELP),
            ParamField("keep_labels", "bool", False, label="Keep labels in place", help=_KEEP_HELP),
            ParamField("labels", "text", "", label="Labels", help=_LABELS_HELP),
            ParamField("scales", "text", "", label="Scales", help=_SCALES_HELP),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)
        order = QtWidgets.QLineEdit()
        order.setPlaceholderText("Z,T   or   Z,X,Y")
        order.setToolTip(_ORDER_HELP)
        keep = QtWidgets.QCheckBox("Keep labels in place")
        keep.setToolTip(_KEEP_HELP)
        labels = QtWidgets.QLineEdit()
        labels.setPlaceholderText("X,Y,T   or   Z=T")
        labels.setToolTip(_LABELS_HELP)
        scales = QtWidgets.QLineEdit()
        scales.setPlaceholderText("Z=0.25   or   Z=0.25, T=2")
        scales.setToolTip(_SCALES_HELP)
        layout.addRow("Order:", order)
        layout.addRow("", keep)
        layout.addRow("Labels:", labels)
        layout.addRow("Scales:", scales)

        def get_values():
            return {"order": order.text().strip(), "keep_labels": keep.isChecked(),
                    "labels": labels.text().strip(), "scales": scales.text().strip()}

        def set_values(values):
            order.setText(str(values.get("order", "") or ""))
            keep.setChecked(bool(values.get("keep_labels", False)))
            labels.setText(str(values.get("labels", "") or ""))
            scales.setText(str(values.get("scales", "") or ""))

        widget.get_values = get_values
        widget.set_values = set_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        shape = shape_for_result(result)
        labels = axis_labels_for_result(result)
        scales = axis_scales_for_result(result)
        permutation = parse_order(params.get("order", ""), labels)
        moved = permutation != tuple(range(len(shape)))
        if moved and not bool(params.get("keep_labels", False)):
            labels = [labels[index] for index in permutation]
            scales = [scales[index] for index in permutation]
        new_labels = parse_labels(params.get("labels", ""), labels)
        new_scales = parse_scales(params.get("scales", ""), new_labels, scales)

        data = result.data
        if moved:
            # A view of an in-memory array; a lazy array is read, as it has no transpose of its own.
            data = np.transpose(data if isinstance(data, np.ndarray) else np.asarray(data), permutation)

        # Pixels stay where they were only while the last two (displayed) axes do.
        tail = tuple(range(len(shape)))[-2:]
        # Changing the spacing of those two axes changes the physical grid.
        same_grid = permutation[-2:] == tail and list(new_scales[-2:]) == [float(v) for v in scales[-2:]]
        out = ArrayProcessingResult(
            name=f"{result.name} (axes)",
            data=data,
            axis_labels=new_labels,
            view_modes=list(getattr(result, "view_modes", [])),
            display_levels=getattr(result, "display_levels", None),
            axis_scales=new_scales,
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={
                "source_result": result.name,
                "operation": self.id,
                "source_axis_labels": axis_labels_for_result(result),
                "permutation": list(permutation),
                "axis_labels": list(new_labels),
                "axis_scales": list(new_scales),
            },
        )
        return out.adopt_identity_from(result, same_grid=same_grid)


__all__ = ["AxisSwapProcessor", "ProcessorInputError", "parse_labels", "parse_order", "parse_scales"]
