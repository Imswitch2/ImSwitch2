"""Persistent SLM/section/plane-scoped FOV position calibrations."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any,Callable

from ..core.cgh.feedback.fov_calibration import FOVPositionCalibration
from ..core.engine.device import SLMIdentity

_logger = logging.getLogger(__name__)
_VERSION = 1


class SLMFOVPositionCalibrationStore:
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

    def list(self,identity: SLMIdentity | str,section_key: str,plane_name: str) -> tuple[str,...]:
        directory = self._scope_directory(identity,section_key,plane_name)
        if not directory.is_dir():
            return ()
        serial = self._serial(identity)
        items = []
        for path in sorted(directory.glob("*.json")):
            try:
                calibration = self._load_path(path)
            except Exception:
                _logger.exception("Could not read FOV position calibration %s",path)
                continue
            if (
                calibration.slm_serial == serial
                and calibration.section_key == str(section_key).strip()
                and calibration.plane_name == str(plane_name).strip()
            ):
                items.append(calibration.name)
        return tuple(sorted(items,key=str.casefold))

    def exists(self,identity: SLMIdentity | str,section_key: str,plane_name: str,name: str) -> bool:
        return self._path(identity,section_key,plane_name,name).is_file()

    def load(self,identity: SLMIdentity | str,section_key: str,plane_name: str,name: str) -> FOVPositionCalibration:
        path = self._path(identity,section_key,plane_name,name)
        if not path.is_file():
            raise FileNotFoundError(
                f'FOV position calibration "{name}" does not exist for '
                f'{self._serial(identity)}/{section_key}/{plane_name}.'
            )
        calibration = self._load_path(path)
        expected = (
            self._serial(identity),str(section_key).strip(),str(plane_name).strip(),
            str(name).strip(),
        )
        actual = (
            calibration.slm_serial,calibration.section_key,
            calibration.plane_name,calibration.name,
        )
        if actual != expected:
            raise ValueError("FOV calibration file scope/name does not match its path")
        return calibration

    def save(self,calibration: FOVPositionCalibration,*,overwrite: bool=False) -> Path:
        if not isinstance(calibration,FOVPositionCalibration):
            raise TypeError("calibration must be an FOVPositionCalibration")
        path = self._path(
            calibration.slm_serial,calibration.section_key,
            calibration.plane_name,calibration.name,
        )
        if path.exists() and not overwrite:
            raise FileExistsError(
                f'FOV position calibration "{calibration.name}" already exists.'
            )
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_json(path,{"version":_VERSION,"calibration":calibration.to_dict()})
        self._notify()
        return path

    def delete(self,identity: SLMIdentity | str,section_key: str,plane_name: str,name: str) -> Path:
        path = self._path(identity,section_key,plane_name,name)
        if not path.is_file():
            raise FileNotFoundError(f'FOV position calibration "{name}" does not exist.')
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

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:self._listeners.remove(callback)
        except ValueError:pass

    def _scope_directory(self,identity,section_key,plane_name) -> Path:
        return self.directory/_slug(self._serial(identity),"SLM serial")/_slug(section_key,"Section")/_slug(plane_name,"Plane")

    def _path(self,identity,section_key,plane_name,name) -> Path:
        return self._scope_directory(identity,section_key,plane_name)/(f"{_slug(name,'Calibration')}.json")

    @staticmethod
    def _load_path(path: Path) -> FOVPositionCalibration:
        with path.open("r",encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload,dict):
            raise ValueError("FOV calibration file must contain an object")
        version = int(payload.get("version",0))
        if version != _VERSION:
            raise ValueError(f"Unsupported FOV calibration version: {version}")
        return FOVPositionCalibration.from_dict(payload["calibration"])

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:callback()
            except Exception:_logger.exception("FOV-calibration-store listener failed")


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


__all__ = ["SLMFOVPositionCalibrationStore"]
