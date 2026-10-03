"""Persistent catalog of SLM-to-sample mapping definitions."""

from __future__ import annotations

from dataclasses import asdict,dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any,Callable

_VERSION = 1
_FILENAME = "mappings.json"


@dataclass(frozen=True)
class MappingDefinition:
    """Named SLM-to-sample mapping profile without observation metadata."""

    name: str
    description: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("Mapping name cannot be empty")
        object.__setattr__(self,"name",name)
        object.__setattr__(self,"description",str(self.description or "").strip())
        object.__setattr__(
            self,"created_at",str(self.created_at or datetime.now().isoformat()),
        )


class MappingStore:
    """JSON-backed mapping catalog independent of detector/observation state."""

    def __init__(self,directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path = self.directory/_FILENAME
        self._listeners: list[Callable[[],None]] = []
        self._definitions: dict[str,MappingDefinition] = {}
        self.last_load_error: Exception | None = None
        self.reload()

    def reload(self) -> None:
        try:
            if not self.path.is_file():
                self._definitions = {}
            else:
                with self.path.open("r",encoding="utf-8") as file:
                    payload = json.load(file)
                if int(payload.get("version",0)) != _VERSION:
                    raise ValueError("Unsupported mapping-store version")
                definitions: dict[str,MappingDefinition] = {}
                slugs: dict[str,str] = {}
                for item in payload.get("mappings",[]):
                    definition = MappingDefinition(**dict(item))
                    slug = mapping_slug(definition.name)
                    if definition.name in definitions:
                        raise ValueError(f'Mapping "{definition.name}" is duplicated')
                    if slug in slugs:
                        raise ValueError(
                            f'Mapping "{definition.name}" conflicts with "{slugs[slug]}".'
                        )
                    definitions[definition.name] = definition
                    slugs[slug] = definition.name
                self._definitions = definitions
            self.last_load_error = None
        except Exception as error:
            self._definitions = {}
            self.last_load_error = error

    def list(self) -> tuple[MappingDefinition,...]:
        return tuple(self._definitions.values())

    @property
    def names(self) -> tuple[str,...]:
        return tuple(self._definitions)

    def has(self,name: str) -> bool:
        return str(name or "").strip() in self._definitions

    def get(self,name: str) -> MappingDefinition:
        key = str(name or "").strip()
        try:
            return self._definitions[key]
        except KeyError as error:
            raise KeyError(f'Mapping "{key}" is not defined.') from error

    def add(self,definition: MappingDefinition | dict[str,Any]) -> MappingDefinition:
        value = _coerce_definition(definition)
        if value.name in self._definitions:
            raise ValueError(f'Mapping "{value.name}" already exists.')
        slug = mapping_slug(value.name)
        for existing in self._definitions:
            if mapping_slug(existing) == slug:
                raise ValueError(
                    f'Mapping "{value.name}" conflicts with existing mapping "{existing}".'
                )
        updated = dict(self._definitions)
        updated[value.name] = value
        self._commit(updated)
        return value

    def delete(self,name: str) -> MappingDefinition:
        value = self.get(name)
        updated = dict(self._definitions)
        del updated[value.name]
        self._commit(updated)
        return value

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _commit(self,definitions: dict[str,MappingDefinition]) -> None:
        payload = {
            "version":_VERSION,
            "mappings":[asdict(item) for item in definitions.values()],
        }
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w",encoding="utf-8") as file:
            json.dump(payload,file,indent=2,sort_keys=True)
            file.write("\n")
        os.replace(temporary,self.path)
        self._definitions = definitions
        for callback in tuple(self._listeners):
            callback()


def mapping_slug(name: Any) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[^0-9a-z_ -]","",text)
    text = re.sub(r"[\s-]+","_",text)
    text = re.sub(r"_+","_",text).strip("_")
    if not text:
        raise ValueError("Mapping name must contain at least one letter or number.")
    return text


def _coerce_definition(value: MappingDefinition | dict[str,Any]) -> MappingDefinition:
    if isinstance(value,MappingDefinition):
        return value
    if isinstance(value,dict):
        return MappingDefinition(**value)
    raise TypeError("definition must be a MappingDefinition")


__all__ = ["MappingDefinition","MappingStore","mapping_slug"]
