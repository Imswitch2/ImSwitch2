from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CalibrationPreferences:
    """Host capability for persisting one SLM's default active planes."""

    get_default_plane: Callable[[str], str | None]
    set_default_plane: Callable[[str, str | None], None]

    def __post_init__(self) -> None:
        if not callable(self.get_default_plane):
            raise TypeError("get_default_plane must be callable")
        if not callable(self.set_default_plane):
            raise TypeError("set_default_plane must be callable")

    def get(self,section_key: str) -> str | None:
        value = self.get_default_plane(str(section_key))
        text = str(value or "").strip()
        return text or None

    def set(self,section_key: str,plane_name: str | None) -> None:
        plane = str(plane_name or "").strip() or None
        self.set_default_plane(str(section_key),plane)
