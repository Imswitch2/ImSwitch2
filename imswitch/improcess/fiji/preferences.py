"""Where Fiji is and which Python runs the host, kept per computer.

Plugins > Fiji > Fiji location... edits :attr:`FijiPreferences.fijiDir`. Like
the default folders, it describes the computer (where Fiji was unpacked), so it
lives in ``improcess_options.json`` in the ImSwitch config folder and not in a
setup file. Keys this module does not know are left in the file untouched.

This module imports no Qt and nothing that needs ``imagej``: it only decides
whether the link can be started and builds the launch configuration.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from imswitch.imcommon.model import initLogger
from imswitch.improcess.fiji.protocol import FijiLinkError, HostLaunchConfig, resolve_launch_config
from imswitch.improcess.model.folder_preferences import options_file_path

_FIELDS = ("fijiDir", "fijiPython")

#: The vendored imglib2 bridge jars (see ``jars/README.md``).
BRIDGE_JARS_DIR = Path(__file__).resolve().parent / "jars"

INSTALL_HINT = 'pyimagej is not installed: pip install "imswitch2[fiji]"'
LOCATION_HINT = "Choose the Fiji folder: Plugins → Fiji → Fiji location…"


@dataclass(frozen=True)
class FijiPreferences:
    #: The Fiji folder the user chose (the one holding ``jars``, or on macOS
    #: the folder holding Fiji.app).
    fijiDir: str = ""
    #: The interpreter that runs the host. Empty means this ImSwitch's own
    #: ``sys.executable``. Advanced, with no menu entry: set it in
    #: improcess_options.json for an environment that holds pyimagej when
    #: ImSwitch's does not.
    fijiPython: str = ""


def _read(path) -> dict:
    with open(path, encoding="utf-8") as file:
        content = json.load(file)
    if not isinstance(content, dict):
        raise ValueError(f"expected a JSON object, found {type(content).__name__}")
    return content


def load_fiji_preferences(path=None) -> FijiPreferences:
    """What the options file holds; the defaults when it is missing. Never raises.

    A value that is not a string is treated as unset.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except FileNotFoundError:
        return FijiPreferences()
    except Exception as error:  # noqa: BLE001 -- startup must go on
        initLogger("ImProcessFiji").warning(
            f"Could not read the Fiji preferences from {path} ({error}); Fiji is not configured."
        )
        return FijiPreferences()
    return FijiPreferences(**{
        field: content[field] if isinstance(content.get(field), str) else "" for field in _FIELDS
    })


def save_fiji_preferences(preferences: FijiPreferences, path=None) -> None:
    """Write the Fiji preferences, keeping every other key the file holds.

    A file that cannot be read as a JSON object is replaced rather than merged
    into. Raises OSError when the file cannot be written.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except Exception:  # noqa: BLE001 -- missing or unreadable: start afresh
        content = {}
    for field in _FIELDS:
        content[field] = str(getattr(preferences, field) or "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(content, file, indent=4, sort_keys=True)


def physical_ram_bytes() -> int | None:
    """Physical memory in bytes, or None when this platform cannot tell.

    None is passed on rather than a guess: the launch configuration then omits
    the heap limit and says so.
    """
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:  # noqa: BLE001 -- unknown, reported as such
            pass
    return None


def launch_config(preferences: FijiPreferences, *, ram_bytes: int | None = None) -> HostLaunchConfig:
    """The host launch configuration for the chosen Fiji folder.

    ``ram_bytes`` defaults to the machine's physical memory (None when that is
    unknown). Raises a :class:`FijiLinkError` when the folder is unusable.
    """
    if ram_bytes is None:
        ram_bytes = physical_ram_bytes()
    jars = sorted(BRIDGE_JARS_DIR.glob("*.jar"))
    return resolve_launch_config(preferences.fijiDir, ram_bytes=ram_bytes, bridge_jars=jars)


def availability(
    preferences: FijiPreferences,
    *,
    find_spec: Callable = importlib.util.find_spec,
) -> tuple[bool, str]:
    """Whether Fiji can be started, and the reason when it cannot."""
    # With an explicit fijiPython the host runs in that interpreter, so this
    # one need not hold pyimagej; a missing package there shows up as the
    # host's own start failure, which quotes its log.
    if not preferences.fijiPython and find_spec("imagej") is None:
        return False, INSTALL_HINT
    if not preferences.fijiDir:
        return False, LOCATION_HINT
    try:
        launch_config(preferences)
    except FijiLinkError as exc:
        return False, str(exc)
    return True, ""
