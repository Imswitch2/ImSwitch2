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

    def __post_init__(self) -> None:
        startup = str(self.startup_config or "").strip() or None
        planes = {}
        for section,value in dict(self.default_planes or {}).items():
            section_key = str(section or "").strip()
            plane_name = str(value or "").strip()
            if section_key and plane_name:
                planes[section_key] = plane_name
        display_mode = str(self.section_display_mode or "tabs").strip() or "tabs"
        object.__setattr__(self,"startup_config",startup)
        object.__setattr__(self,"default_planes",MappingProxyType(planes))
        object.__setattr__(self,"section_display_mode",display_mode)

    def to_dict(self) -> dict[str,Any]:
        return {
            "startup_config":self.startup_config,
            "default_planes":dict(self.default_planes),
            "section_display_mode":self.section_display_mode,
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
        return cls(
            startup_config=data.get("startup_config"),
            default_planes=dict(planes),
            section_display_mode=data.get("section_display_mode","tabs"),
        )
