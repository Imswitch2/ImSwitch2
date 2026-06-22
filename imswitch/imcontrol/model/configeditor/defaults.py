"""Default device builders for config editor.

This module provides Qt-free functions for building default device configurations
and merging edited devices while preserving unknown fields (including nested dicts).
"""

from typing import Optional
from .coercion import display_to_json


def build_default_device(
    manager_name: str,
    *,
    template: Optional[dict],
    json_schema: Optional[dict],
) -> dict:
    """Return a new device dict with default values from template and/or schema.
    
    Args:
        manager_name: The manager class name
        template: The builtin_templates JSON for this manager (or None)
        json_schema: Resolved managerProperties JSON Schema (or None)
    
    Returns:
        A device dict with managerName, managerProperties, and default field values.
    """
    d: dict = {"managerName": manager_name, "managerProperties": {}}
    
    # Start with template-driven defaults (must reproduce existing behavior exactly)
    if template:
        # Process top-level fields
        for f in template.get("top", []):
            v = f["default"]
            if f["type"] == "bool" and isinstance(v, bool):
                d[f["key"]] = v
            elif v == "null":
                d[f["key"]] = None
            else:
                d[f["key"]] = display_to_json(str(v), f["type"]) if v != "" else v
        
        # Process props (managerProperties) fields
        for f in template.get("props", []):
            v = f["default"]
            d["managerProperties"][f["key"]] = (
                display_to_json(str(v), f["type"]) if v != "" else v
            )
        
        # Process nested fields
        for nest_key, nest_fields in template.get("nested", {}).items():
            sub = {}
            for f in nest_fields:
                v = f["default"]
                sub[f["key"]] = display_to_json(str(v), f["type"]) if v != "" else v
            d["managerProperties"][nest_key] = sub
    
    # Add schema-only properties if no template or template doesn't cover them
    if json_schema:
        schema_props = json_schema.get("properties", {})
        
        for prop_key, prop_schema in schema_props.items():
            # Only add if not already present from template
            if prop_key not in d["managerProperties"]:
                if "default" in prop_schema:
                    d["managerProperties"][prop_key] = prop_schema["default"]
                else:
                    # Use type-appropriate empty value
                    schema_type = prop_schema.get("type")
                    if schema_type == "integer":
                        d["managerProperties"][prop_key] = 0
                    elif schema_type == "number":
                        d["managerProperties"][prop_key] = 0.0
                    elif schema_type == "boolean":
                        d["managerProperties"][prop_key] = False
                    elif schema_type == "string":
                        d["managerProperties"][prop_key] = ""
                    elif schema_type == "array":
                        d["managerProperties"][prop_key] = []
                    elif schema_type == "object":
                        d["managerProperties"][prop_key] = {}
                    else:
                        d["managerProperties"][prop_key] = None
    
    return d


def merge_preserving_unknown(
    original: dict,
    edited: dict,
    *,
    schema_top_keys: set[str],
    schema_prop_keys: set[str],
) -> dict:
    """Merge edited device dict back with original, preserving unknown fields.
    
    This ensures that unknown top-level keys and unknown managerProperties
    (INCLUDING nested dicts) survive a load → edit → apply cycle.
    
    Args:
        original: The original device dict before editing
        edited: The edited device dict from the form
        schema_top_keys: Set of known top-level keys (from template + schema)
        schema_prop_keys: Set of known managerProperties keys (from template + schema)
    
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
    
    return result
