"""The workflow being edited, and the operations an editor needs on it.

A :class:`WorkflowDocument` wraps one :class:`Workflow`. Every edit goes
through a method here rather than through the step dataclasses directly, so
the rules that make a workflow file run stay in one place: an id that is
free, a reference that points backwards, ``params`` that hold only what
differs from the plugin's defaults, a step that is never moved in front of
its inputs. After any edit :meth:`issues` says what is now wrong -- the
runtime's own :func:`validate` first, then what the editor can add to it
(a value that is not one of its field, a path template with a placeholder
nobody fills, two saves writing one file).

Qt-free, so the same object serves a test, a command line and a window.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR, RECONSTRUCTOR, Catalog, PluginEntry
from imswitch.improcess.workflows.sources import SourceSpec
from imswitch.improcess.workflows.steps import (
    DEFAULT_PORT,
    DEFAULT_SAVE_TEMPLATE,
    SOURCE_PORT,
    Consolidate,
    Issue,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    Workflow,
    WorkflowError,
    parse_ref,
    validate,
)

STEP_KINDS = ("source", "reconstruct", "consolidate", "process", "save")
_ID_PREFIX = {"source": "src", "reconstruct": "rec", "consolidate": "cons", "process": "proc", "save": "save"}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")
_KEEP = object()


class DocumentError(ValueError):
    """The edit cannot be made as asked; the document is unchanged."""


@dataclass(frozen=True)
class PortOption:
    """One port an earlier step can offer to a later one.

    ``port`` is ``None`` for a step's only port (a source's ``data``, a
    reconstruction's ``out``): the reference is then just the step id, as
    the file format prefers. A ``pattern`` option stands for a family of
    data-dependent ports (``C0, C1, …``); its ``port`` is the pattern and the
    user types the member.
    """

    step: str
    port: str | None
    label: str
    pattern: bool = False

    @property
    def ref(self) -> str:
        return self.step if self.port is None or self.pattern else f"{self.step}.{self.port}"


def _kind(step) -> str:
    return str(getattr(step, "kind", ""))


def _refs_of(step) -> list[Ref]:
    if isinstance(step, Save):
        return [step.input]
    return list(getattr(step, "inputs", []) or [])


def _set_refs(step, refs: list[Ref]) -> None:
    if isinstance(step, Save):
        step.input = refs[0]
    else:
        step.inputs = list(refs)


def _refs_point_backwards(steps: list) -> str | None:
    """Why the order is wrong (a reference to a later or missing step), or None."""
    seen: set[str] = set()
    for step in steps:
        for ref in _refs_of(step):
            if ref.step not in seen:
                return f"{step.id} would come before its input {ref.step!r}"
        seen.add(step.id)
    return None


class WorkflowDocument:
    """One workflow under edit; see the module docstring."""

    def __init__(self, workflow: Workflow | None = None, *, catalog: Catalog | None = None, path=None):
        self.workflow = workflow if workflow is not None else Workflow("workflow")
        self.catalog = catalog
        self.path = Path(path) if path else None
        self.dirty = False
        #: Bumped on every edit; a view compares it to know whether to rebuild.
        self.revision = 0
        #: Notes from the last import (replay warnings); informational.
        self.notes: list[str] = []

    # -- files ------------------------------------------------------------------

    @classmethod
    def load(cls, path, catalog: Catalog | None = None) -> "WorkflowDocument":
        return cls(Workflow.load(path), catalog=catalog, path=path)

    def save(self, path=None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise DocumentError("no file name to save to")
        written = self.workflow.save(target)
        self.path = written
        self.dirty = False
        return written

    def to_dict(self) -> dict:
        return self.workflow.to_dict()

    def to_yaml(self) -> str:
        return self.workflow.to_yaml()

    @property
    def name(self) -> str:
        return self.workflow.name

    @property
    def editor_metadata(self) -> dict:
        """Editor-only state, kept under ``metadata.editor``: it round-trips
        through the file and the runner never reads it."""
        return self.workflow.metadata.setdefault("editor", {})

    # -- queries ----------------------------------------------------------------

    @property
    def steps(self) -> list:
        return self.workflow.steps

    def has(self, step_id: str) -> bool:
        return any(step.id == step_id for step in self.steps)

    def step(self, step_id: str):
        return self.workflow.step(step_id)

    def index_of(self, step_id: str) -> int:
        for index, step in enumerate(self.steps):
            if step.id == step_id:
                return index
        raise KeyError(step_id)

    def entry_for(self, step) -> PluginEntry | None:
        """The catalogue entry behind a reconstruct/consolidate/process step."""
        if self.catalog is None:
            return None
        if isinstance(step, (Reconstruct, Consolidate)):
            return self.catalog.reconstructor(step.reconstructor)
        if isinstance(step, Process):
            return self.catalog.processor(step.processor)
        return None

    def defaults_for(self, step_id: str) -> dict:
        entry = self.entry_for(self.step(step_id))
        return entry.defaults() if entry is not None else {}

    def effective_params(self, step_id: str) -> dict:
        """What the run will use: the defaults with the step's params over them."""
        step = self.step(step_id)
        return {**self.defaults_for(step_id), **dict(getattr(step, "params", {}) or {})}

    def new_id(self, kind: str) -> str:
        """The first free ``src1``, ``rec1``, ``cons1``, ``proc1`` or ``save1``."""
        prefix = _ID_PREFIX[kind]
        taken = {step.id for step in self.steps}
        number = 1
        while f"{prefix}{number}" in taken:
            number += 1
        return f"{prefix}{number}"

    def producers_before(self, step_id: str | None) -> list:
        """Earlier steps that produce a port (every kind but ``Save``)."""
        stop = self.index_of(step_id) if step_id is not None else len(self.steps)
        return [step for step in self.steps[:stop] if not isinstance(step, Save)]

    def port_options(self, step_id: str | None) -> list[PortOption]:
        """Every port a step placed at ``step_id`` (or at the end) may reference."""
        options: list[PortOption] = []
        for step in self.producers_before(step_id):
            if isinstance(step, Source):
                options.append(PortOption(step.id, None, f"{step.id} ({SOURCE_PORT})"))
            elif isinstance(step, (Reconstruct, Consolidate)):
                options.append(PortOption(step.id, None, f"{step.id} ({DEFAULT_PORT})"))
            elif isinstance(step, Process):
                entry = self.entry_for(step)
                spec = entry.output_spec(step.params) if entry is not None else None
                ports = tuple(spec.ports or ()) if spec is not None else (DEFAULT_PORT,)
                fan_out = len(step.inputs) > 1 and entry is not None and entry.max_inputs == 1
                for index, port in enumerate(ports):
                    only = len(ports) == 1 and not (spec is not None and spec.pattern)
                    options.append(PortOption(step.id, None if only and index == 0 else port,
                                              f"{step.id}.{port}" if not only else f"{step.id} ({port})"))
                    if fan_out and only:
                        for position in range(1, len(step.inputs)):
                            options.append(PortOption(step.id, f"{port}{position}", f"{step.id}.{port}{position}"))
                if spec is not None and spec.pattern:
                    options.append(PortOption(step.id, spec.pattern, f"{step.id}.<{spec.pattern}>", pattern=True))
        return options

    # -- issues -----------------------------------------------------------------

    def issues(self) -> list[Issue]:
        """Everything wrong, the runtime's checks first, the editor's after."""
        registry = self.catalog.registry if self.catalog is not None else None
        found = list(validate(self.workflow, registry))
        found.extend(self._editor_issues())
        return found

    def issues_for(self, step_id: str) -> list[Issue]:
        return [issue for issue in self.issues() if issue.step == step_id]

    def _editor_issues(self) -> list[Issue]:
        found: list[Issue] = []
        placeholders = set(self.catalog.path_placeholders) if self.catalog is not None else None
        saves_seen: dict[tuple, Save] = {}
        for step in self.steps:
            if isinstance(step, (Reconstruct, Process)):
                entry = self.entry_for(step)
                if entry is None:
                    continue
                for key, value in dict(step.params or {}).items():
                    field = entry.field(key)
                    if field is None:
                        continue
                    problem = field.problem_with(value)
                    if problem:
                        found.append(Issue(step.id, f"{key}: {value!r} {problem}"))
            elif isinstance(step, Save):
                if placeholders is not None:
                    for unknown in self._unknown_placeholders(step.path_template, placeholders):
                        found.append(Issue(step.id, f"unknown placeholder {{{unknown}}} in path template"))
                signature = self._save_signature(step)
                if signature is not None:
                    earlier = saves_seen.get(signature)
                    if earlier is not None:
                        found.append(Issue(step.id, f"writes the same file as save {earlier.id!r}"))
                    else:
                        saves_seen[signature] = step
        return found

    @staticmethod
    def _unknown_placeholders(template: str, known: set[str]) -> list[str]:
        try:
            names = [name for _text, name, _spec, _conv in string.Formatter().parse(template) if name]
        except ValueError:
            return ["<malformed template>"]
        return sorted({name.split(".")[0].split("[")[0] for name in names} - known)

    @staticmethod
    def _save_signature(step: Save):
        """What makes two saves land on one file: the same template, the same
        format, and nothing in the template that tells them apart."""
        template = step.path_template
        if "{step}" in template:
            return None
        input_part = str(step.input) if "{input_step}" in template or "{name}" in template else ""
        return (template, step.fmt, input_part)

    # -- edits ------------------------------------------------------------------

    def _touch(self) -> None:
        self.dirty = True
        self.revision += 1

    def set_name(self, name: str) -> None:
        self.workflow.name = str(name or "workflow")
        self._touch()

    def set_description(self, text: str) -> None:
        self.workflow.description = str(text or "")
        self._touch()

    def add_step(self, kind: str, plugin_id: str | None = None, *, after: str | None = None,
                 step_id: str | None = None, inputs: Iterable = ()):
        """Insert a new step and return it.

        It goes right after ``after`` (or at the end), but never before one
        of its ``inputs``. A process or save step given no inputs takes the
        step it follows as its input when that step produces a port.
        """
        if kind not in STEP_KINDS:
            raise DocumentError(f"unknown step kind {kind!r}")
        step_id = self._check_free_id(step_id) if step_id else self.new_id(kind)
        refs = [parse_ref(ref) for ref in inputs]
        if not refs and after is not None and kind in ("process", "save"):
            previous = self.step(after)
            if not isinstance(previous, Save):
                refs = [Ref(previous.id)]
        if kind in ("reconstruct", "consolidate", "process") and not plugin_id:
            raise DocumentError(f"a {kind} step needs a plugin id")
        if kind == "source":
            step = Source(step_id)
        elif kind == "reconstruct":
            step = Reconstruct(step_id, plugin_id, inputs=refs)
        elif kind == "consolidate":
            step = Consolidate(step_id, plugin_id, inputs=refs)
        elif kind == "process":
            step = Process(step_id, plugin_id, inputs=refs)
        else:
            if not refs:
                raise DocumentError("a save step needs an input")
            step = Save(step_id, input=refs[0])
        index = self.index_of(after) + 1 if after is not None else len(self.steps)
        for ref in refs:
            if self.has(ref.step):
                index = max(index, self.index_of(ref.step) + 1)
        self.steps.insert(index, step)
        self._touch()
        return step

    def remove_step(self, step_id: str):
        """Remove a step. References to it are left in place and reported."""
        removed = self.steps.pop(self.index_of(step_id))
        self._touch()
        return removed

    def move_step(self, step_id: str, new_index: int) -> None:
        """Reorder; refused when a step would precede one of its inputs."""
        steps = list(self.steps)
        step = steps.pop(self.index_of(step_id))
        new_index = max(0, min(int(new_index), len(steps)))
        steps.insert(new_index, step)
        problem = _refs_point_backwards(steps)
        if problem:
            raise DocumentError(problem)
        self.steps[:] = steps
        self._touch()

    def rename_step(self, step_id: str, new_id: str) -> None:
        """Rename a step and every reference to it."""
        if new_id == step_id:
            return
        new_id = self._check_free_id(new_id)
        step = self.step(step_id)
        step.id = new_id
        for later in self.steps:
            refs = _refs_of(later)
            if any(ref.step == step_id for ref in refs):
                _set_refs(later, [Ref(new_id, ref.port) if ref.step == step_id else ref for ref in refs])
        self._touch()

    def _check_free_id(self, step_id: str) -> str:
        step_id = str(step_id or "")
        if not _ID_PATTERN.match(step_id):
            raise DocumentError(f"bad step id {step_id!r}; use letters, digits, '_' and '-'")
        if self.has(step_id):
            raise DocumentError(f"a step named {step_id!r} already exists")
        return step_id

    def set_plugin(self, step_id: str, plugin_id: str) -> None:
        """Change the plugin; parameters the new one does not take are dropped."""
        step = self.step(step_id)
        if isinstance(step, (Reconstruct, Consolidate)):
            step.reconstructor = str(plugin_id)
        elif isinstance(step, Process):
            step.processor = str(plugin_id)
        else:
            raise DocumentError(f"{step_id} is a {_kind(step)} step and has no plugin")
        entry = self.entry_for(step)
        allowed = entry.param_keys() if entry is not None else None
        if allowed is not None and step.params:
            step.params = {key: value for key, value in step.params.items() if key in allowed}
        self._touch()

    def set_param(self, step_id: str, key: str, value) -> None:
        """Set one parameter; a value equal to the plugin's default is removed
        instead, so ``params`` keeps holding only what differs."""
        step = self.step(step_id)
        if not isinstance(step, (Reconstruct, Process)):
            raise DocumentError(f"{step_id} takes no parameters")
        params = dict(step.params or {})
        defaults = self.defaults_for(step_id)
        if key in defaults and _same(defaults[key], value):
            params.pop(key, None)
        else:
            params[key] = value
        step.params = params
        self._touch()

    def clear_param(self, step_id: str, key: str) -> None:
        step = self.step(step_id)
        params = dict(getattr(step, "params", {}) or {})
        params.pop(key, None)
        if hasattr(step, "params"):
            step.params = params
        self._touch()

    def set_params(self, step_id: str, params: Mapping[str, Any]) -> None:
        """Replace every parameter; defaults are dropped as in :meth:`set_param`."""
        step = self.step(step_id)
        if not isinstance(step, (Reconstruct, Process)):
            raise DocumentError(f"{step_id} takes no parameters")
        defaults = self.defaults_for(step_id)
        step.params = {
            key: value for key, value in dict(params).items()
            if not (key in defaults and _same(defaults[key], value))
        }
        self._touch()

    def minimize_params(self) -> int:
        """Drop every parameter equal to its default, workflow-wide.

        A workflow exported from a result carries the full effective
        parameter set; this brings it back to "only what you change".
        Returns how many values were dropped.
        """
        dropped = 0
        for step in self.steps:
            if not isinstance(step, (Reconstruct, Process)) or not step.params:
                continue
            defaults = self.defaults_for(step.id)
            kept = {key: value for key, value in step.params.items()
                    if not (key in defaults and _same(defaults[key], value))}
            dropped += len(step.params) - len(kept)
            step.params = kept
        if dropped:
            self._touch()
        return dropped

    def set_inputs(self, step_id: str, refs: Iterable) -> None:
        step = self.step(step_id)
        parsed = [parse_ref(ref) for ref in refs]
        if isinstance(step, Source):
            raise DocumentError("a source has no inputs")
        if isinstance(step, Save):
            if len(parsed) != 1:
                raise DocumentError("a save step takes exactly one input")
            step.input = parsed[0]
        else:
            step.inputs = parsed
        self._touch()

    def set_source(self, step_id: str, *, path=_KEEP, dataset=_KEEP, source_kind=_KEEP) -> None:
        """Change where a source points. A new path or dataset drops the
        recorded fingerprint, which described the old file."""
        step = self.step(step_id)
        if not isinstance(step, Source):
            raise DocumentError(f"{step_id} is not a source step")
        old = step.source
        new_path = old.path if path is _KEEP else (str(path) if path else None)
        new_dataset = old.dataset if dataset is _KEEP else (str(dataset) if dataset else None)
        new_kind = old.source_kind if source_kind is _KEEP else str(source_kind or "auto")
        changed = (new_path, new_dataset) != (old.path, old.dataset)
        try:
            step.source = SourceSpec(
                path=new_path, dataset=new_dataset, source_kind=new_kind,
                metadata=dict(old.metadata), fingerprint={} if changed else dict(old.fingerprint),
            )
        except Exception as exc:
            raise DocumentError(str(exc)) from exc
        self._touch()

    def set_save(self, step_id: str, *, fmt: str | None = None, path_template: str | None = None) -> None:
        step = self.step(step_id)
        if not isinstance(step, Save):
            raise DocumentError(f"{step_id} is not a save step")
        if fmt is not None:
            step.fmt = str(fmt)
        if path_template is not None:
            step.path_template = str(path_template) or DEFAULT_SAVE_TEMPLATE
        self._touch()

    def set_restriction(self, step_id: str, restriction: dict | None) -> None:
        step = self.step(step_id)
        if not isinstance(step, Process):
            raise DocumentError(f"{step_id} is not a process step")
        step.restriction = dict(restriction) if restriction else None
        self._touch()

    # -- import -----------------------------------------------------------------

    @classmethod
    def from_result(cls, result, catalog: Catalog, *, name: str | None = None) -> "WorkflowDocument":
        """The workflow that made ``result``, from its provenance, minimized.

        Raises :class:`~imswitch.improcess.workflows.replay.ReplayError` when
        the record cannot be run again, listing every reason.
        """
        from imswitch.improcess.model.provenance import graph_of
        from imswitch.improcess.model.save_protocol import ProvenanceDocument
        from imswitch.improcess.workflows.replay import workflow_from_provenance

        graph = graph_of(result)
        if graph is None:
            raise WorkflowError(f"'{getattr(result, 'name', 'result')}' carries no provenance")
        replay = workflow_from_provenance(ProvenanceDocument(graph=graph), registry=catalog.registry, name=name)
        document = cls(replay.workflow, catalog=catalog)
        document.minimize_params()
        document.notes = [str(warning) for warning in replay.warnings]
        document.dirty = True
        return document


def _same(a, b) -> bool:
    from imswitch.improcess.model.param_spec import _same as same

    return same(a, b)


__all__ = ["STEP_KINDS", "DocumentError", "PortOption", "WorkflowDocument"]


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
