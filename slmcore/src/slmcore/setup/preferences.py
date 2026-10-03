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
class FOVMappingCorrectionPreferences:
    """Persistent default FOV-correction names by mapping."""

    mappings: Mapping[str,str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        planes = {}
        for mapping,value in dict(self.mappings or {}).items():
            mapping_name = str(mapping or "").strip()
            correction_name = str(value or "").strip()
            if mapping_name and correction_name:
                planes[mapping_name] = correction_name
        object.__setattr__(self,"mappings",MappingProxyType(planes))

    def to_dict(self) -> dict[str,Any]:
        return {"mappings":dict(self.mappings)}

    @classmethod
    def from_value(cls,value: Any) -> "FOVMappingCorrectionPreferences":
        if isinstance(value,cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value,Mapping):
            raise TypeError(
                "startup_preferences.fov_mapping_corrections entries must be mappings"
            )
        mappings = value.get("mappings",value) or {}
        if not isinstance(mappings,Mapping):
            raise TypeError(
                "startup_preferences.fov_mapping_corrections.*.mappings must be a mapping"
            )
        return cls(mappings=dict(mappings))


@dataclass(frozen=True)
class FocusedEditingPreferences:
    """Persistent editor visibility selection for one SLM section.

    ``groups`` contains groups rendered atomically in Focused editing.
    ``items`` contains selected registry-backed item keys for groups whose
    editors can be pruned item-by-item.  This is presentation-only state; it
    never carries active/inactive values.
    """

    groups: tuple[str,...] = ()
    items: Mapping[str,tuple[str,...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        groups = []
        seen_groups = set()
        for value in tuple(self.groups or ()):
            key = str(value or "").strip()
            if key and key not in seen_groups:
                groups.append(key)
                seen_groups.add(key)

        items = {}
        for group,value in dict(self.items or {}).items():
            group_key = str(group or "").strip()
            if not group_key:
                continue
            selected = []
            seen_items = set()
            for item in tuple(value or ()):
                item_key = str(item or "").strip()
                if item_key and item_key not in seen_items:
                    selected.append(item_key)
                    seen_items.add(item_key)
            if selected:
                items[group_key] = tuple(selected)

        object.__setattr__(self,"groups",tuple(groups))
        object.__setattr__(self,"items",MappingProxyType(items))

    @property
    def configured(self) -> bool:
        return bool(self.groups or self.items)

    def item_keys(self,group_key: str) -> tuple[str,...]:
        return tuple(self.items.get(str(group_key),()))

    def selects_group(self,group_key: str) -> bool:
        key = str(group_key)
        return key in self.groups or bool(self.items.get(key))

    def to_dict(self) -> dict[str,Any]:
        return {
            "groups":list(self.groups),
            "items":{key:list(value) for key,value in self.items.items()},
        }

    @classmethod
    def from_value(cls,value: Any) -> "FocusedEditingPreferences":
        if isinstance(value,cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value,Mapping):
            raise TypeError(
                "startup_preferences.focused_editing entries must be mappings"
            )
        groups = value.get("groups",()) or ()
        items = value.get("items",{}) or {}
        if isinstance(groups,(str,bytes)) or not isinstance(groups,(list,tuple)):
            raise TypeError(
                "startup_preferences.focused_editing.*.groups must be a sequence"
            )
        if not isinstance(items,Mapping):
            raise TypeError(
                "startup_preferences.focused_editing.*.items must be a mapping"
            )
        return cls(groups=tuple(groups),items=dict(items))


@dataclass(frozen=True)
class SLMStartupPreferences:
    """Persistent defaults applied when constructing an SLM session."""

    startup_config: str | None = None
    default_mappings: Mapping[str,str] = field(default_factory=dict)
    section_display_mode: str = "tabs"
    feedback_orientations: Mapping[str,FeedbackOrientationPreferences] = field(
        default_factory=dict,
    )
    fov_mapping_corrections: Mapping[
        str,FOVMappingCorrectionPreferences
    ] = field(default_factory=dict)
    focused_editing: Mapping[str,FocusedEditingPreferences] = field(
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        startup = str(self.startup_config or "").strip() or None
        planes = {}
        for section,value in dict(self.default_mappings or {}).items():
            section_key = str(section or "").strip()
            mapping_name = str(value or "").strip()
            if section_key and mapping_name:
                planes[section_key] = mapping_name
        display_mode = str(self.section_display_mode or "tabs").strip() or "tabs"
        orientations = {}
        for section,value in dict(self.feedback_orientations or {}).items():
            section_key = str(section or "").strip()
            if section_key:
                orientations[section_key] = FeedbackOrientationPreferences.from_value(
                    value,
                )
        object.__setattr__(self,"startup_config",startup)
        object.__setattr__(self,"default_mappings",MappingProxyType(planes))
        object.__setattr__(self,"section_display_mode",display_mode)
        object.__setattr__(
            self,"feedback_orientations",MappingProxyType(orientations),
        )
        fov_defaults = {}
        for section,value in dict(self.fov_mapping_corrections or {}).items():
            section_key = str(section or "").strip()
            if section_key:
                fov_defaults[section_key] = FOVMappingCorrectionPreferences.from_value(
                    value
                )
        object.__setattr__(
            self,"fov_mapping_corrections",MappingProxyType(fov_defaults),
        )
        focused = {}
        for section,value in dict(self.focused_editing or {}).items():
            section_key = str(section or "").strip()
            if section_key:
                selection = FocusedEditingPreferences.from_value(value)
                if selection.configured:
                    focused[section_key] = selection
        object.__setattr__(
            self,"focused_editing",MappingProxyType(focused),
        )

    def to_dict(self) -> dict[str,Any]:
        return {
            "startup_config":self.startup_config,
            "default_mappings":dict(self.default_mappings),
            "section_display_mode":self.section_display_mode,
            "feedback_orientations":{
                section:value.to_dict()
                for section,value in self.feedback_orientations.items()
            },
            "fov_mapping_corrections":{
                section:value.to_dict()
                for section,value in self.fov_mapping_corrections.items()
            },
            "focused_editing":{
                section:value.to_dict()
                for section,value in self.focused_editing.items()
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
        planes = data.get("default_mappings",{})
        if planes is None:
            planes = {}
        if not isinstance(planes,Mapping):
            raise TypeError("startup_preferences.default_mappings must be a mapping")
        orientations = data.get("feedback_orientations",{}) or {}
        if not isinstance(orientations,Mapping):
            raise TypeError("startup_preferences.feedback_orientations must be a mapping")
        fov_defaults = data.get("fov_mapping_corrections",{}) or {}
        if not isinstance(fov_defaults,Mapping):
            raise TypeError(
                "startup_preferences.fov_mapping_corrections must be a mapping"
            )
        focused = data.get("focused_editing",{}) or {}
        if not isinstance(focused,Mapping):
            raise TypeError(
                "startup_preferences.focused_editing must be a mapping"
            )
        return cls(
            startup_config=data.get("startup_config"),
            default_mappings=dict(planes),
            section_display_mode=data.get("section_display_mode","tabs"),
            feedback_orientations=dict(orientations),
            fov_mapping_corrections=dict(fov_defaults),
            focused_editing=dict(focused),
        )
