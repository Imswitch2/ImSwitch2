from __future__ import annotations

from dataclasses import replace
from typing import Any,Callable

from ..setup import SLMStartupPreferences


class StartupPreferencesState:
    """Session-local preference state with one persistence callback."""

    def __init__(
        self,
        preferences: SLMStartupPreferences,
        on_changed: Callable[[SLMStartupPreferences],None],
    ) -> None:
        if not isinstance(preferences,SLMStartupPreferences):
            raise TypeError("preferences must be an SLMStartupPreferences")
        if not callable(on_changed):
            raise TypeError("on_changed must be callable")
        self._value = preferences
        self._on_changed = on_changed

    @property
    def value(self) -> SLMStartupPreferences:
        return self._value

    def startup_config(self) -> str | None:
        return self._value.startup_config

    def set_startup_config(self,filename: str | None) -> None:
        value = str(filename or "").strip() or None
        self._commit(replace(self._value,startup_config=value))

    def default_mapping(self,section_key: str) -> str | None:
        return self._value.default_mappings.get(str(section_key))

    def set_default_mapping(
        self,section_key: str,mapping_name: str | None,
    ) -> None:
        section = str(section_key)
        mapping = str(mapping_name or "").strip() or None
        mappings = dict(self._value.default_mappings)
        if mapping is None:
            mappings.pop(section,None)
        else:
            mappings[section] = mapping
        self._commit(replace(self._value,default_mappings=mappings))

    def section_display_mode(self) -> str:
        return self._value.section_display_mode

    def set_section_display_mode(self,value: Any) -> None:
        normalized = getattr(value,"value",value)
        mode = str(normalized or "tabs").strip() or "tabs"
        self._commit(replace(self._value,section_display_mode=mode))


    def focused_editing(self,section_key: str):
        from ..setup import FocusedEditingPreferences
        return self._value.focused_editing.get(
            str(section_key),FocusedEditingPreferences(),
        )

    def set_focused_editing(self,section_key: str,value: Any) -> None:
        from ..setup import FocusedEditingPreferences
        section = str(section_key)
        selection = FocusedEditingPreferences.from_value(value)
        focused = dict(self._value.focused_editing)
        if selection.configured:
            focused[section] = selection
        else:
            focused.pop(section,None)
        self._commit(replace(self._value,focused_editing=focused))

    def feedback_orientation_default(self,section_key: str) -> str:
        settings = self._value.feedback_orientations.get(str(section_key))
        return "identity" if settings is None else settings.default

    def feedback_orientation_for_observation_plane(
        self,section_key: str,observation_plane_name: str | None,
    ) -> str | None:
        plane = str(observation_plane_name or "").strip()
        if not plane:
            return None
        settings = self._value.feedback_orientations.get(str(section_key))
        if settings is None:
            return None
        return settings.planes.get(plane)

    def feedback_orientation(
        self,section_key: str,observation_plane_name: str | None=None,
    ) -> str:
        override = self.feedback_orientation_for_observation_plane(section_key,observation_plane_name)
        if override is not None:
            return override
        return self.feedback_orientation_default(section_key)

    def set_feedback_orientation_default(
        self,section_key: str,value: Any,
    ) -> None:
        from ..core.cgh.feedback import FeedbackOrientation
        from ..setup import FeedbackOrientationPreferences
        section = str(section_key)
        orientation = FeedbackOrientation.normalize(value).value
        orientations = dict(self._value.feedback_orientations)
        previous = orientations.get(section,FeedbackOrientationPreferences())
        orientations[section] = FeedbackOrientationPreferences(
            default=orientation,planes=previous.planes,
        )
        self._commit(replace(self._value,feedback_orientations=orientations))

    def set_feedback_orientation_for_observation_plane(
        self,section_key: str,observation_plane_name: str,value: Any,
    ) -> None:
        from ..core.cgh.feedback import FeedbackOrientation
        from ..setup import FeedbackOrientationPreferences
        section = str(section_key)
        plane = str(observation_plane_name or "").strip()
        if not plane:
            raise ValueError("observation_plane_name must not be empty")
        orientation = FeedbackOrientation.normalize(value).value
        orientations = dict(self._value.feedback_orientations)
        previous = orientations.get(section,FeedbackOrientationPreferences())
        planes = dict(previous.planes)
        planes[plane] = orientation
        orientations[section] = FeedbackOrientationPreferences(
            default=previous.default,planes=planes,
        )
        self._commit(replace(self._value,feedback_orientations=orientations))

    def clear_feedback_orientation_for_observation_plane(
        self,section_key: str,observation_plane_name: str,
    ) -> None:
        from ..setup import FeedbackOrientationPreferences
        section = str(section_key)
        plane = str(observation_plane_name or "").strip()
        if not plane:
            return
        orientations = dict(self._value.feedback_orientations)
        previous = orientations.get(section)
        if previous is None or plane not in previous.planes:
            return
        planes = dict(previous.planes)
        planes.pop(plane,None)
        orientations[section] = FeedbackOrientationPreferences(
            default=previous.default,planes=planes,
        )
        self._commit(replace(self._value,feedback_orientations=orientations))

    def default_fov_correction(
        self,section_key: str,mapping_name: str | None,
    ) -> str | None:
        mapping = str(mapping_name or "").strip()
        if not mapping:
            return None
        settings = self._value.fov_mapping_corrections.get(str(section_key))
        if settings is None:
            return None
        return settings.mappings.get(mapping)

    def set_default_fov_correction(
        self,section_key: str,mapping_name: str,name: str | None,
    ) -> None:
        from ..setup import FOVMappingCorrectionPreferences
        section = str(section_key)
        mapping = str(mapping_name or "").strip()
        if not mapping:
            raise ValueError("mapping_name must not be empty")
        correction_name = str(name or "").strip() or None
        values = dict(self._value.fov_mapping_corrections)
        previous = values.get(section,FOVMappingCorrectionPreferences())
        mappings = dict(previous.mappings)
        if correction_name is None:
            mappings.pop(mapping,None)
        else:
            mappings[mapping] = correction_name
        if mappings:
            values[section] = FOVMappingCorrectionPreferences(mappings=mappings)
        else:
            values.pop(section,None)
        self._commit(replace(self._value,fov_mapping_corrections=values))

    def remove_mapping(self,mapping_name: str) -> None:
        """Remove all startup/default state owned by one mapping profile."""
        from ..setup import FOVMappingCorrectionPreferences
        mapping = str(mapping_name or "").strip()
        if not mapping:
            raise ValueError("mapping_name must not be empty")

        default_mappings = {
            section:value for section,value in self._value.default_mappings.items()
            if str(value or "").strip() != mapping
        }
        fov_values = {}
        for section,settings in self._value.fov_mapping_corrections.items():
            mappings = dict(settings.mappings)
            mappings.pop(mapping,None)
            if mappings:
                fov_values[section] = FOVMappingCorrectionPreferences(mappings=mappings)

        self._commit(replace(
            self._value,
            default_mappings=default_mappings,
            fov_mapping_corrections=fov_values,
        ))

    def _commit(self,new_value: SLMStartupPreferences) -> None:
        if new_value == self._value:
            return
        # Persist first. A failing host callback leaves session preference state
        # unchanged so callers can safely roll back the corresponding runtime UI.
        self._on_changed(new_value)
        self._value = new_value
