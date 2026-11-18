"""Auto-indexing: run a plugin once per index of the axes in front of its core dims.

LabVIEW wires an array into a loop and the loop indexes it; nobody writes
the loop. Here a plugin that declares its core dims
(:mod:`~imswitch.improcess.model.core_dims`) is handed an input with more
axes, and the axes in front -- T, C, a stage position -- are looped over:
each index is sliced out (only that slice is read from a lazy source), the
plugin runs on it, and the outputs are stacked with the loop axes kept in
front. A SNOUTY deskew of a ``(T, Z, Y, X)`` stack is then 120 deskews of a
``(Z, Y, X)`` stack, and a MoNaLISA run on the deskewed output is 120
MoNaLISA runs, without a ``for`` anywhere in the workflow.

The rules that keep it a DAG and keep it replayable:

* the provenance gets **one** node for the step, with ``iterated_over``
  (``{"T": 120}``), never one node per index; a replay loops again because
  the data has the same axes;
* the per-index call shares no mutable state with the others (it could run
  in parallel later);
* every index checks for cancellation first, and a cancelled loop leaves no
  half-stacked result anywhere;
* outputs of different shapes across indices are an error naming the index.

The one helper here is used by every path that runs a plugin -- the
workflow runner and the GUI's reconstruct paths all go through
:func:`~imswitch.improcess.reconstructors.run.run_reconstruction` -- so the
loop is written once.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np

from imswitch.improcess.model.core_dims import (
    CoreMatch,
    match_core_dims,
    wants_iteration,
    without_reserved,
)

#: Fraction of the memory available when the first index finished that the
#: stacked output may take. In memory only for now: beyond this the run
#: stops with a message rather than swapping the machine to a halt.
#: TODO: write the iterations into a temporary zarr store and return a lazy
#: result instead (the resource-estimate machinery decides the threshold).
MEMORY_FRACTION = 0.8


class IterationError(RuntimeError):
    """An auto-indexed run could not be put together."""


def plan(plugin, axis_labels: Sequence[str], shape: Sequence[int], params: dict | None, *,
         labels_declared: bool = True) -> CoreMatch:
    """How ``plugin`` will be run over an input of these axes: the loop (maybe empty).

    No loop when the plugin declares no core dims (``None`` or
    ``WHOLE_INPUT``), when the step asked for the whole stack
    (``_iterate: false``), or when the input's axis labels are a *guess*
    (``labels_declared=False``: a raw file that names no axes gets labels from
    its rank alone). Guessed labels must not decide which axis is time and
    which is a scan axis, so such an input is handed to the plugin whole, as
    before auto-indexing existed. Results, and files that name their axes,
    are looped.
    """
    core = getattr(plugin, "core_dims_in", None)
    if not core or not wants_iteration(params) or not labels_declared:
        return CoreMatch(core_labels=tuple(str(label) for label in axis_labels))
    return match_core_dims(axis_labels, shape, core)


def index_text(match: CoreMatch, index: tuple[int, ...]) -> str:
    """``T=3/120`` (1-based), ``T=1/3, C=2/2`` for a progress line."""
    return ", ".join(
        f"{label}={position + 1}/{size}"
        for label, position, size in zip(match.loop_labels, index, match.loop_shape)
    )


def unindexed_name(name: str, match: CoreMatch) -> str:
    """``name`` without the ``[T=1/120]`` the first slice was given.

    The per-index results are named after their slice; the stacked result
    stands for every index, and a downstream step's provenance label must not
    read as if only the first one was used.
    """
    first = f" [{index_text(match, (0,) * len(match.loop_shape))}]"
    return str(name).replace(first, "") if first.strip(" []") else str(name)


def iterated_over(match: CoreMatch) -> dict[str, int]:
    """``{"T": 120}``: what the provenance node records."""
    return {str(label): int(size) for label, size in zip(match.loop_labels, match.loop_shape)}


def _available_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def guard_memory(first, count: int, what: str) -> None:
    """Refuse a loop whose stacked output cannot fit in memory, after one index."""
    nbytes = int(getattr(np.asarray(getattr(first, "data", first)), "nbytes", 0)) if first is not None else 0
    available = _available_bytes()
    if not nbytes or available is None:
        return
    needed = nbytes * int(count)
    if needed > available * MEMORY_FRACTION:
        from imswitch.imcommon.model import memory_limits

        raise IterationError(
            f"{what}: the {count} outputs together need {memory_limits.describeBytes(needed)}, "
            f"more than the {memory_limits.describeBytes(available)} of memory available; "
            "crop or bin the input first, or run fewer indices at a time"
        )


def check_cancelled(cancel: Callable[[], bool] | None, context=None) -> None:
    """Stop before the next index when asked to (either way of asking)."""
    if context is not None:
        context.check_cancelled()
    if cancel is not None and cancel():
        from imswitch.imcommon.model import OperationCancelled

        raise OperationCancelled("cancelled")


# --------------------------------------------------------------------------
# stacking
# --------------------------------------------------------------------------

def _loop_label(label: str, taken: set[str]) -> str:
    """``label`` unless an output axis already has it; then a distinct name."""
    name = str(label)
    while name in taken:
        name = f"{name}_loop"
    return name


def stack_arrays(arrays: list, out_labels: Sequence[str], match: CoreMatch) -> tuple[np.ndarray, list[str], list[tuple]]:
    """``(data, labels, origin)`` of the per-index arrays stacked.

    The loop axes go in front -- except a loop axis whose label the output
    already has with length 1 (MoNaLISA's ``T`` of one timepoint per call):
    the indices fill that axis instead of adding a second ``T``. ``origin``
    says, per final axis, ``(loop axis or None, output axis or None)``.
    """
    first = np.asarray(arrays[0])
    shape = first.shape
    for position, array in enumerate(arrays[1:], start=1):
        if np.shape(array) != shape:
            index = np.unravel_index(position, match.loop_shape)
            raise IterationError(
                f"the output at {index_text(match, tuple(int(i) for i in index))} has shape "
                f"{tuple(np.shape(array))}, the first one {tuple(shape)}: the outputs of an "
                "auto-indexed run must all have one shape"
            )
    out_labels = [str(label) for label in out_labels]
    if len(out_labels) != len(shape):
        out_labels = [f"axis{index}" for index in range(len(shape))]
    data = np.stack([np.asarray(array) for array in arrays], axis=0).reshape(tuple(match.loop_shape) + shape)
    loops = len(match.loop_shape)
    labels: list[str] = []
    origin: list[tuple] = []
    # The final order: the output's axes, with a size-1 axis of a loop's label
    # replaced by that loop; the remaining loop axes in front.
    absorbed: dict[int, int] = {}
    for loop_index, label in enumerate(match.loop_labels):
        hits = [i for i, out in enumerate(out_labels) if out == label and shape[i] == 1]
        if len(hits) == 1 and hits[0] not in absorbed:
            absorbed[hits[0]] = loop_index
    taken = set(out_labels)
    order: list[int] = []
    front = [i for i in range(loops) if i not in absorbed.values()]
    for loop_index in front:
        order.append(loop_index)
        name = _loop_label(match.loop_labels[loop_index], taken)
        taken.add(name)
        labels.append(name)
        origin.append((loop_index, None))
    for out_index, label in enumerate(out_labels):
        if out_index in absorbed:
            order.append(absorbed[out_index])
            origin.append((absorbed[out_index], out_index))
        else:
            order.append(loops + out_index)
            origin.append((None, out_index))
        labels.append(label)
    squeezed_axes = tuple(loops + out_index for out_index in absorbed)
    # Drop the size-1 axes the loops take the place of, then put every axis
    # where ``order`` says.
    keep = [axis for axis in range(data.ndim) if axis not in squeezed_axes]
    data = np.squeeze(data, axis=squeezed_axes) if squeezed_axes else data
    position = {axis: index for index, axis in enumerate(keep)}
    data = np.transpose(data, [position[axis] for axis in order])
    return np.ascontiguousarray(data), labels, origin


def _same(a, b) -> bool:
    try:
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
            return np.array_equal(np.asarray(a), np.asarray(b))
        return bool(a == b)
    except Exception:
        return False


def common_attrs(results: list) -> dict:
    """The attributes every per-index result agrees on.

    The stacked result stands for all indices; an attribute that differs
    between them (a value one call derived from its own slice) describes one
    index, not the stack, and a later step must not read it as if it held for
    all -- so it is left out rather than taken from the first.
    """
    if not results:
        return {}
    first = dict(getattr(results[0], "attrs", None) or {})
    others = [dict(getattr(r, "attrs", None) or {}) for r in results[1:]]
    return {key: value for key, value in first.items()
            if all(key in other and _same(value, other[key]) for other in others)}


def stack_results(results: list, match: CoreMatch, *, loop_scales=None):
    """One result from the per-index results (the default
    :meth:`Reconstructor.stack_results <imswitch.improcess.reconstructors.base.Reconstructor.stack_results>`).

    The type is generic (an image, or labels if the outputs were labels):
    a plugin whose own result type matters overrides the hook. Scales come
    from the input for the loop axes and from the outputs for theirs; the
    unit is the first output's; the attributes are those every output agrees
    on (:func:`common_attrs`).
    """
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.contrast import finite_range
    from imswitch.improcess.model.labels_result import LabelsResult

    if not results:
        raise IterationError("an auto-indexed run produced nothing")
    first = results[0]
    data, labels, origin = stack_arrays([r.data for r in results], list(getattr(first, "axis_labels", []) or []), match)
    out_scales = list(getattr(first, "axis_scales", None) or [])
    if len(out_scales) != np.ndim(first.data):
        out_scales = [1.0] * int(np.ndim(first.data))
    loop_scales = list(loop_scales or [1.0] * len(match.loop_shape))
    scales = []
    for loop_index, out_index in origin:
        if loop_index is not None:
            scales.append(float(loop_scales[loop_index]) if loop_index < len(loop_scales) else 1.0)
        else:
            scales.append(float(out_scales[out_index]))
    metadata = dict(getattr(first, "metadata", None) or {})
    if getattr(first, "kind", "image") == "labels":
        stacked = LabelsResult(first.name, data, labels, axis_scales=scales,
                               scale_unit=getattr(first, "scale_unit", "px"), metadata=metadata)
    else:
        stacked = ArrayProcessingResult(first.name, data, labels, axis_scales=scales,
                                        scale_unit=getattr(first, "scale_unit", "px"),
                                        display_levels=finite_range(data), metadata=metadata)
    stacked.attrs = common_attrs(results)
    return stacked


# --------------------------------------------------------------------------
# reconstructions
# --------------------------------------------------------------------------

def slice_input(data_obj, match: CoreMatch, index: tuple[int, ...], *, attrs: dict, scales: list[float]):
    """One index of ``data_obj`` as a ``DataObj`` with only the core axes.

    Read through the source's handle, so a lazy source reads that slice and
    nothing more. The attributes lose the raw-recording layout keys (see
    :func:`~imswitch.improcess.model.result_source.slice_attrs`): a slice is
    not the recording the layout describes.
    """
    from imswitch.improcess.model.result_source import ResultDataObj

    handle = getattr(data_obj, "data_handle", None)
    if handle is None:
        handle = data_obj.data
    piece = np.asarray(handle[tuple(index)])
    loops = len(match.loop_shape)
    name = f"{getattr(data_obj, 'name', 'data')} [{index_text(match, index)}]"
    return ResultDataObj(
        data_obj,
        name=name,
        data=piece,
        axis_labels=list(match.core_labels),
        axis_scales=list(scales[loops:]) if len(scales) == len(match.loop_shape) + piece.ndim else None,
        scale_unit=getattr(data_obj, "scale_unit", None),
        attrs=attrs,
        parent=getattr(data_obj, "provenance_parent", None) or data_obj,
    )


def reconstruct_each(reconstructor, data_obj, params: dict, match: CoreMatch, *, context=None,
                     progress: Callable[[int, int, str], None] | None = None,
                     cancel: Callable[[], bool] | None = None):
    """Run ``reconstructor.process`` once per loop index; returns the stacked result.

    No provenance is recorded here: the caller records one node for the
    whole loop.
    """
    from imswitch.improcess.model.result_source import slice_attrs

    try:
        attrs = slice_attrs(dict(getattr(data_obj, "attrs", None) or {}))
    except Exception:
        attrs = {}
    scales = list(getattr(data_obj, "axis_scales", None) or [])
    total = match.count
    outputs = []
    what = getattr(reconstructor, "name", None) or getattr(reconstructor, "id", "reconstruction")
    for number, index in enumerate(np.ndindex(*match.loop_shape)):
        check_cancelled(cancel, context)
        piece = slice_input(data_obj, match, index, attrs=attrs, scales=scales)
        if context is not None:
            output = reconstructor.process(piece, dict(params), context=context)
        else:
            output = reconstructor.process(piece, dict(params))
        if number == 0:
            guard_memory(output, total, str(what))
        outputs.append(output)
        if progress is not None:
            progress(number + 1, total, index_text(match, index))
    check_cancelled(cancel, context)
    stack = getattr(reconstructor, "stack_results", None)
    loop_scales = scales[: len(match.loop_shape)] if len(scales) >= len(match.loop_shape) else None
    if callable(stack):
        stacked = stack(outputs, match, loop_scales=loop_scales)
    else:
        stacked = stack_results(outputs, match, loop_scales=loop_scales)
    stacked.name = unindexed_name(stacked.name, match)
    return stacked


# --------------------------------------------------------------------------
# processors
# --------------------------------------------------------------------------

def slice_result(result, match: CoreMatch, index: tuple[int, ...]):
    """One index of ``result`` as a result with only the core axes."""
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.labels_result import LabelsResult
    from imswitch.improcess.model.result_source import slice_attrs

    piece = np.asarray(result.data[tuple(index)])
    loops = len(match.loop_shape)
    scales = list(getattr(result, "axis_scales", None) or [])
    core_scales = scales[loops:] if len(scales) == loops + piece.ndim else [1.0] * piece.ndim
    name = f"{result.name} [{index_text(match, index)}]"
    unit = getattr(result, "scale_unit", "px")
    if getattr(result, "kind", "image") == "labels":
        made = LabelsResult(name, piece, list(match.core_labels), axis_scales=core_scales, scale_unit=unit)
    else:
        made = ArrayProcessingResult(name, piece, list(match.core_labels), axis_scales=core_scales, scale_unit=unit)
    made.attrs = slice_attrs(getattr(result, "attrs", None) or {})
    return made


def process_each(processor, result, params: dict, match: CoreMatch, *,
                 progress: Callable[[int, int, str], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> tuple[list, list[str]]:
    """Run ``processor.apply`` once per loop index of one input.

    Returns ``(stacked results, ports)``: one stacked result per output port,
    in the order the first index produced them. No provenance is recorded
    here (the caller records one node).
    """
    from imswitch.improcess.processors.base import ProcessorOutput

    per_port: dict[str, list] = {}
    ports: list[str] | None = None
    total = match.count
    what = getattr(processor, "name", None) or getattr(processor, "id", "processor")
    for number, index in enumerate(np.ndindex(*match.loop_shape)):
        check_cancelled(cancel)
        output = processor.apply(slice_result(result, match, index), dict(params))
        if isinstance(output, ProcessorOutput):
            produced = list(output.results)
            keys = list(output.keys) if output.keys is not None else None
        elif isinstance(output, (list, tuple)):
            produced, keys = list(output), None
        else:
            produced, keys = [output], None
        if keys is None:
            keys = ["out"] if len(produced) == 1 else [f"out{i}" for i in range(len(produced))]
        if ports is None:
            ports = keys
            guard_memory(produced[0], total * len(produced), str(what))
        elif keys != ports:
            raise IterationError(
                f"{what}: the output at {index_text(match, index)} has ports {keys}, the first one "
                f"{ports}: the outputs of an auto-indexed run must match"
            )
        for key, item in zip(keys, produced):
            per_port.setdefault(key, []).append(item)
        if progress is not None:
            progress(number + 1, total, index_text(match, index))
    check_cancelled(cancel)
    scales = list(getattr(result, "axis_scales", None) or [])
    loop_scales = scales[: len(match.loop_shape)] if len(scales) >= len(match.loop_shape) else None
    stacked = [stack_results(per_port[key], match, loop_scales=loop_scales) for key in ports or []]
    for item in stacked:
        item.name = unindexed_name(item.name, match)
    return stacked, list(ports or [])


__all__ = [
    "IterationError",
    "MEMORY_FRACTION",
    "check_cancelled",
    "guard_memory",
    "index_text",
    "iterated_over",
    "plan",
    "process_each",
    "reconstruct_each",
    "slice_input",
    "slice_result",
    "common_attrs",
    "stack_arrays",
    "stack_results",
    "unindexed_name",
    "without_reserved",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
