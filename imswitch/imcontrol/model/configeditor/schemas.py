"""Normalized field model for the config editor.

Pure Python, no Qt. A device form is the merge of two sources:

* the manager's ``managerProperties`` JSON Schema -- generated from the
  manager's source (``schemas/managers/``) or shipped by a plugin -- which is
  the **type authority**: its ``x-imswitch-kind`` decides the widget, its
  ``required`` list what must be written, its ``x-imswitch-aliases`` which
  spellings are one property;
* the builtin template, which is a **presentation overlay**: label, group,
  tooltip, option lists, and a widget refinement (``select`` over a string,
  ``ref`` over a device name, ``path`` over a file name).

Where the two disagree on a type, the schema wins unless the template's type
is a compatible refinement, or the schema's kind rests on nothing stronger
than an example value. A template field with no ``type`` at all takes the
schema's kind (the template drift test strips types that merely repeat it).
"""

import copy
import re
from dataclasses import dataclass, replace
from typing import Optional

from .kinds import NOT_FORM_FIELDS

#: Editor widget type for each schema kind.
_KIND_TO_TYPE = {
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "string": "text",
    "array": "json",
    "object": "json",
}

#: Template types that say the same thing as a schema kind.
_EQUIVALENT_KINDS = {
    "text": ("string",),
    "int": ("integer",),
    "float": ("number",),
    "bool": ("boolean",),
    "json": ("array", "object"),
}

#: Template types that refine the widget for these kinds without changing
#: what is stored.
_REFINEMENTS = {
    "select": ("string", "integer", "number", "boolean"),
    "multiselect": ("array",),
    "ref": ("string",),
    "path": ("string",),
    "text": ("array",),  # a list of names as comma-separated text; read back as a list
}

_SCALAR_KINDS = ("integer", "number", "string")
_NUMERIC_TYPES = ("int", "float")
_REQUIREDNESS_TAGS = ("code:required", "code:optional", "code:uncertain")


@dataclass(frozen=True)
class FieldSpec:
    """Normalized field specification for config editor forms."""

    key: str
    label: str
    type: str  # text|int|float|bool|select|multiselect|ref|path|json
    default: object
    required: bool
    group: str  # UI group/tab, e.g. "Basic"
    tooltip: str
    options: tuple  # enum values, or the device categories for a ``ref``
    location: str  # "top" | "prop" | "nested"
    nested_key: Optional[str]  # set when location == "nested"
    aliases: tuple = ()  # other spellings the manager reads for this key
    nullable: bool = False  # the manager treats None as "unset"
    widget: str = ""  # ``x-imswitch-widget`` as the schema states it
    ref_category: str = ""  # the category a ``ref`` names devices from
    required_by_schema: bool = False  # proven by the code, not a template hint


def normalized_fields(
    *,
    template: Optional[dict],
    json_schema: Optional[dict],
    kind_schema: Optional[dict] = None,
) -> list[FieldSpec]:
    """Merge template, kind schema and JSON schema into normalized field specifications.

    - The template's ``top``/``props``/``nested`` fields come first, with the
      template's presentation.
    - A top-level key the kind schema (the ``SetupInfo`` dataclass, see
      :mod:`kinds`) describes is refined the same way; one the template does
      not lay out is appended to the ``Device`` group. Its requiredness is a
      label hint, never ``required_by_schema``: ``infer_missing`` loads a
      missing required field as ``None``, so the file was accepted without
      it and Apply must not invent it.
    - A property the manager schema also describes is *refined*: the
      resolved type, options from ``enum`` when the template lists none,
      aliases, nullability, and requiredness (the schema can strengthen,
      never weaken).
    - A property only the schema describes is appended to the ``Properties``
      group. Alias spellings (``x-imswitch-alias-of``) are not fields: the
      canonical property carries them.
    """
    fields: list[FieldSpec] = []
    seen_props: set[str] = set()

    if template:
        for f in template.get("top", []):
            fields.append(_template_field_to_spec(f, location="top", nested_key=None))
        for f in template.get("props", []):
            fields.append(_template_field_to_spec(f, location="prop", nested_key=None))
            seen_props.add(f["key"])
        for nest_key, nest_fields in template.get("nested", {}).items():
            seen_props.add(nest_key)  # the container itself is laid out by the template
            for f in nest_fields:
                fields.append(_template_field_to_spec(f, location="nested", nested_key=nest_key))

    if kind_schema:
        kind_required = set(kind_schema.get("required") or [])
        index_of = {(field.location, field.key): i for i, field in enumerate(fields)}
        for key, prop in (kind_schema.get("properties") or {}).items():
            if key in NOT_FORM_FIELDS or not isinstance(prop, dict):
                continue
            index = index_of.get(("top", key))
            if index is not None:
                fields[index] = _refine(fields[index], prop, required=key in kind_required, proven=False)
            else:
                fields.append(_kind_field_to_spec(key, prop, required=key in kind_required))

    if json_schema:
        schema_props = json_schema.get("properties") or {}
        required_props = set(json_schema.get("required") or [])
        index_of = {(field.location, field.key): i for i, field in enumerate(fields)}
        for prop_key, prop_schema in schema_props.items():
            if not isinstance(prop_schema, dict) or "x-imswitch-alias-of" in prop_schema:
                continue
            required = prop_key in required_props
            if prop_key in seen_props:
                index = index_of.get(("prop", prop_key))
                if index is not None:
                    fields[index] = _refine(fields[index], prop_schema, required=required)
                continue
            fields.append(_schema_field_to_spec(prop_key, prop_schema, required=required))

    # A template field with no type that no schema typed either is text.
    return [replace(field, type="text") if not field.type else field for field in fields]


