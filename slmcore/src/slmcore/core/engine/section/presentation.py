from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping


@dataclass(frozen=True)
class SectionPresentation:
    """Persisted non-numerical presentation preferences for one section."""

    show_calibration_interface: bool = True
    title: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.show_calibration_interface,bool):
            raise TypeError(
                "show_calibration_interface must be a boolean"
            )
        if self.title is not None and not isinstance(self.title,str):
            raise TypeError("title must be a string or None")
        if isinstance(self.title,str):
            normalized = self.title.strip()
            object.__setattr__(
                self,"title",normalized if normalized else None,
            )

    def to_dict(self):
        data = {
            "show_calibration_interface":self.show_calibration_interface,
        }
        if self.title is not None:
            data["title"] = self.title
        return data

    @classmethod
    def from_dict(
        cls,data: Mapping[str, Any] | None,
    ) -> "SectionPresentation":
        if data is None:
            return cls()
        if not isinstance(data,Mapping):
            raise TypeError(
                "Section presentation must be a mapping or None"
            )

        show_calibration_interface = data.get(
            "show_calibration_interface",True,
        )
        if not isinstance(show_calibration_interface,bool):
            raise TypeError(
                "show_calibration_interface must be a boolean"
            )
        title = data.get("title")
        if title is not None and not isinstance(title,str):
            raise TypeError("title must be a string or None")

        return cls(
            show_calibration_interface=show_calibration_interface,
            title=title,
        )

    def copy(self) -> "SectionPresentation":
        return self
