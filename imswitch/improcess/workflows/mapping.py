"""Running a ``map`` step: a sub-workflow once per index, the outputs stacked.

Auto-indexing (:mod:`~imswitch.improcess.model.iteration`) loops *one* plugin
over the axes in front of its core dims. A ``map`` loops a *group* of steps --
deskew, crop, reconstruct -- over one named axis, for a chain whose
intermediates are only wanted frame by frame. It stays a DAG: the body is a
fixed list of steps, the number of indices is the length of the axis, and the
provenance records the body once with ``iterated_over``, never a node per
index, so a replay runs the same map again.

Each index runs the body through :func:`~imswitch.improcess.workflows.runner.run`
itself -- the same paths, checks and provenance as any workflow -- with the
slice and the broadcast outer results *bound* as results: the body is
prefixed with stand-in steps that are never run, only bound.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.workflows.steps import (
    DEFAULT_PORT,
    ITEM_STEP,
    Fold,
    Map,
    Process,
    Ref,
    Source,
    Workflow,
    WorkflowError,
)

#: The stand-in source the bound stand-ins hang from (never opened).
_ROOT = "_map_root"


def _standin(step_id: str, ports: list[str]) -> Process:
    """A step that is bound to a result and never run; it only has to
    validate, with the ports the body reads from it."""
    names = ", ".join(port for port in ports if port) or DEFAULT_PORT
    return Process(step_id, "python", {"code": "out = data\n", "ports": names}, inputs=[_ROOT])


def slice_along(result, axis_label: str, index: int):
    """``result`` at ``index`` of its ``axis_label`` axis, as a result without that axis."""
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.labels_result import LabelsResult
    from imswitch.improcess.model.result_source import slice_attrs

    labels = list(getattr(result, "axis_labels", None) or [])
    axis = labels.index(axis_label)
    key = (slice(None),) * axis + (int(index),)
    piece = np.asarray(result.data[key])
    scales = list(getattr(result, "axis_scales", None) or [1.0] * len(labels))
    rest_labels = labels[:axis] + labels[axis + 1:]
    rest_scales = scales[:axis] + scales[axis + 1:]
    name = f"{result.name} [{axis_label}={index + 1}]"
    unit = getattr(result, "scale_unit", "px")
    if getattr(result, "kind", "image") == "labels":
        made = LabelsResult(name, piece, rest_labels, axis_scales=rest_scales, scale_unit=unit)
    else:
        made = ArrayProcessingResult(name, piece, rest_labels, axis_scales=rest_scales, scale_unit=unit)
    made.attrs = slice_attrs(getattr(result, "attrs", None) or {})
    return made


def run_map(step: Map, report, registry, *, logger=None, progress=None, cancel=None):
    """Run ``step`` against the results in ``report``; returns the stacked output."""
    from imswitch.improcess.model import iteration
    from imswitch.improcess.model.core_dims import CoreMatch
    from imswitch.improcess.model.provenance import (
        _attach,
        _describe,
        _graphs_of,
        make_node,
        merge_nodes,
        output_ref,
    )
    from imswitch.improcess.workflows.runner import run

    if isinstance(step, Fold):
        raise WorkflowError(
            f"{step.id}: fold steps are designed (they load, validate and save) but cannot run "
            "yet; use a map, or a Python step that keeps its state, for now"
        )
    source = report.result(step.input)
    labels = list(getattr(source, "axis_labels", None) or [])
    if step.over not in labels:
        raise WorkflowError(
            f"{step.id}: the input has no {step.over!r} axis to map over (its axes are {', '.join(labels)})"
        )
    axis = labels.index(step.over)
    count = int(np.shape(source.data)[axis])
    outer = step.outer_refs()
    # One stand-in per outer step the body reads, bound to that step's
    # results (every port the body names).
    outer_ports: dict[str, list[str]] = {}
    for ref in outer:
        outer_ports.setdefault(ref.step, [])
        if ref.port and ref.port not in outer_ports[ref.step]:
            outer_ports[ref.step].append(ref.port)
    outer_bindings = {}
    for step_id, ports in outer_ports.items():
        if ports:
            outer_bindings[step_id] = {port: report.result(Ref(step_id, port)) for port in ports}
        else:
            outer_bindings[step_id] = report.result(Ref(step_id))
    standins = [Source(_ROOT)]
    standins += [_standin(ITEM_STEP, [])]
    standins += [_standin(step_id, ports) for step_id, ports in outer_ports.items()]
    body = Workflow(f"{step.id}-body", [*standins, *step.body])

    outputs = []
    for index in range(count):
        iteration.check_cancelled(cancel)
        item = slice_along(source, step.over, index)
        bindings = {ITEM_STEP: item, **outer_bindings}
        with run(body, registry=registry, bindings=bindings, logger=logger, cancel=cancel,
                 write_saves=False) as inner:
            produced = inner.result(step.output)
        if index == 0:
            iteration.guard_memory(produced, count, f"map {step.id!r}")
        outputs.append(produced)
        if progress is not None:
            progress(index + 1, count, f"{step.over}={index + 1}/{count}")
    iteration.check_cancelled(cancel)

    scales = list(getattr(source, "axis_scales", None) or [])
    match = CoreMatch(loop_labels=(step.over,), loop_shape=(count,),
                      core_labels=tuple(labels[:axis] + labels[axis + 1:]))
    stacked = iteration.stack_results(outputs, match, loop_scales=[scales[axis]] if len(scales) > axis else None)

    # One node for the whole map: the body as it was written, what was
    # looped over, and the inputs it read (the mapped one first).
    consumed = [source] + [
        value for binding in outer_bindings.values()
        for value in (binding.values() if isinstance(binding, dict) else [binding])
    ]
    broadcast = {}
    position = 1
    for step_id, binding in outer_bindings.items():
        if isinstance(binding, dict):
            broadcast[step_id] = {port: position + offset for offset, port in enumerate(binding)}
            position += len(binding)
        else:
            broadcast[step_id] = position
            position += 1
    node_id, node = make_node(
        "map",
        params={"over": step.over, "output": str(step.output)},
        inputs=[output_ref(item) for item in consumed],
        input_labels=[_describe(item) for item in consumed],
        outputs=(DEFAULT_PORT,),
        extra={
            "label": f"map over {step.over}",
            "body": step.to_dict()["body"],
            "broadcast": broadcast,
            "iterated_over": {step.over: count},
        },
    )
    ancestors = merge_nodes(*(graph.get("nodes", {}) for graph in _graphs_of(consumed)))
    _attach(stacked, merge_nodes(ancestors, {node_id: node}), node_id, DEFAULT_PORT)
    return stacked


__all__ = ["run_map", "slice_along"]


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