def _template_field_to_spec(
    field: dict,
    location: str,
    nested_key: Optional[str],
) -> FieldSpec:
    """Convert a template field dict to a FieldSpec."""
    return FieldSpec(
        key=field["key"],
        label=field.get("label", field["key"]),
        type=field.get("type") or "",  # "" until the schema says; "text" if nothing does
        default=field.get("default", ""),
        required=field.get("req", False),
        group=field.get("grp", "Basic"),
        tooltip=field.get("tip", ""),
        options=tuple(field.get("opts", []) or []),
        location=location,
        nested_key=nested_key,
    )


def _schema_field_to_spec(key: str, prop: dict, *, required: bool) -> FieldSpec:
    """A field the template does not know: typed and labelled from the schema alone."""
    field_type = _infer_type_from_schema(prop)
    if "default" in prop:
        default = copy.deepcopy(prop["default"])
    elif required:
        default = _default_for_type(field_type)
    else:
        # Shown blank; never written unless the operator edits it. A form
        # may show a default, it does not save one unasked.
        default = _blank_for_type(field_type)
    return FieldSpec(
        key=key,
        label=_make_label(key),
        type=field_type,
        default=default,
        required=required,
        group="Properties",
        tooltip=prop.get("description", "") or "",
        options=_options_from_schema(prop, field_type),
        location="prop",
        nested_key=None,
        aliases=tuple(prop.get("x-imswitch-aliases") or ()),
        nullable=bool(prop.get("x-imswitch-nullable")),
        widget=prop.get("x-imswitch-widget") or "",
        ref_category=prop.get("x-imswitch-ref-category") or "",
        required_by_schema=required,
    )


def _refine(field: FieldSpec, prop: dict, *, required: bool, proven: bool = True) -> FieldSpec:
    """A template field and the schema's description of the same property.

    ``proven`` says whether ``required`` may drive Apply (a manager reads the
    key unguarded) or is only a label hint (a dataclass field without a
    default, which still loads as ``None``).
    """
    resolved = resolve_type(field.type or None, prop)
    options = field.options
    if not options:
        options = _options_from_schema(prop, resolved)
    return replace(
        field,
        type=resolved,
        options=options,
        required=field.required or required,
        aliases=tuple(prop.get("x-imswitch-aliases") or ()),
        nullable=bool(prop.get("x-imswitch-nullable")),
        widget=prop.get("x-imswitch-widget") or "",
        ref_category=prop.get("x-imswitch-ref-category") or "",
        required_by_schema=required and proven,
    )


def _kind_field_to_spec(key: str, prop: dict, *, required: bool) -> FieldSpec:
    """A top-level key the template does not lay out, typed from the dataclass.

    The form shows the dataclass default for an absent key -- that is what
    an omitted key means -- and a required key without one as blank.
    """
    field_type = _infer_type_from_schema(prop)
    if "default" in prop:
        default = copy.deepcopy(prop["default"])
    else:
        default = _blank_for_type(field_type)
    return FieldSpec(
        key=key,
        label=_make_label(key),
        type=field_type,
        default=default,
        required=required,
        group="Device",
        tooltip=prop.get("description", "") or "",
        options=_options_from_schema(prop, field_type),
        location="top",
        nested_key=None,
        nullable=bool(prop.get("x-imswitch-nullable")),
        widget=prop.get("x-imswitch-widget") or "",
        required_by_schema=False,
    )


