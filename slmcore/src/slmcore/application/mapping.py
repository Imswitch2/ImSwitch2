"""Application-facing SLM-to-sample mapping catalog service."""

from __future__ import annotations

from typing import Any,Callable

from ..workspace.mapping_store import MappingDefinition,MappingStore


class MappingService:
    """Thin application API around the workspace mapping catalog."""

    def __init__(self,store: MappingStore | None=None) -> None:
        self.store = store

    @property
    def names(self) -> tuple[str,...]:
        return () if self.store is None else self.store.names

    @property
    def last_load_error(self) -> Exception | None:
        return None if self.store is None else self.store.last_load_error

    def list(self) -> tuple[MappingDefinition,...]:
        return () if self.store is None else self.store.list()

    def get(self,name: str) -> MappingDefinition:
        return self._require_store().get(name)

    def has(self,name: str) -> bool:
        return False if self.store is None else self.store.has(name)

    def add(self,definition: MappingDefinition | dict[str,Any]) -> MappingDefinition:
        return self._require_store().add(definition)

    def delete(self,name: str) -> MappingDefinition:
        return self._require_store().delete(name)

    def add_listener(self,callback: Callable[[],None]) -> None:
        self._require_store().add_listener(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        if self.store is not None:
            self.store.remove_listener(callback)

    def _require_store(self) -> MappingStore:
        if self.store is None:
            raise RuntimeError("Mapping storage is not configured")
        return self.store


__all__ = ["MappingService"]
