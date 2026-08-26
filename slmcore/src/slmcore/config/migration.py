from __future__ import annotations

from copy import deepcopy
from typing import Any,Mapping

from .model import SLM_CONFIG_SCHEMA_VERSION


def migrate_slm_config_dict(
    data: Mapping[str,Any],
) -> dict[str, Any]:
    """Return config data migrated to the current SLM config schema.

    No historical migrations exist yet. Current-schema data is returned as a
    detached copy, while unsupported schema versions are rejected.
    """
    try:
        version = int(
            data.get("schema_version",SLM_CONFIG_SCHEMA_VERSION)
        )
    except (TypeError,ValueError) as error:
        raise ValueError(
            f"Invalid SLM config schema version: "
            f"{data.get('schema_version')!r}"
        ) from error

    if version not in (1,SLM_CONFIG_SCHEMA_VERSION):
        raise ValueError(
            f"Unsupported SLM config schema version {version}; "
            f"expected {SLM_CONFIG_SCHEMA_VERSION}"
        )

    migrated = deepcopy(dict(data))
    if version == 1:
        _migrate_v1_to_v2(migrated)
        version = SLM_CONFIG_SCHEMA_VERSION
    migrated["schema_version"] = version
    _migrate_legacy_tab_names(migrated)
    return migrated


def _migrate_v1_to_v2(data: dict[str, Any]) -> None:
    sections = data.get("sections")
    if not isinstance(sections,Mapping):
        return
    for section in sections.values():
        if not isinstance(section,dict):
            continue
        section.pop("cgh_result",None)
        section.setdefault("cgh_session",None)


def _migrate_legacy_tab_names(data: dict[str, Any]) -> None:
    tab_names = data.pop("tab_names",None)
    if not isinstance(tab_names,Mapping):
        return

    sections = data.get("sections")
    if not isinstance(sections,Mapping):
        return

    for section_key,title in tab_names.items():
        if not isinstance(section_key,str) or not isinstance(title,str):
            continue
        title = title.strip()
        if not title:
            continue
        section = sections.get(section_key)
        if not isinstance(section,dict):
            continue
        presentation = section.setdefault("presentation",{})
        if isinstance(presentation,dict):
            presentation.setdefault("title",title)
