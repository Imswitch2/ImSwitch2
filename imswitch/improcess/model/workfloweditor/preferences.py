"""What the workflow editor remembers between sessions. Qt-free.

Kept under ``workflowEditor`` in ``improcess_options.json`` (the file
:mod:`imswitch.improcess.model.folder_preferences` writes), so it describes
this computer and this user, and every other key in the file survives a
round trip: Graph or List, the steps and files used last, and the folder
*Write files* writes to.
"""

from __future__ import annotations

import json
import os

from imswitch.imcommon.model import initLogger

KEY = "workflowEditor"
_RECENT = 8


class EditorPreferences:
    """Read once, written on every change; never raises on a bad file."""

    def __init__(self, path=None):
        if path is None:
            from imswitch.improcess.model.folder_preferences import options_file_path

            path = options_file_path()
        self.path = str(path)
        self._values = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as file:
                content = json.load(file)
        except FileNotFoundError:
            return {}
        except Exception as error:  # noqa: BLE001 - the editor must open anyway
            initLogger("WorkflowEditor").warning(f"Could not read {self.path} ({error}); editor settings reset.")
            return {}
        values = content.get(KEY) if isinstance(content, dict) else None
        return dict(values) if isinstance(values, dict) else {}

    def _save(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as file:
                content = json.load(file)
            if not isinstance(content, dict):
                content = {}
        except Exception:  # noqa: BLE001 - missing or unreadable: start afresh
            content = {}
        content[KEY] = self._values
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump(content, file, indent=2)
        except OSError as error:
            initLogger("WorkflowEditor").warning(f"Could not save editor settings to {self.path} ({error}).")

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def set(self, key: str, value) -> None:
        if self._values.get(key) != value:
            self._values[key] = value
            self._save()

    # -- the lists -------------------------------------------------------------------

    def _push(self, key: str, value: str) -> None:
        items = [item for item in self._values.get(key, []) if isinstance(item, str) and item != value]
        self.set(key, [value] + items[: _RECENT - 1])

    @property
    def mode(self) -> str:
        return "list" if self.get("mode") == "list" else "graph"

    @mode.setter
    def mode(self, value: str) -> None:
        self.set("mode", "list" if value == "list" else "graph")

    def recent_steps(self) -> list[tuple[str, str]]:
        found = []
        for item in self.get("recentSteps", []) or []:
            if isinstance(item, str) and ":" in item:
                kind, plugin = item.split(":", 1)
                found.append((kind, plugin))
        return found

    def add_recent_step(self, kind: str, plugin: str) -> None:
        self._push("recentSteps", f"{kind}:{plugin}")

    def recent_files(self) -> list[str]:
        return [item for item in self.get("recentFiles", []) or [] if isinstance(item, str)]

    def add_recent_file(self, path) -> None:
        self._push("recentFiles", os.path.abspath(str(path)))


__all__ = ["EditorPreferences", "KEY"]
