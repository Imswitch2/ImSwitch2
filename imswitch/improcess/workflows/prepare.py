"""Turn a workflow file (the *recipe*) into the plain workflow a run executes.

A schema-2 recipe may name **controls** (parameters a run may set by name;
the value lives in the target steps) and **bypass** processing or Save steps.
Nothing downstream of this module knows either exists: every run --
``run()``, ``run_over()``, the command line, the GUI's run commands and the
editor's partial runs -- goes through :func:`prepare_run`, which in a fixed
order

1. checks the recipe (control entries and targets, bypass eligibility);
2. takes bypassed steps out, rewriting every reference to a bypassed
   processing step to that step's own input (following chains), and refuses
   a binding that names a bypassed step;
3. cuts the workflow to a subset (the editor's *Run to here*) and its
   ancestors;
4. works out which steps bindings replace (``steps_replaced_by_bindings``,
   unchanged, on the rewritten graph);
5. works out which control targets are active (their step runs);
6. applies this run's values (file → run-wide → row), converted and checked
   by the value contract against each control's combined field;
7. drops the control definitions;
8. validates the result as a plain workflow.

See ``docs/design/plans/improcess-workflow-schema-2.md`` for the reasons.
"""

from __future__ import annotations

import math
import numbers
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from imswitch.improcess.workflows.steps import (
    CONTROL_NAME_PATTERN,
    DEFAULT_PORT,
    Control,
    Issue,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    Workflow,
    WorkflowError,
    _defaults,
    parse_target,
    step_from_dict,
    validate_plain,
)

#: The parameter types a control may set: one value, typed on a command line
#: or in a manifest cell. ``code``, ``json`` and ``multiselect`` are refused.
CONTROL_FIELD_TYPES = ("int", "float", "bool", "text", "select", "path")

#: Why a target is not active in a run.
BYPASSED = "bypassed"
REPLACED = "replaced by a bound result"
FEEDS_REPLACED = "only feeds a replaced step"
OUTSIDE = "outside this run"

_MISSING = object()


class ControlText(str):
    """Raw text for a control's value (``--set name=TEXT``, a manifest cell),
    converted by the value contract (:func:`convert_text`). Any other value
    handed to :func:`prepare_run` is a typed value (a dialog's, Python's)."""

    __slots__ = ()


class ControlValueError(WorkflowError):
    """A value given for a control could not be used; ``control`` names it
    (a batch row attributes it to its manifest column)."""

    def __init__(self, control: str, reason: str):
        super().__init__(f"control {control!r}: {reason}")
        self.control = control
        self.reason = reason


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CombinedField:
    """One control's field, from its active targets' fields (plan 4.4)."""

    type: str
    options: tuple = ()
    nullable: bool = False
    min: float | None = None
    max: float | None = None
    step: float | None = None
    decimals: int | None = None
    suffix: str = ""
    label: str = ""
    help: str = ""
    #: The default every active target shares, when they share one.
    reset: Any = None
    has_reset: bool = False


@dataclass(frozen=True)
class TargetState:
    target: str
    step: str
    param: str
    active: bool
    reason: str = ""


@dataclass
class ControlState:
    """A control in one run: its targets, whether they run, its field and value."""

    control: Control
    targets: list[TargetState] = field(default_factory=list)
    spec: CombinedField | None = None
    #: The value its active targets hold in the file (``has_value`` when they
    #: agree and the value could be read).
    value: Any = None
    has_value: bool = False
    #: ``(target, ParamField, file value)`` of the active targets whose plugin
    #: is installed.
    fields: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.control.name

    @property
    def active(self) -> bool:
        return any(target.active for target in self.targets)

    @property
    def inactive_reasons(self) -> list[tuple[str, str]]:
        return [(target.target, target.reason) for target in self.targets if not target.active]


@dataclass
class PreparedRun:
    """What :func:`prepare_run` hands a run."""

    #: The execution workflow: no controls, no bypass, schema-1 meaning.
    workflow: Workflow
    recipe: Workflow
    #: Steps taken out (bypassed processing and Save steps), in file order.
    bypassed: tuple[str, ...] = ()
    #: Every active control's canonical value for this run (given or the file's).
    values: dict[str, Any] = field(default_factory=dict)
    #: Controls with no active target: name -> [(target, why)].
    inactive: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: (consumer step, input index or -1 for a Save) -> (old ref, new ref).
    rewired: dict[tuple[str, int], tuple[str, str]] = field(default_factory=dict)
    #: Steps the bindings replace (and what only fed them).
    replaced: frozenset = frozenset()
    #: Things worth saying that are not errors.
    notes: list[str] = field(default_factory=list)
    controls: dict[str, ControlState] = field(default_factory=dict)


