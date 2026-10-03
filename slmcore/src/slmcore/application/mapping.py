"""Application-facing SLM-to-sample mapping catalog and FOV-correction service."""

from __future__ import annotations

from typing import Any,Callable

from ..core.calibration import FOVMappingCorrection
from ..core.engine.device import SLMIdentity
from ..workspace.fov_mapping_correction_store import FOVMappingCorrectionStore
from ..workspace.mapping_store import MappingDefinition,MappingStore


class MappingService:
    """Application owner of named mappings and their persistent FOV corrections."""

    def __init__(
        self,
        store: MappingStore | None=None,
        fov_correction_store: FOVMappingCorrectionStore | None=None,
        identity_provider: Callable[[],SLMIdentity] | None=None,
    ) -> None:
        self.store = store
        self.fov_correction_store = fov_correction_store
        self._identity_provider = identity_provider

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

    # ------------------------------------------------------------------
    # FOV corrections owned by the mapping domain
    # ------------------------------------------------------------------

    @property
    def fov_corrections_available(self) -> bool:
        return self.fov_correction_store is not None

    def fov_correction_names(
        self,section_key: str,mapping_name: str,
    ) -> tuple[str,...]:
        store = self.fov_correction_store
        if store is None:
            return ()
        return store.list(self._identity,section_key,mapping_name)

    def fov_correction_exists(
        self,section_key: str,mapping_name: str,name: str,
    ) -> bool:
        store = self.fov_correction_store
        return False if store is None else store.exists(
            self._identity,section_key,mapping_name,name,
        )

    def load_fov_correction(
        self,section_key: str,mapping_name: str,name: str,
    ) -> FOVMappingCorrection:
        return self._require_fov_store().load(
            self._identity,section_key,mapping_name,name,
        )

    def save_fov_correction(
        self,correction: FOVMappingCorrection,*,overwrite: bool=False,
    ) -> FOVMappingCorrection:
        if correction.slm_serial != self._identity.serial_number:
            raise ValueError("FOV mapping correction belongs to a different SLM")
        self._require_fov_store().save(correction,overwrite=overwrite)
        return correction

    def delete_fov_correction(
        self,section_key: str,mapping_name: str,name: str,
    ) -> None:
        self._require_fov_store().delete(
            self._identity,section_key,mapping_name,name,
        )

    def add_fov_correction_listener(self,callback: Callable[[],None]) -> None:
        self._require_fov_store().add_listener(callback)

    def remove_fov_correction_listener(self,callback: Callable[[],None]) -> None:
        if self.fov_correction_store is not None:
            self.fov_correction_store.remove_listener(callback)

    @property
    def _identity(self) -> SLMIdentity:
        if self._identity_provider is None:
            raise RuntimeError("Mapping FOV-correction storage has no SLM identity provider")
        return self._identity_provider()

    def _require_store(self) -> MappingStore:
        if self.store is None:
            raise RuntimeError("Mapping storage is not configured")
        return self.store

    def _require_fov_store(self) -> FOVMappingCorrectionStore:
        if self.fov_correction_store is None:
            raise RuntimeError("FOV mapping-correction storage is unavailable")
        return self.fov_correction_store


__all__ = ["MappingService"]
