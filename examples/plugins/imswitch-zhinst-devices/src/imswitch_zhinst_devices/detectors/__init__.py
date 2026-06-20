"""Detector managers contributed by the Zurich Instruments example plugin."""

from .mfli_lockin import MockZhinstLockinDetectorManager, ZhinstLockinDetectorManager

__all__ = [
    "MockZhinstLockinDetectorManager",
    "ZhinstLockinDetectorManager",
]
