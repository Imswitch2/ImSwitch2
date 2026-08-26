from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SLMWorkspaceLayout:
    """Filesystem layout used by :class:`SLMWorkspace`."""

    configs: str | Path = "configs"
    corrections: str | Path = "corrections"
    calibrations: str | Path = "calibrations"
    preferences: str | Path = "preferences.json"
