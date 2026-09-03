from __future__ import annotations

from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping


@dataclass(frozen=True)
class FeedbackOrientationPreferences:
    """Persistent feedback-orientation defaults for one SLM section."""

    default: str = "identity"
    planes: Mapping[str,str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        default = str(self.default or "identity").strip() or "identity"
        planes = {}
        for plane,value in dict(self.planes or {}).items():
            plane_name = str(plane or "").strip()
            orientation = str(value or "identity").strip() or "identity"
            if plane_name:
                planes[plane_name] = orientation
        object.__setattr__(self,"default",default)
        object.__setattr__(self,"planes",MappingProxyType(planes))

    def to_dict(self) -> dict[str,Any]:
        return {
            "default":self.default,
            "planes":dict(self.planes),
        }

    @classmethod
    def from_value(cls,value: Any) -> "FeedbackOrientationPreferences":
        if isinstance(value,cls):
            return value
        # Compatibility with the first pre-release implementation, where
        # feedback_orientations mapped section -> orientation string directly.
        if isinstance(value,str):
            return cls(default=value)
        if value is None:
            return cls()
        if not isinstance(value,Mapping):
            raise TypeError(
                "startup_preferences.feedback_orientations entries must be "
                "orientation strings or mappings"
            )
        planes = value.get("planes",{}) or {}
        if not isinstance(planes,Mapping):
            raise TypeError(
                "startup_preferences.feedback_orientations.*.planes must be a mapping"
            )
        return cls(
            default=value.get("default","identity"),
            planes=dict(planes),
        )


@dataclass(frozen=True)
class FOVPositionCalibrationPreferences:
    """Persistent default FOV calibration names by measurement plane."""

    planes: Mapping[str,str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        planes = {}
        for plane,value in dict(self.planes or {}).items():
            plane_name = str(plane or "").strip()
            calibration_name = str(value or "").strip()
            if plane_name and calibration_name:
                planes[plane_name] = calibration_name
        object.__setattr__(self,"planes",MappingProxyType(planes))

    def to_dict(self) -> dict[str,Any]:
        return {"planes":dict(self.planes)}

    @classmethod
    def from_value(cls,value: Any) -> "FOVPositionCalibrationPreferences":
        if isinstance(value,cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value,Mapping):
            raise TypeError(
                "startup_preferences.fov_position_calibrations entries must be mappings"
            )
        planes = value.get("planes",value) or {}
        if not isinstance(planes,Mapping):
            raise TypeError(
                "startup_preferences.fov_position_calibrations.*.planes must be a mapping"
            )
        return cls(planes=dict(planes))


@dataclass(frozen=True)
class SLMStartupPreferences:
    """Persistent defaults applied when constructing an SLM session."""

    startup_config: str | None = None
    default_planes: Mapping[str,str] = field(default_factory=dict)
    section_display_mode: str = "tabs"
    feedback_orientations: Mapping[str,FeedbackOrientationPreferences] = field(
        default_factory=dict,
    )
    fov_position_calibrations: Mapping[
        str,FOVPositionCalibrationPreferences
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        startup = str(self.startup_config or "").strip() or None
        planes = {}
        for section,value in dict(self.default_planes or {}).items():
            section_key = str(section or "").strip()
            plane_name = str(value or "").strip()
            if section_key and plane_name:
                planes[section_key] = plane_name
        display_mode = str(self.section_display_mode or "tabs").strip() or "tabs"
        orientations = {}
        for section,value in dict(self.feedback_orientations or {}).items():
            section_key = str(section or "").strip()
            if section_key:
                orientations[section_key] = FeedbackOrientationPreferences.from_value(
                    value,
                )
        object.__setattr__(self,"startup_config",startup)
        object.__setattr__(self,"default_planes",MappingProxyType(planes))
        object.__setattr__(self,"section_display_mode",display_mode)
        object.__setattr__(
            self,"feedback_orientations",MappingProxyType(orientations),
        )
        fov_defaults = {}
        for section,value in dict(self.fov_position_calibrations or {}).items():
            section_key = str(section or "").strip()
            if section_key:
                fov_defaults[section_key] = FOVPositionCalibrationPreferences.from_value(
                    value
                )
        object.__setattr__(
            self,"fov_position_calibrations",MappingProxyType(fov_defaults),
        )

    def to_dict(self) -> dict[str,Any]:
        return {
            "startup_config":self.startup_config,
            "default_planes":dict(self.default_planes),
            "section_display_mode":self.section_display_mode,
            "feedback_orientations":{
                section:value.to_dict()
                for section,value in self.feedback_orientations.items()
            },
            "fov_position_calibrations":{
                section:value.to_dict()
                for section,value in self.fov_position_calibrations.items()
            },
        }

    @classmethod
    def from_dict(
        cls,data: Mapping[str,Any] | None,
    ) -> "SLMStartupPreferences":
        if data is None:
            return cls()
        if not isinstance(data,Mapping):
            raise TypeError("startup_preferences must be a mapping")
        planes = data.get("default_planes",{})
        if planes is None:
            planes = {}
        if not isinstance(planes,Mapping):
            raise TypeError("startup_preferences.default_planes must be a mapping")
        orientations = data.get("feedback_orientations",{}) or {}
        if not isinstance(orientations,Mapping):
            raise TypeError("startup_preferences.feedback_orientations must be a mapping")
        fov_defaults = data.get("fov_position_calibrations",{}) or {}
        if not isinstance(fov_defaults,Mapping):
            raise TypeError(
                "startup_preferences.fov_position_calibrations must be a mapping"
            )
        return cls(
            startup_config=data.get("startup_config"),
            default_planes=dict(planes),
            section_display_mode=data.get("section_display_mode","tabs"),
            feedback_orientations=dict(orientations),
            fov_position_calibrations=dict(fov_defaults),
        )
