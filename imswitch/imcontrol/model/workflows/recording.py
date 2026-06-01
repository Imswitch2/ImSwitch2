"""Backward-compatible aliases for the WidefieldStarss workflow.

The polarisation-resolved WFS recording workflow is now named
``WidefieldStarssWorkflow``. This module keeps the old import path working for
user scripts that still import ``RecordingWorkflow`` / ``RecordingParams``.
"""

from __future__ import annotations

from .widefield_starss import WidefieldStarssParams, WidefieldStarssWorkflow

RecordingParams = WidefieldStarssParams
RecordingWorkflow = WidefieldStarssWorkflow

__all__ = [
    "RecordingParams",
    "RecordingWorkflow",
    "WidefieldStarssParams",
    "WidefieldStarssWorkflow",
]
