"""Persistent catalog of observation-plane definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
from typing import Callable

_VERSION = 1
_FILENAME = "observation_planes.json"


@dataclass(frozen=True)
class ObservationPlaneDefinition:
    """Persistent measurement-view definition independent of SLM mapping state."""

    name: str
    preferred_detector: str | None = None
    default_pixel_size_um: float | None = None
    description: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("Observation plane name cannot be empty")
        detector = (
            None if self.preferred_detector is None or not str(self.preferred_detector).strip()
            else str(self.preferred_detector).strip()
        )
        pixel_size = self.default_pixel_size_um
        if pixel_size is not None:
            pixel_size = float(pixel_size)
            if not math.isfinite(pixel_size) or pixel_size <= 0:
                raise ValueError("default_pixel_size_um must be a positive finite value")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "preferred_detector", detector)
        object.__setattr__(self, "default_pixel_size_um", pixel_size)
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "created_at", str(self.created_at or datetime.now().isoformat()))


class ObservationPlaneStore:
    """JSON-backed observation-plane catalog with no SLM/section dependency."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / _FILENAME
        self._listeners: list[Callable[[], None]] = []
        self._definitions: dict[str, ObservationPlaneDefinition] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.is_file():
            self._definitions = {}
            return
        with self.path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if int(payload.get("version", 0)) != _VERSION:
            raise ValueError("Unsupported observation-plane store version")
        definitions = {}
        for item in payload.get("observation_planes", []):
            definition = ObservationPlaneDefinition(**dict(item))
            if definition.name in definitions:
                raise ValueError(f'Observation plane "{definition.name}" is duplicated')
            definitions[definition.name] = definition
        self._definitions = definitions

    def list(self) -> tuple[ObservationPlaneDefinition, ...]:
        return tuple(self._definitions.values())

    def get(self, name: str) -> ObservationPlaneDefinition:
        key = str(name or "").strip()
        try:
            return self._definitions[key]
        except KeyError as error:
            raise KeyError(f'Observation plane "{key}" is not defined.') from error

    def add(self, definition: ObservationPlaneDefinition) -> ObservationPlaneDefinition:
        value = _coerce_definition(definition)
        if value.name in self._definitions:
            raise ValueError(f'Observation plane "{value.name}" already exists.')
        updated = dict(self._definitions)
        updated[value.name] = value
        self._commit(updated)
        return value

    def update(self, observation_plane_name: str, **changes) -> ObservationPlaneDefinition:
        current = self.get(observation_plane_name)
        if "name" in changes and str(changes["name"]).strip() != current.name:
            raise ValueError("Renaming observation planes is not supported")
        changes.pop("name", None)
        value = replace(current, **changes)
        updated = dict(self._definitions)
        updated[current.name] = value
        self._commit(updated)
        return value

    def delete(self, name: str) -> ObservationPlaneDefinition:
        value = self.get(name)
        updated = dict(self._definitions)
        del updated[value.name]
        self._commit(updated)
        return value

    def add_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _commit(self, definitions: dict[str, ObservationPlaneDefinition]) -> None:
        payload = {
            "version": _VERSION,
            "observation_planes": [asdict(item) for item in definitions.values()],
        }
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary, self.path)
        self._definitions = definitions
        for callback in tuple(self._listeners):
            callback()


def _coerce_definition(value) -> ObservationPlaneDefinition:
    if isinstance(value, ObservationPlaneDefinition):
        return value
    if isinstance(value, dict):
        return ObservationPlaneDefinition(**value)
    raise TypeError("definition must be an ObservationPlaneDefinition")


__all__ = ["ObservationPlaneDefinition", "ObservationPlaneStore"]
