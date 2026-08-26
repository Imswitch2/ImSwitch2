from __future__ import annotations

from dataclasses import replace
from typing import Any,Callable

from ...setup import SLMStartupPreferences


class _StartupPreferencesState:
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

    def _commit(self,new_value: SLMStartupPreferences) -> None:
        if new_value == self._value:
            return
        # Persist first. A failing host callback leaves session preference state
        # unchanged so callers can safely roll back the corresponding runtime UI.
        self._on_changed(new_value)
        self._value = new_value
