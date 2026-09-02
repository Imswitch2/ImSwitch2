"""Workspace-wide persistent position-reference resources."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any,Callable

from ..core.cgh.feedback.reference import PositionReference


_logger = logging.getLogger(__name__)
_POSITION_REFERENCE_STORE_VERSION = 1


class SLMPositionReferenceStore:
    """Persist named detector-space position references by measurement plane."""

    def __init__(self,directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]] = []

    def list(self,plane_name: str) -> tuple[str,...]:
        directory = self._plane_directory(plane_name)
        if not directory.is_dir():
            return ()
        requested_plane = str(plane_name).strip()
        items = []
        for path in sorted(directory.glob("*.json")):
            try:
                reference = self._load_path(path)
            except Exception:
                _logger.exception("Could not read position reference %s",path)
                continue
            # Plane directory names are filesystem-normalized. Filter by the
            # canonical name as well so normalization collisions can never leak
            # a resource from a different plane into this listing.
            if reference.plane_name == requested_plane:
                items.append(reference.name)
        return tuple(sorted(items,key=str.casefold))

    def exists(self,plane_name: str,name: str) -> bool:
        return self._path(plane_name,name).is_file()

    def load(self,plane_name: str,name: str) -> PositionReference:
        path = self._path(plane_name,name)
        if not path.is_file():
            raise FileNotFoundError(
                f'Position reference "{name}" does not exist for plane "{plane_name}".'
            )
        reference = self._load_path(path)
        if reference.plane_name != str(plane_name).strip():
            raise ValueError("Position-reference file plane does not match its directory")
        if reference.name != str(name).strip():
            raise ValueError(
                "Position-reference name collides after filesystem normalization"
            )
        return reference

    def save(
        self,reference: PositionReference,*,overwrite: bool=False,
    ) -> Path:
        if not isinstance(reference,PositionReference):
            raise TypeError("reference must be a PositionReference")
        path = self._path(reference.plane_name,reference.name)
        if path.exists():
            existing = self._load_path(path)
            if (
                existing.name != reference.name
                or existing.plane_name != reference.plane_name
            ):
                raise ValueError(
                    "Position-reference name collides after filesystem normalization; "
                    "choose a different name."
                )
            if not overwrite:
                raise FileExistsError(
                    f'Position reference "{reference.name}" already exists for '
                    f'plane "{reference.plane_name}".'
                )
        path.parent.mkdir(parents=True,exist_ok=True)
        payload = {
            "version":_POSITION_REFERENCE_STORE_VERSION,
            "reference":reference.to_dict(),
        }
        _write_json(path,payload)
        self._notify()
        return path

    def delete(self,plane_name: str,name: str) -> Path:
        path = self._path(plane_name,name)
        if not path.is_file():
            raise FileNotFoundError(
                f'Position reference "{name}" does not exist for plane "{plane_name}".'
            )
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        self._notify()
        return path

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _plane_directory(self,plane_name: str) -> Path:
        return self.directory/_slug(plane_name,"Plane")

    def _path(self,plane_name: str,name: str) -> Path:
        return self._plane_directory(plane_name)/(f"{_slug(name,'Reference')}.json")

    @staticmethod
    def _load_path(path: Path) -> PositionReference:
        with path.open("r",encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload,dict):
            raise ValueError("Position-reference file must contain an object")
        version = int(payload.get("version",0))
        if version != _POSITION_REFERENCE_STORE_VERSION:
            raise ValueError(f"Unsupported position-reference version: {version}")
        return PositionReference.from_dict(payload["reference"])

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:
                _logger.exception("Position-reference-store listener failed")


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


__all__ = ["SLMPositionReferenceStore"]
