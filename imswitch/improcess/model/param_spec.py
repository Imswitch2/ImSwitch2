"""Field-level description of a plugin's parameters, for forms and catalogues.

``default_params()`` says which keys a plugin takes and what they start at;
it is the root of the headless contract (:mod:`plugin_contract`). It does
not say what a key *is*: whether ``'gaussian'`` is one of four choices or
free text, whether ``2.0`` is a radius in pixels bounded below by 0.1,
whether ``None`` means "unset". That knowledge lived only inside each
plugin's Qt widget. ``param_spec()`` on
:class:`~imswitch.improcess.processors.base.Processor` and
:class:`~imswitch.improcess.reconstructors.base.Reconstructor` puts it
where a form, a catalogue or a workflow editor can read it, without Qt.

A plugin that declares nothing still gets a spec: :func:`spec_from_defaults`
derives one field per key from the default value's type, which is enough
for a plain, correct form. A plugin that declares more (choices, bounds,
units, help) must agree with its defaults: :func:`spec_problems` says how it
does not, and the contract test pins every built-in.

Nothing here imports Qt or a plugin module, so the registry stays headless
and ``python -m imswitch.improcess.workflows list --json`` can print it.
"""

from __future__ import annotations

import numbers
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Iterable, Mapping

#: The field vocabulary. It is the config editor's ``FieldSpec.type`` set
#: without ``ref`` (a workflow names no devices) and ``bool_auto`` (a
#: parameter is always present, at its default), and it maps one-to-one onto
#: pyqtgraph ``Parameter`` types, which the reconstructor widgets use.
FIELD_TYPES = ("int", "float", "bool", "text", "code", "select", "multiselect", "path", "json")


