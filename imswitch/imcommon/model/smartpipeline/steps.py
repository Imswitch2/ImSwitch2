"""The five step kinds a smart pipeline adds to a workflow.

Registered with the workflow's step-type registry at import, so a pipeline
file loads through ``Workflow.load`` like any workflow and the editor finds
the classes by kind. Each declares its ``ports`` (what a later step may
reference) and ``declared_kind`` (what those ports carry, given what the
inputs carry), which is all the schema's checks and the editor ask of a
step kind. None of them runs here: the engine
(``imswitch.imcontrol.model.smartpipeline``) does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from imswitch.imcommon.model.kinds import EVENT_KIND, POINTS_KIND, UNKNOWN_KIND
from imswitch.imcommon.model.workflow.steps import (
    DEFAULT_PORT,
    SOURCE_PORT,
    Ref,
    WorkflowError,
    _check_id,
    parse_ref,
    register_step_type,
)

LIVE_MODES = ("live", "clock")
GATE_RULES = ("condition", "min-interval", "once", "count-limit", "busy")
SINK_KINDS = ("event-log", "overlay")

#: The port a gate's yes/no leaves on.
FIRED_PORT = "fired"
#: The port an action's completion leaves on.
DONE_PORT = "done"


def _refs(values) -> list[Ref]:
    return [parse_ref(r) for r in (values or [])]


def _non_negative(step_id: str, name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise WorkflowError(f"{step_id}: {name} must be a number, got {value!r}") from None
    if number < 0:
        raise WorkflowError(f"{step_id}: {name} must not be negative, got {number!r}")
    return number


@dataclass
class Live:
    """Frames from a detector, as they come (``live``) or one every
    ``interval_s`` seconds (``clock``). Its single port is ``data``."""

    id: str
    detector: str = ""
    mode: str = "live"
    interval_s: float = 0.0

    kind = "live"
    ports = (SOURCE_PORT,)

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.detector = str(self.detector or "")
        self.mode = str(self.mode or "live").lower()
        if self.mode not in LIVE_MODES:
            raise WorkflowError(f"{self.id}: live mode must be one of {LIVE_MODES}, got {self.mode!r}")
        self.interval_s = _non_negative(self.id, "interval_s", self.interval_s)
        if self.mode == "clock" and self.interval_s <= 0:
            raise WorkflowError(f"{self.id}: a clock source needs an interval_s above 0")

    def declared_kind(self, kinds: dict) -> str:
        return "image"

    def to_dict(self) -> dict:
        payload = {"step": "live", "id": self.id, "detector": self.detector}
        if self.mode != "live":
            payload["mode"] = self.mode
        if self.interval_s:
            payload["interval_s"] = self.interval_s
        return payload


@dataclass
class Transform:
    """Points from one frame of reference into another, by a registered
    transform (``identity`` when they already match). Port ``out``."""

    id: str
    transform: str = "identity"
    inputs: list[Ref] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    kind = "transform"
    ports = (DEFAULT_PORT,)

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.transform = str(self.transform or "identity")
        self.inputs = _refs(self.inputs)
        self.params = dict(self.params or {})

    def declared_kind(self, kinds: dict) -> str:
        return POINTS_KIND

    def to_dict(self) -> dict:
        payload = {"step": "transform", "id": self.id, "transform": self.transform,
                   "inputs": [str(r) for r in self.inputs]}
        if self.params:
            payload["params"] = dict(self.params)
        return payload


@dataclass
class Gate:
    """Pure logic on one input: lets the tick through (``out``, the input
    unchanged) or not, and says so (``fired``, an event).

    Rules: ``condition`` (points: at least ``min_count`` of them, 1 by
    default; a value: within ``min``..``max``; an event: fired),
    ``min-interval`` (at most once per ``seconds``), ``once`` (the first
    time only, until re-armed), ``count-limit`` (``count`` times), ``busy``
    (not while an action of this pipeline is still running).
    """

    id: str
    rule: str = "condition"
    params: dict = field(default_factory=dict)
    inputs: list[Ref] = field(default_factory=list)

    kind = "gate"
    ports = (DEFAULT_PORT, FIRED_PORT)

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.rule = str(self.rule or "condition").lower()
        if self.rule not in GATE_RULES:
            raise WorkflowError(f"{self.id}: gate rule must be one of {GATE_RULES}, got {self.rule!r}")
        self.params = dict(self.params or {})
        self.inputs = _refs(self.inputs)
        if self.rule == "min-interval":
            if _non_negative(self.id, "seconds", self.params.get("seconds", 0)) <= 0:
                raise WorkflowError(f"{self.id}: a min-interval gate needs seconds above 0")
        elif self.rule == "count-limit":
            try:
                count = int(self.params.get("count", 0))
            except (TypeError, ValueError):
                raise WorkflowError(f"{self.id}: count must be a whole number") from None
            if count < 1:
                raise WorkflowError(f"{self.id}: a count-limit gate needs a count of at least 1")
            self.params["count"] = count
        elif self.rule == "condition":
            for key in ("min", "max"):
                if self.params.get(key) is not None:
                    self.params[key] = float(self.params[key])
            if self.params.get("min_count") is not None:
                self.params["min_count"] = max(0, int(self.params["min_count"]))

    def declared_kind(self, kinds: dict) -> str:
        """``out`` carries what the input carries."""
        if not self.inputs:
            return UNKNOWN_KIND
        return str(kinds.get(self.inputs[0].step, UNKNOWN_KIND))

    def port_kind(self, port: str | None, kinds: dict) -> str:
        return EVENT_KIND if port == FIRED_PORT else self.declared_kind(kinds)

    def to_dict(self) -> dict:
        payload = {"step": "gate", "id": self.id, "rule": self.rule, "inputs": [str(r) for r in self.inputs]}
        if self.params:
            payload["params"] = dict(self.params)
        return payload


@dataclass
class Action:
    """Hardware, through the action catalogue. Its inputs feed the action
    (points to move to, a value to set, an event to wait for); ``params``
    are the catalogue entry's. Port ``done``: an event with the outcome."""

    id: str
    action: str = ""
    params: dict = field(default_factory=dict)
    inputs: list[Ref] = field(default_factory=list)
    timeout_s: float | None = None

    kind = "action"
    ports = (DONE_PORT,)

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.action = str(self.action or "")
        if not self.action:
            raise WorkflowError(f"{self.id}: an action step names an action")
        self.params = dict(self.params or {})
        self.inputs = _refs(self.inputs)
        if self.timeout_s is not None:
            self.timeout_s = _non_negative(self.id, "timeout_s", self.timeout_s) or None

    def declared_kind(self, kinds: dict) -> str:
        return EVENT_KIND

    def to_dict(self) -> dict:
        payload = {"step": "action", "id": self.id, "action": self.action, "params": dict(self.params),
                   "inputs": [str(r) for r in self.inputs]}
        if self.timeout_s is not None:
            payload["timeout_s"] = self.timeout_s
        return payload


