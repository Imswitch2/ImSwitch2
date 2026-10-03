from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from ..core.calibration.slm_section_calibration import SLMSectionCalibration
from .mapping_store import mapping_slug

_CALIBRATION_STORE_VERSION = 2


class SLMCalibrationStore:
    """Per-SLM/section mapping-calibration persistence."""

    def __init__(self,directory: Any) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)

    def load_calibration(
        self,identity: Any,section_key: str,mapping_name: str,
    ) -> SLMSectionCalibration:
        value = _load_section_calibration(
            self.directory,identity.serial_number,section_key,mapping_name,
        ).copy()
        value.mapping_name = str(mapping_name)
        return value

    def delete_calibration(
        self,identity: Any,section_key: str,mapping_name: str,
    ) -> str | None:
        path = _calibration_file_path(
            self.directory,identity.serial_number,section_key,mapping_name,
        )
        if not path.is_file():
            return None
        path.unlink()
        return str(path)

    def save_calibration(
        self,
        identity: Any,
        section_key: str,
        mapping_name: str,
        calibration: Any,
    ) -> SLMSectionCalibration:
        value = SLMSectionCalibration.from_dict(calibration).copy()
        value.mapping_name = str(mapping_name)
        if value.is_valid() and value.section_geometry is None:
            raise ValueError("Valid section calibrations must record section_geometry")
        _save_section_calibration(
            self.directory,
            str(getattr(identity,"display_name",None) or identity.key),
            identity.serial_number,
            section_key,
            mapping_name,
            value,
        )
        return value

    def delete_mapping_calibrations(self,mapping_name: str) -> tuple[str,...]:
        deleted = _delete_mapping_calibration_files(self.directory,mapping_name)
        return tuple(str(path) for path in deleted)


def _calibration_file_path(
    directory: Path,slm_serial: str,section_key: str,mapping_name: str,
) -> Path:
    return (
        directory/str(slm_serial)/str(section_key)/
        (f"{mapping_slug(mapping_name)}.json")
    )


def _save_section_calibration(
    directory: Path,
    slm_name: str,
    slm_serial: str,
    section_key: str,
    mapping_name: str,
    calibration: Any,
) -> Path:
    calibration = SLMSectionCalibration.from_dict(calibration)
    if not calibration.created_at:
        calibration.created_at = datetime.now().isoformat()
    path = _calibration_file_path(directory,slm_serial,section_key,mapping_name)
    _write_json(path,{
        "version":_CALIBRATION_STORE_VERSION,
        "slm_name":str(slm_name),
        "slm_serial":str(slm_serial),
        "section":str(section_key),
        "mapping_name":str(mapping_name),
        "created_at":calibration.created_at,
        "calibration":calibration.to_dict(),
    })
    return path


def _load_section_calibration(
    directory: Path,slm_serial: str,section_key: str,mapping_name: str,
) -> SLMSectionCalibration:
    path = _calibration_file_path(directory,slm_serial,section_key,mapping_name)
    if not path.is_file():
        return SLMSectionCalibration()
    with path.open("r",encoding="utf-8") as file:
        payload = json.load(file)
    if int(payload.get("version",0)) != _CALIBRATION_STORE_VERSION:
        raise ValueError("Unsupported mapping-calibration store version")
    return SLMSectionCalibration.from_dict(payload)


def _delete_mapping_calibration_files(
    directory: Path,mapping_name: str,
) -> list[Path]:
    filename = f"{mapping_slug(mapping_name)}.json"
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
