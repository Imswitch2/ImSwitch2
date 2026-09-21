"""Read the generated schemas from package data.

The Qt-free loader the discovery plan's item B asks for, limited to what the
schema work needs: one generated schema by manager name, the index, and the
alias bookkeeping validation and the editor both consult. Nothing here
imports a manager, Qt, or the extractor; the files it reads are produced by
``tools/extract_manager_schemas.py --write`` and live beside this module under
``schemas/``. A root can be injected so tests can point it at a temporary
directory.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional

_PACKAGE = "imswitch.imcontrol.model.configeditor"
_SCHEMAS_DIR = "schemas"
_MANAGERS_DIR = "managers"
_INDEX_FILE = "index.json"


def default_root() -> Path:
    """The packaged ``schemas/`` directory, wherever the package is installed."""
    return Path(str(resources.files(_PACKAGE) / _SCHEMAS_DIR))


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=None)
def _cached_schema(root: str, manager_name: str) -> Optional[dict]:
    return _read_json(Path(root) / _MANAGERS_DIR / f"{manager_name}.json")


def generated_schema_for(manager_name: str, root: Optional[Path] = None) -> Optional[dict]:
    """The generated ``managerProperties`` schema for ``manager_name``, or None.

    Cached per root: the editor asks for the same few schemas repeatedly, and
    the files do not change while the process runs. Callers must not mutate
    the returned dict; copy it first.
    """
    if not manager_name or "/" in manager_name or "\\" in manager_name:
        return None
    return _cached_schema(str(root or default_root()), manager_name)


def clear_cache() -> None:
    """Forget cached schemas (tests that write into a temporary root)."""
    _cached_schema.cache_clear()


def index(root: Optional[Path] = None) -> Optional[dict]:
    """The generated ``index.json``: generator version, per-manager hashes, coverage."""
    return _read_json(Path(root or default_root()) / _INDEX_FILE)


def alias_conflicts(schema: dict, properties: dict) -> list[tuple[str, list[str]]]:
    """``(canonical, spellings present)`` for each property saved under more than one spelling.

    A manager reading ``props.get("mockPhotonCountMean", props.get("mock_photon_count_mean"))``
    prefers the first spelling; a file carrying both is a configuration that
    says two things, and the second is silently ignored.
    """
    conflicts: list[tuple[str, list[str]]] = []
    for key, prop in (schema.get("properties") or {}).items():
        aliases = prop.get("x-imswitch-aliases") or []
        if not aliases:
            continue
        present = [spelling for spelling in [key, *aliases] if spelling in properties]
        if len(present) > 1:
            conflicts.append((key, present))
    return conflicts


def canonical_spelling(schema: dict, key: str) -> str:
    """The spelling the manager prefers for ``key`` (itself, unless it is an alias)."""
    prop = (schema.get("properties") or {}).get(key) or {}
    return prop.get("x-imswitch-alias-of", key)


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
