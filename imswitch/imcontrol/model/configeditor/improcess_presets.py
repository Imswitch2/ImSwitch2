"""The processor-only setups ImSwitch ships, offered as starting points for the
ImProcess section of a setup.

Qt-free. A *preset* is a shipped setup whose only content is a ``processing``
block: its top-level keys, ignoring comment keys (starting with ``//``) and
reserved keys (starting with ``__``), are exactly ``{"processing"}``. The rule,
not a list of file names, selects them, so a new such setup shows up by being
added to the package data.

The presets are read from the package data, never from the user's
``ImSwitchConfig`` copy, which the user may have edited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The one top-level key a preset consists of.
PRESET_SECTION = "processing"

#: Setup keys that carry no configuration: comments and reserved bookkeeping.
_NON_CONFIG_PREFIXES = ("//", "__")

#: The comment key that describes a shipped setup.
_DESCRIPTION_KEY = "// description"

#: The comment key that names a shipped setup for people ("MoNaLISA reconstruction").
_TITLE_KEY = "// title"

_SETUPS_DIR_NAME = "imcontrol_setups"


@dataclass(frozen=True)
class Preset:
    """One shipped ImProcess starting point."""

    file_name: str
    title: str          # the setup's ``// title``; the file stem when it has none
    description: str    # the setup's ``// description``, or ""
    processing: dict    # the block exactly as shipped

    @property
    def stem(self) -> str:
        """The file name without ``.json``."""
        return self.file_name[:-len(".json")] if self.file_name.endswith(".json") else self.file_name


def shipped_setups_directory() -> Path:
    """The packaged ``imcontrol_setups`` folder.

    Taken from ``DataFileDirs.UserDefaults``; when ImSwitch's common package
    cannot be imported (the editor can run as a stand-alone script), from the
    path relative to this module.
    """
    try:
        from imswitch.imcommon.model.dirtools import DataFileDirs

        return Path(DataFileDirs.UserDefaults) / _SETUPS_DIR_NAME
    except Exception:
        return (Path(__file__).resolve().parents[3] / "_data" / "user_defaults"
                / _SETUPS_DIR_NAME)


def _title(value, fallback: str) -> str:
    """``// title`` when it is a non-empty string, else ``fallback`` (the file stem)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _description(value) -> str:
    """``// description`` as one string: a string, or a list of lines joined by a space."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return ""


def shipped_improcess_presets(directory: Path | str | None = None) -> list[Preset]:
    """The shipped processor-only setups, sorted by file name.

    ``directory`` overrides the package data folder (tests). Files that cannot
    be read as a JSON object are skipped.
    """
    folder = Path(directory) if directory is not None else shipped_setups_directory()
    presets: list[Preset] = []
    if not folder.is_dir():
        return presets
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        keys = {k for k in data if not str(k).startswith(_NON_CONFIG_PREFIXES)}
        block = data.get(PRESET_SECTION)
        if keys != {PRESET_SECTION} or not isinstance(block, dict):
            continue
        presets.append(Preset(
            file_name=path.name,
            title=_title(data.get(_TITLE_KEY), path.stem),
            description=_description(data.get(_DESCRIPTION_KEY)),
            processing=block,
        ))
    return presets
