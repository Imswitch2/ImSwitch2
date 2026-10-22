"""Generic helpers for editing structured top-level setup objects.

The normal config-editor field model is intentionally flat: device keys and
managerProperties.  Some integrations own richer nested JSON objects outside
ImSwitch (currently slmcore's ``definition`` and ``startup_preferences``).
This module adapts ordinary JSON Schema objects to the same field widgets
without making the Qt editor understand any particular integration.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .schemas import _blank_for_type, _infer_type_from_schema, _make_label, _options_from_schema


@dataclass(frozen=True)
class StructuredFieldSpec:
    """One editable leaf in a nested object schema."""

    path: tuple[str, ...]
    section_titles: tuple[str, ...]
    key: str
    label: str
    type: str
    default: object
    required: bool
    tooltip: str
    options: tuple

    def as_field_dict(self) -> dict[str, Any]:
        """Return the long-standing field-dict shape consumed by FieldWidget."""
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": copy.deepcopy(self.default),
            "req": self.required,
            "grp": "",
            "tip": self.tooltip,
            "opts": list(self.options),
        }


def _fixed_object_schema(prop: Mapping[str, Any]) -> bool:
    """Whether ``prop`` is a fixed-shape object worth expanding recursively."""
    stated = prop.get("type")
    is_object = stated == "object" or (
        isinstance(stated, list) and "object" in stated and "null" not in stated
    )
    return bool(is_object and isinstance(prop.get("properties"), Mapping) and prop["properties"])


def flatten_object_schema(schema: Mapping[str, Any]) -> tuple[StructuredFieldSpec, ...]:
    """Flatten a fixed JSON object schema into editable leaves.

    Fixed-shape nested objects are expanded. Dynamic maps (``additionalProperties``),
    arrays, unions, and otherwise complex values stay atomic and therefore use the
    editor's JSON fallback. Unknown properties are deliberately *not* represented;
    callers round-trip them from the original object unchanged.
    """
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
        raise ValueError("structured editor requires an object schema with properties")

    fields: list[StructuredFieldSpec] = []

    def walk(
        object_schema: Mapping[str, Any],
        *,
        prefix: tuple[str, ...],
        section_titles: tuple[str, ...],
    ) -> None:
        required = set(object_schema.get("required") or ())
        for key, raw_prop in (object_schema.get("properties") or {}).items():
            if not isinstance(raw_prop, Mapping):
                continue
            prop = dict(raw_prop)
            if _fixed_object_schema(prop):
                title = str(prop.get("title") or _make_label(key))
                walk(
                    prop,
                    prefix=prefix + (key,),
                    section_titles=section_titles + (title,),
                )
                continue

            field_type = _infer_type_from_schema(prop)
            default = copy.deepcopy(prop["default"]) if "default" in prop else _blank_for_type(field_type)
            fields.append(StructuredFieldSpec(
                path=prefix + (key,),
                section_titles=section_titles,
                key=key,
                label=str(prop.get("title") or _make_label(key)),
                type=field_type,
                default=default,
                required=key in required,
                tooltip=str(prop.get("description") or ""),
                options=_options_from_schema(prop, field_type),
            ))

    walk(schema, prefix=(), section_titles=())
    return tuple(fields)


def path_exists(data: object, path: Sequence[str]) -> bool:
    value = data
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return False
        value = value[key]
    return True


def path_value(data: object, path: Sequence[str], default: object = None) -> object:
    value = data
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return default
        value = value[key]
    return value


def set_path(data: dict[str, Any], path: Sequence[str], value: object) -> None:
    if not path:
        raise ValueError("path cannot be empty")
    cursor = data
    for key in path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[path[-1]] = value
