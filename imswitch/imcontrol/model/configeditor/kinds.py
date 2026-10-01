"""Top-level device fields, read from the ``SetupInfo`` dataclasses.

A device entry in a setup file (``detectors.cam``) has two halves: the
``managerProperties`` the manager reads -- extracted from the manager's source
by :mod:`extraction` -- and the top-level keys (``forAcquisition``,
``axes``, ``wavelength``, …) that ImSwitch's own ``SetupInfo`` dataclasses
declare. The dataclass *is* the contract for that half: the field's
annotation is its type, a field without a default is required, the default
is what an omitted key means, and the attribute docstring is its
description. This module reads those declarations with :mod:`ast` -- the
docstrings are not visible at runtime -- and a test checks the reading
against ``dataclasses.fields()`` so it cannot drift.

The output is one ``kinds/<kind>.json`` per device kind, produced by
:mod:`schemagen` alongside the manager schemas.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Optional

#: Which ``SetupInfo`` dataclass describes a device entry of each kind
#: (``setup_metadata.KIND_METADATA`` keys). Rotators are plain
#: ``DeviceInfo``; pulse generators use a bespoke loader and have no
#: per-device dataclass.
KIND_INFO_CLASSES: dict[str, str] = {
    "detector": "DetectorInfo",
    "laser": "LaserInfo",
    "positioner": "PositionerInfo",
    "rotator": "DeviceInfo",
    "rs232": "RS232Info",
    "slm": "SLMsInfo",
    "flip_mirror": "FlipMirrorInfo",
    "stand": "MicroscopeStandInfo",
}

#: Top-level keys the editor handles outside the form.
NOT_FORM_FIELDS = ("managerName", "managerProperties")

_NAME_TYPES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "Dict": "object",
    "list": "array",
    "List": "array",
}
_ANY_NAMES = ("Any", "object")
_SKIP_ANNOTATIONS = ("CatchAll",)


@dataclass(frozen=True)
class InfoField:
    name: str
    types: tuple  # JSON types the annotation admits, ``null`` aside; empty = anything
    nullable: bool
    required: bool  # no default in the dataclass
    has_default: bool
    default: object = None  # meaningful only when ``default_is_json``
    default_is_json: bool = False
    items_types: tuple = ()  # for an array: the element types
    description: str = ""
    annotation: str = ""
    owner: str = ""  # the class that declares the field


@dataclass
class InfoClass:
    name: str
    bases: tuple
    fields: list = dc_field(default_factory=list)  # own fields, in order
    lineno: int = 0


def extract_info_classes(source: str) -> dict[str, InfoClass]:
    """Every dataclass in ``source`` with its own fields (bases not yet folded in)."""
    tree = ast.parse(source)
    classes: dict[str, InfoClass] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
            continue
        info = InfoClass(node.name, tuple(_base_name(b) for b in node.bases if _base_name(b)), lineno=node.lineno)
        body = node.body
        for index, statement in enumerate(body):
            if not (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)):
                continue
            name = statement.target.id
            if name.startswith("_") or _annotation_name(statement.annotation) in _SKIP_ANNOTATIONS:
                continue
            types, nullable, items = _annotation_types(statement.annotation)
            has_default = statement.value is not None
            default, default_is_json = _default_of(statement.value) if has_default else (None, False)
            description = ""
            following = body[index + 1] if index + 1 < len(body) else None
            if isinstance(following, ast.Expr) and isinstance(following.value, ast.Constant) \
                    and isinstance(following.value.value, str):
                description = _collapse(following.value.value)
            info.fields.append(InfoField(
                name=name, types=tuple(types), nullable=nullable, required=not has_default,
                has_default=has_default, default=default, default_is_json=default_is_json,
                items_types=tuple(items), description=description,
                annotation=ast.unparse(statement.annotation), owner=node.name,
            ))
        classes[node.name] = info
    return classes


def resolve_fields(name: str, classes: dict[str, InfoClass]) -> tuple[list[InfoField], list[str]]:
    """The fields of ``name`` with its bases folded in (base first), and the class chain."""
    if name not in classes:
        raise KeyError(f"no dataclass named {name!r} in SetupInfo")
    chain: list[str] = []
    fields: dict[str, InfoField] = {}

    def visit(cls_name: str) -> None:
        cls = classes.get(cls_name)
        if cls is None:
            return
        for base in cls.bases:
            visit(base)
        for f in cls.fields:
            fields[f.name] = f  # a subclass redeclaring a field replaces it in place
        if cls_name not in chain:
            chain.append(cls_name)

    visit(name)
    return list(fields.values()), list(reversed(chain))


def load_info_classes(path: Path) -> dict[str, InfoClass]:
    return extract_info_classes(Path(path).read_text(encoding="utf-8"))


# ── annotations ───────────────────────────────────────────────────────────

def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _annotation_name(target) == "dataclass":
            return True
    return False


def _base_name(node: ast.AST) -> Optional[str]:
    return node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else None)


def _annotation_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Subscript):
        node = node.value
    return _base_name(node)


def _annotation_types(node: ast.AST) -> tuple[list, bool, list]:
    """``(json types, nullable, item types)`` for an annotation.

    ``Optional[X]`` and ``Union[..., None]`` make the field nullable; ``Any``
    and ``object`` admit anything (no ``type`` is emitted); ``List[X]`` is an
    array whose items are ``X``.
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return [], True, []
    name = _annotation_name(node)
    if isinstance(node, ast.Subscript):
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if name == "Optional":
            types, _nullable, items = _annotation_types(args[0])
            return types, True, items
        if name == "Union":
            types: list = []
            items: list = []
            nullable = False
            for arg in args:
                sub_types, sub_nullable, sub_items = _annotation_types(arg)
                nullable = nullable or sub_nullable
                for t in sub_types:
                    if t not in types:
                        types.append(t)
                items.extend(t for t in sub_items if t not in items)
            return types, nullable, items
        if name in ("List", "list"):
            item_types, _n, _i = _annotation_types(args[0])
            return ["array"], False, item_types
        if name in ("Dict", "dict"):
            return ["object"], False, []
    if name in _NAME_TYPES:
        return [_NAME_TYPES[name]], False, []
    if name in _ANY_NAMES:
        return [], False, []
    return [], False, []  # an unknown annotation constrains nothing


