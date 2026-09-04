"""Persistent SLM/section/plane geometry-orientation calibrations."""

from __future__ import annotations

import json,logging,os,re
from pathlib import Path
from typing import Any,Callable

from ..core.cgh.feedback.geometry_calibration import GeometryOrientationCalibration
from ..core.engine.device import SLMIdentity

_logger=logging.getLogger(__name__)
_VERSION=1


class SLMGeometryOrientationCalibrationStore:
    """One authoritative calibration file per SLM/section/plane."""

    def __init__(self,directory: str | Path) -> None:
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]]=[]

    @staticmethod
    def _serial(identity: SLMIdentity | str) -> str:
        return identity.serial_number if isinstance(identity,SLMIdentity) else str(identity or "").strip()

    def exists(self,identity,section_key,plane_name) -> bool:
        return self._path(identity,section_key,plane_name).is_file()

    def load(self,identity,section_key,plane_name) -> GeometryOrientationCalibration:
        path=self._path(identity,section_key,plane_name)
        if not path.is_file():
            raise FileNotFoundError(
                "Geometry orientation calibration does not exist for %s/%s/%s."
                % (self._serial(identity),section_key,plane_name)
            )
        with path.open("r",encoding="utf-8") as file:
            payload=json.load(file)
        if int(payload.get("version",0)) != _VERSION:
            raise ValueError("Unsupported geometry orientation calibration version")
        calibration=GeometryOrientationCalibration.from_dict(payload["calibration"])
        expected=(self._serial(identity),str(section_key).strip(),str(plane_name).strip())
        actual=(calibration.slm_serial,calibration.section_key,calibration.plane_name)
        if actual != expected:
            raise ValueError("Geometry calibration file scope does not match its path")
        return calibration

    def save(self,calibration: GeometryOrientationCalibration) -> Path:
        if not isinstance(calibration,GeometryOrientationCalibration):
            raise TypeError("calibration must be a GeometryOrientationCalibration")
        path=self._path(calibration.slm_serial,calibration.section_key,calibration.plane_name)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(".json.tmp")
        with temporary.open("w",encoding="utf-8") as file:
            json.dump({"version":_VERSION,"calibration":calibration.to_dict()},file,indent=2,sort_keys=True)
            file.write("\n")
        os.replace(temporary,path)
        self._notify()
        return path

    def delete(self,identity,section_key,plane_name) -> Path:
        path=self._path(identity,section_key,plane_name)
        if not path.is_file():
            raise FileNotFoundError("Geometry orientation calibration does not exist")
        path.unlink(); self._notify(); return path

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:self._listeners.remove(callback)
        except ValueError:pass

    def _path(self,identity,section_key,plane_name) -> Path:
        return self.directory/_slug(self._serial(identity),"SLM serial")/_slug(section_key,"Section")/(_slug(plane_name,"Plane")+".json")

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:callback()
            except Exception:_logger.exception("Geometry-calibration-store listener failed")


def _slug(value: Any,label: str) -> str:
    text=str(value or "").strip().lower()
    text=re.sub(r"[^0-9a-z_ -]","",text)
    text=re.sub(r"[\s-]+","_",text); text=re.sub(r"_+","_",text).strip("_")
    if not text:raise ValueError(f"{label} must contain at least one letter or number")
    return text


__all__=["SLMGeometryOrientationCalibrationStore"]