def role_field(key: str, prop: dict, *, group: str) -> dict:
    """A template-shaped optional field for a property a consumer reads (``schemas/roles/``)."""
    field_type = _infer_type_from_schema(prop)
    out = {
        "key": key,
        "label": _make_label(key),
        "type": field_type,
        "default": _blank_for_type(field_type),
        "req": False,
        "grp": group,
        "tip": prop.get("description", "") or "",
        "opts": list(_options_from_schema(prop, field_type)),
    }
    if prop.get("x-imswitch-nullable"):
        out["nullable"] = True
    return out


def empty_for_kind(prop: dict) -> object:
    """The empty value of a property's first kind: what a new device carries for a required key.

    Numbers and strings are ``None`` -- a blank box the operator must fill,
    flagged by the form's required warning -- because the editor does not
    invent a wavelength; a list or a dict is empty, which the warning flags
    too.
    """
    kinds = _kinds_of(prop) or []
    if not kinds:
        return None
    return {"array": [], "object": {}}.get(kinds[0])


def _options_from_schema(prop: dict, field_type: str) -> tuple:
    if field_type == "ref":
        category = prop.get("x-imswitch-ref-category")
        return (category,) if category else ()
    return tuple(prop.get("enum") or ())


def _kinds_of(prop: dict) -> Optional[list]:
    """The JSON kinds a property may hold, ``null`` aside; None when nothing says."""
    stated = prop.get("x-imswitch-kind")
    if stated is None:
        stated = prop.get("type")
    if stated is None:
        return None
    kinds = [stated] if isinstance(stated, str) else list(stated)
    return [kind for kind in kinds if kind != "null"]


def _schema_states_a_type(prop: dict) -> bool:
    return any(key in prop for key in ("enum", "x-imswitch-kind", "type", "x-imswitch-widget"))


def _kind_is_from_code(prop: dict) -> bool:
    """Whether the kind rests on the manager's code, not only on an example or the docs."""
    return any(
        source.startswith("code:") and not source.startswith(_REQUIREDNESS_TAGS)
        for source in prop.get("x-imswitch-source") or []
    )


def _infer_type_from_schema(prop_schema: dict) -> str:
    """The editor widget type a JSON Schema property asks for.

    ``enum`` is a select; ``x-imswitch-widget`` names the ``ref`` combo or
    the path picker. Otherwise the kind (``x-imswitch-kind``, the editor's
    preference) decides, falling back to the validation ``type``: one kind is
    that kind's widget; a union of numeric kinds is the number box; a union
    within integer/number/string is the text box, which reads a value back as
    the kind it was; anything else, or nothing known at all, is the JSON
    widget, which round-trips every value.
    """
    if "enum" in prop_schema:
        return "select"
    widget = prop_schema.get("x-imswitch-widget")
    if widget == "ref":
        return "ref"
    if widget == "path":
        return "path"
    if widget == "bool_auto":
        # A boolean whose absence means something (the consumer's fallback):
        # Automatic / On / Off, with Automatic leaving the key out.
        return "bool_auto"
    if widget == "text":
        # A list of names edited as comma-separated text; the text widget
        # reads it back as a list when the value was one.
        return "text"
    kinds = _kinds_of(prop_schema)
    if not kinds:
        return "json"
    if len(kinds) == 1:
        return _KIND_TO_TYPE.get(kinds[0], "json")
    if set(kinds) <= {"integer", "number"}:
        return "float"
    if set(kinds) <= set(_SCALAR_KINDS):
        return "text"
    return "json"


