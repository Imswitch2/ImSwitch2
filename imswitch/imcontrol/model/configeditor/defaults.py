"""Default device builders for config editor.

This module provides Qt-free functions for building default device configurations
and merging edited devices while preserving unknown fields (including nested dicts).
"""

import copy
from typing import Optional

from .coercion import display_to_json


def build_default_device(
    manager_name: str,
    *,
    template: Optional[dict],
    json_schema: Optional[dict],
    kind_schema: Optional[dict] = None,
) -> dict:
    """Return a new device dict with default values from template and/or schema.

    Seeding rules (the editor's invariant applies to new devices too: a form
    may show a default, it does not save one unasked):

    - every template field is seeded with the template's default, coerced by
      the template's own type, or by the resolved kind when the template
      states none (the template drift test strips types that merely repeat
      the schema);
    - a schema-only property is seeded when the schema gives a ``default``,
      or when it is **required** (a kind-appropriate empty value);
    - an optional schema-only property, nullable or not, is left out;
    - alias spellings are never seeded: the canonical spelling is;
    - a top-level key the dataclass requires (``kind_schema``) and the
      template does not seed is carried as the empty value of its kind --
      ``None`` for a number or a string, so the form's required warning
      makes the operator fill it -- and one with a dataclass default is left
      out, since omitting it *means* that default.

    Args:
        manager_name: The manager class name
        template: The builtin_templates JSON for this manager (or None)
        json_schema: Resolved managerProperties JSON Schema (or None)
    """
    from .kinds import NOT_FORM_FIELDS
    from .schemas import _default_for_type, _infer_type_from_schema, empty_for_kind, normalized_fields

    d: dict = {"managerName": manager_name, "managerProperties": {}}
    resolved = {
        (field.location, field.nested_key, field.key): field.type
        for field in normalized_fields(template=template, json_schema=json_schema, kind_schema=kind_schema)
    }

    def field_type(f: dict, location: str, nested_key: Optional[str] = None) -> str:
        return f.get("type") or resolved.get((location, nested_key, f["key"])) or "text"

    if template:
        for f in template.get("top", []):
            v = f.get("default", "")
            tp = field_type(f, "top")
            if tp == "bool" and isinstance(v, bool):
                d[f["key"]] = v
            elif v == "null":
                d[f["key"]] = None
            else:
                d[f["key"]] = _default_value(v, tp)
        for f in template.get("props", []):
            d["managerProperties"][f["key"]] = _default_value(f.get("default", ""), field_type(f, "prop"))
        for nest_key, nest_fields in template.get("nested", {}).items():
            sub = {}
            for f in nest_fields:
                sub[f["key"]] = _default_value(f.get("default", ""), field_type(f, "nested", nest_key))
            d["managerProperties"][nest_key] = sub

    if kind_schema:
        required = set(kind_schema.get("required") or [])
        for key, prop in (kind_schema.get("properties") or {}).items():
            if key in NOT_FORM_FIELDS or key in d or not isinstance(prop, dict):
                continue
            if key in required:
                d[key] = empty_for_kind(prop)

    if json_schema:
        required = set(json_schema.get("required") or [])
        for prop_key, prop_schema in (json_schema.get("properties") or {}).items():
            if not isinstance(prop_schema, dict) or prop_key in d["managerProperties"]:
                continue
            if "x-imswitch-alias-of" in prop_schema:
                continue
            if "default" in prop_schema:
                d["managerProperties"][prop_key] = copy.deepcopy(prop_schema["default"])
            elif prop_key in required:
                d["managerProperties"][prop_key] = _default_for_type(_infer_type_from_schema(prop_schema))

    return d


def _default_value(default, field_type: str):
    """A template default as the JSON value a new device should carry.

    A template may state a default as the value itself (``9600``) or as the
    text of it (``"9600"``); only the latter needs the field type to say what
    kind it is. Running the former through ``str()`` and back turned a numeric
    select default into a string on every new device.
    """
    if default == "":
        return default
    if isinstance(default, str):
        return display_to_json(default, field_type)
    return default


def merge_preserving_unknown(
    original: dict,
    edited: dict,
    *,
    schema_top_keys: set[str],
    schema_prop_keys: set[str],
    schema_nested_prop_keys: Optional[dict[str, set[str]]] = None,
) -> dict:
    """Merge edited device dict back with original, preserving unknown fields.
    
    This ensures that unknown top-level keys and unknown managerProperties
    (INCLUDING nested dicts) survive a load → edit → apply cycle.
    
    Args:
        original: The original device dict before editing
        edited: The edited device dict from the form
        schema_top_keys: Set of known top-level keys (from template + schema)
        schema_prop_keys: Set of known managerProperties keys (from template + schema)
        schema_nested_prop_keys: Known keys within each template-defined nested
            managerProperties dict.  Unknown keys in a known nested dict are
            retained, allowing newer plugin settings to survive an older UI.
    
    Returns:
        The edited dict with unknown fields from original restored.
    """
    result = dict(edited)
    
    # Always preserve these core keys as known
    core_top_keys = {"managerName", "managerProperties"}
    all_known_top = core_top_keys | schema_top_keys
    
    # Restore unknown top-level keys
    for key, value in original.items():
        if key not in all_known_top and key not in result:
            result[key] = value
    
    # Restore unknown managerProperties (including nested dicts)
    if "managerProperties" in original:
        if "managerProperties" not in result:
            result["managerProperties"] = {}
        
        original_props = original["managerProperties"]
        result_props = result["managerProperties"]
        
        for key, value in original_props.items():
            if key not in schema_prop_keys and key not in result_props:
                # This is an unknown property - restore it verbatim
                # This includes nested dicts, which is the bug being fixed
                result_props[key] = value

        # A template can know a nested *container* while not yet knowing a
        # setting a newer plugin version has added to it.  Preserve those
        # members just like entirely unknown properties.
        for nest_key, known_keys in (schema_nested_prop_keys or {}).items():
            original_nested = original_props.get(nest_key)
            edited_nested = result_props.get(nest_key)
            if not isinstance(original_nested, dict) or not isinstance(edited_nested, dict):
                continue
            for key, value in original_nested.items():
                if key not in known_keys and key not in edited_nested:
                    edited_nested[key] = value
    
    return result
