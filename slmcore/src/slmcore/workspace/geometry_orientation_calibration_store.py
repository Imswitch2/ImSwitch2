"""Persistent SLM/section/observation plane geometry-orientation calibrations."""

from __future__ import annotations

import json,logging,os,re
from pathlib import Path
from typing import Any,Callable

from ..core.cgh.feedback.geometry_calibration import GeometryOrientationCalibration
from ..core.engine.device import SLMIdentity

_logger=logging.getLogger(__name__)
_VERSION=2


class SLMGeometryOrientationCalibrationStore:
    """One authoritative calibration file per SLM/section/observation-plane."""

    def __init__(self,directory: str | Path) -> None:
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]]=[]

    @staticmethod
    def _serial(identity: SLMIdentity | str) -> str:
        return identity.serial_number if isinstance(identity,SLMIdentity) else str(identity or "").strip()

    def exists(self,identity,section_key,observation_plane_name) -> bool:
        return self._path(identity,section_key,observation_plane_name).is_file()

    def load(self,identity,section_key,observation_plane_name) -> GeometryOrientationCalibration:
        path=self._path(identity,section_key,observation_plane_name)
        if not path.is_file():
            raise FileNotFoundError(
                "Geometry orientation calibration does not exist for %s/%s/%s."
                % (self._serial(identity),section_key,observation_plane_name)
            )
        with path.open("r",encoding="utf-8") as file:
            payload=json.load(file)
        version=int(payload.get("version",0))
        migrated=False
        if version == 1:
            payload=_migrate_v1_payload(payload)
            migrated=True
        elif version != _VERSION:
            raise ValueError("Unsupported geometry orientation calibration version")
        calibration=GeometryOrientationCalibration.from_dict(payload["calibration"])
        expected=(self._serial(identity),str(section_key).strip(),str(observation_plane_name).strip())
        actual=(calibration.slm_serial,calibration.section_key,calibration.observation_plane_name)
        if actual != expected:
            raise ValueError("Geometry calibration file scope does not match its path")
        if migrated:
            _write_calibration_payload(path,calibration)
            _logger.info("Migrated geometry orientation calibration v1 -> v2: %s",path)
        return calibration

    def save(self,calibration: GeometryOrientationCalibration) -> Path:
        if not isinstance(calibration,GeometryOrientationCalibration):
            raise TypeError("calibration must be a GeometryOrientationCalibration")
        path=self._path(calibration.slm_serial,calibration.section_key,calibration.observation_plane_name)
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_calibration_payload(path,calibration)
        self._notify()
        return path

    def delete(self,identity,section_key,observation_plane_name) -> Path:
        path=self._path(identity,section_key,observation_plane_name)
        if not path.is_file():
            raise FileNotFoundError("Geometry orientation calibration does not exist")
        path.unlink(); self._notify(); return path

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:self._listeners.remove(callback)
        except ValueError:pass

    def _path(self,identity,section_key,observation_plane_name) -> Path:
        return self.directory/_slug(self._serial(identity),"SLM serial")/_slug(section_key,"Section")/(_slug(observation_plane_name,"Plane")+".json")

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:callback()
            except Exception:_logger.exception("Geometry-calibration-store listener failed")


def _migrate_v1_payload(payload: Any) -> dict[str,Any]:
    if not isinstance(payload,dict):
        raise ValueError("Geometry orientation calibration file must contain an object")
    calibration=payload.get("calibration")
    if not isinstance(calibration,dict):
        raise ValueError("Geometry orientation calibration payload is invalid")
    migrated=dict(calibration)
    old_name=str(migrated.get("plane_name","") or "").strip()
    new_name=str(migrated.get("observation_plane_name","") or "").strip()
    if new_name and old_name and new_name != old_name:
        raise ValueError("Geometry calibration contains conflicting plane identities")
    if not new_name:
        migrated["observation_plane_name"]=old_name
    migrated.pop("plane_name",None)
    return {"version":_VERSION,"calibration":migrated}


def _write_calibration_payload(path: Path,calibration: GeometryOrientationCalibration) -> None:
    temporary=path.with_suffix(".json.tmp")
    with temporary.open("w",encoding="utf-8") as file:
        json.dump({"version":_VERSION,"calibration":calibration.to_dict()},file,indent=2,sort_keys=True)
        file.write("\n")
    os.replace(temporary,path)


def _slug(value: Any,label: str) -> str:
    text=str(value or "").strip().lower()
    text=re.sub(r"[^0-9a-z_ -]","",text)
    text=re.sub(r"[\s-]+","_",text); text=re.sub(r"_+","_",text).strip("_")
    if not text:raise ValueError(f"{label} must contain at least one letter or number")
    return text


__all__=["SLMGeometryOrientationCalibrationStore"]
