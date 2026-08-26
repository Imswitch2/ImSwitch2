
"""Definitions describing an SLM device's identity and geometry."""

from __future__ import annotations

from dataclasses import dataclass,field
from typing import Mapping,Any

@dataclass(frozen=True)
class SLMIdentity:
    key: str
    serial_number: str
    display_name: str | None = field(default=None,compare=False)

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        serial = str(self.serial_number or "").strip()
        display_name = str(self.display_name or "").strip() or None
        if not key:
            raise ValueError("SLM key cannot be empty")
        if not serial:
            raise ValueError("SLM serial_number cannot be empty")
        object.__setattr__(self,"key",key)
        object.__setattr__(self,"serial_number",serial)
        object.__setattr__(self,"display_name",display_name)

    def to_dict(self):
        return {
            "key":self.key,
            "serial_number":self.serial_number,
            "display_name":self.display_name,
        }

    @classmethod
    def from_dict(
        cls,data: Mapping[str,Any],*,key: str | None=None,
    ) -> SLMIdentity:
        return cls(
            key=data.get("key") if key is None else key,
            serial_number=data["serial_number"],
            display_name=data.get("display_name"),
        )


@dataclass(frozen=True)
class SLMGeometry:
    width: int
    height: int
    pixel_size_um: float

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("SLM width and height must be > 0")
        if self.pixel_size_um <= 0:
            raise ValueError("SLM pixel_size_um must be > 0")

    @property
    def shape(self) -> tuple[int, int]:
        return self.height,self.width

    def to_dict(self):
        return {
            "width":self.width,
            "height":self.height,
            "pixel_size_um":self.pixel_size_um,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> SLMGeometry:
        return cls(
            width=int(data["width"]),
            height=int(data["height"]),
            pixel_size_um=float(data["pixel_size_um"]),
        )