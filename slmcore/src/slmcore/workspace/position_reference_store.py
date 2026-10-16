"""Workspace-scoped named position-reference collection with optional persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
from typing import Any,Callable

from ..core.cgh.feedback.reference import PositionReference


_logger = logging.getLogger(__name__)
_POSITION_REFERENCE_STORE_VERSION = 2


class PositionReferenceLifetime(str,Enum):
    """Lifetime of a named position reference."""

    SESSION = "session"
    PERSISTENT = "persistent"

    @classmethod
    def normalize(cls,value: Any) -> "PositionReferenceLifetime":
        if isinstance(value,cls):
            return value
        return cls(str(value or cls.SESSION.value).strip().lower())


@dataclass(frozen=True)
class PositionReferenceRecord:
    """One live named reference and the lifetime policy attached to it."""

    reference: PositionReference
    lifetime: PositionReferenceLifetime=PositionReferenceLifetime.SESSION

    def __post_init__(self) -> None:
        if not isinstance(self.reference,PositionReference):
            raise TypeError("reference must be a PositionReference")
        object.__setattr__(self,"lifetime",PositionReferenceLifetime.normalize(self.lifetime))

    @property
    def name(self) -> str:
        return self.reference.name

    @property
    def observation_plane_name(self) -> str:
        return self.reference.observation_plane_name


class PositionReferenceStore:
    """Manage the live named references shared by an SLM application.

    Session references exist only in memory. Persistent references use the same
    live collection but are additionally backed by the optional persistence
    directory using the existing position-reference JSON format.
    """

    def __init__(self,persistent_root: str | Path | None=None) -> None:
        self.persistent_root = (
            None if persistent_root is None else Path(persistent_root)
        )
        self._entries: dict[tuple[str,str],PositionReferenceRecord] = {}
        self._listeners: list[Callable[[],None]] = []
        if self.persistent_root is not None:
            self._load_persistent_entries()

    @property
    def persistence_available(self) -> bool:
        return self.persistent_root is not None

    def list(self,observation_plane_name: str) -> tuple[PositionReferenceRecord,...]:
        plane = self._normalize_name(observation_plane_name,"Observation plane")
        records = [
            record for (record_plane,_name),record in self._entries.items()
            if record_plane == plane
        ]
        return tuple(sorted(records,key=lambda item:item.name.casefold()))

    def list_all(self) -> tuple[PositionReferenceRecord,...]:
        """Return all live references across observation planes deterministically."""
        return tuple(sorted(
            self._entries.values(),
            key=lambda item:(
                item.observation_plane_name.casefold(),
                item.name.casefold(),
            ),
        ))

    def names(self,observation_plane_name: str) -> tuple[str,...]:
        return tuple(record.name for record in self.list(observation_plane_name))

    def exists(self,observation_plane_name: str,name: str) -> bool:
        try:
            key = self._key(observation_plane_name,name)
        except ValueError:
            return False
        return key in self._entries

    def get(self,observation_plane_name: str,name: str) -> PositionReference:
        return self.get_record(observation_plane_name,name).reference

    def get_record(self,observation_plane_name: str,name: str) -> PositionReferenceRecord:
        key = self._key(observation_plane_name,name)
        try:
            return self._entries[key]
        except KeyError as error:
            raise FileNotFoundError(
                f'Position reference "{name}" does not exist for observation plane "{observation_plane_name}".'
            ) from error

    def add(
        self,
        reference: PositionReference,
        *,
        lifetime: PositionReferenceLifetime | str=PositionReferenceLifetime.SESSION,
        overwrite: bool=False,
    ) -> PositionReferenceRecord:
        if not isinstance(reference,PositionReference):
            raise TypeError("reference must be a PositionReference")
        lifetime = PositionReferenceLifetime.normalize(lifetime)
        key = self._key(reference.observation_plane_name,reference.name)
        existing = self._entries.get(key)
        if existing is not None and not overwrite:
            raise FileExistsError(
                f'Position reference "{reference.name}" already exists for '
                f'plane "{reference.observation_plane_name}".'
            )

        # Normal replacement changes content without demoting an existing
        # persistent reference. An explicitly persistent replacement may still
        # promote a session reference.
        effective_lifetime = (
            existing.lifetime
            if existing is not None and lifetime is PositionReferenceLifetime.SESSION
            else lifetime
        )
        record = PositionReferenceRecord(reference,effective_lifetime)
        if effective_lifetime is PositionReferenceLifetime.PERSISTENT:
            self._write_reference(reference,overwrite=existing is not None)
        self._entries[key] = record
        self._notify()
        return record

    def persist(self,observation_plane_name: str,name: str) -> PositionReferenceRecord:
        key = self._key(observation_plane_name,name)
        record = self.get_record(observation_plane_name,name)
        if record.lifetime is PositionReferenceLifetime.PERSISTENT:
            return record
        if self.persistent_root is None:
            raise RuntimeError("Persistent position-reference storage is unavailable")
        self._write_reference(record.reference,overwrite=False)
        persistent = PositionReferenceRecord(
            record.reference,PositionReferenceLifetime.PERSISTENT,
        )
        self._entries[key] = persistent
        self._notify()
        return persistent

    def delete(self,observation_plane_name: str,name: str) -> PositionReferenceRecord:
        key = self._key(observation_plane_name,name)
        record = self.get_record(observation_plane_name,name)
        if record.lifetime is PositionReferenceLifetime.PERSISTENT:
            path = self._path(record.observation_plane_name,record.name)
            if not path.is_file():
                raise FileNotFoundError(
                    f'Persistent position reference "{record.name}" is missing '
                    f'for observation plane "{record.observation_plane_name}".'
                )
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
        del self._entries[key]
        self._notify()
        return record

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _load_persistent_entries(self) -> None:
        if self.persistent_root is None or not self.persistent_root.is_dir():
            return
        for path in sorted(self.persistent_root.rglob("*.json")):
            try:
                reference = self._load_path(path)
                key = self._key(reference.observation_plane_name,reference.name)
                if key in self._entries:
                    raise ValueError(
                        "Duplicate persistent position-reference identity: "
                        f'{reference.observation_plane_name!r}/{reference.name!r}'
                    )
                expected = self._path(reference.observation_plane_name,reference.name)
                if path.resolve() != expected.resolve():
                    raise ValueError(
                        "Position-reference file path does not match its embedded identity"
                    )
                self._entries[key] = PositionReferenceRecord(
                    reference,PositionReferenceLifetime.PERSISTENT,
                )
            except Exception:
                _logger.exception("Could not read position reference %s",path)

    def _write_reference(
        self,reference: PositionReference,*,overwrite: bool,
    ) -> Path:
        if self.persistent_root is None:
            raise RuntimeError("Persistent position-reference storage is unavailable")
        path = self._path(reference.observation_plane_name,reference.name)
        if path.exists():
            existing = self._load_path(path)
            if (
                existing.name != reference.name
                or existing.observation_plane_name != reference.observation_plane_name
            ):
                raise ValueError(
                    "Position-reference name collides after filesystem normalization; "
                    "choose a different name."
                )
            if not overwrite:
                raise FileExistsError(
                    f'Position reference "{reference.name}" already exists for '
                    f'plane "{reference.observation_plane_name}".'
                )
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_json(path,{
            "version":_POSITION_REFERENCE_STORE_VERSION,
            "reference":reference.to_dict(),
        })
        return path

    def _plane_directory(self,observation_plane_name: str) -> Path:
        if self.persistent_root is None:
            raise RuntimeError("Persistent position-reference storage is unavailable")
        return self.persistent_root/_slug(observation_plane_name,"Observation plane")

    def _path(self,observation_plane_name: str,name: str) -> Path:
        return self._plane_directory(observation_plane_name)/(f"{_slug(name,'Reference')}.json")

    @staticmethod
    def _load_path(path: Path) -> PositionReference:
        with path.open("r",encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload,dict):
            raise ValueError("Position-reference file must contain an object")
        version = int(payload.get("version",0))
        migrated=False
        if version == 1:
            payload=_migrate_v1_payload(payload)
            migrated=True
        elif version != _POSITION_REFERENCE_STORE_VERSION:
            raise ValueError(f"Unsupported position-reference version: {version}")
        reference=PositionReference.from_dict(payload["reference"])
        if migrated:
            _write_json(path,{
                "version":_POSITION_REFERENCE_STORE_VERSION,
                "reference":reference.to_dict(),
            })
            _logger.info("Migrated position reference v1 -> v2: %s",path)
        return reference

    @classmethod
    def _key(cls,observation_plane_name: str,name: str) -> tuple[str,str]:
        return (
            cls._normalize_name(observation_plane_name,"Observation plane"),
            cls._normalize_name(name,"Reference"),
        )

    @staticmethod
    def _normalize_name(value: Any,label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label} name must not be empty")
        return text

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:
                _logger.exception("Position-reference-store listener failed")


def _migrate_v1_payload(payload: Any) -> dict[str,Any]:
    if not isinstance(payload,dict):
        raise ValueError("Position-reference file must contain an object")
    reference=payload.get("reference")
    if not isinstance(reference,dict):
        raise ValueError("Position-reference payload is invalid")
    migrated=dict(reference)
    old_name=str(migrated.get("plane_name","") or "").strip()
    new_name=str(migrated.get("observation_plane_name","") or "").strip()
    if new_name and old_name and new_name != old_name:
        raise ValueError("Position reference contains conflicting plane identities")
    if not new_name:
        migrated["observation_plane_name"]=old_name
    migrated.pop("plane_name",None)
    return {
        "version":_POSITION_REFERENCE_STORE_VERSION,
        "reference":migrated,
    }


def _slug(value: Any,label: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-z_ -]","",text)
    text = re.sub(r"[\s-]+","_",text)
    text = re.sub(r"_+","_",text).strip("_")
    if not text:
        raise ValueError(f"{label} name must contain at least one letter or number.")
    return text


def _write_json(path: Path,data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w",encoding="utf-8") as file:
        json.dump(data,file,indent=2,sort_keys=True)
        file.write("\n")
    os.replace(temporary,path)


__all__ = [
    "PositionReferenceLifetime",
    "PositionReferenceRecord",
    "PositionReferenceStore",
]
