"""A smart pipeline as a document: a workflow with pipeline settings and the
rules a pipeline must keep beyond those of a workflow.

**Shape (v1).** One source (a ``live`` detector or an ImProcess ``source``
for a replay), then a processing graph (ImProcess steps, transforms, gates),
then **one sequential action chain**: the action steps in file order, each
starting after the one before it completed, with the values the tick had
when the chain started. Sinks may read anything. A gate that does not fire
suppresses everything downstream of it for that tick. The engine owns the
loop; this module only says what is well-formed.

**The coordinate rule (strict, decision 4 of the plan).** Points reach an
action only through a ``transform`` step -- ``identity`` when the frames
already match, one click away but never implicit -- and when the transform
registry is at hand the transform's target frame must be the frame the
action says it takes. So a camera pixel can never become a stage position
by accident.

The settings (roles, replay interval) ride in ``workflow.metadata`` under
:data:`PIPELINE_METADATA_KEY`, so the file stays a workflow file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imswitch.imcommon.model.kinds import EVENT_KIND, POINTS_KIND, RESULT_KINDS, UNKNOWN_KIND
from imswitch.imcommon.model.smartpipeline.actions import ActionCatalog
from imswitch.imcommon.model.smartpipeline.frames import CoordinateError, FrameTag
from imswitch.imcommon.model.smartpipeline.steps import (
    FIRED_PORT,
    Action,
    Gate,
    Live,
    Sink,
    Transform,
)
from imswitch.imcommon.model.smartpipeline.transforms import TransformRegistry
from imswitch.imcommon.model.workflow.steps import (
    Issue,
    Process,
    Ref,
    Save,
    Source,
    Workflow,
    WorkflowError,
    declared_kinds,
    validate,
)

PIPELINE_METADATA_KEY = "smart_pipeline"

#: The smart-microscopy roles a pipeline may name a setup mode for.
ROLES = ("scouting", "event", "resume", "idle")


@dataclass
class PipelineSettings:
    """What the pipeline needs beyond its steps.

    ``roles`` maps a role (:data:`ROLES`) to the setup mode applied when the
    pipeline enters it; without a ``scouting``/``idle`` pair the engine arms
    only in Visualize. ``replay_interval_s`` is the simulated time between
    two frames of a replayed recording (0: the recording's own timestamps,
    else one frame per index).
    """

    roles: dict[str, str] = field(default_factory=dict)
    replay_interval_s: float = 0.0

    def __post_init__(self):
        roles = {}
        for role, mode in dict(self.roles or {}).items():
            role = str(role).lower()
            if role not in ROLES:
                raise WorkflowError(f"pipeline role {role!r} is not one of {ROLES}")
            if mode:
                roles[role] = str(mode)
        self.roles = roles
        self.replay_interval_s = float(self.replay_interval_s or 0.0)
        if self.replay_interval_s < 0:
            raise WorkflowError("replay_interval_s must not be negative")

    @property
    def arms_live(self) -> bool:
        """Whether the roles allow an Experiment or Validate run."""
        return "scouting" in self.roles and "idle" in self.roles

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {}
        if self.roles:
            payload["roles"] = dict(self.roles)
        if self.replay_interval_s:
            payload["replay_interval_s"] = self.replay_interval_s
        return payload

    @classmethod
    def from_dict(cls, payload) -> "PipelineSettings":
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise WorkflowError(f"metadata.{PIPELINE_METADATA_KEY} must be an object")
        unknown = sorted(set(payload) - {"roles", "replay_interval_s"})
        if unknown:
            raise WorkflowError(f"metadata.{PIPELINE_METADATA_KEY}: unknown key(s) {unknown}")
        return cls(roles=dict(payload.get("roles") or {}),
                   replay_interval_s=payload.get("replay_interval_s", 0.0))


def pipeline_settings(workflow: Workflow) -> PipelineSettings:
    return PipelineSettings.from_dict((workflow.metadata or {}).get(PIPELINE_METADATA_KEY))


def source_step_of(workflow: Workflow):
    """The pipeline's one source step (a ``Live`` or a ``Source``), or ``None``."""
    for step in workflow.steps:
        if isinstance(step, (Live, Source)):
            return step
    return None


def action_chain(workflow: Workflow) -> list[Action]:
    """The action steps, in the order they run."""
    return [step for step in workflow.steps if isinstance(step, Action)]