def _default_of(node: ast.AST) -> tuple[object, bool]:
    """The JSON value a default evaluates to, and whether it is one."""
    if isinstance(node, ast.Constant):
        return node.value, True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value, True
    if isinstance(node, ast.Call) and _base_name(node.func) == "field":
        for keyword in node.keywords:
            if keyword.arg == "default":
                return _default_of(keyword.value)
            if keyword.arg == "default_factory":
                factory = keyword.value
                if _base_name(factory) == "dict":
                    return {}, True
                if _base_name(factory) == "list":
                    return [], True
                if isinstance(factory, ast.Lambda):
                    return _default_of(factory.body)
        return None, False
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple)):
        try:
            return ast.literal_eval(node), True
        except ValueError:
            return None, False
    return None, False


def _collapse(text: str) -> str:
    """A docstring as one paragraph: the first paragraph only, whitespace folded."""
    first = text.strip().split("\n\n", 1)[0]
    return re.sub(r"\s+", " ", first).strip()


# ── the schema ────────────────────────────────────────────────────────────

def field_schema(f: InfoField) -> dict:
    prop: dict = {}
    types = list(f.types)
    if types:
        if f.nullable and "null" not in types:
            prop["type"] = types + ["null"]
        else:
            prop["type"] = types[0] if len(types) == 1 else types
        kinds = [t for t in types if t != "null"]
        if kinds:
            prop["x-imswitch-kind"] = kinds[0] if len(kinds) == 1 else kinds
    elif f.nullable:
        prop["type"] = ["null"] if not types else prop.get("type")
        prop.pop("type", None)
    if f.items_types:
        prop["items"] = {"type": f.items_types[0] if len(f.items_types) == 1 else list(f.items_types)}
        if list(f.items_types) == ["string"]:
            # A list of names is edited as comma-separated text, which the
            # text widget already reads back as a list.
            prop["x-imswitch-widget"] = "text"
    if f.nullable:
        prop["x-imswitch-nullable"] = True
    if f.has_default and f.default_is_json:
        prop["default"] = f.default
    if f.description:
        prop["description"] = f.description
    prop["x-imswitch-source"] = [f"dataclass:{f.owner}.{f.name}: {f.annotation}"]
    return prop


def build_kind_schema(kind: str, classes: dict[str, InfoClass], dialect: str) -> dict:
    """The schema for a device entry of ``kind``: its top-level keys, from the dataclass."""
    class_name = KIND_INFO_CLASSES[kind]
    fields, chain = resolve_fields(class_name, classes)
    properties = {f.name: field_schema(f) for f in fields}
    required = sorted(f.name for f in fields if f.required)
    schema: dict = {
        "$schema": dialect,
        "title": f"{kind} device entry ({class_name})",
        "description": (
            f"The top-level keys of a {kind} entry in a setup file, as imswitch.imcontrol.model."
            f"SetupInfo.{class_name} declares them. managerProperties is described per manager "
            f"under managers/. Keys the dataclass does not declare are kept but not read."
        ),
        "type": "object",
        "additionalProperties": True,
        "properties": dict(sorted(properties.items())),
        "x-imswitch-info-class": class_name,
        "x-imswitch-classes": chain,
    }
    if required:
        schema["required"] = required
    return schema
