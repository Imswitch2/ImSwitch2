from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping

from ..engine.section.geometry import SectionGeometry


_GEOMETRY_FIELDS = ("key","x","y","width","height")


def section_geometry_to_dict(geometry: SectionGeometry) -> dict[str, Any]:
    if not isinstance(geometry,SectionGeometry):
        raise TypeError("geometry must be a SectionGeometry")
    return {
        "key":geometry.key,
        "x":int(geometry.x),
        "y":int(geometry.y),
        "width":int(geometry.width),
        "height":int(geometry.height),
    }


def normalize_section_geometry_data(
    data: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if data is None:
        return None
    if not isinstance(data,Mapping):
        raise TypeError("section_geometry must be a mapping or None")
    missing = [name for name in _GEOMETRY_FIELDS if name not in data]
    if missing:
        raise ValueError(
            "section_geometry is missing: " + ", ".join(missing)
        )
    geometry = SectionGeometry(
        key=str(data["key"]),
        x=int(data["x"]),
        y=int(data["y"]),
        width=int(data["width"]),
        height=int(data["height"]),
    )
    return section_geometry_to_dict(geometry)


def calibration_geometry_data(calibration: Any) -> dict[str, Any] | None:
    if calibration is None:
        return None
    return normalize_section_geometry_data(
        getattr(calibration,"section_geometry",None)
    )


def calibration_geometry_matches(
    calibration: Any,geometry: SectionGeometry,
) -> bool:
    if calibration is None:
        return True
    validator = getattr(calibration,"is_valid",None)
    if callable(validator) and not validator():
        return True
    recorded = calibration_geometry_data(calibration)
    if recorded is None:
        return False
    return recorded == section_geometry_to_dict(geometry)


def attach_calibration_geometry(calibration: Any,geometry: SectionGeometry):
    if calibration is None:
        return None
    copier = getattr(calibration,"copy",None)
    value = copier() if callable(copier) else calibration
    value.section_geometry = section_geometry_to_dict(geometry)
    return value


@dataclass(frozen=True)
class CalibrationGeometryMismatch:
    section_key: str
    plane_name: str | None
    calibration_geometry: Mapping[str,Any]
    section_geometry: Mapping[str,Any]

    def summary(self) -> str:
        old = self.calibration_geometry
        new = self.section_geometry
        return (
            "%s%s: x=%s, y=%s, %sx%s -> x=%s, y=%s, %sx%s"
            % (
                self.section_key,
                " / %s" % self.plane_name if self.plane_name else "",
                old.get("x"),old.get("y"),old.get("width"),old.get("height"),
                new.get("x"),new.get("y"),new.get("width"),new.get("height"),
            )
        )


def calibration_geometry_mismatches(
    section_items,
) -> tuple[CalibrationGeometryMismatch, ...]:
    """Return mismatches for ``(key, geometry, calibration)`` triples."""
    mismatches = []
    for section_key,geometry,calibration in section_items:
        if calibration is None:
            continue
        validator = getattr(calibration,"is_valid",None)
        if callable(validator) and not validator():
            continue
        if calibration_geometry_matches(calibration,geometry):
            continue
        recorded = calibration_geometry_data(calibration)
        if recorded is None:
            raise ValueError(
                "Valid calibration for %s must record section_geometry"
                % section_key
            )
        mismatches.append(CalibrationGeometryMismatch(
            section_key=str(section_key),
            plane_name=(
                str(getattr(calibration,"plane",None))
                if getattr(calibration,"plane",None) else None
            ),
            calibration_geometry=recorded,
            section_geometry=section_geometry_to_dict(geometry),
        ))
    return tuple(mismatches)


def config_calibration_geometry_mismatches(config):
    return calibration_geometry_mismatches(
        (key,section.geometry,section.calibration)
        for key,section in config.sections.items()
    )
