from __future__ import annotations

import json
from pathlib import Path
from typing import Any,Mapping

from .model import SLMSetup
from .preferences import SLMStartupPreferences


SLM_SETUP_FILE_SCHEMA_VERSION = 1


def load_slm_setup_file(
    path: str | Path,
    *,
    key: str | None=None,
) -> tuple[SLMSetup,SLMStartupPreferences]:
    """Load one canonical slmcore JSON setup file."""
    source = Path(path).expanduser()
    try:
        with source.open("r",encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError,json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read SLM setup file: {source}") from error
    if not isinstance(data,Mapping):
        raise ValueError("SLM setup file root must be a JSON object")
    version = data.get("schema_version",SLM_SETUP_FILE_SCHEMA_VERSION)
    if version != SLM_SETUP_FILE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported SLM setup file schema version: {version!r}")
    setup_data = data.get("setup")
    if not isinstance(setup_data,Mapping):
        raise ValueError("SLM setup file must contain a 'setup' object")
    preferences_data = data.get("startup_preferences",{})
    return (
        SLMSetup.from_dict(setup_data,key=key),
        SLMStartupPreferences.from_dict(preferences_data),
    )


def save_slm_startup_preferences(
    path: str | Path,
    preferences: SLMStartupPreferences,
) -> None:
    """Atomically update startup preferences in a canonical slmcore setup file."""
    if not isinstance(preferences,SLMStartupPreferences):
        raise TypeError("preferences must be an SLMStartupPreferences")
    destination = Path(path).expanduser()
    try:
        with destination.open("r",encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError,json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read SLM setup file: {destination}") from error
    if not isinstance(data,dict):
        raise ValueError("SLM setup file root must be a JSON object")
    if not isinstance(data.get("setup"),Mapping):
        raise ValueError("SLM setup file must contain a 'setup' object")
    version = data.get("schema_version",SLM_SETUP_FILE_SCHEMA_VERSION)
    if version != SLM_SETUP_FILE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported SLM setup file schema version: {version!r}")
    data["schema_version"] = SLM_SETUP_FILE_SCHEMA_VERSION
    data["startup_preferences"] = preferences.to_dict()

    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("w",encoding="utf-8") as handle:
            json.dump(data,handle,indent=2,sort_keys=True)
            handle.write("\n")
        temporary.replace(destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        raise
