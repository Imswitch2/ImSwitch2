"""A workflow: sources, reconstructions, processing steps and saves, in order.

Python-first, as the imcontrol cookbooks are: a workflow is a list of step
dataclasses, and YAML/JSON is how it is written down (and what replay
emits). Every step has an id; later steps refer to earlier ones as
``"<id>"`` (that step's only, or first, output port) or ``"<id>.<port>"``.

Ports are what make a workflow a graph rather than a chain: a channel
split has one port per channel, a background subtraction can have
``signal`` and ``background``, and a merge lists the ports it consumes in
order. Ports that depend on the data (``C0, C1, …``) are validated against
the processor's declared pattern before the run and against the ports
actually produced after it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imswitch.improcess.workflows.sources import SourceSpec

SCHEMA_VERSION = 1

_ID = r"[A-Za-z0-9_\-]+"
_ID_PATTERN = re.compile(rf"^{_ID}$")
_REF_PATTERN = re.compile(rf"^(?P<step>{_ID})(?:\.(?P<port>{_ID}))?$")

SOURCE_PORT = "data"
DEFAULT_PORT = "out"

DEFAULT_SAVE_TEMPLATE = "{out_dir}/{source_stem}_{step}{ext}"


class WorkflowError(ValueError):
    """The workflow is malformed."""


@dataclass(frozen=True)
class Ref:
    step: str
    port: str | None = None

    def __str__(self) -> str:
        return self.step if self.port is None else f"{self.step}.{self.port}"


def parse_ref(text) -> Ref:
    if isinstance(text, Ref):
        return text
    match = _REF_PATTERN.match(str(text))
    if not match:
        raise WorkflowError(f"bad step reference {text!r}; expected 'step' or 'step.port'")
    return Ref(match.group("step"), match.group("port"))


def _check_id(step_id) -> str:
    step_id = str(step_id or "")
    if not _ID_PATTERN.match(step_id):
        raise WorkflowError(f"bad step id {step_id!r}; use letters, digits, '_' and '-'")
    return step_id


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

@dataclass
class Source:
    """A raw input. Its single port is ``data``."""

    id: str
    source: SourceSpec = field(default_factory=SourceSpec)

    def __init__(self, id: str, source: SourceSpec | None = None, *, path=None, dataset=None, source_kind: str = "auto"):
        self.id = _check_id(id)
        if source is None:
            source = SourceSpec(path=str(path) if path else None, dataset=dataset, source_kind=source_kind)
        self.source = source

    kind = "source"

    def to_dict(self) -> dict:
        return {"step": "source", "id": self.id, **self.source.to_dict()}


@dataclass
class Reconstruct:
    """Run a reconstructor over one source. Its single port is ``out``."""

    id: str
    reconstructor: str
    params: dict = field(default_factory=dict)
    inputs: list[Ref] = field(default_factory=list)

    kind = "reconstruct"

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.inputs = [parse_ref(r) for r in (self.inputs or [])]

    def to_dict(self) -> dict:
        return {"step": "reconstruct", "id": self.id, "reconstructor": self.reconstructor,
                "params": dict(self.params), "inputs": [str(r) for r in self.inputs]}


@dataclass
class Consolidate:
    """Merge several reconstructions of one reconstructor. Port ``out``."""

    id: str
    reconstructor: str
    inputs: list[Ref] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    kind = "consolidate"

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.inputs = [parse_ref(r) for r in (self.inputs or [])]

    def to_dict(self) -> dict:
        return {"step": "consolidate", "id": self.id, "reconstructor": self.reconstructor,
                "inputs": [str(r) for r in self.inputs], "params": dict(self.params)}


@dataclass
class Process:
    """Run a processor over one or more results. Ports per the processor.

    ``restriction`` is an ROI restriction in the form the provenance records
    it (``ROIRestriction.encode_provenance()``): the run is narrowed to
    those regions the way the GUI's "apply within ROI" narrows it, and the
    processor never sees the ROIs themselves.
    """

    id: str
    processor: str
    params: dict = field(default_factory=dict)
    inputs: list[Ref] = field(default_factory=list)
    restriction: dict | None = None

    kind = "process"

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.inputs = [parse_ref(r) for r in (self.inputs or [])]
        if self.restriction is not None and not isinstance(self.restriction, dict):
            raise WorkflowError(f"{self.id}: restriction must be an object")

    def to_dict(self) -> dict:
        payload = {"step": "process", "id": self.id, "processor": self.processor,
                   "params": dict(self.params), "inputs": [str(r) for r in self.inputs]}
        if self.restriction:
            payload["restriction"] = dict(self.restriction)
        return payload


@dataclass
class Save:
    """Write one result. Produces no port."""

    id: str
    input: Ref
    fmt: str = "tiff"
    path_template: str = DEFAULT_SAVE_TEMPLATE

    kind = "save"

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.input = parse_ref(self.input)

    def to_dict(self) -> dict:
        return {"step": "save", "id": self.id, "input": str(self.input), "fmt": self.fmt,
                "path_template": self.path_template}


Step = Source | Reconstruct | Consolidate | Process | Save

_STEP_TYPES = {"source": Source, "reconstruct": Reconstruct, "consolidate": Consolidate,
               "process": Process, "save": Save}


def step_from_dict(payload: dict) -> Step:
    if not isinstance(payload, dict):
        raise WorkflowError(f"a step must be an object, got {type(payload).__name__}")
    kind = str(payload.get("step") or payload.get("kind") or "").lower()
    if kind not in _STEP_TYPES:
        raise WorkflowError(f"unknown step kind {kind!r} (id {payload.get('id')!r})")
    data = {k: v for k, v in payload.items() if k not in ("step", "kind")}
    if kind == "source":
        source = SourceSpec.from_dict({k: v for k, v in data.items() if k != "id"})
        return Source(data.get("id"), source)
    cls = _STEP_TYPES[kind]
    try:
        return cls(**data)
    except TypeError as exc:
        raise WorkflowError(f"step {data.get('id')!r}: {exc}") from exc


# --------------------------------------------------------------------------
# workflow
# --------------------------------------------------------------------------

@dataclass
class Workflow:
    name: str
    steps: list = field(default_factory=list)
    schema: int = SCHEMA_VERSION
    description: str = ""
    #: Free-form, carried through serialisation: replay records here which
    #: provenance node each step came from and what the recorded run was.
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        seen = set()
        for step in self.steps:
            if step.id in seen:
                raise WorkflowError(f"duplicate step id {step.id!r}")
            seen.add(step.id)

    def step(self, step_id: str):
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def sources(self) -> list[Source]:
        return [s for s in self.steps if isinstance(s, Source)]

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        payload = {"name": self.name, "schema": self.schema}
        if self.description:
            payload["description"] = self.description
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "Workflow":
        if not isinstance(payload, dict):
            raise WorkflowError("a workflow must be an object")
        schema = int(payload.get("schema", SCHEMA_VERSION) or SCHEMA_VERSION)
        if schema > SCHEMA_VERSION:
            raise WorkflowError(f"workflow schema {schema} is newer than this ImSwitch understands")
        steps = [step_from_dict(item) for item in (payload.get("steps") or [])]
        return cls(name=str(payload.get("name") or "workflow"), steps=steps, schema=schema,
                   description=str(payload.get("description") or ""),
                   metadata=dict(payload.get("metadata") or {}))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, text: str) -> "Workflow":
        return cls.from_dict(json.loads(text))

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "Workflow":
        import yaml

        return cls.from_dict(yaml.safe_load(text))

    def save(self, path) -> Path:
        path = Path(path)
        if path.suffix.lower() in (".yaml", ".yml"):
            path.write_text(self.to_yaml(), encoding="utf-8")
        else:
            path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path) -> "Workflow":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            return cls.from_yaml(text)
        return cls.from_json(text)


# --------------------------------------------------------------------------
# static validation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    step: str
    message: str

    def __str__(self) -> str:
        return f"{self.step}: {self.message}"


def _port_ok(step, port: str | None, registry) -> tuple[bool, str]:
    """Whether ``port`` is one ``step`` can produce (statically)."""
    if isinstance(step, Source):
        return (port in (None, SOURCE_PORT)), SOURCE_PORT
    if isinstance(step, (Reconstruct, Consolidate)):
        return (port in (None, DEFAULT_PORT)), DEFAULT_PORT
    if isinstance(step, Process):
        processor = registry.get_processor(step.processor, raise_on_missing=False) if registry else None
        if processor is None:
            return True, "?"
        params = {**_defaults(processor), **step.params}
        spec = processor.output_spec(params)
        if port is None:
            return (spec.ports is not None and len(spec.ports) >= 1) or spec.pattern is not None, spec.describe()
        if spec.matches(port):
            return True, spec.describe()
        # A single-input processor run over several inputs yields one result
        # per input; the second one onwards is the port with the input's
        # position appended (out1, out2, ...).
        fan_out = len(step.inputs) > 1 and getattr(processor, "max_inputs", 1) == 1
        if fan_out:
            match = re.fullmatch(r"(.+?)(\d+)", str(port))
            if match and spec.matches(match.group(1)) and 0 < int(match.group(2)) < len(step.inputs):
                return True, spec.describe()
        return False, spec.describe()
    return False, "(no ports)"


def _defaults(plugin) -> dict:
    getter = getattr(type(plugin), "default_params", None)
    try:
        return dict(getter()) if callable(getter) else {}
    except Exception:
        return {}


def _unknown_params(step, plugin) -> list[Issue]:
    """Parameter keys the plugin does not declare (``param_keys``).

    Checked whenever the plugin declares a key set, including an empty one:
    a plugin with no parameters accepts none, and a typo must fail before
    the run rather than being ignored by ``params.get(...)``.
    """
    getter = getattr(type(plugin), "param_keys", None)
    try:
        allowed = getter() if callable(getter) else None
    except Exception:
        allowed = None
    if allowed is None:
        return []
    unknown = sorted(set(step.params) - set(allowed))
    if not unknown:
        return []
    plugin_id = getattr(plugin, "id", type(plugin).__name__)
    return [Issue(step.id, f"unknown parameter(s) for {plugin_id!r}: {unknown} (accepted: {sorted(allowed) or 'none'})")]


def validate(workflow: Workflow, registry=None) -> list[Issue]:
    """Everything wrong with ``workflow`` that can be known before running.

    With a registry: plugin ids, arity and ports are checked too. Without,
    only structure (ids, references, order).
    """
    issues: list[Issue] = []
    by_id: dict[str, Any] = {}
    for step in workflow.steps:
        if isinstance(step, Source):
            by_id[step.id] = step
            continue
        refs = [step.input] if isinstance(step, Save) else list(step.inputs)
        for ref in refs:
            target = by_id.get(ref.step)
            if target is None:
                issues.append(Issue(step.id, f"references {ref!r} which is not an earlier step"))
                continue
            if isinstance(target, Save):
                issues.append(Issue(step.id, f"references save step {ref.step!r}, which produces nothing"))
                continue
            ok, described = _port_ok(target, ref.port, registry)
            if not ok:
                issues.append(Issue(step.id, f"port {ref.port!r} is not one {ref.step!r} produces ({described})"))

        if isinstance(step, Reconstruct):
            if len(step.inputs) != 1:
                issues.append(Issue(step.id, "a reconstruction takes exactly one source"))
            elif not isinstance(by_id.get(step.inputs[0].step), Source):
                issues.append(Issue(step.id, "a reconstruction's input must be a source step"))
            if registry:
                plugin = registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
                if plugin is None:
                    issues.append(Issue(step.id, f"unknown reconstructor {step.reconstructor!r}"))
                else:
                    issues.extend(_unknown_params(step, plugin))
        elif isinstance(step, Consolidate):
            if not step.inputs:
                issues.append(Issue(step.id, "a consolidation needs at least one input"))
            if step.params:
                issues.append(Issue(step.id, f"a consolidation takes no parameters; got {sorted(step.params)}"))
            for ref in step.inputs:
                target = by_id.get(ref.step)
                if target is not None and not isinstance(target, Reconstruct):
                    issues.append(Issue(step.id, f"consolidation input {ref.step!r} is not a reconstruction"))
                elif target is not None and target.reconstructor != step.reconstructor:
                    issues.append(Issue(step.id, f"{ref.step!r} was made by {target.reconstructor!r}, not {step.reconstructor!r}"))
            if registry:
                plugin = registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
                if plugin is None:
                    issues.append(Issue(step.id, f"unknown reconstructor {step.reconstructor!r}"))
                elif not getattr(plugin, "supports_consolidation", False):
                    issues.append(Issue(step.id, f"{step.reconstructor!r} does not support consolidation"))
        elif isinstance(step, Process):
            if not step.inputs:
                issues.append(Issue(step.id, "a processing step needs at least one input"))
            if registry:
                processor = registry.get_processor(step.processor, raise_on_missing=False)
                if processor is None:
                    issues.append(Issue(step.id, f"unknown processor {step.processor!r}"))
                else:
                    count = len(step.inputs)
                    low = int(getattr(processor, "min_inputs", 1) or 1)
                    high = getattr(processor, "max_inputs", 1)
                    if high == 1 and count > 1:
                        pass   # a single-input processor over several inputs runs once per input
                    elif count < low or (high is not None and count > high):
                        issues.append(Issue(step.id, f"{step.processor!r} takes {low}..{high if high is not None else '∞'} inputs, got {count}"))
                    issues.extend(_unknown_params(step, processor))
        elif isinstance(step, Save):
            from imswitch.improcess.model.save_protocol import UnsupportedSaveFormat, normalize_format

            try:
                normalize_format(step.fmt)
            except UnsupportedSaveFormat as exc:
                issues.append(Issue(step.id, str(exc)))
        by_id[step.id] = step
    return issues


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_SAVE_TEMPLATE",
    "Consolidate",
    "Issue",
    "Process",
    "Reconstruct",
    "Ref",
    "SCHEMA_VERSION",
    "SOURCE_PORT",
    "Save",
    "Source",
    "Step",
    "Workflow",
    "WorkflowError",
    "parse_ref",
    "step_from_dict",
    "validate",
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
