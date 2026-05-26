"""Shared path helpers for headless workflow outputs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def default_measurements_root() -> Path:
    """Return the default root directory for workflow measurement files."""
    env_root = os.environ.get("IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    if os.name == "nt":
        return Path("D:/Measurements")
    return Path.home() / "ImSwitchMeasurements"


def resolve_measurements_root(root: Optional[str | Path]) -> Path:
    """Return a usable measurement root for optional string/Path inputs."""
    return Path(root).expanduser() if root else default_measurements_root()
