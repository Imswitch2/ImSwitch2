from __future__ import annotations

from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping


@dataclass(frozen=True)
class SLMStartupPreferences:
    """Persistent defaults applied when constructing an SLM session."""

    startup_config: str | None = None
    default_planes: Mapping[str,str] = field(default_factory=dict)
    section_display_mode: str = "tabs"
    feedback_orientations: Mapping[str,str] = field(default_factory=dict)

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
            orientation = str(value or "identity").strip() or "identity"
            if section_key:
                orientations[section_key] = orientation
        object.__setattr__(self,"startup_config",startup)
        object.__setattr__(self,"default_planes",MappingProxyType(planes))
        object.__setattr__(self,"section_display_mode",display_mode)
        object.__setattr__(self,"feedback_orientations",MappingProxyType(orientations))

    def to_dict(self) -> dict[str,Any]:
        return {
            "startup_config":self.startup_config,
            "default_planes":dict(self.default_planes),
            "section_display_mode":self.section_display_mode,
            "feedback_orientations":dict(self.feedback_orientations),
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
        return cls(
            startup_config=data.get("startup_config"),
            default_planes=dict(planes),
            section_display_mode=data.get("section_display_mode","tabs"),
            feedback_orientations=dict(orientations),
        )
