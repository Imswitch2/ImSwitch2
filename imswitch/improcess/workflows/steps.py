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

import functools
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
    """Run a reconstructor over one input -- a source, or any step that
    produces an image. Its single port is ``out``."""

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


#: How a ``map``/``fold`` body names the slice of the current index (and, in
#: a fold, the carried value). Written ``$item`` in a file; held as a
#: reference to a reserved step id inside, so every reference check works on
#: it unchanged.
ITEM_REF = "$item"
CARRY_REF = "$carry"
ITEM_STEP = "_item"
CARRY_STEP = "_carry"
_RESERVED_BODY_IDS = (ITEM_STEP, CARRY_STEP)


def _body_ref_in(text):
    text = str(text)
    if text == ITEM_REF or text.startswith(ITEM_REF + "."):
        return ITEM_STEP + text[len(ITEM_REF):]
    if text == CARRY_REF or text.startswith(CARRY_REF + "."):
        return CARRY_STEP + text[len(CARRY_REF):]
    return text


def _body_ref_out(ref: Ref) -> str:
    if ref.step == ITEM_STEP:
        return ITEM_REF + (f".{ref.port}" if ref.port else "")
    if ref.step == CARRY_STEP:
        return CARRY_REF + (f".{ref.port}" if ref.port else "")
    return str(ref)


def _body_refs(step) -> list[Ref]:
    if isinstance(step, Save):
        return [step.input]
    return list(getattr(step, "inputs", None) or [])


def _parse_body(owner: str, body) -> list:
    if not isinstance(body, list) or not body:
        raise WorkflowError(f"step {owner!r}: a body must be a non-empty list of steps")
    steps = []
    for item in body:
        if isinstance(item, dict):
            item = dict(item)
            if "inputs" in item and isinstance(item["inputs"], list):
                item["inputs"] = [_body_ref_in(ref) for ref in item["inputs"]]
            if "input" in item:
                item["input"] = _body_ref_in(item["input"])
            step = step_from_dict(item)
        else:
            step = item
        if step.id in _RESERVED_BODY_IDS:
            raise WorkflowError(f"step {owner!r}: {step.id!r} is reserved inside a body")
        steps.append(step)
    seen = set()
    for step in steps:
        if step.id in seen:
            raise WorkflowError(f"step {owner!r}: duplicate body step id {step.id!r}")
        seen.add(step.id)
    return steps


def _body_to_dicts(body) -> list[dict]:
    out = []
    for step in body:
        payload = step.to_dict()
        if "inputs" in payload:
            payload["inputs"] = [_body_ref_out(ref) for ref in _body_refs(step)]
        if "input" in payload and isinstance(step, Save):
            payload["input"] = _body_ref_out(step.input)
        out.append(payload)
    return out


@dataclass
class Map:
    """Run a sub-workflow (``body``) once per index of the ``over`` axis of
    ``input``, and stack its ``output`` along that axis. Port ``out``.

    For what auto-indexing cannot express: a *group* of steps per index
    (deskew, crop, reconstruct, frame by frame), where only the stacked end
    result is wanted. The body names the slice of the current index
    ``$item``; it may also read any earlier step of the workflow (that result
    is handed unchanged to every index). Save is not allowed inside a body.
    The provenance records the body once, with ``iterated_over``.
    """

    id: str
    input: Ref
    over: str
    body: list = field(default_factory=list)
    output: Ref | None = None

    kind = "map"
    ports = (DEFAULT_PORT,)

    def __post_init__(self):
        self.id = _check_id(self.id)
        self.input = parse_ref(self.input)
        self.over = str(self.over or "")
        self.body = _parse_body(self.id, self.body)
        if self.output is None:
            self.output = Ref(self.body[-1].id)
        self.output = parse_ref(self.output)

    @property
    def body_ids(self) -> set[str]:
        return {step.id for step in self.body}

    def outer_refs(self) -> list[Ref]:
        """References from the body to steps of the enclosing workflow, in order, once each."""
        inner = self.body_ids | set(_RESERVED_BODY_IDS)
        seen, found = set(), []
        for step in self.body:
            for ref in _body_refs(step):
                if ref.step not in inner and str(ref) not in seen:
                    seen.add(str(ref))
                    found.append(ref)
        return found

    @property
    def inputs(self) -> list[Ref]:
        """What this step reads from the enclosing workflow: ``input`` first,
        then whatever the body broadcasts. Read by every graph walk."""
        return [self.input, *[ref for ref in self.outer_refs() if str(ref) != str(self.input)]]

    @inputs.setter
    def inputs(self, refs) -> None:
        # The editor rewires the cable into the step; the body's own
        # references to the enclosing workflow are the body's.
        refs = list(refs or [])
        if refs:
            self.input = parse_ref(refs[0])

    def rename_outer(self, mapping: dict[str, Ref]) -> None:
        """Point the body's references to outer steps at new ids (``old id -> Ref``)."""
        for step in self.body:
            if isinstance(step, Save):
                continue
            step.inputs = [
                (Ref(mapping[ref.step].step, ref.port if ref.port is not None else mapping[ref.step].port)
                 if ref.step in mapping else ref)
                for ref in _body_refs(step)
            ]

    def _payload(self) -> dict:
        return {"step": self.kind, "id": self.id, "input": str(self.input), "over": self.over,
                "body": _body_to_dicts(self.body), "output": str(self.output)}

    def to_dict(self) -> dict:
        return self._payload()


