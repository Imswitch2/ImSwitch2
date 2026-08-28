from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any,Callable

from ..core.calibration.slm_section_calibration import SLMSectionCalibration


_logger = logging.getLogger(__name__)
_PLANE_DEFINITIONS_FILENAME = "planes.json"
_PLANE_STORE_VERSION = 1
_CALIBRATION_STORE_VERSION = 1


class SLMCalibrationStore:
    """Shared plane catalog and per-SLM calibration persistence."""

    def __init__(self,directory: Any) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners: list[Callable[[],None]] = []
        self._definitions = _empty_plane_definitions()
        self.last_load_error: Exception | None = None
        self.reload()

    def reload(self) -> None:
        try:
            self._definitions = _load_plane_definitions(self.directory)
            self.last_load_error = None
        except Exception as error:
            self._definitions = _empty_plane_definitions()
            self.last_load_error = error

    @property
    def plane_names(self) -> tuple[str,...]:
        return tuple(str(name) for name in self._definitions.get("planes",{}))

    def has_plane(self,plane_name: str) -> bool:
        return str(plane_name or "").strip() in self._definitions.get("planes",{})

    def plane_definition(self,plane_name: str) -> dict[str,Any]:
        name = str(plane_name or "").strip()
        try:
            return dict(self._definitions["planes"][name])
        except KeyError as error:
            raise KeyError(f'Plane "{name}" is not defined.') from error

    def add_plane(self,definition: dict[str,Any]) -> str:
        updated = _add_plane_definition(self._definitions,dict(definition or {}))
        _save_plane_definitions(self.directory,updated)
        self._definitions = updated
        slug = _plane_slug(definition.get("name"))
        name = next(key for key in updated["planes"] if _plane_slug(key) == slug)
        self._notify()
        return str(name)

    def delete_plane(self,plane_name: str) -> tuple[str,...]:
        name = str(plane_name or "").strip()
        updated = _remove_plane_definition(self._definitions,name)
        _save_plane_definitions(self.directory,updated)
        deleted = _delete_plane_calibration_files(self.directory,name)
        self._definitions = updated
        self._notify()
        return tuple(str(path) for path in deleted)

    def load_calibration(
        self,identity: Any,section_key: str,plane_name: str,
    ) -> SLMSectionCalibration:
        definition = self.plane_definition(plane_name)
        calibration = _load_section_calibration(
            self.directory,identity.serial_number,section_key,plane_name,
        ).copy()
        calibration.plane = str(plane_name)
        calibration.cam_px_size_um = float(definition["detector_pixel_size_um"])
        return calibration

    def save_calibration(
        self,
        identity: Any,
        section_key: str,
        plane_name: str,
        calibration: Any,
    ) -> SLMSectionCalibration:
        definition = self.plane_definition(plane_name)
        value = SLMSectionCalibration.from_dict(calibration).copy()
        value.plane = str(plane_name)
        value.cam_px_size_um = float(definition["detector_pixel_size_um"])
        if value.is_valid() and value.section_geometry is None:
            raise ValueError("Valid section calibrations must record section_geometry")
        _save_section_calibration(
            self.directory,
            str(getattr(identity,"display_name",None) or identity.key),
            identity.serial_number,
            section_key,
            plane_name,
            value,
        )
        return value

    def add_listener(self,callback: Callable[[],None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:
                _logger.exception("SLM calibration-store listener failed")


def _empty_plane_definitions() -> dict:
    return {"version":_PLANE_STORE_VERSION,"planes":{}}


def _plane_slug(plane_name: Any) -> str:
    text = str(plane_name or "").strip().lower()
    text = re.sub(r"[^0-9a-z_ -]","",text)
    text = re.sub(r"[\s-]+","_",text)
    text = re.sub(r"_+","_",text).strip("_")
    if not text:
        raise ValueError("Plane name must contain at least one letter or number.")
    return text


def _normalize_plane_definition(definition: dict) -> dict:
    if not isinstance(definition,dict):
        raise ValueError("Plane definition must be a dictionary.")
    name = str(definition.get("name","")).strip()
    detector_name = str(definition.get("detector_name","")).strip()
    description = str(definition.get("description","") or "").strip()
    if not name:
        raise ValueError("Plane name is required.")
    if not detector_name:
        raise ValueError("Detector name is required.")
    try:
        detector_pixel_size_um = float(definition.get("detector_pixel_size_um"))
    except Exception as error:
        raise ValueError("Detector pixel size must be a number.") from error
    if detector_pixel_size_um <= 0.0:
        raise ValueError("Detector pixel size must be > 0.")
    return {
        "name":name,
        "detector_name":detector_name,
        "detector_pixel_size_um":detector_pixel_size_um,
        "description":description,
        "created_at":str(definition.get("created_at") or datetime.now().isoformat()),
    }


def _load_plane_definitions(directory: Path) -> dict:
    path = directory/_PLANE_DEFINITIONS_FILENAME
    if not path.is_file():
        return _empty_plane_definitions()
    with path.open("r",encoding="utf-8") as file:
        data = json.load(file)
    planes = data.get("planes",{}) if isinstance(data,dict) else {}
    if not isinstance(planes,dict):
        raise ValueError("Plane definitions file must contain a 'planes' object.")
    normalized = {}
    seen_slugs = {}
    for key,definition in planes.items():
        definition = dict(definition or {})
        definition.setdefault("name",key)
        plane = _normalize_plane_definition(definition)
        slug = _plane_slug(plane["name"])
        if slug in seen_slugs:
            raise ValueError(
                f'Plane "{plane["name"]}" conflicts with "{seen_slugs[slug]}".'
            )
        seen_slugs[slug] = plane["name"]
        normalized[plane["name"]] = plane
    return {"version":_PLANE_STORE_VERSION,"planes":normalized}


def _save_plane_definitions(directory: Path,definitions: dict) -> None:
    _write_json(directory/_PLANE_DEFINITIONS_FILENAME,definitions)


def _add_plane_definition(definitions: dict,definition: dict) -> dict:
    data = {
        "version":_PLANE_STORE_VERSION,
        "planes":dict((definitions or {}).get("planes",{}) or {}),
    }
    plane = _normalize_plane_definition(definition)
    name,slug = plane["name"],_plane_slug(plane["name"])
    if name in data["planes"]:
        raise ValueError(f'Plane "{name}" already exists.')
    for existing_name in data["planes"]:
        if _plane_slug(existing_name) == slug:
            raise ValueError(
                f'Plane "{name}" conflicts with existing plane "{existing_name}".'
            )
    data["planes"][name] = plane
    return data


def _remove_plane_definition(definitions: dict,plane_name: str) -> dict:
    name = str(plane_name or "").strip()
    data = {
        "version":_PLANE_STORE_VERSION,
        "planes":dict((definitions or {}).get("planes",{}) or {}),
    }
    if name not in data["planes"]:
        raise KeyError(f'Plane "{name}" does not exist.')
    del data["planes"][name]
    return data


def _calibration_file_path(
    directory: Path,slm_serial: str,section_key: str,plane_name: str,
) -> Path:
    return directory/str(slm_serial)/str(section_key)/(f"{_plane_slug(plane_name)}.json")


def _save_section_calibration(
    directory: Path,
    slm_name: str,
    slm_serial: str,
    section_key: str,
    plane_name: str,
    calibration: Any,
) -> Path:
    calibration = SLMSectionCalibration.from_dict(calibration)
    if not calibration.created_at:
        calibration.created_at = datetime.now().isoformat()
    path = _calibration_file_path(directory,slm_serial,section_key,plane_name)
    _write_json(path,{
        "version":_CALIBRATION_STORE_VERSION,
        "slm_name":str(slm_name),
        "slm_serial":str(slm_serial),
        "section":str(section_key),
        "plane_name":str(plane_name),
        "created_at":calibration.created_at,
        "calibration":calibration.to_dict(),
    })
    return path


def _load_section_calibration(
    directory: Path,slm_serial: str,section_key: str,plane_name: str,
) -> SLMSectionCalibration:
    path = _calibration_file_path(directory,slm_serial,section_key,plane_name)
    if not path.is_file():
        return SLMSectionCalibration()
    with path.open("r",encoding="utf-8") as file:
        payload = json.load(file)
    return SLMSectionCalibration.from_dict(payload)


def _delete_plane_calibration_files(directory: Path,plane_name: str) -> list[Path]:
    filename = f"{_plane_slug(plane_name)}.json"
    deleted: list[Path] = []
    if not directory.is_dir():
        return deleted
    for root,_dirs,files in os.walk(directory):
        root_path = Path(root)
        try:
            relative = root_path.relative_to(directory)
        except ValueError:
            continue
        if len(relative.parts) < 2 or filename not in files:
            continue
        path = root_path/filename
        path.unlink()
        deleted.append(path)
    return deleted


def _write_json(path: Path,data: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w",encoding="utf-8") as file:
        json.dump(data,file,indent=2,sort_keys=True)
        file.write("\n")
    os.replace(tmp_path,path)


__all__ = ["SLMCalibrationStore"]
