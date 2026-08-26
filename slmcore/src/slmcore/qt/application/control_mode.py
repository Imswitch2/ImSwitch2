from __future__ import annotations

from enum import Enum


class SLMControlMode(str,Enum):
    """Qt application control mode for one SLM session."""

    EDITOR = "editor"
    FAST_CONFIG = "fast_config"

    @classmethod
    def normalize(cls,value) -> "SLMControlMode":
        if isinstance(value,cls):
            return value
        text = str(value or "").strip().lower()
        aliases = {
            "edit":cls.EDITOR,
            "editor":cls.EDITOR,
            "fast":cls.FAST_CONFIG,
            "fast_config":cls.FAST_CONFIG,
            "fast config":cls.FAST_CONFIG,
        }
        try:
            return aliases[text]
        except KeyError as error:
            raise ValueError("Unknown SLM control mode %r" % value) from error
