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

    def default_plane(self,section_key: str) -> str | None:
        return self._value.default_planes.get(str(section_key))

    def set_default_plane(
        self,section_key: str,plane_name: str | None,
    ) -> None:
        section = str(section_key)
        plane = str(plane_name or "").strip() or None
        planes = dict(self._value.default_planes)
        if plane is None:
            planes.pop(section,None)
        else:
            planes[section] = plane
        self._commit(replace(self._value,default_planes=planes))

    def section_display_mode(self) -> str:
        return self._value.section_display_mode

    def set_section_display_mode(self,value: Any) -> None:
        normalized = getattr(value,"value",value)
        mode = str(normalized or "tabs").strip() or "tabs"
        self._commit(replace(self._value,section_display_mode=mode))


    def feedback_orientation_default(self,section_key: str) -> str:
        settings = self._value.feedback_orientations.get(str(section_key))
        return "identity" if settings is None else settings.default

    def feedback_orientation_for_plane(
        self,section_key: str,plane_name: str | None,
    ) -> str | None:
        plane = str(plane_name or "").strip()
        if not plane:
            return None
        settings = self._value.feedback_orientations.get(str(section_key))
        if settings is None:
            return None
        return settings.planes.get(plane)

    def feedback_orientation(
        self,section_key: str,plane_name: str | None=None,
    ) -> str:
        override = self.feedback_orientation_for_plane(section_key,plane_name)
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

    def set_feedback_orientation_for_plane(
        self,section_key: str,plane_name: str,value: Any,
    ) -> None:
        from ..core.cgh.feedback import FeedbackOrientation
        from ..setup import FeedbackOrientationPreferences
        section = str(section_key)
        plane = str(plane_name or "").strip()
        if not plane:
            raise ValueError("plane_name must not be empty")
        orientation = FeedbackOrientation.normalize(value).value
        orientations = dict(self._value.feedback_orientations)
        previous = orientations.get(section,FeedbackOrientationPreferences())
        planes = dict(previous.planes)
        planes[plane] = orientation
        orientations[section] = FeedbackOrientationPreferences(
            default=previous.default,planes=planes,
        )
        self._commit(replace(self._value,feedback_orientations=orientations))

    def clear_feedback_orientation_for_plane(
        self,section_key: str,plane_name: str,
    ) -> None:
        from ..setup import FeedbackOrientationPreferences
        section = str(section_key)
        plane = str(plane_name or "").strip()
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

    # Compatibility helper for the first pre-release section-only behavior.
    def set_feedback_orientation(self,section_key: str,value: Any) -> None:
        self.set_feedback_orientation_default(section_key,value)

    def _commit(self,new_value: SLMStartupPreferences) -> None:
        if new_value == self._value:
            return
        # Persist first. A failing host callback leaves session preference state
        # unchanged so callers can safely roll back the corresponding runtime UI.
        self._on_changed(new_value)
        self._value = new_value