@dataclass(frozen=True)
class ParamField:
    """One parameter of a plugin, as a form should show it.

    ``key`` and ``default`` are the ones ``default_params()`` declares; the
    rest is presentation and constraint. A ``select`` chooses one of
    ``options``; a ``multiselect`` any subset, as a list. ``min``/``max``
    bound a number (inclusive); ``step`` and ``decimals`` shape its spin box;
    ``suffix`` is its unit. ``nullable`` says ``None`` is a value in its own
    right ("unset", "from the recording"). ``group`` collects related fields
    under one heading; ``advanced`` collapses a field by default. A ``code``
    field is multi-line text (a script, an expression) that a form shows in
    a code editor rather than a one-line box; its value is a ``str`` like a
    ``text`` field's, kept exactly as typed.
    """

    key: str
    type: str
    default: Any
    label: str = ""
    help: str = ""
    options: tuple = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None
    decimals: int | None = None
    suffix: str = ""
    group: str = ""
    nullable: bool = False
    advanced: bool = False

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError(f"a field needs a non-empty key, got {self.key!r}")
        if self.type not in FIELD_TYPES:
            raise ValueError(
                f"{self.key!r}: unknown field type {self.type!r}; expected one of {FIELD_TYPES}"
            )
        object.__setattr__(self, "options", tuple(self.options or ()))
        for name in ("min", "max", "step"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, numbers.Real)):
                raise ValueError(f"{self.key!r}: {name} must be a number or None, got {value!r}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"{self.key!r}: min {self.min!r} is above max {self.max!r}")
        if self.step is not None and self.step <= 0:
            raise ValueError(f"{self.key!r}: step must be positive, got {self.step!r}")
        if self.decimals is not None and (
            isinstance(self.decimals, bool) or not isinstance(self.decimals, numbers.Integral)
            or self.decimals < 0
        ):
            raise ValueError(f"{self.key!r}: decimals must be a non-negative integer")
        if self.type in ("select", "multiselect") and not self.options:
            raise ValueError(f"{self.key!r}: a {self.type} field needs options")

    @property
    def title(self) -> str:
        """The label to show: the declared one, else the key."""
        return self.label or self.key

    @property
    def is_number(self) -> bool:
        return self.type in ("int", "float")

    def problem_with(self, value) -> str | None:
        """Why ``value`` is not a value of this field, or ``None`` when it is.

        A type-level check: an ``int`` field takes integers within its bounds,
        a ``select`` one of its options, and so on. ``None`` passes a
        ``nullable`` or ``json`` field only.
        """
        if value is None:
            if self.nullable or self.type == "json":
                return None
            return "must not be None"
        if self.type == "json":
            return None
        if self.type == "bool":
            return None if isinstance(value, bool) else "must be true or false"
        if self.type in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                return "must be a number" if self.type == "float" else "must be an integer"
            if self.type == "int" and not isinstance(value, numbers.Integral):
                return "must be an integer"
            if self.min is not None and value < self.min:
                return f"is below the minimum {self.min!r}"
            if self.max is not None and value > self.max:
                return f"is above the maximum {self.max!r}"
            return None
        if self.type in ("text", "code", "path"):
            return None if isinstance(value, str) else "must be text"
        if self.type == "select":
            return None if _in_options(value, self.options) else f"is not one of {list(self.options)!r}"
        if self.type == "multiselect":
            if not isinstance(value, (list, tuple)):
                return "must be a list of options"
            bad = [item for item in value if not _in_options(item, self.options)]
            return f"contains {bad!r}, not among {list(self.options)!r}" if bad else None
        return None  # pragma: no cover - every type is handled above

    def to_dict(self) -> dict:
        """JSON-shaped form: ``key``, ``type`` and ``default`` always, the rest
        only when set, so a catalogue line stays short."""
        payload: dict[str, Any] = {"key": self.key, "type": self.type, "default": self.default}
        for name in ("label", "help", "suffix", "group"):
            value = getattr(self, name)
            if value:
                payload[name] = value
        if self.options:
            payload["options"] = list(self.options)
        for name in ("min", "max", "step", "decimals"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        for name in ("nullable", "advanced"):
            if getattr(self, name):
                payload[name] = True
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ParamField":
        known = {f.name for f in dataclass_fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"unknown field attribute(s) {unknown}")
        data = dict(payload)
        if "options" in data:
            data["options"] = tuple(data["options"])
        return cls(**data)


def _in_options(value, options) -> bool:
    return any(_same(value, option) for option in options)


def _same(a, b) -> bool:
    """Equality that survives numpy values, nested lists and dicts."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        result = a == b
        if hasattr(result, "all"):  # a numpy array comparison
            return bool(result.all())
        return bool(result)
    except Exception:
        return False


# --------------------------------------------------------------------------
# deriving and checking a spec
# --------------------------------------------------------------------------

def field_from_default(key: str, value) -> ParamField:
    """The plainest field that holds ``value``: its type, and nothing more."""
    if isinstance(value, bool):
        return ParamField(key, "bool", value)
    if isinstance(value, numbers.Integral):
        return ParamField(key, "int", int(value))
    if isinstance(value, numbers.Real):
        return ParamField(key, "float", float(value))
    if isinstance(value, str):
        return ParamField(key, "text", value)
    if value is None:
        # A None default says "unset" without saying what a set value looks
        # like; only the plugin can say more, so the fallback is free-form.
        return ParamField(key, "json", None, nullable=True)
    return ParamField(key, "json", value)


def spec_from_defaults(defaults: Mapping[str, Any]) -> tuple[ParamField, ...]:
    """One field per key of ``defaults``, typed from each default value.

    Total: every JSON-encodable default maps to a field, and anything else
    (a tuple, an array) becomes a ``json`` field holding it as is.
    """
    return tuple(field_from_default(str(key), value) for key, value in dict(defaults).items())


def _class_of(plugin_or_cls) -> type:
    return plugin_or_cls if isinstance(plugin_or_cls, type) else type(plugin_or_cls)


def spec_for(plugin_or_cls) -> tuple[ParamField, ...]:
    """The plugin's declared spec, or one derived from its defaults.

    Never raises: a ``param_spec()`` that fails, or that is not a sequence of
    fields, is replaced by the derived spec, so a catalogue or a form is
    never empty because of one plugin's bug (the contract test reports it).
    """
    cls = _class_of(plugin_or_cls)
    try:
        declared = tuple(cls.param_spec())
        if all(isinstance(item, ParamField) for item in declared):
            return declared
    except Exception:
        pass
    try:
        return spec_from_defaults(cls.default_params())
    except Exception:
        return ()


def fields_by_key(spec: Iterable[ParamField]) -> dict[str, ParamField]:
    return {field.key: field for field in spec}


def spec_problems(plugin_or_cls) -> list[str]:
    """Every way ``param_spec()`` disagrees with ``default_params()``.

    The same keys, the same defaults, and each default a value of its own
    field: a ``select`` default among its options, a bounded number within
    its bounds, ``None`` only where the field is nullable. Empty when the
    spec holds.
    """
    cls = _class_of(plugin_or_cls)
    try:
        defaults = dict(cls.default_params())
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return [f"default_params() failed: {exc}"]
    try:
        spec = tuple(cls.param_spec())
    except Exception as exc:  # noqa: BLE001
        return [f"param_spec() failed: {exc}"]
    problems: list[str] = []
    wrong_type = [item for item in spec if not isinstance(item, ParamField)]
    if wrong_type:
        return [f"param_spec() must return ParamField objects, got {type(wrong_type[0]).__name__}"]
    keys = [field.key for field in spec]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        problems.append(f"duplicate field(s): {duplicates}")
    only_spec = sorted(set(keys) - set(defaults))
    only_defaults = sorted(set(defaults) - set(keys))
    if only_spec:
        problems.append(f"fields not in default_params(): {only_spec}")
    if only_defaults:
        problems.append(f"default_params() keys without a field: {only_defaults}")
    for field in spec:
        if field.key not in defaults:
            continue
        declared = defaults[field.key]
        if not _same(field.default, declared):
            problems.append(f"{field.key!r}: field default {field.default!r} != declared {declared!r}")
        problem = field.problem_with(declared)
        if problem:
            problems.append(f"{field.key!r}: default {declared!r} {problem}")
    return problems


__all__ = [
    "FIELD_TYPES",
    "ParamField",
    "field_from_default",
    "fields_by_key",
    "spec_for",
    "spec_from_defaults",
    "spec_problems",
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
