"""Persistent SLM/section/observation-plane registration calibrations."""

from __future__ import annotations

import json,logging,os,re
from pathlib import Path
from typing import Any,Callable

from ..core.engine.device import SLMIdentity
from ..core.measurement.observation_registration import ObservationRegistrationCalibration

_logger=logging.getLogger(__name__)
_VERSION=1


class ObservationRegistrationStore:
    """One native-to-canonical registration file per SLM/section/observation plane."""

    def __init__(self,directory: str | Path) -> None:
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]]=[]

    @staticmethod
    def _serial(identity: SLMIdentity | str) -> str:
        return identity.serial_number if isinstance(identity,SLMIdentity) else str(identity or "").strip()

    def exists(self,identity,section_key,observation_plane_name) -> bool:
        return self._path(identity,section_key,observation_plane_name).is_file()

    def load(self,identity,section_key,observation_plane_name) -> ObservationRegistrationCalibration:
        path=self._path(identity,section_key,observation_plane_name)
        if not path.is_file():
            raise FileNotFoundError(
                "Observation registration does not exist for %s/%s/%s."
                % (self._serial(identity),section_key,observation_plane_name)
            )
        with path.open("r",encoding="utf-8") as file:
            payload=json.load(file)
        if int(payload.get("version",0))!=_VERSION:
            raise ValueError("Unsupported observation-registration version")
        calibration=ObservationRegistrationCalibration.from_dict(payload["calibration"])
        expected=(
            self._serial(identity),str(section_key).strip(),str(observation_plane_name).strip(),
        )
        if calibration.native_frame.scope!=expected:
            raise ValueError("Observation-registration file scope does not match its path")
        return calibration

    def registrations(
        self,observation_plane_name: str | None=None,
    ) -> tuple[ObservationRegistrationCalibration,...]:
        """Return persisted registrations, optionally limited to one observation plane.

        The scan is workspace-wide rather than tied to one SLM because observation
        registration may explicitly bridge sections belonging to different SLMs.
        """
        plane=None if observation_plane_name is None else str(observation_plane_name or "").strip()
        if observation_plane_name is not None and not plane:
            raise ValueError("Observation plane is required")
        calibrations=[]
        for path in sorted(self.directory.glob("*/*/*.json")):
            with path.open("r",encoding="utf-8") as file:
                payload=json.load(file)
            if int(payload.get("version",0))!=_VERSION:
                raise ValueError("Unsupported observation-registration version")
            calibration=ObservationRegistrationCalibration.from_dict(payload["calibration"])
            native=calibration.native_frame
            if self._path(native.slm_serial,native.section_key,native.observation_plane_name)!=path:
                raise ValueError("Observation-registration file scope does not match its path")
            if plane is None or native.observation_plane_name==plane:
                calibrations.append(calibration)
        return tuple(calibrations)

    def save(self,calibration: ObservationRegistrationCalibration) -> Path:
        if not isinstance(calibration,ObservationRegistrationCalibration):
            raise TypeError("calibration must be an ObservationRegistrationCalibration")
        native=calibration.native_frame
        path=self._path(native.slm_serial,native.section_key,native.observation_plane_name)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(".json.tmp")
        with temporary.open("w",encoding="utf-8") as file:
            json.dump(
                {"version":_VERSION,"calibration":calibration.to_dict()},
                file,indent=2,sort_keys=True,
            )
            file.write("\n")
        os.replace(temporary,path)
        self._notify()
        return path

    def delete(self,identity,section_key,observation_plane_name) -> Path:
        path=self._path(identity,section_key,observation_plane_name)
        if not path.is_file():
            raise FileNotFoundError("Observation registration does not exist")
        path.unlink()
        self._notify()
        return path

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:self._listeners.remove(callback)
        except ValueError:pass

    def _path(self,identity,section_key,observation_plane_name) -> Path:
        return (
            self.directory
            / _slug(self._serial(identity),"SLM serial")
            / _slug(section_key,"Section")
            / (_slug(observation_plane_name,"Plane")+".json")
        )

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:callback()
            except Exception:_logger.exception("Observation-registration-store listener failed")


def _slug(value: Any,label: str) -> str:
    text=str(value or "").strip().lower()
    text=re.sub(r"[^0-9a-z_ -]","",text)
    text=re.sub(r"[\s-]+","_",text); text=re.sub(r"_+","_",text).strip("_")
    if not text:raise ValueError(f"{label} must contain at least one letter or number")
    return text


__all__=["ObservationRegistrationStore"]
