"""Which version of a plugin ran, derived from where its code came from.

A provenance node that says ``plugin_id: filter`` is only half an answer;
the other half is *which* filter, and an optional ``version`` attribute a
plugin author may or may not remember to bump is not that. The runtime
stamps the version itself when a plugin is registered:

* a built-in (its module lives under ``imswitch``) carries the ImSwitch
  distribution version;
* a drop-in file (under the user plugins directory) carries a digest of
  the file, so an edited plugin is a different version even if nobody
  changed a number;
* anything else installed as a package carries that distribution's version.
"""

from __future__ import annotations

import hashlib
import inspect
import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def imswitch_version() -> str:
    try:
        from importlib.metadata import version

        return str(version("imswitch"))
    except Exception:
        try:
            from imswitch import __version__

            return str(__version__)
        except Exception:
            return "unknown"


def _file_digest(path: Path) -> str:
    try:
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "file:unreadable"


def _distribution_version(module_name: str) -> str | None:
    try:
        from importlib.metadata import packages_distributions, version
    except Exception:
        return None
    top = module_name.split(".")[0]
    try:
        distributions = packages_distributions().get(top) or []
    except Exception:
        distributions = []
    for name in distributions:
        try:
            return f"{name} {version(name)}"
        except Exception:
            continue
    return None


def plugin_version(plugin) -> str:
    """The version string recorded for ``plugin`` (see the module docstring)."""
    cls = plugin if inspect.isclass(plugin) else type(plugin)
    module_name = str(getattr(cls, "__module__", "") or "")
    try:
        source = Path(inspect.getsourcefile(cls) or "")
    except (TypeError, OSError):
        source = Path("")

    if source.name:
        try:
            from imswitch.improcess.plugins.user_plugins import user_plugins_directory

            plugins_dir = Path(user_plugins_directory(create=False))
            if plugins_dir and os.path.commonpath([str(source.resolve()), str(plugins_dir.resolve())]) == str(plugins_dir.resolve()):
                return _file_digest(source)
        except Exception:
            pass

    if module_name == "imswitch" or module_name.startswith("imswitch."):
        return imswitch_version()

    distribution = _distribution_version(module_name)
    if distribution:
        return distribution
    if source.name:
        return _file_digest(source)
    return "unknown"


def stamp_plugin_version(plugin) -> str:
    """Set ``plugin.version`` from the runtime's knowledge; returns it."""
    version = plugin_version(plugin)
    try:
        plugin.version = version
    except Exception:
        pass
    return version


__all__ = ["imswitch_version", "plugin_version", "stamp_plugin_version"]


# Copyright (C) 2020-2026 ImSwitch developers
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