class SmartPipelineDocument:
    """A workflow plus its pipeline settings, saved as one workflow file."""

    def __init__(self, workflow: Workflow | None = None, *, name: str = "pipeline",
                 settings: PipelineSettings | None = None):
        self.workflow = workflow if workflow is not None else Workflow(name)
        if settings is not None:
            self.settings = settings

    @property
    def name(self) -> str:
        return self.workflow.name

    @property
    def steps(self) -> list:
        return self.workflow.steps

    @property
    def settings(self) -> PipelineSettings:
        return pipeline_settings(self.workflow)

    @settings.setter
    def settings(self, value: PipelineSettings) -> None:
        metadata = dict(self.workflow.metadata or {})
        payload = value.to_dict()
        if payload:
            metadata[PIPELINE_METADATA_KEY] = payload
        else:
            metadata.pop(PIPELINE_METADATA_KEY, None)
        self.workflow.metadata = metadata

    @property
    def source(self):
        return source_step_of(self.workflow)

    @property
    def actions(self) -> list[Action]:
        return action_chain(self.workflow)

    def validate(self, *, registry=None, actions: ActionCatalog | None = None,
                 transforms: TransformRegistry | None = None, **hooks) -> list[Issue]:
        return validate_pipeline(self.workflow, registry=registry, actions=actions, transforms=transforms, **hooks)

    # -- files --------------------------------------------------------------

    def to_dict(self) -> dict:
        return self.workflow.to_dict()

    @classmethod
    def from_dict(cls, payload) -> "SmartPipelineDocument":
        document = cls(Workflow.from_dict(payload))
        document.settings  # noqa: B018 - refuse bad settings on load, not on first use
        return document

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)

    def save(self, path) -> Path:
        return self.workflow.save(path)

    @classmethod
    def load(cls, path) -> "SmartPipelineDocument":
        return cls.from_dict(Workflow.load(path).to_dict())


# --------------------------------------------------------------------------
# kinds and checks
# --------------------------------------------------------------------------

def pipeline_kinds(workflow: Workflow, registry=None) -> dict[str, str]:
    """What each step's default port carries (step id -> kind), with the
    pipeline kinds filled in: a live source makes images, a transform points,
    a gate what it is given, an action an event."""
    return declared_kinds(workflow, registry)


def ref_kind(workflow: Workflow, ref: Ref, kinds: dict[str, str]) -> str:
    """The kind on ``ref`` (a gate's ``fired`` port is an event)."""
    try:
        step = workflow.step(ref.step)
    except KeyError:
        return UNKNOWN_KIND
    if isinstance(step, Gate) and ref.port == FIRED_PORT:
        return EVENT_KIND
    return str(kinds.get(ref.step, UNKNOWN_KIND))


def _parse_tag(tag: str):
    try:
        return FrameTag.parse(tag)
    except CoordinateError:
        return None


def declared_frame(workflow: Workflow, ref: Ref) -> str:
    """The frame tag the points on ``ref`` are measured in, as the step that
    made them declares it (a ``frame`` parameter, as regionprops has), looking
    through gates and picks; "" when nothing declares one."""
    seen = set()
    while ref.step not in seen:
        seen.add(ref.step)
        try:
            step = workflow.step(ref.step)
        except KeyError:
            return ""
        if isinstance(step, Gate) and ref.port != FIRED_PORT and step.inputs:
            ref = step.inputs[0]
            continue
        if isinstance(step, Process):
            frame = (step.params or {}).get("frame")
            if isinstance(frame, str) and frame and frame != "unknown":
                return frame
            if step.processor == "pick" and step.inputs:
                ref = step.inputs[0]
                continue
        return ""
    return ""


def _points_producer(workflow: Workflow, ref: Ref):
    """The step whose points ``ref`` ultimately carries, looking through
    gates (which pass their input on) and pick-like processing steps (which
    keep the frame of what they are given)."""
    seen = set()
    while ref.step not in seen:
        seen.add(ref.step)
        try:
            step = workflow.step(ref.step)
        except KeyError:
            return None
        if isinstance(step, Gate) and ref.port != FIRED_PORT and step.inputs:
            ref = step.inputs[0]
            continue
        if isinstance(step, Process) and step.processor == "pick" and step.inputs:
            ref = step.inputs[0]
            continue
        return step
    return None


