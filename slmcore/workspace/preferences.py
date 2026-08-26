from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..host.calibration import CalibrationPreferences
from ..host.configuration import ConfigurationPreferences,SectionViewPreferences
from ..setup import SLMSetup


class SLMPreferenceStore:
    """JSON-backed persistent preferences shared by an SLM workspace."""

    SCHEMA_VERSION = 1

    def __init__(self,path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._data = self._load()

    def configuration_preferences(self,setup: SLMSetup) -> ConfigurationPreferences:
        serial = self._serial(setup)
        return ConfigurationPreferences(
            get_startup_config=lambda:self.get_startup_config(serial),
            set_startup_config=lambda value:self.set_startup_config(serial,value),
        )

    def calibration_preferences(self,setup: SLMSetup) -> CalibrationPreferences:
        serial = self._serial(setup)
        return CalibrationPreferences(
            get_default_plane=(
                lambda section:self.get_default_plane(serial,section)
            ),
            set_default_plane=(
                lambda section,value:self.set_default_plane(serial,section,value)
            ),
        )

    def section_view_preferences(self,setup: SLMSetup) -> SectionViewPreferences:
        serial = self._serial(setup)
        return SectionViewPreferences(
            get_display_mode=lambda:self.get_section_display_mode(serial),
            set_display_mode=lambda value:self.set_section_display_mode(serial,value),
        )

    def get_startup_config(self,serial_number: str) -> str | None:
        value = self._slm(serial_number).get("startup_config")
        return None if not value else str(value)

    def set_startup_config(self,serial_number: str,value: str | None) -> None:
        entry = self._slm(serial_number)
        if value:
            entry["startup_config"] = str(value)
        else:
            entry.pop("startup_config",None)
        self._save()

    def get_default_plane(
        self,serial_number: str,section_key: str,
    ) -> str | None:
        planes = self._slm(serial_number).get("default_planes",{})
        value = planes.get(str(section_key)) if isinstance(planes,dict) else None
        return None if not value else str(value)

    def set_default_plane(
        self,serial_number: str,section_key: str,value: str | None,
    ) -> None:
        entry = self._slm(serial_number)
        planes = entry.setdefault("default_planes",{})
        if value:
            planes[str(section_key)] = str(value)
        else:
            planes.pop(str(section_key),None)
        if not planes:
            entry.pop("default_planes",None)
        self._save()

    def get_section_display_mode(self,serial_number: str) -> str | None:
        value = self._slm(serial_number).get("section_display_mode")
        return None if not value else str(value)

    def set_section_display_mode(self,serial_number: str,value: Any) -> None:
        entry = self._slm(serial_number)
        normalized = getattr(value,"value",value)
        if normalized:
            entry["section_display_mode"] = str(normalized)
        else:
            entry.pop("section_display_mode",None)
        self._save()

    @staticmethod
    def _serial(setup: SLMSetup) -> str:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        return setup.identity.serial_number

    def _slm(self,serial_number: str) -> dict[str,Any]:
        serial = str(serial_number or "").strip()
        if not serial:
            raise ValueError("serial_number cannot be empty")
        slms = self._data.setdefault("slms",{})
        return slms.setdefault(serial,{})

    def _load(self) -> dict[str,Any]:
        if not self.path.is_file():
            return {"schema_version":self.SCHEMA_VERSION,"slms":{}}
        try:
            with self.path.open("r",encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError,json.JSONDecodeError) as error:
            raise RuntimeError(
                "Could not read SLM workspace preferences from %s" % self.path
            ) from error
        if not isinstance(value,dict):
            raise ValueError("SLM workspace preferences root must be a JSON object")
        version = value.get("schema_version",self.SCHEMA_VERSION)
        if version != self.SCHEMA_VERSION:
            raise ValueError(
                "Unsupported SLM workspace preferences schema version: %r" % version
            )
        slms = value.get("slms",{})
        if not isinstance(slms,dict):
            raise ValueError("SLM workspace preferences 'slms' entry must be an object")
        value["schema_version"] = self.SCHEMA_VERSION
        value["slms"] = slms
        return value

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w",encoding="utf-8") as handle:
            json.dump(self._data,handle,indent=2,sort_keys=True)
            handle.write("\n")
        temporary.replace(self.path)