@dataclass
class Sink:
    """Where a value goes to be seen: the event log, or an overlay on the
    live view. Produces no port."""

    id: str
    sink: str = "event-log"
    params: dict = field(default_factory=dict)
    inputs: list[Ref] = field(default_factory=list)

    kind = "sink"
    ports = ()

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.sink = str(self.sink or "event-log").lower()
        if self.sink not in SINK_KINDS:
            raise WorkflowError(f"{self.id}: sink must be one of {SINK_KINDS}, got {self.sink!r}")
        self.params = dict(self.params or {})
        self.inputs = _refs(self.inputs)

    def to_dict(self) -> dict:
        payload = {"step": "sink", "id": self.id, "sink": self.sink, "inputs": [str(r) for r in self.inputs]}
        if self.params:
            payload["params"] = dict(self.params)
        return payload


PIPELINE_STEP_TYPES = (Live, Transform, Gate, Action, Sink)
for _cls in PIPELINE_STEP_TYPES:
    register_step_type(_cls.kind, _cls)
del _cls


__all__ = [
    "DONE_PORT",
    "FIRED_PORT",
    "GATE_RULES",
    "LIVE_MODES",
    "PIPELINE_STEP_TYPES",
    "SINK_KINDS",
    "Action",
    "Gate",
    "Live",
    "Sink",
    "Transform",
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
