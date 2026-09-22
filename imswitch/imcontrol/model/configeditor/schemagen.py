"""Turn extracted manager contracts into checked-in JSON Schemas.

:mod:`extraction` says what a manager reads; this module says it in JSON
Schema, deterministically, so the result can live in package data and a test
can regenerate it and diff. Three kinds of file come out:

* ``schemas/managers/<Manager>.json`` -- one Draft 2020-12 schema per manager;
* ``schemas/fixtures/<Manager>.json`` -- a synthetic device satisfying it, so
  every manager exercises the editor's form path without a hand-written setup;
* ``schemas/index.json`` -- generator version, per-manager source hashes and
  the coverage totals, so a diff says *why* a schema moved.

``schemas/overrides/<Manager>.json`` is the one directory here that is written
by people and never by this module: it is merged last, wins, and is where a
validation type narrower than the code proves comes from (the plan's
"evidence is not a constraint" rule). :func:`write` refuses to touch it.

Everything is sorted and rendered the same way every time; a regeneration that
changes nothing changes no bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import extraction as ex

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
GENERATOR_VERSION = 1

MANAGERS_DIR = "managers"
FIXTURES_DIR = "fixtures"
OVERRIDES_DIR = "overrides"
INDEX_FILE = "index.json"

REGENERATE_COMMAND = "python tools/extract_manager_schemas.py --write"

#: Keys an override file may carry at its top level. Anything else is a typo
#: and is refused, because a silently ignored override is worse than none.
_OVERRIDE_TOP_KEYS = {"description", "properties", "required", "optional"}

#: A value of each kind that a fixture can carry.
_EXAMPLES = {
    ex.KIND_INTEGER: 1,
    ex.KIND_NUMBER: 1.5,
    ex.KIND_BOOLEAN: True,
    ex.KIND_STRING: "example",
    ex.KIND_ARRAY: [],
    ex.KIND_OBJECT: {},
}


def schemas_root() -> Path:
    """Where the generated files live inside the package."""
    return Path(__file__).resolve().parent / "schemas"


def render(document: dict) -> str:
    """The one serialisation every generated file uses."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# =============================================================================
# One manager -> one schema
# =============================================================================

def _requiredness_tags(spec: ex.PropertySpec) -> list[str]:
    if spec.required == ex.REQUIRED:
        return ["code:required"]
    if spec.required == ex.UNCERTAIN:
        return ["code:uncertain"]
    accesses = {read.access for read in spec.reads}
    if "get" in accesses:
        return ["code:optional(get)"]
    if "in" in accesses:
        return ["code:optional(in)"]
    guards = sorted({read.guard for read in spec.reads if read.guarded and read.guard})
    if guards:
        return [f"code:optional({guard})" for guard in guards]
    return ["code:optional"]


def _property_schema(spec: ex.PropertySpec) -> dict:
    """A property's schema: constraints where proven, preferences as ``x-imswitch-*``."""
    prop: dict = {}
    if spec.constraint:
        type_value = spec.constraint["type"]
        if spec.nullable:
            types = [type_value] if isinstance(type_value, str) else list(type_value)
            if "null" not in types:
                types.append("null")
            type_value = types
        prop["type"] = type_value
    if spec.kind is not None:
        prop["x-imswitch-kind"] = spec.kind
    if spec.nullable:
        prop["x-imswitch-nullable"] = True
    if spec.widget:
        prop["x-imswitch-widget"] = spec.widget
    if spec.ref_category:
        prop["x-imswitch-ref-category"] = spec.ref_category
    if spec.aliases:
        prop["x-imswitch-aliases"] = list(spec.aliases)
    if spec.required == ex.UNCERTAIN:
        prop["x-imswitch-required"] = ex.UNCERTAIN
    if spec.description:
        prop["description"] = spec.description
    sources = list(spec.kind_source) + _requiredness_tags(spec)
    if spec.constraint_source:
        sources.append(spec.constraint_source)
    prop["x-imswitch-source"] = sorted(set(sources))
    if spec.sub_properties:
        # ``defaults = props.get("defaults", {})`` then ``defaults.get("x", 1)``:
        # the manager reads named keys inside this dict, so they are listed
        # with their own kinds. The dict stays open: unknown sub-keys pass.
        prop["properties"] = {
            key: _property_schema(sub) for key, sub in sorted(spec.sub_properties.items())
        }
        prop["additionalProperties"] = True
        required = sorted(key for key, sub in spec.sub_properties.items() if sub.required == ex.REQUIRED)
        if required:
            prop["required"] = required
    return prop


