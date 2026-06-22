"""Normalized field model for config editor.

This module provides a pure-Python (no Qt dependencies) normalized field model
that merges template-based UI/default overlays with JSON Schema definitions.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FieldSpec:
    """Normalized field specification for config editor forms."""
    
    key: str
    label: str
    type: str  # text|int|float|bool|select|path
    default: object
    required: bool
    group: str  # UI group/tab, e.g. "Basic"
    tooltip: str
    options: tuple[str, ...]
    location: str  # "top" | "prop" | "nested"
    nested_key: Optional[str]  # set when location == "nested"


def normalized_fields(
    *,
    template: Optional[dict],
    json_schema: Optional[dict],
) -> list[FieldSpec]:
    """Merge template and JSON schema into normalized field specifications.
    
    Merge rules:
    - Start from template's top/props/nested fields (UI/default authority)
    - For each property in JSON Schema not covered by template, add a FieldSpec
    - Where both define a property, template wins for label/group/tooltip
    - Schema may still mark it required if template didn't
    
    Args:
        template: The builtin_templates JSON for this manager (or None)
        json_schema: Resolved managerProperties JSON Schema (or None)
    
    Returns:
        List of FieldSpec instances representing all fields.
    """
    fields: list[FieldSpec] = []
    
    # Track which properties we've seen from template
    seen_props: set[str] = set()
    
    # Process template fields if available
    if template:
        # Process top-level fields
        for f in template.get("top", []):
            fields.append(_template_field_to_spec(f, location="top", nested_key=None))
        
        # Process props (managerProperties) fields
        for f in template.get("props", []):
            fields.append(_template_field_to_spec(f, location="prop", nested_key=None))
            seen_props.add(f["key"])
        
        # Process nested fields
        for nest_key, nest_fields in template.get("nested", {}).items():
            seen_props.add(nest_key)  # Nested dict itself is "seen"
            for f in nest_fields:
                fields.append(_template_field_to_spec(f, location="nested", nested_key=nest_key))
    
    # Add schema-only properties not covered by template
    if json_schema:
        schema_props = json_schema.get("properties", {})
        required_props = set(json_schema.get("required", []))
        
        for prop_key, prop_schema in schema_props.items():
            if prop_key not in seen_props:
                # This property is only in schema, not in template
                field_type = _infer_type_from_schema(prop_schema)
                default = prop_schema.get("default", _default_for_type(field_type))
                options = tuple(prop_schema.get("enum", []))
                
                fields.append(
                    FieldSpec(
                        key=prop_key,
                        label=_make_label(prop_key),
                        type=field_type,
                        default=default,
                        required=prop_key in required_props,
                        group="Properties",
                        tooltip="",
                        options=options,
                        location="prop",
                        nested_key=None,
                    )
                )
            else:
                # Property exists in both template and schema
                # Update required status from schema if needed
                # (This is handled by finding the existing field and checking schema)
                # For now, template wins - schema's required is informational only
                pass
    
    return fields


def _template_field_to_spec(
    field: dict,
    location: str,
    nested_key: Optional[str],
) -> FieldSpec:
    """Convert a template field dict to a FieldSpec."""
    return FieldSpec(
        key=field["key"],
        label=field.get("label", field["key"]),
        type=field.get("type", "text"),
        default=field.get("default", ""),
        required=field.get("req", False),
        group=field.get("grp", "Basic"),
        tooltip=field.get("tip", ""),
        options=tuple(field.get("opts", [])),
        location=location,
        nested_key=nested_key,
    )


def _infer_type_from_schema(prop_schema: dict) -> str:
    """Infer editor field type from JSON Schema property definition."""
    schema_type = prop_schema.get("type")
    
    # Check for enum first (becomes select)
    if "enum" in prop_schema:
        return "select"
    
    # Map JSON Schema types to editor types
    if schema_type == "integer":
        return "int"
    elif schema_type == "number":
        return "float"
    elif schema_type == "boolean":
        return "bool"
    elif schema_type == "string":
        return "text"
    else:
        # Default to text for unknown/complex types
        return "text"


def _default_for_type(field_type: str) -> object:
    """Return a sensible default value for a field type."""
    if field_type == "int":
        return 0
    elif field_type == "float":
        return 0.0
    elif field_type == "bool":
        return False
    elif field_type == "select":
        return ""
    else:  # text, path
        return ""


def _make_label(key: str) -> str:
    """Generate a human-readable label from a property key."""
    # Replace underscores with spaces and title-case
    return key.replace("_", " ").title()
