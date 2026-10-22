"""Thin bridge from slmcore's authoring contract to the generic Config Editor.

slmcore remains the source of truth for nested SLM setup structure and semantic
validation.  This module contains no copied SLM field list.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StructuredTopLevelObject:
    key: str
    title: str
    schema: dict[str, Any]


# These are ImSwitch compatibility fields, not slmcore authoring fields. They
# remain loadable so old setups keep working, but canonical SLM entries should
# not present them beside the slmcore-owned definition/preferences.
_LEGACY_TOP_LEVEL_KEYS = frozenset({
    "serial_number",
    "width",
    "height",
    "pixelSize",
    "nSections",
    "correctionPatternsDir",
    "wavelengthTableFile",
    "widgetOptions",
})
_LEGACY_MANAGER_PROPERTY_KEYS = frozenset({"startConfig"})


def has_canonical_definition(device: Mapping[str, Any]) -> bool:
    definition = device.get("definition")
    return isinstance(definition, Mapping) and bool(definition)


def prepare_editor_schema(schema: Mapping[str, Any], device: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ImSwitch presentation policy around slmcore-owned objects.

    In canonical mode all ordinary fields are host/hardware integration and
    therefore share one ``Hardware`` tab. Compatibility-only fields disappear.
    In a legacy entry they remain editable, but only under an explicit
    ``Legacy compatibility`` tab. No canonical SLM field list is copied here.
    """
    result = copy.deepcopy(dict(schema))
    result.setdefault("top", [])
    result.setdefault("props", [])
    result.setdefault("nested", {})
    result.setdefault("hidden_top_keys", set())
    result.setdefault("hidden_prop_keys", set())
    canonical = has_canonical_definition(device)

    def classify(
        fields: list[dict[str, Any]],
        legacy_keys: frozenset[str],
        *,
        hidden_key: str,
    ) -> list[dict[str, Any]]:
        kept = []
        for field in fields:
            key = field.get("key")
            if key in legacy_keys:
                if canonical:
                    # Hidden compatibility values still round-trip untouched;
                    # this marker only keeps them out of the raw "Other" UI.
                    result[hidden_key].add(key)
                    continue
                field["grp"] = "Legacy compatibility"
                prefix = "Legacy compatibility field. "
                if not str(field.get("tip") or "").startswith(prefix):
                    field["tip"] = prefix + str(field.get("tip") or "")
            else:
                field["grp"] = "Hardware"
            kept.append(field)
        return kept

    result["top"] = classify(
        result["top"], _LEGACY_TOP_LEVEL_KEYS, hidden_key="hidden_top_keys"
    )
    result["props"] = classify(
        result["props"], _LEGACY_MANAGER_PROPERTY_KEYS, hidden_key="hidden_prop_keys"
    )
    for children in result["nested"].values():
        for field in children:
            field["grp"] = "Hardware"
    return result


def structured_top_level_objects() -> tuple[StructuredTopLevelObject, ...]:
    """Return slmcore-owned top-level objects, or nothing when slmcore is unavailable."""
    try:
        from slmcore import slm_definition_schema, slm_startup_preferences_schema
    except (ImportError, AttributeError):
        return ()

    return (
        StructuredTopLevelObject(
            key="definition",
            title="Definition",
            schema=slm_definition_schema(host_managed_key=True),
        ),
        StructuredTopLevelObject(
            key="startup_preferences",
            title="Startup",
            schema=slm_startup_preferences_schema(),
        ),
    )


def validate_device(slm_key: str, device: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate canonical SLM objects through slmcore.

    Legacy SLM entries that have no canonical definition are left alone; this
    editor integration must not make merely opening/applying an old setup a
    migration requirement. Once a canonical definition/preferences object is
    present, however, slmcore is authoritative and invalid edits are rejected.
    """
    try:
        from slmcore import validate_slm_definition, validate_slm_startup_preferences
    except (ImportError, AttributeError):
        return ()

    errors: list[str] = []
    definition = device.get("definition")
    if definition not in (None, {}):
        try:
            validate_slm_definition(definition, key=slm_key)
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"Definition: {exc}")

    if "startup_preferences" in device and device.get("startup_preferences") is not None:
        try:
            validate_slm_startup_preferences(device.get("startup_preferences"))
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"Startup: {exc}")

    return tuple(errors)