def _alias_copy(canonical: str, prop: dict) -> dict:
    """The same constraints under another spelling, marked as such."""
    copy_ = {key: value for key, value in prop.items() if key != "x-imswitch-aliases"}
    copy_["x-imswitch-alias-of"] = canonical
    return copy_


def build_schema(manager: ex.ManagerExtraction, override: Optional[dict] = None) -> dict:
    """The manager's ``managerProperties`` schema, override applied last.

    Alias spellings are copied from the canonical property *after* the
    override has been merged, so a constraint written once for ``seed`` holds
    for ``old_seed`` too; an override that names an alias spelling itself is
    then merged on top of that copy, so the two can still be told apart on
    purpose.
    """
    properties: dict[str, dict] = {}
    required: list[str] = []
    for key in sorted(manager.properties):
        spec = manager.properties[key]
        properties[key] = _property_schema(spec)
        if spec.required == ex.REQUIRED:
            required.append(key)

    schema: dict = {
        "$schema": SCHEMA_DIALECT,
        "title": f"{manager.name} managerProperties",
        "type": "object",
        "additionalProperties": True,
        "properties": properties,
        "x-imswitch-classes": list(manager.classes),
    }
    if required:
        schema["required"] = sorted(required)
    if manager.open_passthrough:
        schema["x-imswitch-open-passthrough"] = True
        schema["description"] = (
            "This manager hands its whole managerProperties dict to a driver; "
            "the driver's own keys are not listed here."
        )
    if manager.unresolved:
        # The contract is incomplete and says so: a reader that cannot be
        # named is a property this schema does not know about.
        schema["x-imswitch-unresolved-reads"] = sorted(
            f"{item.expr} (line {item.lineno}; {item.reason})" for item in manager.unresolved
        )
    if manager.writes:
        schema["x-imswitch-writes-ignored"] = sorted(
            f"{item.key} ({item.kind}, line {item.lineno})" for item in manager.writes
        )
    if override:
        schema = apply_override(schema, override)
    _copy_aliases(schema, (override or {}).get("properties") or {})
    return _finalize_required(schema)


def _copy_aliases(schema: dict, override_props: dict) -> None:
    """Materialise ``x-imswitch-aliases`` as alias-spelled copies of the final property."""
    properties = schema["properties"]
    for key in sorted(properties):
        prop = properties[key]
        if "x-imswitch-alias-of" in prop:
            continue
        for alias in prop.get("x-imswitch-aliases") or []:
            copy_ = _alias_copy(key, prop)
            patch = override_props.get(alias)
            if isinstance(patch, dict):
                _deep_merge(copy_, patch)
                _mark_overridden(copy_, patch)
            properties[alias] = copy_
    schema["properties"] = dict(sorted(properties.items()))


def _finalize_required(schema: dict) -> dict:
    """Let a required property with aliases be satisfied by any spelling.

    Extraction alone never produces one -- the nested ``.get`` that reveals
    an alias also makes the key optional -- so this is reached through an
    override that requires an aliased key. ``required`` keeps the plain keys;
    each aliased one becomes an ``anyOf`` over its spellings.
    """
    required = list(schema.get("required", []))
    plain, alternatives = [], []
    for key in required:
        aliases = schema["properties"].get(key, {}).get("x-imswitch-aliases") or []
        if aliases:
            alternatives.append({"anyOf": [{"required": [key]}] + [{"required": [a]} for a in aliases]})
        else:
            plain.append(key)
    if plain:
        schema["required"] = sorted(plain)
    else:
        schema.pop("required", None)
    if len(alternatives) == 1:
        schema["anyOf"] = alternatives[0]["anyOf"]
    elif alternatives:
        schema["allOf"] = alternatives
    return schema


# =============================================================================
# Overrides
# =============================================================================