# --------------------------------------------------------------------------
# bypass
# --------------------------------------------------------------------------

def _refs(step) -> list[Ref]:
    if isinstance(step, Source):
        return []
    if isinstance(step, Save):
        return [step.input]
    return list(step.inputs)


def _default_port(ref: Ref) -> bool:
    return ref.port in (None, DEFAULT_PORT)


def bypass_upstream(workflow: Workflow) -> dict[str, Ref]:
    """For every bypassed processing step the rules allow (plan 5.4), the
    reference that stands in for it: its input, followed through further
    bypassed steps. A bypass the rules refuse has no stand-in, so the step
    stays in and its readers are not rewired to something they never read."""
    read_by_port = {ref.step for step in workflow.steps for ref in _refs(step) if not _default_port(ref)}
    upstream: dict[str, Ref] = {}
    for step in workflow.steps:
        if (isinstance(step, Process) and step.bypass and len(step.inputs) == 1
                and step.id not in read_by_port):
            upstream[step.id] = resolve_ref(step.inputs[0], upstream)
    return upstream


def resolve_ref(ref: Ref, upstream: Mapping[str, Ref]) -> Ref:
    """``ref`` with a bypassed step replaced by what it passes through."""
    if ref.step in upstream and _default_port(ref):
        return upstream[ref.step]
    return ref


def bypass_issues(workflow: Workflow) -> list[Issue]:
    """Bypasses the rules refuse (plan 5.4): a processing step with other
    than one input, or one read by a port other than its default."""
    issues: list[Issue] = []
    bypassed = {step.id: step for step in workflow.steps if isinstance(step, Process) and step.bypass}
    for step in bypassed.values():
        if len(step.inputs) != 1:
            issues.append(Issue(step.id, f"cannot be bypassed: it reads {len(step.inputs)} inputs, and a "
                                          "bypassed step passes exactly one through"))
    for step in workflow.steps:
        for ref in _refs(step):
            if ref.step in bypassed and not _default_port(ref):
                issues.append(Issue(ref.step, f"cannot be bypassed: {step.id!r} reads its port {ref.port!r}, "
                                              "which nothing would stand for"))
    return issues


def _copy(step):
    copied = step_from_dict(step.to_dict())
    if hasattr(copied, "bypass"):
        copied.bypass = False
    return copied


def _rewrite(recipe: Workflow, upstream: Mapping[str, Ref]):
    """The recipe's steps with bypassed ones taken out and their readers
    rewired; a bypassed step the rules refuse stays in (for validation), as if
    it were not bypassed."""
    steps = []
    rewired: dict[tuple[str, int], tuple[str, str]] = {}
    bypassed: list[str] = []
    for step in recipe.steps:
        if isinstance(step, Save) and step.bypass:
            bypassed.append(step.id)
            continue
        if isinstance(step, Process) and step.bypass and step.id in upstream:
            bypassed.append(step.id)
            continue
        copied = _copy(step)
        if isinstance(copied, Save):
            new = resolve_ref(copied.input, upstream)
            if new != copied.input:
                rewired[(copied.id, -1)] = (str(copied.input), str(new))
                copied.input = new
        elif not isinstance(copied, Source):
            refs = []
            for index, ref in enumerate(copied.inputs):
                new = resolve_ref(ref, upstream)
                if new != ref:
                    rewired[(copied.id, index)] = (str(ref), str(new))
                refs.append(new)
            copied.inputs = refs
        steps.append(copied)
    return steps, rewired, bypassed


def _cut(steps: list, wanted: Iterable[str]) -> list:
    keep = set(wanted)
    for step in reversed(steps):
        if step.id in keep:
            keep.update(ref.step for ref in _refs(step))
    return [step for step in steps if step.id in keep]


# --------------------------------------------------------------------------
# controls: activity, fields, agreement
# --------------------------------------------------------------------------

