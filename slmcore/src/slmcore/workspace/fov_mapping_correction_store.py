"""Persistent SLM/section/mapping-scoped FOV mapping corrections."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any,Callable

from ..core.calibration.fov_mapping_correction import FOVMappingCorrection
from ..core.engine.device import SLMIdentity

_logger = logging.getLogger(__name__)
_VERSION = 2


class FOVMappingCorrectionStore:
    """JSON store scoped by SLM identity, section and mapping name."""

    def __init__(self,directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]] = []

    @staticmethod
    def _serial(identity: SLMIdentity | str) -> str:
        return (
            identity.serial_number if isinstance(identity,SLMIdentity)
            else str(identity or "").strip()
        )

    def list(
        self,identity: SLMIdentity | str,section_key: str,mapping_name: str,
    ) -> tuple[str,...]:
        directory = self._scope_directory(identity,section_key,mapping_name)
        if not directory.is_dir():
            return ()
        serial = self._serial(identity)
        items = []
        for path in sorted(directory.glob("*.json")):
            try:
                correction = self._load_path(path)
            except Exception:
                _logger.exception("Could not read FOV mapping correction %s",path)
                continue
            if (
                correction.slm_serial == serial
                and correction.section_key == str(section_key).strip()
                and correction.mapping_name == str(mapping_name).strip()
            ):
                items.append(correction.name)
        return tuple(sorted(items,key=str.casefold))

    def exists(
        self,identity: SLMIdentity | str,section_key: str,mapping_name: str,name: str,
    ) -> bool:
        return self._path(identity,section_key,mapping_name,name).is_file()

    def load(
        self,identity: SLMIdentity | str,section_key: str,mapping_name: str,name: str,
    ) -> FOVMappingCorrection:
        path = self._path(identity,section_key,mapping_name,name)
        if not path.is_file():
            raise FileNotFoundError(
                f'FOV mapping correction "{name}" does not exist for '
                f'{self._serial(identity)}/{section_key}/{mapping_name}.'
            )
        correction = self._load_path(path)
        expected = (
            self._serial(identity),str(section_key).strip(),
            str(mapping_name).strip(),str(name).strip(),
        )
        actual = (
            correction.slm_serial,correction.section_key,
            correction.mapping_name,correction.name,
        )
        if actual != expected:
            raise ValueError("FOV mapping-correction file scope/name does not match its path")
        return correction

    def save(self,correction: FOVMappingCorrection,*,overwrite: bool=False) -> Path:
        if not isinstance(correction,FOVMappingCorrection):
            raise TypeError("correction must be an FOVMappingCorrection")
        path = self._path(
            correction.slm_serial,correction.section_key,
            correction.mapping_name,correction.name,
        )
        if path.exists() and not overwrite:
            raise FileExistsError(
                f'FOV mapping correction "{correction.name}" already exists.'
            )
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_json(path,{"version":_VERSION,"correction":correction.to_dict()})
        self._notify()
        return path

    def delete(
        self,identity: SLMIdentity | str,section_key: str,mapping_name: str,name: str,
    ) -> Path:
        path = self._path(identity,section_key,mapping_name,name)
        if not path.is_file():
            raise FileNotFoundError(f'FOV mapping correction "{name}" does not exist.')
        path.unlink()
        directory = path.parent
        for _ in range(3):
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent
        self._notify()
        return path

    def delete_mapping(self,mapping_name: str) -> tuple[Path,...]:
        """Delete every persisted correction owned by one workspace mapping.

        Mapping definitions are workspace-wide, so their dependent corrections
        are removed across all SLM serials and sections, matching base-calibration
        deletion semantics.
        """
        mapping_dir_name = _slug(mapping_name,"Mapping")
        deleted: list[Path] = []
        if not self.directory.is_dir():
            return ()
        for serial_dir in tuple(self.directory.iterdir()):
            if not serial_dir.is_dir():
                continue
            for section_dir in tuple(serial_dir.iterdir()):
                if not section_dir.is_dir():
                    continue
                mapping_dir = section_dir/mapping_dir_name
                if not mapping_dir.is_dir():
                    continue
                deleted.extend(path for path in mapping_dir.rglob("*") if path.is_file())
                shutil.rmtree(mapping_dir)
                for parent in (section_dir,serial_dir):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
        if deleted:
            self._notify()
        return tuple(deleted)

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:self._listeners.remove(callback)
        except ValueError:pass

    def _scope_directory(self,identity,section_key,mapping_name) -> Path:
        return (
            self.directory
            /_slug(self._serial(identity),"SLM serial")
            /_slug(section_key,"Section")
            /_slug(mapping_name,"Mapping")
        )

    def _path(self,identity,section_key,mapping_name,name) -> Path:
        return self._scope_directory(identity,section_key,mapping_name)/(f"{_slug(name,'Correction')}.json")

    @staticmethod
    def _load_path(path: Path) -> FOVMappingCorrection:
        with path.open("r",encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload,dict):
            raise ValueError("FOV mapping-correction file must contain an object")
        version = int(payload.get("version",0))
        if version == 1:
            # Schema v1 stored polynomial coefficients directly on the FOV
            # correction.  Migrate in memory only; the next explicit save emits
            # schema v2 with the shared transform payload.
            return FOVMappingCorrection.from_v1_dict(payload["correction"])
        if version != _VERSION:
            raise ValueError(f"Unsupported FOV mapping-correction version: {version}")
        return FOVMappingCorrection.from_dict(payload["correction"])

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:callback()
            except Exception:_logger.exception("FOV mapping-correction-store listener failed")


def _slug(value: Any,label: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-z_ -]","",text)
    text = re.sub(r"[\s-]+","_",text)
    text = re.sub(r"_+","_",text).strip("_")
    if not text:
        raise ValueError(f"{label} must contain at least one letter or number")
    return text


def _write_json(path: Path,data: dict) -> None:
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w",encoding="utf-8") as file:
        json.dump(data,file,indent=2,sort_keys=True); file.write("\n")
    os.replace(temporary,path)


__all__ = ["FOVMappingCorrectionStore"]