def apply_override(schema: dict, override: dict) -> dict:
    """Merge a hand-written override into a generated schema; the override wins.

    ``properties`` merges recursively: objects merge key by key (so a patch of
    ``defaults.properties.gain.minimum`` keeps ``gain``'s other keys and its
    sibling sub-properties), anything else -- lists included, so ``enum``,
    ``type`` and a nested ``required`` -- replaces. A new key creates the
    property. ``required`` adds, ``optional`` removes; ``description``
    replaces. Every property and sub-property the override touches gets
    ``override`` in its provenance.
    """
    unknown = set(override) - _OVERRIDE_TOP_KEYS
    if unknown:
        raise ValueError(f"override has unknown top-level keys: {sorted(unknown)}")
    result = copy.deepcopy(schema)
    for key, patch in (override.get("properties") or {}).items():
        if not isinstance(patch, dict):
            raise ValueError(f"override for property {key!r} must be an object")
        prop = result["properties"].setdefault(key, {})
        _deep_merge(prop, patch)
        _mark_overridden(prop, patch)
    required = set(result.get("required", []))
    required |= set(override.get("required") or [])
    required -= set(override.get("optional") or [])
    if required:
        result["required"] = sorted(required)
    else:
        result.pop("required", None)
    if "description" in override:
        result["description"] = override["description"]
    return result