def _plugin(step, registry):
    if registry is None:
        return None
    if isinstance(step, Process):
        return registry.get_processor(step.processor, raise_on_missing=False)
    if isinstance(step, Reconstruct):
        return registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
    return None


def _param_keys(plugin) -> set | None:
    getter = getattr(type(plugin), "param_keys", None)
    try:
        keys = getter() if callable(getter) else None
    except Exception:
        keys = None
    if keys is None:
        keys = _defaults(plugin).keys() or None
    return set(keys) if keys is not None else None


def _field(plugin, param):
    from imswitch.improcess.model.param_spec import fields_by_key, spec_for

    return fields_by_key(spec_for(plugin)).get(param)


def control_states(recipe: Workflow, registry=None, reason_for=None) -> tuple[dict[str, ControlState], list[Issue]]:
    """Every control's targets, activity, combined field and file value, and
    the issues its entry and targets raise (plan 4.5).

    ``reason_for(step_id)`` says why a step's targets are inactive in this
    run ("" when active); by default a bypassed step's are.
    """
    by_id = {step.id: step for step in recipe.steps}
    if reason_for is None:
        def reason_for(step_id):
            step = by_id.get(step_id)
            return BYPASSED if getattr(step, "bypass", False) else ""
    issues: list[Issue] = []
    states: dict[str, ControlState] = {}
    owner: dict[str, str] = {}
    for control in recipe.controls:
        name = control.name
        where = "controls"
        if not CONTROL_NAME_PATTERN.match(name):
            issues.append(Issue(where, f"bad control name {name!r}: use letters, digits and '_', "
                                       "not starting with a digit"))
        if name in states:
            issues.append(Issue(where, f"two controls are named {name!r}"))
            continue
        if name in by_id:
            issues.append(Issue(where, f"control {name!r} has the name of a step; a manifest column "
                                       "could not tell them apart"))
        state = ControlState(control)
        states[name] = state
        if not control.targets:
            issues.append(Issue(where, f"control {name!r} has no targets"))
        seen: set[str] = set()
        for text in control.targets:
            parsed = parse_target(text)
            if parsed is None:
                issues.append(Issue(where, f"control {name!r}: bad target {text!r}; expected 'step.param'"))
                continue
            step_id, param = parsed
            if text in seen:
                issues.append(Issue(step_id, f"control {name!r} names target {text!r} twice"))
                continue
            seen.add(text)
            step = by_id.get(step_id)
            if step is None:
                issues.append(Issue(where, f"control {name!r}: target {text!r} names no step"))
                continue
            if not isinstance(step, (Reconstruct, Process)):
                issues.append(Issue(step_id, f"control {name!r}: {text!r} is a {step.kind} step; a control "
                                             "sets parameters of reconstruct and process steps"))
                continue
            if text in owner:
                issues.append(Issue(step_id, f"{text!r} is set by two controls, {owner[text]!r} and {name!r}"))
                continue
            owner[text] = name
            if isinstance(step, Process) and step.processor == "python" and param == "ports":
                issues.append(Issue(step_id, f"control {name!r}: a Python step's ports are its outputs and "
                                             "cannot be a control"))
                continue
            reason = reason_for(step_id) or ""
            target = TargetState(text, step_id, param, active=not reason, reason=reason)
            state.targets.append(target)
            plugin = _plugin(step, registry)
            if plugin is None:
                continue        # unknown plugin: plain validation says so for a running step
            keys = _param_keys(plugin)
            plugin_id = getattr(step, "processor", None) or getattr(step, "reconstructor", "")
            if keys is not None and param not in keys:
                issues.append(Issue(step_id, f"control {name!r}: {plugin_id!r} has no parameter {param!r}"))
                continue
            spec = _field(plugin, param)
            if spec is None:
                issues.append(Issue(step_id, f"control {name!r}: {plugin_id!r} declares no field for {param!r}"))
                continue
            if spec.type not in CONTROL_FIELD_TYPES:
                issues.append(Issue(step_id, f"control {name!r}: {text!r} is a {spec.type} parameter; a control "
                                             f"takes {', '.join(CONTROL_FIELD_TYPES)}"))
                continue
            if target.active:
                value = {**_defaults(plugin), **dict(step.params)}.get(param, _MISSING)
                state.fields.append((text, spec, value))
        if state.fields:
            combined, problems = combine(control, [(text, spec) for text, spec, _value in state.fields])
            state.spec = combined
            issues.extend(Issue(state.fields[0][0].split(".", 1)[0], problem) for problem in problems)
            values = [value for _text, _spec, value in state.fields if value is not _MISSING]
            if values and len(values) == len(state.fields):
                if all(_agree(values[0], other) for other in values[1:]):
                    state.value, state.has_value = values[0], True
                else:
                    listed = ", ".join(f"{text} = {value!r}" for text, _spec, value in state.fields)
                    issues.append(Issue(state.fields[0][0].split(".", 1)[0],
                                        f"control {name!r}: its targets disagree ({listed}); a control has one value"))
    return states, issues


