from __future__ import annotations

import logging
from pathlib import Path
from typing import Any,Callable

from .slm_plane_calibration import (
    add_plane_definition,
    delete_plane_calibration_files,
    empty_plane_definitions,
    load_plane_definitions,
    load_section_calibration,
    plane_slug,
    remove_plane_definition,
    save_plane_definitions,
    save_section_calibration,
)
from .slm_section_calibration import SLMSectionCalibration

_logger = logging.getLogger(__name__)


class SLMCalibrationStore:
    """Shared plane catalog and per-SLM calibration persistence.

    One store instance may be shared by several ``SLMQtSession`` objects.
    Plane-catalog listeners are intentionally lightweight Python callbacks so
    this storage layer remains independent of Qt.
    """

    def __init__(self,directory: Any) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self._listeners = []
        self._definitions = empty_plane_definitions()
        self.last_load_error: Exception | None = None
        self.reload()

    def reload(self) -> None:
        try:
            self._definitions = load_plane_definitions(str(self.directory))
            self.last_load_error = None
        except Exception as error:
            self._definitions = empty_plane_definitions()
            self.last_load_error = error

    @property
    def plane_names(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self._definitions.get("planes",{}))

    def has_plane(self,plane_name: str) -> bool:
        return str(plane_name or "").strip() in self._definitions.get("planes",{})

    def plane_definition(self,plane_name: str) -> dict[str, Any]:
        name = str(plane_name or "").strip()
        try:
            return dict(self._definitions["planes"][name])
        except KeyError as error:
            raise KeyError('Plane "%s" is not defined.' % name) from error

    def add_plane(self,definition: dict[str, Any]) -> str:
        updated = add_plane_definition(self._definitions,dict(definition or {}))
        save_plane_definitions(str(self.directory),updated)
        self._definitions = updated
        slug = plane_slug(definition.get("name"))
        name = next(
            key for key in updated["planes"] if plane_slug(key) == slug
        )
        self._notify()
        return str(name)

    def delete_plane(self,plane_name: str) -> tuple[str, ...]:
        name = str(plane_name or "").strip()
        updated = remove_plane_definition(self._definitions,name)
        save_plane_definitions(str(self.directory),updated)
        deleted = delete_plane_calibration_files(str(self.directory),name)
        self._definitions = updated
        self._notify()
        return tuple(str(path) for path in deleted)

    def load_calibration(
        self,identity: Any,section_key: str,plane_name: str,
    ) -> SLMSectionCalibration:
        definition = self.plane_definition(plane_name)
        serial = identity.serial_number
        calibration = load_section_calibration(
            str(self.directory),serial,section_key,plane_name,
        ).copy()
        calibration.plane = str(plane_name)
        calibration.cam_px_size_um = float(
            definition["detector_pixel_size_um"]
        )
        return calibration

    def save_calibration(
        self,
        identity: Any,
        display_name: str,
        section_key: str,
        plane_name: str,
        calibration: Any,
    ) -> SLMSectionCalibration:
        definition = self.plane_definition(plane_name)
        value = SLMSectionCalibration.from_dict(calibration).copy()
        value.plane = str(plane_name)
        value.cam_px_size_um = float(definition["detector_pixel_size_um"])
        if value.is_valid() and value.section_geometry is None:
            raise ValueError(
                "Valid section calibrations must record section_geometry"
            )
        serial = identity.serial_number
        save_section_calibration(
            str(self.directory),
            str(display_name or getattr(identity,"key",serial)),
            serial,
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
                # Storage mutations have already committed. A presentation or
                # runtime listener must not make the persisted catalog appear
                # to have failed after the fact.
                _logger.exception("SLM calibration-store listener failed")
