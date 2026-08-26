from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PreviewPlacement(str,Enum):
    """Placement of the integrated SLM preview inside :class:`SLMPanel`."""

    TOP = "top"
    LEFT = "left"
    RIGHT = "right"
    NONE = "none"

    @classmethod
    def normalize(cls,value) -> "PreviewPlacement":
        if isinstance(value,cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except Exception as error:
            raise ValueError("Unknown preview placement: %r" % (value,)) from error


class PreviewContainer(str,Enum):
    """Presentation container used for an integrated SLM preview."""

    COLLAPSIBLE = "collapsible"
    PLAIN = "plain"

    @classmethod
    def normalize(cls,value) -> "PreviewContainer":
        if isinstance(value,cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except Exception as error:
            raise ValueError("Unknown preview container: %r" % (value,)) from error


@dataclass(frozen=True)
class SLMPanelLayoutPolicy:
    """Host-selected presentation policy for one reusable SLM panel.

    The policy is deliberately presentation-only. It is not part of SLM
    runtime/config state and preview size changes are not persisted.

    ``preview_initial_size`` / ``preview_min_size`` refer to height for a top
    preview and width for a left/right preview.
    """

    preview_placement: PreviewPlacement = PreviewPlacement.TOP
    preview_container: PreviewContainer = PreviewContainer.COLLAPSIBLE
    preview_resizable: bool = True
    preview_initial_size: int = 220
    preview_min_size: int = 100
    highlight_active_section: bool = True

    def __post_init__(self) -> None:
        placement = PreviewPlacement.normalize(self.preview_placement)
        container = PreviewContainer.normalize(self.preview_container)
        initial_size = int(self.preview_initial_size)
        min_size = int(self.preview_min_size)

        if initial_size <= 0:
            raise ValueError("preview_initial_size must be > 0")
        if min_size < 0:
            raise ValueError("preview_min_size must be >= 0")
        if initial_size < min_size:
            raise ValueError(
                "preview_initial_size must be >= preview_min_size"
            )
        if (
            placement not in (PreviewPlacement.TOP,PreviewPlacement.NONE)
            and container == PreviewContainer.COLLAPSIBLE
        ):
            raise ValueError(
                "A collapsible preview is only valid with TOP placement"
            )

        object.__setattr__(self,"preview_placement",placement)
        object.__setattr__(self,"preview_container",container)
        object.__setattr__(self,"preview_resizable",bool(self.preview_resizable))
        object.__setattr__(self,"preview_initial_size",initial_size)
        object.__setattr__(self,"preview_min_size",min_size)
        object.__setattr__(
            self,"highlight_active_section",bool(self.highlight_active_section),
        )


DEFAULT_SLM_PANEL_LAYOUT_POLICY = SLMPanelLayoutPolicy()


__all__ = [
    "DEFAULT_SLM_PANEL_LAYOUT_POLICY",
    "PreviewContainer",
    "PreviewPlacement",
    "SLMPanelLayoutPolicy",
]