@dataclass
class Fold(Map):
    """Like :class:`Map`, with state carried from one index to the next
    (LabVIEW's shift register): a drift reference, a crop box chosen on the
    first frame.

    ``carry`` is ``{"init": ref or null, "next": "body_step.port"}``: the
    body reads the carried value as ``$carry`` (``init`` at the first index,
    or nothing when ``init`` is null) and its ``next`` port is what the next
    index finds there. **Designed, not implemented yet**: a fold validates,
    loads, saves and round-trips; running one is refused with a clear error.
    """

    carry: dict = field(default_factory=dict)

    kind = "fold"

    def __init__(self, id, input, over, body=None, output=None, carry=None):
        self.id = id
        self.input = input
        self.over = over
        self.body = body or []
        self.output = output
        self.carry = dict(carry or {})
        self.__post_init__()
        if set(self.carry) - {"init", "next"}:
            raise WorkflowError(f"step {self.id!r}: carry has only 'init' and 'next'")
        if not self.carry.get("next"):
            raise WorkflowError(f"step {self.id!r}: carry needs 'next', the body port that is carried on")
        self.carry["next"] = str(parse_ref(self.carry["next"]))
        if self.carry.get("init") is not None:
            self.carry["init"] = str(parse_ref(self.carry["init"]))
        else:
            self.carry["init"] = None

    @property
    def inputs(self) -> list[Ref]:
        refs = Map.inputs.fget(self)
        init = self.carry.get("init")
        if init and init not in {str(ref) for ref in refs}:
            refs.append(parse_ref(init))
        return refs

    @inputs.setter
    def inputs(self, refs) -> None:
        Map.inputs.fset(self, refs)

    def to_dict(self) -> dict:
        payload = self._payload()
        payload["carry"] = {"init": self.carry.get("init"), "next": self.carry["next"]}
        return payload


Step = Source | Reconstruct | Consolidate | Process | Save | Map | Fold

_STEP_TYPES = {"source": Source, "reconstruct": Reconstruct, "consolidate": Consolidate,
               "process": Process, "save": Save, "map": Map, "fold": Fold}


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

        return yaml.dump(
            self.to_dict(), Dumper=_block_style_dumper(), sort_keys=False, allow_unicode=True,
        )

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


