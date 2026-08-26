from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable


@dataclass(frozen=True)
class ConfigurationPreferences:
    """Host capability for one SLM's startup config selection."""

    get_startup_config: Callable[[], str | None]
    set_startup_config: Callable[[str | None], None]

    def __post_init__(self) -> None:
        if not callable(self.get_startup_config):
            raise TypeError("get_startup_config must be callable")
        if not callable(self.set_startup_config):
            raise TypeError("set_startup_config must be callable")

    def get(self) -> str | None:
        value = self.get_startup_config()
        text = str(value or "").strip()
        return text or None

    def set(self,filename: str | None) -> None:
        value = str(filename or "").strip() or None
        self.set_startup_config(value)


@dataclass(frozen=True)
class SectionViewPreferences:
    """Host capability for setup-level section-view presentation state."""

    get_display_mode: Callable[[],Any]
    set_display_mode: Callable[[Any],None]

    def __post_init__(self) -> None:
        if not callable(self.get_display_mode):
            raise TypeError("get_display_mode must be callable")
        if not callable(self.set_display_mode):
            raise TypeError("set_display_mode must be callable")

    def get(self):
        return self.get_display_mode()

    def set(self,value) -> None:
        self.set_display_mode(value)
