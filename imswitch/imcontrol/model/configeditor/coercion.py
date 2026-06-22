"""Type coercion helpers for config editor.

This module provides Qt-free functions for converting between display strings
and JSON-appropriate Python types. These functions match the exact behavior of
the original editor implementation to ensure byte-for-byte compatibility.
"""


def json_to_display(value, field_type: str) -> str:
    """Convert a JSON value to a display string for a text/select widget.
    
    Args:
        value: The JSON value (can be None, int, float, str, bool, etc.)
        field_type: The field type hint ("int", "float", "bool", "text", etc.)
    
    Returns:
        A string representation suitable for display in UI widgets.
    """
    if value is None:
        return "null"
    if field_type in ("int", "float"):
        return str(value)
    return str(value)


def display_to_json(text: str, field_type: str):
    """Convert a display string back to the correct Python type.
    
    This reproduces the EXACT logic of the editor's original implementation:
    - "null" (case-insensitive) → None
    - int field: try int(), fall back to text on failure
    - float field: try float(), fall back to text on failure  
    - all other types (including bool): return text as-is
    
    Note: This means a bool field with "False" becomes the string "False"
    (not the boolean False). This is the current behavior preserved for
    byte-for-byte compatibility.
    
    Args:
        text: The display string from UI
        field_type: The field type hint ("int", "float", "bool", "text", etc.)
    
    Returns:
        The Python value appropriate for JSON serialization.
    """
    text = text.strip()
    if text.lower() == "null":
        return None
    if field_type == "int":
        try:
            return int(text)
        except ValueError:
            return text
    if field_type == "float":
        try:
            return float(text)
        except ValueError:
            return text
    return text
