"""A pipeline's exposed parameters, as the Smart Pipeline panel shows them.

A pipeline exposes a parameter the way any workflow does (schema 2): a
**control** in the file names it (``name``), says which step parameters it
sets (``targets``, each ``"step.param"``) and how to show it (``label``,
``help``). The value lives in the target steps; the type, default, bounds
and unit are those of the parameter's own field -- a processor's declared
spec, a gate rule's field, an action's catalogue entry -- so a control
cannot disagree with what it sets. The designer exposes a parameter from
its context menu (*Expose as control…*); this module is the panel's side:
what a pipeline exposes (:func:`exposed_parameters`), a value checked as the
control would check it (:func:`checked_value`), and the parameter changes a
set of values makes (:func:`changes_for`, :func:`apply_changes`).

**While armed.** The engine reads a processing step's and a gate's
parameters every frame and an action's when it starts, so an edit can be
applied to the running pipeline between two frames
(:meth:`~.engine.Engine.set_params`), except where that would change what
the run reserved or checked at arm: an action parameter whose new value
changes the devices the action needs (:func:`live_refusal`). Such an edit
is kept for the next arm, and the panel says so.

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from imswitch.imcommon.model.smartpipeline import Action
from imswitch.imcommon.model.smartpipeline.descriptors import param_field
from imswitch.imcommon.model.workflow.prepare import CombinedField, ControlState, control_states, control_value


@dataclass
class ExposedParameter:
    """One control of a pipeline, ready for a form."""

    name: str
    label: str
    help: str = ""
    #: The combined field of its targets (type, bounds, unit, default), or
    #: ``None`` when it cannot be shown (``problem`` says why).
    spec: CombinedField | None = None
    #: The value the file holds (its targets agree on it), or ``None``.
    file_value: Any = None
    #: ``(step id, param)`` of every target that runs.
    targets: list = field(default_factory=list)
    problem: str = ""
    state: ControlState | None = None

    @property
    def unit(self) -> str:
        return self.spec.suffix if self.spec is not None else ""

    @property
    def usable(self) -> bool:
        return self.spec is not None and bool(self.targets) and not self.problem


def field_for(actions=None):
    """The ``field_for`` hook of a pipeline: gates' and actions' parameters
    may be exposed too (``actions`` is the action catalogue)."""
    return lambda step, key: param_field(step, key, actions)


def exposed_parameters(workflow, *, registry=None, actions=None) -> list[ExposedParameter]:
    """Every control of ``workflow`` as an :class:`ExposedParameter`, in file
    order. ``registry`` is ImProcess's (the fields of processing steps; without
    it those controls carry a problem), ``actions`` the action catalogue."""
    controls = list(getattr(workflow, "controls", None) or [])
    if not controls:
        return []
    states, issues = control_states(workflow, registry, field_for=field_for(actions))
    out = []
    for control in controls:
        state = states.get(control.name)
        mine = [issue.message for issue in issues if f"control {control.name!r}" in issue.message]
        spec = state.spec if state is not None else None
        problem = mine[0] if mine else ""
        if not problem and state is not None and state.targets and spec is None:
            problem = "the fields of its parameters are not known (is the processor installed?)"
        if not problem and state is not None and not state.active:
            problem = "none of its parameters runs"
        out.append(ExposedParameter(
            name=control.name, label=(spec.label if spec is not None else "") or control.label or control.name,
            help=(spec.help if spec is not None else "") or control.help,
            spec=spec, file_value=state.value if state is not None and state.has_value else None,
            targets=[(t.step, t.param) for t in (state.targets if state is not None else []) if t.active],
            problem=problem, state=state,
        ))
    return out


def checked_value(parameter: ExposedParameter, value):
    """``value`` made canonical and checked against the parameter's field and
    every target's own (bounds included). Raises ``ValueError``."""
    if parameter.state is None or parameter.spec is None:
        raise ValueError(f"{parameter.name}: {parameter.problem or 'cannot be set'}")
    return control_value(value, parameter.state)


def changes_for(parameters, values: dict) -> dict:
    """``{(step id, param): value}`` that setting ``values`` (``{name:
    value}``, already checked) makes; unknown names are ignored."""
    by_name = {p.name: p for p in parameters}
    changes = {}
    for name, value in values.items():
        parameter = by_name.get(name)
        if parameter is None:
            continue
        for target in parameter.targets:
            changes[tuple(target)] = value
    return changes


def apply_changes(workflow, changes: dict) -> None:
    """Write ``changes`` into the steps of ``workflow`` (a copy to arm)."""
    by_id = {step.id: step for step in workflow.steps}
    for (step_id, key), value in changes.items():
        step = by_id.get(step_id)
        if step is not None and isinstance(getattr(step, "params", None), dict):
            step.params = {**step.params, key: value}


def live_refusal(workflow, changes: dict, backend) -> str:
    """Why ``changes`` cannot be applied to the running pipeline (they are
    then for the next arm), or ``""``: an action whose devices would change
    -- what the run reserved and preflight checked at arm."""
    resources_of = getattr(backend, "resources_for_step", None)
    problems_of = getattr(backend, "step_problems", None)
    by_id = {step.id: step for step in workflow.steps}
    touched: dict[str, dict] = {}
    for (step_id, key), value in changes.items():
        step = by_id.get(step_id)
        if isinstance(step, Action):
            touched.setdefault(step_id, dict(step.params))[key] = value
    for step_id, params in touched.items():
        step = by_id[step_id]
        if callable(resources_of):
            try:
                if resources_of(step.action, params) != resources_of(step.action, step.params):
                    return f"{step_id} would use other devices than it reserved at arm"
            except Exception as exc:  # noqa: BLE001 - an unreadable change is not applied live
                return f"{step_id}: {exc}"
        if callable(problems_of):
            problems = problems_of(step.action, params)
            if problems:
                return f"{step_id}: {problems[0]}"
    return ""


__all__ = ["ExposedParameter", "apply_changes", "changes_for", "checked_value", "exposed_parameters", "field_for",
           "live_refusal"]


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
