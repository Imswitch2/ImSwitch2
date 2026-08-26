"""Shared section-view display mode primitives."""

from __future__ import annotations

from enum import Enum


class SectionsDisplayMode(str,Enum):
    """UI-only arrangement for a collection of retained section views."""

    TABS = "tabs"
    HORIZONTAL = "horizontal"

    @classmethod
    def normalize(cls,value) -> "SectionsDisplayMode":
        if isinstance(value,cls):
            return value
        text = str(value or "").strip().lower()
        if text in ("tab","tabbed"):
            return cls.TABS
        if text in ("side by side","side-by-side","side_by_side"):
            return cls.HORIZONTAL
        for mode in cls:
            if text in (mode.value,mode.name.lower()):
                return mode
        raise ValueError(f"Unknown sections display mode '{value}'")