def _agree(a, b) -> bool:
    from imswitch.improcess.model.param_spec import _same as same

    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return same(a, b)


def combine(control: Control, fields: list) -> tuple[CombinedField | None, list[str]]:
    """The combined field of ``fields`` (``(target, ParamField)``, active
    targets only) and the problems that stop them combining."""
    name = control.name
    problems: list[str] = []
    types = {spec.type for _t, spec in fields}
    if len(types) > 1:
        listed = ", ".join(f"{t} ({spec.type})" for t, spec in fields)
        problems.append(f"control {name!r}: its targets are {listed}; one value cannot fit them")
        return None, problems
    kind = types.pop()
    first = fields[0][1]
    if kind == "select" and any(tuple(spec.options) != tuple(first.options) for _t, spec in fields[1:]):
        problems.append(f"control {name!r}: its targets offer different choices")
    units = {spec.suffix or "" for _t, spec in fields}
    if len(units) > 1:
        listed = ", ".join(f"{t} in {spec.suffix or 'no unit'}" for t, spec in fields)
        problems.append(f"control {name!r}: its targets have different units ({listed}); two units are two meanings")
    lows = [spec.min for _t, spec in fields if spec.min is not None]
    highs = [spec.max for _t, spec in fields if spec.max is not None]
    low = max(lows) if lows else None
    high = min(highs) if highs else None
    if low is not None and high is not None and low > high:
        problems.append(f"control {name!r}: no value fits all its targets (they need at least {low} and at most {high})")
    decimals = [spec.decimals for _t, spec in fields if spec.decimals is not None]
    steps = [spec.step for _t, spec in fields if spec.step is not None]
    defaults = [spec.default for _t, spec in fields]
    shared = all(_agree(defaults[0], other) for other in defaults[1:])
    if problems:
        return None, problems
    return CombinedField(
        type=kind, options=tuple(first.options), nullable=all(spec.nullable for _t, spec in fields),
        min=low, max=high, step=min(steps) if steps else None, decimals=max(decimals) if decimals else None,
        suffix=first.suffix or "", label=control.label or first.label or first.key,
        help=control.help or first.help, reset=defaults[0] if shared else None, has_reset=shared,
    ), problems


# --------------------------------------------------------------------------
# the value contract (plan 4.6)
# --------------------------------------------------------------------------

_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_TEXTUAL = ("text", "path", "select")


def convert_text(text: str, spec: CombinedField):
    """``text`` (from ``--set`` or a manifest cell) as a value of ``spec``.

    Missing values never get here (an empty cell is missing). ``null`` is
    ``None``; ``''`` is the empty string; ``'…'`` is literal text (``''`` for a
    quote inside); anything else is converted by the field's type, never by
    general YAML (``001`` stays text in a text field, ``on`` is no boolean).
    Raises ``ValueError`` with the reason.
    """
    text = str(text)
    if text == "":
        raise ValueError("no value; write '' for empty text or null for none")
    if text == "null":
        if not spec.nullable:
            raise ValueError("null is not allowed: not every target can be left unset")
        return None
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        inner = text[1:-1]
        if "'" in inner.replace("''", ""):
            raise ValueError(f"bad quoting in {text}: write '' for a quote inside quotes")
        if spec.type not in _TEXTUAL:
            raise ValueError(f"quotes make text, and this control takes {_a(spec.type)}")
        literal = inner.replace("''", "'")
        return _option_from_text(literal, spec) if spec.type == "select" else literal
    if spec.type == "int":
        if not _INT_TEXT.fullmatch(text):
            raise ValueError(f"{text!r} is not a whole number")
        return int(text)
    if spec.type == "float":
        if not _FLOAT_TEXT.fullmatch(text):
            raise ValueError(f"{text!r} is not a number (nan and inf are not accepted)")
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(f"{text!r} is too large")
        return value
    if spec.type == "bool":
        lowered = text.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        raise ValueError(f"{text!r} is not true or false")
    if spec.type == "select":
        return _option_from_text(text, spec)
    return text


