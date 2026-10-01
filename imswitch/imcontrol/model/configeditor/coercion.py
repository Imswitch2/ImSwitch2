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


def _same_kind(value, original):
    """Whether ``value`` is the same JSON kind as ``original`` (bool is not int)."""
    if isinstance(original, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(original, bool)
    return type(value) is type(original)


def _parse_like(text: str, original):
    """Parse ``text`` as the same JSON kind as ``original``, or return None.

    ``None`` here means "could not", never a parsed null: callers that want a
    null check for the literal ``"null"`` before calling.
    """
    if isinstance(original, bool):
        lowered = text.lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return None
    if isinstance(original, int):
        try:
            return int(text)
        except ValueError:
            return None
    if isinstance(original, float):
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(original, list):
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if not original:
            return parts
        sample = original[0]
        converted = []
        for part in parts:
            item = _parse_like(part, sample)
            if item is None and not isinstance(sample, str):
                return None
            converted.append(part if item is None else item)
        return converted
    return None


def text_to_json(text: str, original, field_type: str = "text"):
    """Read a ``text`` field back without changing the kind of value it held.

    The editor renders every value it has no better widget for as a line of
    text, and for years read the line back as a string: a setup file that
    said ``"analogChannel": 3`` came back saying ``"3"``, which
    ``SetupInfo.getAnalogChannel()`` then returned verbatim instead of
    building ``Dev1/ao3``. The rule is now: text that still reads as the
    value it was given *is* that value; text that was edited becomes the same
    kind of value if it can, and a string only if it cannot. Typed fields
    (``int``/``float``) keep :func:`display_to_json`'s behaviour.
    """
    stripped = text.strip()
    if stripped.lower() == "null":
        return None
    if field_type in ("int", "float"):
        return display_to_json(stripped, field_type)
    if original is None or isinstance(original, str):
        return stripped
    if isinstance(original, list):
        unchanged = stripped == ", ".join(str(item) for item in original)
    else:
        unchanged = stripped == str(original)
    if unchanged:
        return original
    parsed = _parse_like(stripped, original)
    return stripped if parsed is None else parsed


def raw_text_to_json(text: str, original):
    """Read a raw (untemplated) property back without retyping it.

    Raw properties display bare -- ``5`` for the number and ``5`` for the
    string -- so the text alone cannot say which it was. Reading everything
    through ``json.loads`` turned a serial number ``"0042"`` into ``42`` and a
    port ``"5"`` into ``5``. A property that was a string stays a string; any
    other kind is parsed as JSON, and text that is not valid JSON is kept as
    a string rather than lost.
    """
    import json

    stripped = text.strip()
    if isinstance(original, str):
        return stripped
    if stripped.lower() == "null":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return stripped


def options_like(options, value):
    """Return ``options`` retyped to match ``value`` where that is lossless.

    Templates list select options as strings, but a baud rate or a display
    rotation is a number in every shipped setup. Choosing ``"9600"`` from a
    list of strings wrote a string over the int the driver expects. When the
    saved value is numeric and every option parses as that kind, the options
    become that kind too, so a choice keeps the file's type.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return list(options)
    retyped = []
    for option in options:
        if isinstance(option, (int, float)) and not isinstance(option, bool):
            retyped.append(option)
            continue
        parsed = _parse_like(str(option), value)
        if parsed is None:
            return list(options)
        retyped.append(parsed)
    return retyped

