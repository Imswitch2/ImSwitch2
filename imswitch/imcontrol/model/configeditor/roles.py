"""Role rules: properties read by a consumer other than the device's manager.

``GalvoScanDesigner`` reads ``vel_max``/``acc_max`` from every scanning
positioner whose name does not contain ``mock``; ``SLMsController`` reads
``startConfig`` from any SLM. Neither manager knows these keys, so no
manager schema lists them, and whether they are *required* depends on the
consumer and its own exemptions. A role rule (``schemas/roles/<role>.json``,
hand-written) states the consumer's condition as a small predicate and what
it requires and reads; this module evaluates the predicate. It is called
once, from the shared setup validator, so the CLI and the editor's
validation panel report the same thing, and the editor shows a role's
properties as optional fields -- it never requires or seeds one.

The predicate language is deliberately tiny::

    {"all": [{"setup": "scan.scanDesigner", "equals": "GalvoScanDesigner"},
             {"field": "forScanning", "equals": true},
             {"name": {"not_contains": "mock"}}]}

``field`` looks at the device entry, ``name`` at its key (case-insensitive),
``setup`` at a dotted path into the whole document; ``all``/``any``/``not``
combine. An unknown condition is an error, never silently true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class MissingRead:
    key: str
    fallback: Optional[str]  # what the consumer assumes when the key is absent


@dataclass(frozen=True)
class RoleFinding:
    role: str
    section: str
    name: str
    missing_required: tuple  # keys the consumer requires and the device lacks
    missing_reads: tuple = field(default_factory=tuple)  # MissingRead for absent optional reads


def holds(condition: dict, *, device: dict, name: str, setup: dict) -> bool:
    """Whether ``condition`` is true for this device entry."""
    if not condition:
        return True
    if "all" in condition:
        return all(holds(c, device=device, name=name, setup=setup) for c in condition["all"])
    if "any" in condition:
        return any(holds(c, device=device, name=name, setup=setup) for c in condition["any"])
    if "not" in condition:
        return not holds(condition["not"], device=device, name=name, setup=setup)
    if "field" in condition:
        return _compare(device.get(condition["field"]) if isinstance(device, dict) else None, condition)
    if "name" in condition:
        spec = condition["name"]
        lowered = name.lower()
        if "contains" in spec:
            return spec["contains"].lower() in lowered
        if "not_contains" in spec:
            return spec["not_contains"].lower() not in lowered
        if "equals" in spec:
            return name == spec["equals"]
        raise ValueError(f"unknown name test: {sorted(spec)}")
    if "setup" in condition:
        return _compare(_dotted(setup, condition["setup"]), condition)
    raise ValueError(f"unknown role condition: {sorted(condition)}")


def _compare(value, condition: dict) -> bool:
    if "equals" in condition:
        return value == condition["equals"]
    if "in" in condition:
        return value in condition["in"]
    if "exists" in condition:
        return (value is not None) == bool(condition["exists"])
    if "starts_with" in condition:
        return isinstance(value, str) and value.startswith(condition["starts_with"])
    raise ValueError(f"condition has no comparison: {sorted(condition)}")


def sections_of(role: dict) -> tuple:
    """The setup sections a role applies to (``applies_to`` is one name or a list)."""
    applies = role.get("applies_to") or ()
    return (applies,) if isinstance(applies, str) else tuple(applies)


def _dotted(document, path: str):
    current = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def evaluate(role: dict, data: dict) -> list[RoleFinding]:
    """The devices ``role`` applies to, with what each one lacks."""
    findings: list[RoleFinding] = []
    for section in sections_of(role):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for name, device in entries.items():
            if not isinstance(device, dict) or not holds(role.get("when") or {}, device=device, name=name, setup=data):
                continue
            props = device.get("managerProperties")
            props = props if isinstance(props, dict) else {}
            missing_required = tuple(key for key in role.get("requires") or [] if key not in props)
            missing_reads = tuple(
                MissingRead(read["key"], read.get("fallback"))
                for read in _reads(role) if read["key"] not in props
            )
            findings.append(RoleFinding(role["role"], section, name, missing_required, missing_reads))
    return findings


def _reads(role: dict) -> list[dict]:
    return [read if isinstance(read, dict) else {"key": read} for read in role.get("reads") or []]


def evaluate_all(roles: Iterable[dict], data: dict) -> list[RoleFinding]:
    findings: list[RoleFinding] = []
    for role in roles:
        findings.extend(evaluate(role, data))
    return findings


def applicable(roles: Iterable[dict], *, section: str, name: str, device: dict, setup: dict) -> list[dict]:
    """The roles whose predicate holds for one device (what the editor shows as optional fields)."""
    return [
        role for role in roles
        if section in sections_of(role)
        and holds(role.get("when") or {}, device=device, name=name, setup=setup or {})
    ]


def message_for(role: dict, finding: RoleFinding) -> str:
    """The consumer's own message for a device that lacks what the role requires."""
    template = role.get("message") or (
        "{consumer} requires {required} in managerProperties for '{names}'."
    )
    return template.format(
        names=[finding.name], name=finding.name, consumer=role.get("consumer", role["role"]),
        required=", ".join(f"'{key}'" for key in finding.missing_required),
    )