def _deep_merge(base: dict, patch: dict) -> None:
    """Merge ``patch`` into ``base`` in place: dicts recurse, anything else replaces."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _mark_overridden(prop: dict, patch: dict) -> None:
    """``override`` in the provenance of this property and of every sub-property the patch names."""
    prop["x-imswitch-source"] = sorted(set(prop.get("x-imswitch-source", [])) | {"override"})
    sub_patches = patch.get("properties")
    if isinstance(sub_patches, dict):
        for key, sub_patch in sub_patches.items():
            sub = prop.get("properties", {}).get(key)
            if isinstance(sub, dict) and isinstance(sub_patch, dict):
                _mark_overridden(sub, sub_patch)


def load_overrides(root: Path) -> dict[str, dict]:
    """``manager -> override`` from ``<root>/overrides/*.json``."""
    overrides: dict[str, dict] = {}
    directory = Path(root) / OVERRIDES_DIR
    if not directory.is_dir():
        return overrides
    for path in sorted(directory.glob("*.json")):
        overrides[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return overrides


# =============================================================================
# Fixtures
# =============================================================================

class FixtureError(ValueError):
    """No example value satisfies a property's schema; the override must supply one."""


def _example_for(prop: dict, path: str = "") -> object:
    """A value the property's *final* schema accepts.

    ``const``, then the first of ``examples`` (the hand-authored value an
    override gives when nothing below can satisfy its constraints), then
    ``default``, then the first ``enum`` entry; a nested object is built from
    its sub-properties; a number or string is shaped to its bounds. What
    cannot be satisfied raises :class:`FixtureError` naming the property.
    """
    if "const" in prop:
        return copy.deepcopy(prop["const"])
    if prop.get("examples"):
        return copy.deepcopy(prop["examples"][0])
    if "default" in prop:
        return copy.deepcopy(prop["default"])
    if "enum" in prop and prop["enum"]:
        return prop["enum"][0]
    if prop.get("properties"):
        return {key: _example_for(sub, f"{path}.{key}" if path else key)
                for key, sub in prop["properties"].items()}
    type_value = prop.get("type")
    types = None if type_value is None else ([type_value] if isinstance(type_value, str) else list(type_value))
    kind = prop.get("x-imswitch-kind")
    if isinstance(kind, list):
        kind = kind[0]
    # The kind is the editor's preference; use it whenever the validation
    # type allows it (a float()-proven ["integer", "number", "string"] should
    # carry 1.5 for a number, not the list's first entry).
    if kind is not None and (types is None or kind in types):
        return _shaped_example(kind, prop, path)
    if types is not None:
        for candidate in types:
            if candidate != "null":
                return _shaped_example(candidate, prop, path)
        return None
    return None if prop.get("x-imswitch-nullable") else "example"


_BOUND_KEYS = ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum", "multipleOf")


def _shaped_example(kind: str, prop: dict, path: str) -> object:
    """The kind's example, moved inside whatever bounds the property carries."""
    if kind in (ex.KIND_INTEGER, ex.KIND_NUMBER):
        return _numeric_example(kind == ex.KIND_INTEGER, prop, path)
    if kind == ex.KIND_STRING:
        if "pattern" in prop or "format" in prop:
            raise FixtureError(
                f"{path or 'property'}: a string with a pattern or format needs a hand-authored "
                f"value; add \"examples\": [\"...\"] to its override")
        text = _EXAMPLES[ex.KIND_STRING]
        min_length, max_length = prop.get("minLength"), prop.get("maxLength")
        if min_length is not None and len(text) < min_length:
            text = (text * (min_length // len(text) + 1))[:min_length]
        if max_length is not None and len(text) > max_length:
            text = text[:max_length]
        return text
    if kind == ex.KIND_ARRAY:
        count = prop.get("minItems") or 0
        if count and "items" not in prop:
            raise FixtureError(
                f"{path or 'property'}: an array with minItems needs an items schema or a hand-authored "
                f"value; add \"examples\": [[...]] to its override")
        return [_example_for(prop["items"], f"{path}[]") for _ in range(count)]
    return copy.deepcopy(_EXAMPLES[kind])


def _numeric_example(integer: bool, prop: dict, path: str) -> object:
    low, exclusive_low = prop.get("minimum"), prop.get("exclusiveMinimum")
    high, exclusive_high = prop.get("maximum"), prop.get("exclusiveMaximum")
    step = prop.get("multipleOf")

    def accepted(value) -> bool:
        if integer and float(value) != int(value):
            return False
        if low is not None and value < low:
            return False
        if exclusive_low is not None and value <= exclusive_low:
            return False
        if high is not None and value > high:
            return False
        if exclusive_high is not None and value >= exclusive_high:
            return False
        if step and abs(value / step - round(value / step)) > 1e-9:
            return False
        return True

    candidates: list = [_EXAMPLES[ex.KIND_INTEGER] if integer else _EXAMPLES[ex.KIND_NUMBER]]
    lower = low if low is not None else (exclusive_low + 1 if exclusive_low is not None else None)
    upper = high if high is not None else (exclusive_high - 1 if exclusive_high is not None else None)
    if lower is not None:
        candidates.append(lower)
        if step:
            candidates.append(step * math.ceil(lower / step))
    if exclusive_low is not None and not integer:
        candidates.append(exclusive_low + 0.5)
    if upper is not None:
        candidates.append(upper)
        if step:
            candidates.append(step * math.floor(upper / step))
    if exclusive_high is not None and not integer:
        candidates.append(exclusive_high - 0.5)
    if step:
        candidates.append(step)
    if lower is not None and upper is not None:
        candidates.append((lower + upper) / 2)
    for value in candidates:
        if accepted(value):
            return int(value) if integer else float(value)
    constraints = {key: prop[key] for key in _BOUND_KEYS if key in prop}
    raise FixtureError(
        f"{path or 'property'}: no example satisfies {constraints}; add \"examples\": [<value>] "
        f"to its override")


def build_fixture(manager: ex.ManagerExtraction, category: str, schema: dict) -> dict:
    """A device carrying every required key and every optional key once.

    Alias spellings are left out: a fixture that carried both spellings would
    be the alias-conflict case, which has its own hand-written fixture.
    """
    props = {
        key: _example_for(prop, key)
        for key, prop in schema["properties"].items()
        if "x-imswitch-alias-of" not in prop
    }
    _validate_fixture(manager.name, schema, props)
    return {
        "category": category,
        "device": {"managerName": manager.name, "managerProperties": props},
    }


def _validate_fixture(name: str, schema: dict, props: dict) -> None:
    """Refuse to generate a fixture its own schema rejects.

    The drift test validates every checked-in fixture anyway; failing here,
    at ``--write`` time, names the property and the fix instead of leaving a
    file behind that CI will reject. Needs ``jsonschema`` (the ``test``
    extra); without it the drift test is the check.
    """
    try:
        import jsonschema
    except ImportError:
        return
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(props))
    if errors:
        where = ", ".join(
            f"{'.'.join(str(p) for p in error.path) or '(root)'}: {error.message}" for error in errors
        )
        raise FixtureError(
            f"{name}: the generated fixture does not satisfy the schema ({where}); "
            f"give the property a hand-authored value with \"examples\": [<value>] in its override")


# =============================================================================
# Index and the whole set
# =============================================================================

def source_hash(manager: ex.ManagerExtraction, classes: dict[str, ex.ClassExtraction]) -> str:
    """SHA-256 over the source files of the manager's classes, in resolution order."""
    digest = hashlib.sha256()
    for cls_name in manager.classes:
        module = classes[cls_name].module
        try:
            digest.update(Path(module).read_bytes())
        except OSError:
            digest.update(module.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_index(
    extractions: dict[str, ex.ManagerExtraction],
    classes: dict[str, ex.ClassExtraction],
    *,
    categories: dict[str, str],
    overrides: dict[str, dict],
    report: ex.CoverageReport,
    unresolved: Iterable[str] = (),
) -> dict:
    managers = {}
    for name, manager in sorted(extractions.items()):
        managers[name] = {
            "category": categories.get(name),
            "classes": list(manager.classes),
            "properties": len(manager.properties),
            "required": sum(1 for p in manager.properties.values() if p.required == ex.REQUIRED),
            "overridden": name in overrides,
            "source_sha256": source_hash(manager, classes),
        }
    return {
        "generator_version": GENERATOR_VERSION,
        "dialect": SCHEMA_DIALECT,
        "regenerate": REGENERATE_COMMAND,
        "coverage": report.snapshot()["totals"],
        "managers": managers,
        "unresolved": sorted(unresolved),
    }


@dataclass
class GenerationInputs:
    extractions: dict[str, ex.ManagerExtraction]
    classes: dict[str, ex.ClassExtraction]
    categories: dict[str, str]
    overrides: dict[str, dict]
    report: ex.CoverageReport
    unresolved: tuple[str, ...] = ()


def generate_all(inputs: GenerationInputs) -> dict[str, str]:
    """Every generated file, as ``relative path -> text``. Pure."""
    files: dict[str, str] = {}
    for name, manager in sorted(inputs.extractions.items()):
        schema = build_schema(manager, inputs.overrides.get(name))
        files[f"{MANAGERS_DIR}/{name}.json"] = render(schema)
        category = inputs.categories.get(name, "others")
        files[f"{FIXTURES_DIR}/{name}.json"] = render(build_fixture(manager, category, schema))
    files[INDEX_FILE] = render(build_index(
        inputs.extractions, inputs.classes,
        categories=inputs.categories, overrides=inputs.overrides,
        report=inputs.report, unresolved=inputs.unresolved,
    ))
    return files


def _generated_on_disk(root: Path) -> dict[str, str]:
    """Files under the generated directories as they are on disk."""
    found: dict[str, str] = {}
    for directory in (MANAGERS_DIR, FIXTURES_DIR):
        for path in sorted((Path(root) / directory).glob("*.json")):
            found[f"{directory}/{path.name}"] = path.read_text(encoding="utf-8")
    index = Path(root) / INDEX_FILE
    if index.is_file():
        found[INDEX_FILE] = index.read_text(encoding="utf-8")
    return found


def check(files: dict[str, str], root: Path) -> list[str]:
    """Differences between the generated set and disk; empty means in sync."""
    on_disk = _generated_on_disk(root)
    problems: list[str] = []
    for path, text in sorted(files.items()):
        if path not in on_disk:
            problems.append(f"missing: {path}")
        elif on_disk[path] != text:
            problems.append(f"changed: {path}")
    for path in sorted(set(on_disk) - set(files)):
        problems.append(f"stale: {path}")
    return problems


def write(files: dict[str, str], root: Path) -> list[str]:
    """Write the generated set, remove stale generated files, never touch overrides."""
    root = Path(root)
    for path in files:
        if path.split("/", 1)[0] == OVERRIDES_DIR:
            raise ValueError(f"refusing to generate into {OVERRIDES_DIR}/: {path}")
    written: list[str] = []
    for path, text in sorted(files.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            written.append(path)
    for path in sorted(set(_generated_on_disk(root)) - set(files)):
        (root / path).unlink()
        written.append(f"removed {path}")
    return written


# Copyright (C) 2020-2021 ImSwitch developers
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
