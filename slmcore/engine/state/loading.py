from __future__ import annotations

from dataclasses import dataclass



ConfigPath = tuple[str,...]


@dataclass(frozen=True)
class ConfigWarning:
    path: ConfigPath
    message: str

    def __str__(self) -> str:
        location = ".".join(self.path) or "<section>"
        return f"{location}: {self.message}"