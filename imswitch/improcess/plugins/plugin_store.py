"""Online store for ImProcess drop-in analysis plugins.

Browse, install, update and uninstall plugins from the ``Improcess-plugins``
GitHub registry. The registry is a single ``index.json`` manifest at the repo
root; plugins are plain ``.py`` files downloaded into the user plugins directory
(:func:`~imswitch.improcess.plugins.user_plugins.user_plugins_directory`).
Installed plugins and their versions are tracked in a hidden ``.installed.json``
sidecar there.

This module is Qt-free (urllib + json only) so the logic is unit-testable by
monkeypatching :func:`_get`; the browsing dialog lives in the view layer.

Ported from Picasso's plugin store (``picasso/gui/plugins_loader.py``).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from imswitch.improcess.plugins.user_plugins import user_plugins_directory

# --- Registry location -------------------------------------------------------

REPO = "Imswitch2/Improcess-plugins"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
MANIFEST_URL = f"{RAW_BASE}/index.json"
REPO_URL = f"https://github.com/{REPO}"

_TIMEOUT = 15  # seconds, for every network request
_SIDECAR = ".installed.json"


# --- Version helpers ---------------------------------------------------------


def parse_version(value: str | None) -> tuple[int, ...]:
    """Parse a version string into a tuple of its numeric components.

    Non-numeric suffixes (e.g. the ``a0`` in ``0.1.0a0``) are split on, so
    ``0.1.0a0`` -> ``(0, 1, 0, 0)``. Good enough to order the simple versions
    plugins use; not a full PEP 440 implementation.
    """
    if not value:
        return ()
    return tuple(int(p) for p in re.findall(r"\d+", str(value)))


def compare_versions(a: str | None, b: str | None) -> int:
    """Return -1, 0 or 1 for ``a`` <, == or > ``b`` (zero-padded compare)."""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def improcess_version() -> str:
    """The running ImSwitch/ImProcess version string."""
    try:
        from imswitch import __version__

        return str(__version__)
    except Exception:
        return "0"


def is_compatible(entry: dict) -> bool:
    """Whether the running ImProcess satisfies the plugin's minimum version."""
    minimum = entry.get("min_improcess_version")
    if not minimum:
        return True
    return compare_versions(improcess_version(), minimum) >= 0


# --- Installed-state sidecar -------------------------------------------------


def _sidecar_path() -> str:
    return os.path.join(user_plugins_directory(), _SIDECAR)


def load_state() -> dict:
    """Load the installed-state sidecar, tolerant of a missing/corrupt file."""
    state = {"plugins": {}, "trust_acknowledged": False}
    try:
        with open(_sidecar_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            state["plugins"] = data.get("plugins", {}) or {}
            state["trust_acknowledged"] = bool(data.get("trust_acknowledged"))
    except (OSError, ValueError):
        pass
    return state


def save_state(state: dict) -> None:
    with open(_sidecar_path(), "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


# --- Network + install operations -------------------------------------------


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "imswitch-improcess"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return response.read()


def fetch_manifest() -> list[dict]:
    """Download and parse the registry manifest.

    Returns the list of plugin entries. Raises on any network/parse error so
    the caller can show a message and offer to retry.
    """
    data = json.loads(_get(MANIFEST_URL).decode("utf-8"))
    plugins = data.get("plugins", []) if isinstance(data, dict) else []
    return [p for p in plugins if isinstance(p, dict) and p.get("id")]


def _local_filename(entry: dict) -> str:
    """The on-disk ``.py`` name for an entry: the plugin id plus ``.py``."""
    return f"{entry['id']}.py"


def install(entry: dict, state: dict) -> None:
    """Download ``entry``'s ``.py`` into the plugins folder and record it."""
    content = _get(f"{RAW_BASE}/{entry['file']}")
    target = os.path.join(user_plugins_directory(), _local_filename(entry))
    with open(target, "wb") as handle:
        handle.write(content)
    state["plugins"][entry["id"]] = {
        "file": _local_filename(entry),
        "version": entry.get("version"),
        "display_name": entry.get("display_name", entry["id"]),
    }
    save_state(state)


def uninstall(plugin_id: str, state: dict) -> None:
    """Delete the installed ``.py`` for ``plugin_id`` and forget it."""
    record = state["plugins"].get(plugin_id, {})
    filename = record.get("file", f"{plugin_id}.py")
    try:
        os.remove(os.path.join(user_plugins_directory(), filename))
    except OSError:
        pass
    state["plugins"].pop(plugin_id, None)
    save_state(state)


# --- Status model ------------------------------------------------------------

NOT_INSTALLED = "not_installed"
UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
INCOMPATIBLE = "incompatible"
ORPHAN = "orphan"  # installed but no longer in the manifest


def status_for(entry: dict, state: dict) -> str:
    installed = state["plugins"].get(entry["id"])
    if entry.get("_orphan"):
        return ORPHAN
    if not is_compatible(entry):
        return INCOMPATIBLE
    if installed is None:
        return NOT_INSTALLED
    if compare_versions(entry.get("version"), installed.get("version")) > 0:
        return UPDATE_AVAILABLE
    return UP_TO_DATE


def merged_entries(manifest: list[dict], state: dict) -> list[dict]:
    """Manifest entries plus any installed plugins missing from the manifest.

    Orphans (installed locally but dropped from the registry) are surfaced so
    they can still be uninstalled; they are flagged with ``_orphan``.
    """
    by_id = {e["id"]: e for e in manifest}
    entries = list(manifest)
    for plugin_id, record in state["plugins"].items():
        if plugin_id not in by_id:
            entries.append(
                {
                    "id": plugin_id,
                    "display_name": record.get("display_name", plugin_id),
                    "description": "(no longer in the online registry)",
                    "version": record.get("version"),
                    "_orphan": True,
                }
            )
    return entries


__all__ = [
    "REPO",
    "REPO_URL",
    "MANIFEST_URL",
    "NOT_INSTALLED",
    "UP_TO_DATE",
    "UPDATE_AVAILABLE",
    "INCOMPATIBLE",
    "ORPHAN",
    "parse_version",
    "compare_versions",
    "improcess_version",
    "is_compatible",
    "load_state",
    "save_state",
    "fetch_manifest",
    "install",
    "uninstall",
    "status_for",
    "merged_entries",
]
