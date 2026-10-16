"""Neutral physical addressing for SLM sections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SLMSectionAddress:
    """Immutable physical identity of one section on one SLM.

    The address intentionally contains only the owning SLM key and section key.
    Observation planes, detectors and future logical/experimental roles are
    separate concepts and must not be folded into this physical identity.
    """

    slm_key: str
    section_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.slm_key,str):
            raise TypeError("slm_key must be a string")
        if not isinstance(self.section_key,str):
            raise TypeError("section_key must be a string")
        slm_key = self.slm_key.strip()
        section_key = self.section_key.strip()
        if not slm_key:
            raise ValueError("slm_key cannot be empty")
        if not section_key:
            raise ValueError("section_key cannot be empty")
        object.__setattr__(self,"slm_key",slm_key)
        object.__setattr__(self,"section_key",section_key)


__all__ = ["SLMSectionAddress"]
