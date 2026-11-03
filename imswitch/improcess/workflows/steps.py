"""The workflow schema, as ImProcess sees it.

The schema itself lives in :mod:`imswitch.imcommon.model.workflow.steps`
(shared with the editor and, later, ImControl's pipeline designer); this
module re-exports it under the path ImProcess always used, and keeps
ImProcess's two host checks in :func:`validate` even when no registry is
given: the headless contract (:func:`~imswitch.improcess.model.plugin_contract.contract_problem`)
and the save formats ImProcess can write
(:func:`~imswitch.improcess.model.save_protocol.normalize_format`).
"""

from __future__ import annotations

from imswitch.imcommon.model.workflow.steps import *  # noqa: F401,F403
from imswitch.imcommon.model.workflow.steps import (  # noqa: F401 - names tests and tools import
    _BUILTIN_KINDS,
    _ID_PATTERN,
    _REF_PATTERN,
    _STEP_REGISTRY,
    _STEP_RUNNERS,
    _STEP_TYPES,
    _block_style_dumper,
    _check_id,
    _defaults,
    _kind_issues,
    _port_ok,
    _unknown_params,
    declared_kinds,
    Save,
    Source,
    Workflow,
    WorkflowError,
)
from imswitch.imcommon.model.workflow.steps import __all__ as _ALL
from imswitch.imcommon.model.workflow.steps import validate as _validate


def validate(workflow, registry=None, *, contract_problem=None, normalize_format=None):
    """:func:`imswitch.imcommon.model.workflow.steps.validate` with ImProcess's
    headless contract and save formats, whether or not ``registry`` has them."""
    if contract_problem is None:
        from imswitch.improcess.model.plugin_contract import contract_problem
    if normalize_format is None:
        from imswitch.improcess.model.save_protocol import normalize_format
    return _validate(workflow, registry, contract_problem=contract_problem, normalize_format=normalize_format)


def unbound_inputs(workflow: "Workflow") -> list[str]:
    """Ids of the path-less input sources, the ones a run must be given.

    These are what an export around an interactive step (Fiji, a napari
    plugin) leaves where that step was: replay marks them in their metadata.
    """
    return [
        step.id for step in workflow.steps
        if isinstance(step, Source) and not step.source.bound
        and (step.source.metadata or {}).get("input")
    ]


def part_from_input(workflow: "Workflow", input_id: str) -> "Workflow":
    """The steps a run starting at ``input_id`` needs, and nothing else.

    A workflow exported around a Fiji step has two branches: one that remakes
    what was sent to Fiji, and one that continues from what came back. Applied
    to an image that came back, only the second should run. Kept: every Save
    downstream of the input, and every step those Saves depend on (which
    includes a step that combines the input with something from the other
    branch). Dropped: Saves, and their chains, that the input does not reach.
    """
    by_id = {step.id: step for step in workflow.steps}

    def inputs_of(step) -> list[str]:
        if isinstance(step, Save):
            return [step.input.step]
        return [ref.step for ref in getattr(step, "inputs", []) or []]

    downstream = {input_id}
    changed = True
    while changed:
        changed = False
        for step in workflow.steps:
            if step.id not in downstream and any(i in downstream for i in inputs_of(step)):
                downstream.add(step.id)
                changed = True
    saves = [s.id for s in workflow.steps if isinstance(s, Save) and s.id in downstream]
    if not saves:
        raise WorkflowError(f"no output depends on the input {input_id!r}")
    needed: set[str] = set()
    pending = list(saves)
    while pending:
        sid = pending.pop()
        if sid in needed:
            continue
        needed.add(sid)
        pending.extend(i for i in inputs_of(by_id[sid]) if i in by_id)
    return Workflow(
        workflow.name,
        [step for step in workflow.steps if step.id in needed],
        schema=workflow.schema,
        description=workflow.description,
        metadata=dict(workflow.metadata),
    )


__all__ = list(_ALL) + ["unbound_inputs", "part_from_input"]


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