def validate_pipeline(workflow: Workflow, *, registry=None, actions: ActionCatalog | None = None,
                      transforms: TransformRegistry | None = None, contract_problem=None,
                      normalize_format=None) -> list[Issue]:
    """Everything wrong with ``workflow`` as a smart pipeline that can be
    known before arming: the workflow's own checks, then the pipeline's
    (one source, first; each step's arity and names; the action chain's
    inputs; the coordinate rule). ``actions`` and ``transforms`` make the
    action ids, their parameters and the frames checkable; without them
    those checks are skipped, not failed.
    """
    issues = list(validate(workflow, registry, contract_problem=contract_problem, normalize_format=normalize_format))
    steps = list(workflow.steps)
    try:
        settings = pipeline_settings(workflow)
    except WorkflowError as exc:
        issues.append(Issue("pipeline", str(exc)))
        settings = PipelineSettings()
    sources = [step for step in steps if isinstance(step, (Live, Source))]
    if not sources:
        issues.append(Issue("pipeline", "a pipeline starts with one source: a live detector or a recording"))
    elif len(sources) > 1:
        issues.append(Issue(sources[1].id, "a pipeline has one source; this is the second"))
    elif steps and steps[0] is not sources[0]:
        issues.append(Issue(sources[0].id, "the source must be the first step"))
    for step in steps:
        if isinstance(step, Live) and not step.detector:
            issues.append(Issue(step.id, "a live source names a detector"))
    kinds = pipeline_kinds(workflow, registry)
    for step in steps:
        if isinstance(step, (Transform, Gate, Sink)) and len(step.inputs) != 1:
            issues.append(Issue(step.id, f"a {step.kind} step takes exactly one input, got {len(step.inputs)}"))
            continue
        if isinstance(step, Transform):
            kind = ref_kind(workflow, step.inputs[0], kinds)
            if kind in RESULT_KINDS and kind != POINTS_KIND:
                issues.append(Issue(step.id, f"a transform takes points; {step.inputs[0]} carries {kind}"))
            if transforms is not None and step.transform not in transforms:
                issues.append(Issue(step.id, f"unknown transform {step.transform!r} (known: {', '.join(transforms.ids()) or 'none'})"))
            tag = declared_frame(workflow, step.inputs[0])
            transform = transforms.get(step.transform) if transforms is not None else None
            if tag and transform is not None and str(transform.source_frame) != tag:
                issues.append(Issue(step.id, f"{step.transform!r} reads {transform.source_frame!r}, but the points "
                                             f"are measured in {tag!r}"))
            source = source_step_of(workflow)
            if tag and isinstance(source, Live) and source.detector:
                parsed = _parse_tag(tag)
                if parsed is not None and parsed.kind == "detector" and parsed.name != source.detector:
                    issues.append(Issue(step.id, f"the points are measured in {tag!r}, but the source is "
                                                 f"detector {source.detector!r}"))
        elif isinstance(step, Action):
            issues.extend(_action_issues(workflow, step, kinds, actions, transforms))
    if action_chain(workflow) and not settings.arms_live:
        # Not an error: the engine says so when asked to arm for real.
        pass
    return issues


def _action_issues(workflow, step: Action, kinds, actions, transforms) -> list[Issue]:
    issues: list[Issue] = []
    spec = actions.get(step.action) if actions is not None else None
    if actions is not None and spec is None:
        issues.append(Issue(step.id, f"unknown action {step.action!r} (known: {', '.join(actions.ids()) or 'none'})"))
    if spec is not None:
        unknown = sorted(set(step.params) - set(spec.params))
        if unknown:
            issues.append(Issue(step.id, f"unknown parameter(s) for {step.action!r}: {unknown} "
                                         f"(accepted: {sorted(spec.params) or 'none'})"))
    action_frame = None
    if spec is not None:
        try:
            action_frame = spec.frame_of(step.params)
            if action_frame:
                FrameTag.parse(action_frame)
        except CoordinateError as exc:
            issues.append(Issue(step.id, str(exc)))
            action_frame = None
    for index, ref in enumerate(step.inputs):
        kind = ref_kind(workflow, ref, kinds)
        accepted = spec.accepted_kind(index) if spec is not None else "any"
        if accepted != "any" and kind in RESULT_KINDS and kind != accepted:
            issues.append(Issue(step.id, f"{step.action!r} takes {accepted} on input {index + 1}; {ref} carries {kind}"))
        # The coordinate rule holds for points the graph declares and for an
        # input the action will read as points, whatever the graph knows.
        if kind != POINTS_KIND and accepted != POINTS_KIND:
            continue
        producer = _points_producer(workflow, ref)
        if not isinstance(producer, Transform):
            issues.append(Issue(
                step.id,
                f"points from {ref} reach the action without a transform step; put one in between "
                "(transform 'identity' if they are already in the frame the action takes)",
            ))
            continue
        if transforms is not None and action_frame:
            transform = transforms.get(producer.transform)
            if transform is not None and str(transform.target_frame) != str(FrameTag.parse(action_frame)):
                issues.append(Issue(
                    step.id,
                    f"{producer.id!r} writes {transform.target_frame!r}, but {step.action!r} takes {action_frame!r}",
                ))
    return issues


__all__ = [
    "PIPELINE_METADATA_KEY",
    "ROLES",
    "PipelineSettings",
    "SmartPipelineDocument",
    "action_chain",
    "pipeline_kinds",
    "pipeline_settings",
    "ref_kind",
    "source_step_of",
    "validate_pipeline",
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