@functools.lru_cache(maxsize=1)
def _block_style_dumper():
    """A ``SafeDumper`` that writes multi-line strings in block style (``|``).

    A parameter that holds code (a Python step's ``code``) then reads in the
    file as the lines it is, not as one quoted line full of ``\\n``. Strings
    without a newline keep the default style. PyYAML falls back to a quoted
    scalar for text it cannot write as a block (a tab, trailing spaces), so a
    value always loads back equal. A subclass, so ``yaml.safe_dump`` elsewhere
    is unaffected.
    """
    import yaml

    class BlockStyleDumper(yaml.SafeDumper):
        pass

    def represent_str(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    BlockStyleDumper.add_representer(str, represent_str)
    return BlockStyleDumper


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
    if isinstance(step, (Reconstruct, Consolidate, Map)):
        return (port in (None, DEFAULT_PORT)), DEFAULT_PORT
    if isinstance(step, Process):
        processor = registry.get_processor(step.processor, raise_on_missing=False) if registry else None
        if processor is None:
            return True, "?"
        from imswitch.improcess.processors.base import call_output_spec

        params = {**_defaults(processor), **step.params}
        spec = call_output_spec(processor, params)
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


def declared_kinds(workflow: Workflow, registry) -> dict[str, str]:
    """What each step produces, as declared (step id -> a ``PORT_KINDS`` value).

    A Source produces a recording; a reconstruction or a processor what it
    declares (a processor's ``output_spec`` is told its inputs' kinds); an
    unknown plugin, or one that declares nothing, ``"unknown"``. Save steps
    produce nothing and are left out. Never raises: a plugin whose
    ``output_spec`` fails counts as ``"unknown"``.
    """
    from imswitch.improcess.model.result import RECORDING_KIND, UNKNOWN_KIND
    from imswitch.improcess.processors.base import call_output_spec

    kinds: dict[str, str] = {}
    for step in workflow.steps:
        if isinstance(step, Save):
            continue
        if isinstance(step, Source):
            kinds[step.id] = RECORDING_KIND
            continue
        kind = UNKNOWN_KIND
        if registry is not None and isinstance(step, (Reconstruct, Consolidate)):
            plugin = registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
            kind = getattr(plugin, "output_kind", None) or UNKNOWN_KIND
        elif registry is not None and isinstance(step, Process):
            plugin = registry.get_processor(step.processor, raise_on_missing=False)
            if plugin is not None:
                inputs = tuple(kinds.get(ref.step, UNKNOWN_KIND) for ref in step.inputs)
                try:
                    kind = call_output_spec(plugin, {**_defaults(plugin), **step.params}, inputs).kind
                except Exception:
                    kind = UNKNOWN_KIND
        elif registry is not None and isinstance(step, Map):
            kind = _map_output_kind(workflow, step, registry)
        kinds[step.id] = str(kind or UNKNOWN_KIND)
    return kinds


def _map_output_kind(workflow: Workflow, step: "Map", registry) -> str:
    """What a map produces: what its body's output step declares, the body
    read as the workflow it runs as. ``unknown`` when that cannot be said."""
    from imswitch.improcess.model.result import UNKNOWN_KIND

    before = []
    for item in workflow.steps:
        if item is step:
            break
        if not isinstance(item, Save):
            before.append(item)
    placeholders = [Source(ITEM_STEP)] + ([Source(CARRY_STEP)] if isinstance(step, Fold) else [])
    try:
        sub = Workflow(f"{step.id}-body", [*before, *placeholders, *step.body])
        return declared_kinds(sub, registry).get(step.output.step, UNKNOWN_KIND)
    except Exception:
        return UNKNOWN_KIND


def _kind_issues(step, processor, kinds: dict[str, str]) -> list[Issue]:
    """An input whose *declared* kind the processor does not accept.

    Only a declared result kind counts: "any" (the Python step), "unknown"
    (a plugin that declares nothing) and a raw recording are never refused
    here, so a workflow that runs today is not refused on a guess.
    """
    from imswitch.improcess.model.result import RESULT_KINDS

    accepted = tuple(getattr(processor, "kinds", ()) or ())
    issues = []
    for ref in step.inputs:
        kind = kinds.get(ref.step)
        if accepted and kind in RESULT_KINDS and kind not in accepted:
            issues.append(Issue(
                step.id,
                f"{step.processor!r} does not accept {kind} results, which {ref.step!r} produces "
                f"(it takes {', '.join(accepted)})",
            ))
    return issues


#: What a reconstruction may read: a recording, an image, or a step whose
#: kind is not declared (a Python step, a plugin that says nothing) -- the run
#: checks the data itself, so a workflow that may work is not refused on a guess.
RECONSTRUCTION_INPUT_KINDS = ("recording", "image", "labels", "any", "unknown")


def _reconstruction_input_issues(step, by_id: dict, kinds: dict[str, str]) -> list[Issue]:
    """Whether a reconstruction's one input can be read as image data.

    Decided by what the input *carries*, never by where it came from: a
    Source, a view-only reconstruction, a Python step's reshape and a deskew
    are all images to the next reconstructor. Without a registry the kinds are
    unknown and only the structure is checked.
    """
    ref = step.inputs[0]
    target = by_id.get(ref.step)
    if target is None or isinstance(target, Source):
        return []
    kind = kinds.get(ref.step)
    if kind is None or kind in RECONSTRUCTION_INPUT_KINDS:
        return []
    return [Issue(
        step.id,
        f"a reconstruction's input must be a source or produce an image; {ref.step!r} produces {kind} results",
    )]


def _input_contract_issues(step, plugin) -> list[Issue]:
    """What the plugin's ``check_input`` can say from the parameters alone
    (``data_obj=None``): the data is not known before the run, which checks
    it again with the data before any pixel is processed."""
    check = getattr(plugin, "check_input", None)
    if not callable(check):
        return []
    try:
        problems = check(None, {**_defaults(plugin), **step.params})
    except Exception:
        return []
    return [Issue(step.id, str(problem)) for problem in problems or []]


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
    from imswitch.improcess.model.plugin_contract import contract_problem

    problem = contract_problem(plugin)
    if problem:
        # No declaration (or a widget that contradicts it) means the
        # headless defaults are not the effective ones: refuse, rather than
        # run with parameters the record could not vouch for.
        return [Issue(step.id, f"{problem}; it cannot run in a workflow")]
    getter = getattr(type(plugin), "param_keys", None)
    try:
        allowed = getter() if callable(getter) else None
    except Exception:
        allowed = None
    if allowed is None:
        return []
    # ``_iterate`` is the runner's, not the plugin's (auto-indexing on/off).
    unknown = sorted(set(step.params) - set(allowed) - {"_iterate"})
    if not unknown:
        return []
    plugin_id = getattr(plugin, "id", type(plugin).__name__)
    return [Issue(step.id, f"unknown parameter(s) for {plugin_id!r}: {unknown} (accepted: {sorted(allowed) or 'none'})")]


def _body_issues(step: "Map", outer: list, registry) -> list[Issue]:
    """A map/fold body checked as the workflow it runs as: the enclosing
    steps before it, ``$item`` (and ``$carry``) as inputs, then the body."""
    issues: list[Issue] = []
    what = f"{step.kind} {step.id!r}"
    if not step.over:
        issues.append(Issue(step.id, f"{what}: 'over' must name the axis to iterate over"))
    for inner in step.body:
        if isinstance(inner, (Save, Map)):
            issues.append(Issue(step.id, f"{what}: a {inner.kind} step ({inner.id!r}) is not allowed in a body"))
    outer_ids = {item.id for item in outer}
    for inner in step.body:
        if inner.id in outer_ids:
            issues.append(Issue(step.id, f"{what}: body step {inner.id!r} has the id of a step outside it"))
    if step.output.step not in step.body_ids:
        issues.append(Issue(step.id, f"{what}: output {str(step.output)!r} is not a step of its body"))
    if isinstance(step, Fold):
        nxt = parse_ref(step.carry["next"])
        if nxt.step not in step.body_ids:
            issues.append(Issue(step.id, f"{what}: carry.next {str(nxt)!r} is not a step of its body"))
    if issues:
        return issues
    placeholders = [Source(ITEM_STEP)]
    if isinstance(step, Fold):
        placeholders.append(Source(CARRY_STEP))
    try:
        sub = Workflow(f"{step.id}-body", [*outer, *placeholders, *step.body])
    except WorkflowError as exc:
        return [Issue(step.id, f"{what}: {exc}")]
    body_ids = step.body_ids
    for issue in validate(sub, registry):
        if issue.step in body_ids:
            text = issue.message.replace(repr(ITEM_STEP), repr(ITEM_REF)).replace(repr(CARRY_STEP), repr(CARRY_REF))
            issues.append(Issue(step.id, f"in the body, {issue.step}: {text}"))
    return issues


def validate(workflow: Workflow, registry=None) -> list[Issue]:
    """Everything wrong with ``workflow`` that can be known before running.

    With a registry: plugin ids, arity and ports are checked too. Without,
    only structure (ids, references, order).
    """
    issues: list[Issue] = []
    by_id: dict[str, Any] = {}
    kinds = declared_kinds(workflow, registry) if registry else {}
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
                issues.append(Issue(step.id, "a reconstruction takes exactly one input"))
            else:
                issues.extend(_reconstruction_input_issues(step, by_id, kinds))
            if registry:
                plugin = registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
                if plugin is None:
                    issues.append(Issue(step.id, f"unknown reconstructor {step.reconstructor!r}"))
                else:
                    issues.extend(_unknown_params(step, plugin))
                    issues.extend(_input_contract_issues(step, plugin))
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
                    issues.extend(_kind_issues(step, processor, kinds))
        elif isinstance(step, Map):
            issues.extend(_body_issues(step, list(by_id.values()), registry))
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
    "CARRY_REF",
    "Consolidate",
    "Fold",
    "ITEM_REF",
    "Issue",
    "Map",
    "Process",
    "RECONSTRUCTION_INPUT_KINDS",
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
