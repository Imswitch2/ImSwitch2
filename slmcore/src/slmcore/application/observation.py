"""Application-facing observation-plane catalog service."""

from __future__ import annotations

from ..workspace.observation_plane_store import ObservationPlaneDefinition, ObservationPlaneStore


class ObservationPlaneService:
    """Thin CRUD API for observation planes; owns no selected/active plane."""

    def __init__(self, store: ObservationPlaneStore | None = None) -> None:
        self.store = store

    def list(self) -> tuple[ObservationPlaneDefinition, ...]:
        return () if self.store is None else self.store.list()

    def get(self, name: str) -> ObservationPlaneDefinition:
        if self.store is None:
            raise KeyError(f'Observation plane "{str(name or "").strip()}" is not defined.')
        return self.store.get(name)

    def add(self, definition: ObservationPlaneDefinition) -> ObservationPlaneDefinition:
        return self._require_store().add(definition)

    def update(self, observation_plane_name: str, **changes) -> ObservationPlaneDefinition:
        return self._require_store().update(observation_plane_name, **changes)

    def delete(self, name: str) -> ObservationPlaneDefinition:
        return self._require_store().delete(name)

    def _require_store(self) -> ObservationPlaneStore:
        if self.store is None:
            raise RuntimeError("Observation-plane storage is not configured")
        return self.store


__all__ = ["ObservationPlaneService"]
