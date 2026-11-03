"""What a graph view draws for each step, and which cables may be made. Qt-free.

A box on the canvas is a step: a title in plain words (the plugin's name, not
the file format's ``process``), the parameters the file sets, one input per
reference and one output per port, each with the kind of result it carries.
This module answers those questions from the document and the catalogue, so
the canvas only draws and a test can check what it would draw.

Kinds are the declared ones (:func:`~imswitch.imcommon.model.workflow.steps.declared_kinds`):
a Source carries a ``recording``, a step what its plugin declares, ``any`` or
``unknown`` when that is all that can be known before a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from imswitch.imcommon.model.kinds import ANY_KIND, RECORDING_KIND, RESULT_KINDS, UNKNOWN_KIND
from imswitch.imcommon.model.workflow.steps import (
    DEFAULT_PORT,
    SOURCE_PORT,
    Consolidate,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    declared_kinds,
)

#: Kinds a cable may carry into a step that takes "whatever comes".
_LOOSE = (ANY_KIND, UNKNOWN_KIND)


@dataclass(frozen=True)
class InputSlot:
    """One input of a box.

    ``index`` is the position in the step's inputs (``None`` for the hollow
    slot that adds one); ``ref`` what it reads, ``None`` when unconnected.
    ``required`` marks an empty input the step cannot run without. ``code``
    is the name a Python step's code reads it by (see :func:`code_name`).
    """

    index: int | None
    ref: Ref | None
    label: str
    kind: str
    required: bool = False
    adds: bool = False
    code: str = ""


@dataclass(frozen=True)
class OutputSlot:
    """One output of a box.

    ``ref`` is what a consumer writes to read it (``split.C0``, or just
    ``rec`` for a step's only port). ``placeholder`` marks the hollow slot of
    a data-dependent family (a split's next ``C2``): dragging from it reads
    the member named by ``port``.
    """

    port: str
    ref: str
    label: str
    kind: str
    placeholder: bool = False


@dataclass(frozen=True)
class NodeInfo:
    id: str
    step_kind: str
    title: str
    plugin_id: str
    category: str
    description: str
    summary: tuple[str, ...]
    code: tuple[str, ...]
    inputs: tuple[InputSlot, ...]
    outputs: tuple[OutputSlot, ...]
    issues: tuple[str, ...] = field(default=())
    #: Kept in the file, left out of the run (schema 2).
    bypassed: bool = False
    #: For a bypassed step: what it passes through (``split.C0``), "" for a Save.
    passes: str = ""
    #: ``(key, control)`` for the parameters a control owns.
    exposed: tuple[tuple[str, str], ...] = field(default=())

    @property
    def kind(self) -> str:
        """What the box produces (its first output's kind), or ``""`` for a Save."""
        return self.outputs[0].kind if self.outputs else ""

    def output(self, ref: str) -> OutputSlot | None:
        for slot in self.outputs:
            if slot.ref == ref or f"{self.id}.{slot.port}" == ref:
                return slot
        return None


def step_kind(step) -> str:
    return str(getattr(step, "kind", ""))


def kinds_of(document) -> dict[str, str]:
    """The kind each step makes; a bypassed step makes what it passes through."""
    from imswitch.imcommon.model.workflow.prepare import bypass_upstream

    registry = document.catalog.registry if document.catalog is not None else None
    # declared_kinds without a registry: a Source makes a recording, a
    # plugin step is unknown, a registered kind says (declared_kind).
    found = dict(declared_kinds(document.workflow, registry))
    for step_id, through in bypass_upstream(document.workflow).items():
        found[step_id] = found.get(through.step, UNKNOWN_KIND)
    return found


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(item) for item in value)
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value)
    return text if len(text) <= 40 else text[:39] + "…"


def save_file_name(step: Save, catalog=None) -> str:
    """The file a Save writes, as words: ``‹recording›_diamond.h5``.

    ``catalog.normalize_format`` spells the format the way the host writes
    it; without a catalog the step's own spelling is used."""
    template = str(step.path_template or "")
    template = re.sub(r"^\{out_dir\}[\\/]", "", template)
    normalize = getattr(catalog, "normalize_format", None)
    try:
        fmt = normalize(step.fmt) if callable(normalize) else step.fmt
    except ValueError:
        fmt = step.fmt
    extension = {"hdf5": ".h5", "tiff": ".tif", "zarr": ".zarr", "csv": ".csv"}.get(str(fmt), f".{fmt}")
    text = (template.replace("{source_stem}", "‹recording›").replace("{ext}", extension)
            .replace("{fmt}", str(fmt)).replace("{step}", step.id)
            .replace("{input_step}", step.input.step).replace("{name}", "‹result›"))
    return text


def _summary(document, step, entry) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if isinstance(step, Source):
        spec = step.source
        if not spec.path:
            return ("bound when run",), ()
        name = re.split(r"[\\/]", str(spec.path))[-1]
        lines = [name]
        if spec.dataset:
            lines.append(f"dataset {spec.dataset}")
        return tuple(lines), ()
    if isinstance(step, Save):
        return (f"→ {save_file_name(step, document.catalog)}",), ()
    if isinstance(step, Consolidate):
        count = len(step.inputs)
        return (f"merges {count} reconstruction{'s' if count != 1 else ''}",), ()
    params = dict(getattr(step, "params", {}) or {})
    descriptor = _descriptor(document, step)
    if descriptor is not None:
        labels = {f.key: (f.label or f.key) for f in descriptor.attributes + descriptor.params_fields(step)}
        shown = [(f.key, getattr(step, f.key, None)) for f in descriptor.attributes]
        shown += [(key, value) for key, value in params.items()]
        lines = tuple(f"{labels.get(key, key)}  {_fmt(value)}" for key, value in shown
                      if value not in (None, "", [], {}))
        return (lines[:4] if lines else ("defaults",)), ()
    if isinstance(step, Process) and step.processor == "python":
        code = [line.rstrip() for line in str(params.get("code", "")).splitlines()]
        shown = tuple(line for line in code if line.strip() and not line.lstrip().startswith("#"))[:4]
        return (f"ports: {params.get('ports', DEFAULT_PORT)}",), shown
    labels = {}
    if entry is not None:
        labels = {f.key: (f.label or f.key) for f in entry.fields}
    exposed = document.exposed(step.id) if hasattr(document, "exposed") else {}
    lines = tuple(f"{labels.get(key, key)}  {_fmt(value)}" + (f"  ⇄ {exposed[key]}" if key in exposed else "")
                  for key, value in params.items())
    if isinstance(step, Process) and step.restriction:
        lines += (f"within {len(step.restriction.get('rois') or [])} ROI(s)",)
    return (lines[:4] if lines else ("defaults",)), ()


def title_of(document, step) -> str:
    if isinstance(step, Source):
        return "Recording"
    if isinstance(step, Save):
        return f"Save · {str(step.fmt).upper()}"
    entry = document.entry_for(step)
    if entry is not None:
        return entry.name
    descriptor = _descriptor(document, step)
    if descriptor is not None:
        what = _descriptor_subtitle(step)
        return f"{descriptor.name} · {what}" if what else descriptor.name
    plugin = getattr(step, "processor", None) or getattr(step, "reconstructor", "")
    return f"{plugin} (not installed)"


def _descriptor(document, step):
    """The catalog's descriptor of ``step``'s kind, for a kind the editor
    does not know by class."""
    catalog = getattr(document, "catalog", None)
    if catalog is None or isinstance(step, (Source, Reconstruct, Consolidate, Process, Save)):
        return None
    return catalog.step(step_kind(step))


def _descriptor_subtitle(step) -> str:
    """What a registered step is set to, for its title (the action, the
    rule, the detector): the first attribute that names a choice."""
    for key in ("action", "rule", "transform", "sink", "detector"):
        value = getattr(step, key, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _members_used(document, step_id: str) -> list[str]:
    used = []
    for other in document.steps:
        refs = [other.input] if isinstance(other, Save) else list(getattr(other, "inputs", []) or [])
        for ref in refs:
            if ref.step == step_id and ref.port and ref.port not in used:
                used.append(ref.port)
    return used


def _family(step, used: list[str]) -> tuple[str, list[int]]:
    """The letters of a pattern family (``C`` of ``C0, C1``) and the numbers in use."""
    prefix = ""
    numbers = []
    for port in used:
        match = re.fullmatch(r"([A-Za-z]+)(\d+)", port)
        if match:
            prefix = prefix or match.group(1)
            if match.group(1) == prefix:
                numbers.append(int(match.group(2)))
    if not prefix:
        axis = str(dict(getattr(step, "params", {}) or {}).get("axis", "") or "")
        prefix = axis if re.fullmatch(r"[A-Za-z]+", axis) and axis.lower() not in ("auto", "projection") else (
            "C" if getattr(step, "processor", "") == "channel-split" else "Z")
    return prefix, numbers


def next_member(document, step, spec, used: list[str]) -> str:
    """The next member of a pattern family: after ``C0, C1`` comes ``C2``."""
    prefix, numbers = _family(step, used)
    candidate = f"{prefix}{max(numbers) + 1 if numbers else 0}"
    if spec is not None and not spec.matches(candidate):
        return f"{prefix}0"
    return candidate


def _outputs(document, step, kind: str, kinds=None) -> tuple[OutputSlot, ...]:
    if isinstance(step, Save):
        return ()
    if isinstance(step, Source):
        return (OutputSlot(SOURCE_PORT, step.id, "", kind),)
    if isinstance(step, (Reconstruct, Consolidate)):
        return (OutputSlot(DEFAULT_PORT, step.id, "", kind),)
    descriptor = _descriptor(document, step)
    if descriptor is not None:
        ports = descriptor.ports_of(step)
        if not ports or not descriptor.produces:
            return ()
        port_kind = getattr(step, "port_kind", None)
        slots = []
        for index, port in enumerate(ports):
            this_kind = kind
            if callable(port_kind):
                try:
                    this_kind = str(port_kind(port, dict(kinds or {})))
                except Exception:  # noqa: BLE001
                    this_kind = kind
            lone = len(ports) == 1
            slots.append(OutputSlot(port, step.id if lone else f"{step.id}.{port}", "" if lone else port, this_kind))
        return tuple(slots)
    entry = document.entry_for(step)
    spec = entry.output_spec(step.params) if entry is not None else None
    ports = tuple(spec.ports or ()) if spec is not None else (DEFAULT_PORT,)
    fan_out = entry is not None and entry.max_inputs == 1 and len(step.inputs) > 1
    slots: list[OutputSlot] = []
    lone = len(ports) == 1 and not (spec is not None and spec.pattern) and not fan_out
    for port in ports:
        slots.append(OutputSlot(port, step.id if lone else f"{step.id}.{port}", "" if lone else port, kind))
        if fan_out:
            for position in range(1, len(step.inputs)):
                member = f"{port}{position}"
                slots.append(OutputSlot(member, f"{step.id}.{member}", member, kind))
    if spec is not None and spec.pattern:
        used = [port for port in _members_used(document, step.id) if spec.matches(port) and port not in ports]
        # Every member up to the highest one read (a split's C0 stays when only
        # C1 is used), then any other one read, then the next as a hollow slot.
        prefix, numbers = _family(step, used)
        shown = [f"{prefix}{number}" for number in range(max(numbers) + 1)] if numbers else []
        shown += [port for port in used if port not in shown]
        for port in shown:
            slots.append(OutputSlot(port, f"{step.id}.{port}", port, kind))
        member = next_member(document, step, spec, used)
        slots.append(OutputSlot(member, f"{step.id}.{member}", member, kind, placeholder=True))
    if not slots and isinstance(step, Process) and step.processor == "python":
        return ()
    return tuple(slots)


def _accepted_kind(document, step) -> str:
    """What an empty input of ``step`` expects (for the colour of its stub)."""
    if isinstance(step, Reconstruct):
        return RECORDING_KIND
    descriptor = _descriptor(document, step)
    if descriptor is not None:
        return descriptor.accepted_kinds[0] if descriptor.accepted_kinds else UNKNOWN_KIND
    entry = document.entry_for(step)
    if isinstance(step, Process) and entry is not None and entry.accepted_kinds:
        return entry.accepted_kinds[0]
    return UNKNOWN_KIND


def _takes_several(document, step) -> bool:
    if isinstance(step, Consolidate):
        return True
    descriptor = _descriptor(document, step)
    if descriptor is not None:
        return descriptor.max_inputs is None or descriptor.max_inputs > 1
    if not isinstance(step, Process):
        return False
    entry = document.entry_for(step)
    return entry is not None and (entry.max_inputs is None or entry.max_inputs > 1)


def code_name(index: int) -> str:
    """The name a Python step's code reads input ``index`` by: ``data`` is
    the first input's array, ``inputs[i]`` any input's (``results[i]`` its
    result object)."""
    return "data (inputs[0])" if index == 0 else f"inputs[{index}]"


def _inputs(document, step, kinds) -> tuple[InputSlot, ...]:
    if isinstance(step, Source):
        return ()
    descriptor = _descriptor(document, step)
    if descriptor is not None and descriptor.is_source:
        return ()
    refs = [step.input] if isinstance(step, Save) else list(getattr(step, "inputs", None) or [])
    python = isinstance(step, Process) and step.processor == "python"

    def code(index):
        return code_name(index) if python else ""

    slots = []
    for index, ref in enumerate(refs):
        label = ref.port if ref.port and ref.port not in (DEFAULT_PORT, SOURCE_PORT) else ""
        slots.append(InputSlot(index, ref, label, kinds.get(ref.step, UNKNOWN_KIND), code=code(index)))
    entry = document.entry_for(step)
    needed = 1
    if isinstance(step, Process) and entry is not None:
        needed = max(1, entry.min_inputs)
    elif descriptor is not None:
        needed = max(1, descriptor.min_inputs)
    for index in range(len(slots), needed):
        slots.append(InputSlot(index, None, "", _accepted_kind(document, step), required=True, code=code(index)))
    if _takes_several(document, step) and len(slots) >= needed:
        slots.append(InputSlot(None, None, "", _accepted_kind(document, step), adds=True, code=code(len(slots))))
    return tuple(slots)


def node_infos(document, issues=None) -> dict[str, NodeInfo]:
    """One :class:`NodeInfo` per step, in file order."""
    from imswitch.imcommon.model.workflow.prepare import bypass_upstream, resolve_ref

    kinds = kinds_of(document)
    upstream = bypass_upstream(document.workflow)
    by_step: dict[str, list[str]] = {}
    for issue in issues if issues is not None else document.issues():
        by_step.setdefault(issue.step, []).append(issue.message)
    infos: dict[str, NodeInfo] = {}
    for step in document.steps:
        entry = document.entry_for(step)
        summary, code = _summary(document, step, entry)
        bypassed = bool(getattr(step, "bypass", False))
        passes = ""
        if bypassed and step.id in upstream:
            passes = str(upstream[step.id])
        elif bypassed and isinstance(step, Save):
            passes = str(resolve_ref(step.input, upstream))
        exposed = tuple(document.exposed(step.id).items()) if hasattr(document, "exposed") else ()
        descriptor = _descriptor(document, step)
        infos[step.id] = NodeInfo(
            id=step.id,
            step_kind=step_kind(step),
            title=title_of(document, step),
            plugin_id=str(getattr(step, "processor", None) or getattr(step, "reconstructor", None) or ""),
            category=entry.category if entry is not None else (descriptor.category if descriptor else ""),
            description=(entry.description if entry is not None else
                         descriptor.description if descriptor is not None else "")
            or _STEP_WORDS.get(step_kind(step), ""),
            summary=summary,
            code=code,
            inputs=_inputs(document, step, kinds),
            outputs=_outputs(document, step, kinds.get(step.id, UNKNOWN_KIND), kinds),
            issues=tuple(by_step.get(step.id, ())),
            bypassed=bypassed,
            passes=passes,
            exposed=exposed,
        )
    return infos


_STEP_WORDS = {
    "source": "A recording: a file, or the one bound when the workflow runs",
    "save": "Writes one result to a file",
    "consolidate": "Merges several reconstructions of one kind into one",
}


# -- which cables may be made -----------------------------------------------------

def kind_fits(kind: str, entry) -> bool:
    """Whether a processing step of ``entry`` takes a result of ``kind``."""
    if kind == RECORDING_KIND:
        return False
    if kind in _LOOSE or entry is None or not entry.accepted_kinds:
        return True
    return kind in entry.accepted_kinds


def can_feed(document, producer_id: str, target_id: str, kinds=None) -> bool:
    """Whether a cable from ``producer_id`` into ``target_id`` makes sense:
    no loop, the right kind of step, and a kind the target takes."""
    from imswitch.imcommon.model.workfloweditor.graph import ancestors

    if producer_id == target_id or not document.has(producer_id) or not document.has(target_id):
        return False
    producer, target = document.step(producer_id), document.step(target_id)
    if isinstance(producer, Save) or isinstance(target, Source):
        return False
    producing = _descriptor(document, producer)
    if producing is not None and not producing.produces:
        return False
    taking = _descriptor(document, target)
    if taking is not None and taking.is_source:
        return False
    if target_id in ancestors(document.steps, producer_id):
        return False
    kinds = kinds if kinds is not None else kinds_of(document)
    kind = kinds.get(producer_id, UNKNOWN_KIND)
    if isinstance(target, Reconstruct):
        return isinstance(producer, Source)
    if isinstance(target, Consolidate):
        return isinstance(producer, Reconstruct) and producer.reconstructor == target.reconstructor
    if isinstance(target, Save):
        return kind != RECORDING_KIND
    if taking is not None:
        return kind_fits(kind, taking)
    return kind_fits(kind, document.entry_for(target))


@dataclass(frozen=True)
class Candidate:
    """A step the quick-add picker offers: what to add, and how it reads."""

    step_kind: str
    plugin_id: str
    title: str
    category: str
    description: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.step_kind, self.plugin_id)


def candidates(catalog, kind: str | None = None, *, producer=None) -> list[Candidate]:
    """What can be added, optionally only what takes one cable of ``kind``.

    ``kind=None`` offers everything (Tab, double-click on the canvas). A cable
    from a recording offers reconstructions; from a reconstruction also its
    consolidation; from any result the processing steps that take it with
    one input, and Save.
    """
    found: list[Candidate] = []
    if catalog is None:
        return found
    if kind is None:
        found.append(Candidate("source", "", "Recording", "Steps", _STEP_WORDS["source"]))
    if kind in (None, RECORDING_KIND):
        for entry in catalog.runnable("reconstructor"):
            found.append(Candidate("reconstruct", entry.id, entry.name, "Reconstruct", entry.description))
    if kind is None:
        for entry in catalog.consolidators():
            if entry.gui_only is None:
                found.append(Candidate("consolidate", entry.id, f"Consolidate · {entry.name}", "Reconstruct",
                                       _STEP_WORDS["consolidate"]))
    elif isinstance(producer, Reconstruct):
        entry = catalog.reconstructor(producer.reconstructor)
        if entry is not None and entry.supports_consolidation and entry.gui_only is None:
            found.append(Candidate("consolidate", entry.id, f"Consolidate · {entry.name}", "Reconstruct",
                                   _STEP_WORDS["consolidate"]))
    if kind != RECORDING_KIND:
        for entry in catalog.runnable("processor"):
            if kind is not None and not kind_fits(kind, entry):
                continue
            found.append(Candidate("process", entry.id, entry.name, entry.category, entry.description))
        found.append(Candidate("save", "", "Save", "Steps", _STEP_WORDS["save"]))
    for descriptor in getattr(catalog, "steps", ()):
        if descriptor.is_source:
            if kind is None:
                found.append(Candidate(descriptor.kind, "", descriptor.name, descriptor.category,
                                       descriptor.description))
            continue
        if kind is not None and not kind_fits(kind, descriptor):
            continue
        found.append(Candidate(descriptor.kind, "", descriptor.name, descriptor.category, descriptor.description))
    return found


def matches(candidate: Candidate, text: str) -> bool:
    """Every word of ``text`` in the candidate's name, id, category or description."""
    haystack = " ".join((candidate.title, candidate.plugin_id, candidate.category, candidate.description)).lower()
    return all(word in haystack for word in str(text).lower().split())


__all__ = [
    "Candidate",
    "InputSlot",
    "NodeInfo",
    "OutputSlot",
    "RESULT_KINDS",
    "can_feed",
    "candidates",
    "code_name",
    "kind_fits",
    "kinds_of",
    "matches",
    "next_member",
    "node_infos",
    "save_file_name",
    "step_kind",
    "title_of",
]