def resolve_type(template_type: Optional[str], prop: dict) -> str:
    """The widget type for a property both a template and a schema describe.

    The schema decides; the template's type survives only as a compatible
    refinement (``select``/``multiselect``/``ref``/``path`` over the kinds
    they can present, or the same kind spelled the template's way), or when
    the schema states no type at all, or when both are numeric and the
    schema's kind rests on nothing stronger than an example value.
    """
    schema_type = _infer_type_from_schema(prop)
    if not template_type:
        return schema_type if _schema_states_a_type(prop) else "text"
    if not _schema_states_a_type(prop):
        return template_type
    if "enum" in prop and template_type in ("select", "multiselect"):
        return template_type
    if schema_type == "ref":
        return template_type if template_type in ("select", "ref") else "ref"
    if schema_type == "path":
        return template_type if template_type in ("select", "path") else "path"
    kinds = set(_kinds_of(prop) or ())
    if template_type in _REFINEMENTS and kinds and kinds <= set(_REFINEMENTS[template_type]):
        return template_type
    if template_type in _EQUIVALENT_KINDS and kinds and kinds <= set(_EQUIVALENT_KINDS[template_type]):
        return template_type
    if template_type in _NUMERIC_TYPES and schema_type in _NUMERIC_TYPES and not _kind_is_from_code(prop):
        return template_type
    return schema_type


def _default_for_type(field_type: str) -> object:
    """The seed value for a required field the schema gives no default for."""
    if field_type == "int":
        return 0
    elif field_type == "float":
        return 0.0
    elif field_type == "bool":
        return False
    elif field_type == "multiselect":
        return []
    elif field_type == "json":
        return {}
    else:  # text, path, select, ref
        return ""


def _blank_for_type(field_type: str) -> object:
    """What an optional, absent field shows: nothing, in that widget's terms."""
    if field_type in ("int", "float", "bool", "json"):
        return None
    if field_type == "multiselect":
        return []
    return ""


def _make_label(key: str) -> str:
    """A human-readable label from a key: ``forAcquisition`` -> ``For Acquisition``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key.replace("_", " "))
    return " ".join(word[:1].upper() + word[1:] for word in spaced.split())


def materialize_device_schema(
    *,
    template: Optional[dict],
    json_schema: Optional[dict],
    kind_schema: Optional[dict] = None,
) -> dict:
    """Return a template-shaped device schema enriched by a JSON Schema.

    The Qt editor consumes the long-standing ``top``/``props``/``nested``
    template shape. This adapter lets a manager's schema -- generated or
    shipped by a plugin -- add and type ``managerProperties`` without every
    manager needing a duplicate editor template.

    For a template-backed property the resolved type, options, aliases,
    nullability and requiredness are written into the template's field so
    the form sees the schema; schema-only properties are added to a
    ``Properties`` group. Top-level keys come from ``kind_schema`` (the
    ``SetupInfo`` dataclass) the same way, into a ``Device`` group. The
    inputs are never mutated: they are cached globally by the editor.
    """
    result = copy.deepcopy(template) if template else {}
    result.setdefault("top", [])
    result.setdefault("props", [])
    result.setdefault("nested", {})

    fields = normalized_fields(template=template, json_schema=json_schema, kind_schema=kind_schema)
    template_top = {field.get("key"): field for field in result["top"]}
    for field in fields:
        if field.location != "top":
            continue
        if field.key in template_top:
            existing = template_top[field.key]
            existing["req"] = field.required
            existing["type"] = field.type
            existing["opts"] = list(field.options)
            _annotate(existing, field)
            continue
        new_field = {
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "default": field.default,
            "req": field.required,
            "grp": field.group,
            "tip": field.tooltip,
            "opts": list(field.options),
        }
        _annotate(new_field, field)
        result["top"].append(new_field)
        template_top[field.key] = new_field
    template_props = {field.get("key"): field for field in result["props"]}
    for field in fields:
        if field.location != "prop":
            continue
        if field.key in template_props:
            existing = template_props[field.key]
            existing["req"] = field.required
            existing["type"] = field.type
            existing["opts"] = list(field.options)
            _annotate(existing, field)
            continue
        new_field = {
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "default": field.default,
            "req": field.required,
            "grp": field.group,
            "tip": field.tooltip,
            "opts": list(field.options),
        }
        _annotate(new_field, field)
        result["props"].append(new_field)
        template_props[field.key] = new_field
    for field in result["props"]:
        field.setdefault("type", "text")
    for nest_fields in result["nested"].values():
        for field in nest_fields:
            field.setdefault("type", "text")
    for field in result["top"]:
        field.setdefault("type", "text")
    return result


def _annotate(field_dict: dict, field: FieldSpec) -> None:
    """The schema facts the form acts on beyond the type."""
    if field.aliases:
        field_dict["aliases"] = list(field.aliases)
    else:
        field_dict.pop("aliases", None)
    if field.nullable:
        field_dict["nullable"] = True
    if field.required_by_schema:
        field_dict["schema_req"] = True
    else:
        field_dict.pop("schema_req", None)