def _option_from_text(text: str, spec: CombinedField):
    matches = [option for option in spec.options if str(option) == text]
    if len(matches) == 1:
        return matches[0]
    listed = ", ".join(str(option) for option in spec.options)
    if not matches:
        raise ValueError(f"{text!r} is not one of {listed}")
    raise ValueError(f"{text!r} matches more than one choice ({listed})")


def check_typed(value, spec: CombinedField):
    """A typed value (a dialog's, Python's) checked against ``spec`` and made
    canonical: an integer for an ``int``, a float for a ``float`` (``3`` →
    ``3.0``), the option itself for a ``select``. Raises ``ValueError``."""
    if value is None:
        if not spec.nullable:
            raise ValueError("None is not allowed: not every target can be left unset")
        return None
    kind = spec.type
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            raise ValueError(f"{value!r} is not a whole number")
        return int(value)
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ValueError(f"{value!r} is not a number")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{value!r} is not a finite number")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{value!r} is not true or false")
        return value
    if kind == "select":
        for option in spec.options:
            if _agree(option, value):
                return option
        raise ValueError(f"{value!r} is not one of {', '.join(str(o) for o in spec.options)}")
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not text")
    return value


def control_value(given, state: ControlState):
    """The canonical value ``given`` (raw :class:`ControlText` or typed) means
    for ``state``, checked against its combined field and every active
    target's ``ParamField``. Raises ``ValueError``."""
    spec = state.spec
    value = convert_text(given, spec) if isinstance(given, ControlText) else check_typed(given, spec)
    if spec.type in ("int", "float") and value is not None:
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{value} is below {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{value} is above {spec.max}")
    for target, param_field, _current in state.fields:
        problem = param_field.problem_with(value)
        if problem:
            raise ValueError(f"{target}: {problem}")
    return value


def _a(kind: str) -> str:
    return {"int": "a whole number", "float": "a number", "bool": "true or false"}.get(kind, f"a {kind}")


# --------------------------------------------------------------------------
# preparation
# --------------------------------------------------------------------------

def _result_bound(bindings) -> set[str]:
    from imswitch.improcess.workflows.runner import is_result_binding

    return {sid for sid, value in (bindings or {}).items() if is_result_binding(value)}


def _prepare(recipe: Workflow, registry, values, bindings, bound, subset, *, strict: bool):
    """Shared by :func:`prepare_run` (``strict``: raise) and :func:`check_recipe`."""
    from imswitch.improcess.workflows.runner import steps_replaced_by_bindings

    issues: list[Issue] = []
    by_id = {step.id: step for step in recipe.steps}
    # 1-2. the recipe's bypasses, taken out
    upstream = bypass_upstream(recipe)
    # Problems of the recipe's bypasses and controls; a partial run keeps
    # only those of steps it runs (or of no step), below.
    recipe_issues: list[Issue] = list(bypass_issues(recipe))
    bypassed_all = [step.id for step in recipe.steps if getattr(step, "bypass", False)]
    bound_ids = set(bound or ()) | _result_bound(bindings)
    for sid in dict.fromkeys([*(bindings or {}), *sorted(bound or ())]):
        if sid in bypassed_all:
            stand_in = upstream.get(sid)
            hint = f"; bind {stand_in.step!r}, which it passes through" if stand_in is not None else ""
            issues.append(Issue(sid, f"is bypassed and cannot be bound{hint}"))
    steps, rewired, bypassed = _rewrite(recipe, upstream)
    # 3. the subset
    if subset:
        # Asking for a bypassed step asks for what it passes through (a
        # bypassed Save, for what it would have saved).
        saves = {step.id: step.input for step in recipe.steps if isinstance(step, Save) and step.bypass}
        wanted = []
        for sid in subset:
            ref = saves.get(sid, Ref(sid))
            wanted.append(resolve_ref(ref, upstream).step)
        steps = _cut(steps, wanted)
    execution = Workflow(name=recipe.name, steps=steps, description=recipe.description,
                         metadata=dict(recipe.metadata))
    present = {step.id for step in steps}
    # 4. replaced steps
    bound_present = bound_ids & present
    replaced = steps_replaced_by_bindings(execution, bound_present) if bound_present else set()

    # 5. activity
    def reason_for(step_id):
        if getattr(by_id.get(step_id), "bypass", False):
            return BYPASSED
        if step_id not in present:
            return OUTSIDE
        if step_id in bound_ids:
            return REPLACED
        if step_id in replaced:
            return FEEDS_REPLACED
        return ""

    states, control_issues = control_states(recipe, registry, reason_for)
    for state in states.values():
        # The value the targets hold must be one the control's field can
        # show and check: a hand-edited "2,5" for a number is the file's
        # problem, said here, not a form's crash later.
        if state.active and state.has_value and state.spec is not None:
            try:
                check_typed(state.value, state.spec)
            except ValueError as exc:
                first = next(t for t in state.targets if t.active)
                control_issues.append(Issue(first.step, f"control {state.name!r}: the value its parameters hold, "
                                                        f"{state.value!r}, does not fit it ({exc})"))
    recipe_issues.extend(control_issues)
    if subset:
        recipe_issues = [issue for issue in recipe_issues if issue.step in present or issue.step not in by_id]
    issues[:0] = recipe_issues
    notes = []
    for state in states.values():
        if state.targets and not state.active and all(t.reason == BYPASSED for t in state.targets):
            steps_named = sorted({t.step for t in state.targets})
            verb = "is" if len(steps_named) == 1 else "are"
            notes.append(f"control {state.name!r} has no effect while {', '.join(steps_named)} {verb} bypassed")
    # 6. values
    applied: dict[str, Any] = {}
    value_error: ControlValueError | None = None
    for name, given in (values or {}).items():
        state = states.get(name)
        if state is None:
            known = ", ".join(c.name for c in recipe.controls) or "none"
            value_error = value_error or ControlValueError(name, f"no such control (this workflow has: {known})")
            continue
        if not state.active:
            why = ", ".join(f"{target} ({reason})" for target, reason in state.inactive_reasons) or "no targets"
            value_error = value_error or ControlValueError(
                name, f"has no active target in this run, so a value would change nothing: {why}")
            continue
        if state.spec is None:
            if not state.fields:
                # No installed plugin declares the targets' fields, so the
                # value cannot be checked (or typed): refuse it rather than
                # run without it.
                value_error = value_error or ControlValueError(
                    name, "cannot be set: no installed plugin declares the field of "
                          + ", ".join(t.target for t in state.targets if t.active))
            continue            # else its targets do not combine: already an issue
        try:
            applied[name] = control_value(given, state)
        except ValueError as exc:
            value_error = value_error or ControlValueError(name, str(exc))
    by_exec = {step.id: step for step in steps}
    for name, value in applied.items():
        for target in states[name].targets:
            if target.active:
                by_exec[target.step].params[target.param] = value
    # 8. the execution workflow, validated (always, so every problem is listed)
    through: dict[str, list[str]] = {}
    for (consumer, _index), (old, new) in rewired.items():
        through.setdefault(consumer, []).append(f"{new} through bypassed {old.split('.', 1)[0]!r}")
    for issue in validate_plain(execution, registry):
        if issue.step in through:
            issue = Issue(issue.step, f"{issue.message} (it reads {', '.join(through[issue.step])})")
        issues.append(issue)
    final_values = {}
    for name, state in states.items():
        if not state.active:
            continue
        if name in applied:
            final_values[name] = applied[name]
        elif state.has_value and state.spec is not None:
            try:
                final_values[name] = check_typed(state.value, state.spec)
            except ValueError:
                pass            # reported with the control's issues (stage 5)
    prepared = PreparedRun(
        workflow=execution, recipe=recipe, bypassed=tuple(bypassed), values=final_values,
        inactive={name: state.inactive_reasons for name, state in states.items() if not state.active},
        rewired=rewired, replaced=frozenset(replaced), notes=notes, controls=states,
    )
    if strict:
        if issues:
            raise WorkflowError("workflow is invalid:\n  " + "\n  ".join(str(issue) for issue in issues))
        if value_error is not None:
            raise value_error
    return prepared, issues, value_error


def prepare_run(recipe: Workflow, *, registry=None, values: Mapping[str, Any] | None = None,
                bindings: Mapping[str, Any] | None = None, bound: Iterable[str] | None = None,
                subset: Iterable[str] | None = None) -> PreparedRun:
    """The plain workflow to run for ``recipe``, with this run's control
    ``values`` (raw :class:`ControlText` or typed) applied.

    ``bindings`` are ``run()``'s (the steps bound to in-memory results are
    worked out from them); ``bound`` names result-bound steps directly (a
    batch checking its run-wide values before any row); ``subset`` is the
    editor's *Run to here*. Raises :class:`WorkflowError` listing every
    problem with the recipe, or :class:`ControlValueError` for the first value
    that cannot be used, before anything runs.
    """
    prepared, _issues, _error = _prepare(recipe, registry, values, bindings, bound, subset, strict=True)
    return prepared


def check_recipe(recipe: Workflow, registry=None) -> list[Issue]:
    """Everything wrong with ``recipe`` that can be known before running,
    without raising: its controls and bypasses, then the plain workflow it
    prepares to with no values, bindings or subset. For a schema-1 workflow
    this is exactly :func:`~imswitch.improcess.workflows.steps.validate_plain`."""
    if not recipe.controls and not any(getattr(step, "bypass", False) for step in recipe.steps):
        return validate_plain(recipe, registry)
    _prepared, issues, _error = _prepare(recipe, registry, None, None, None, None, strict=False)
    return issues


def recipe_notes(recipe: Workflow, registry=None) -> list[str]:
    """What is worth saying about ``recipe`` that is not an error (a control
    with no effect while its steps are bypassed)."""
    if not recipe.controls:
        return []
    prepared, _issues, _error = _prepare(recipe, registry, None, None, None, None, strict=False)
    return list(prepared.notes)


def describe_controls(recipe: Workflow, registry=None, *, bound: Iterable[str] | None = None) -> list[dict]:
    """Each control as data, for the ``controls`` command and dialogs: name,
    label, help, its combined field, the file's value, and its targets with
    whether they run."""
    prepared, _issues, _error = _prepare(recipe, registry, None, None, bound, None, strict=False)
    described = []
    for control in recipe.controls:
        state = prepared.controls.get(control.name)
        entry: dict[str, Any] = {"name": control.name, "label": control.label, "help": control.help}
        if state is None:
            described.append(entry)
            continue
        spec = state.spec
        if spec is not None:
            entry.update({
                "label": spec.label, "help": spec.help, "type": spec.type, "nullable": spec.nullable,
                "min": spec.min, "max": spec.max, "step": spec.step, "decimals": spec.decimals,
                "unit": spec.suffix, "options": list(spec.options),
            })
            if spec.has_reset:
                entry["reset"] = spec.reset
        if control.name in prepared.values:
            entry["value"] = prepared.values[control.name]
        entry["active"] = state.active
        entry["targets"] = [{"target": t.target, "active": t.active, **({"reason": t.reason} if t.reason else {})}
                            for t in state.targets]
        described.append(entry)
    return described


def format_value(value) -> str:
    """A canonical value as text a manifest or ``--set`` would accept back
    (text that would read as something else -- empty, ``null``, already
    quoted -- is quoted)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and (value in ("", "null") or (len(value) >= 2 and value[0] == value[-1] == "'")
                                   or value != value.strip()):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


__all__ = [
    "BYPASSED",
    "CONTROL_FIELD_TYPES",
    "FEEDS_REPLACED",
    "OUTSIDE",
    "REPLACED",
    "CombinedField",
    "ControlState",
    "ControlText",
    "ControlValueError",
    "PreparedRun",
    "TargetState",
    "bypass_issues",
    "bypass_upstream",
    "check_recipe",
    "check_typed",
    "combine",
    "control_states",
    "control_value",
    "convert_text",
    "describe_controls",
    "format_value",
    "prepare_run",
    "recipe_notes",
    "resolve_ref",
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
