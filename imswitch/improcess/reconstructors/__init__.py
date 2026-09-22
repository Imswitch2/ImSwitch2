"""
ImProcess reconstructor plugins.

Reconstructors are populated in the global registry at startup based on
config (or standalone defaults). Controllers retrieve plugins via the registry,
never import them directly.

Built-ins are hard-coded imports here. User drop-in plugins (a ``.py`` file in
the user plugins folder defining a ``Reconstructor`` subclass, see
:mod:`imswitch.improcess.plugins`) are discovered at startup and on reload and
join the same enumerations, gated by their presence on disk rather than by
config. Entry-point loading can come later.
"""

from .base import Reconstructor
from .registry import PluginRegistry, get_registry

# Plugin imports
from .monalisa import MonalisaReconstructor
from .snouty import SnoutyReconstructor
from .view_only import ViewOnlyReconstructor
from .snouty_projections import SnoutyProjectionsReconstructor
from .widefield_starss import WidefieldStarssReconstructor
from .smlm import SmlmLocalizer
from .beadrec import BeadRecReconstructor
from .tiling import TilingReconstructor
from .monalisa.legacy import LegacyMonalisaReconstructor


_AVAILABLE_RECONSTRUCTOR_CLASSES = {
    'monalisa': MonalisaReconstructor,
    'snouty': SnoutyReconstructor,
    'view-only': ViewOnlyReconstructor,
    'snouty-projections': SnoutyProjectionsReconstructor,
    'widefield-starss': WidefieldStarssReconstructor,
    'smlm-localizer': SmlmLocalizer,
    'beadrec': BeadRecReconstructor,
    'tiling-mosaic': TilingReconstructor,
    'monalisa-legacy': LegacyMonalisaReconstructor,
}

# User drop-in reconstructors discovered from the plugins directory
# (imswitch/improcess/plugins). Populated by the plugins package's
# load_user_plugins(); empty until then, so importing this package never runs
# user code. Built-in ids always win a collision (enforced at install time), so
# a stray file cannot shadow a core reconstructor.
_USER_RECONSTRUCTOR_CLASSES: dict[str, type] = {}


def _all_reconstructor_classes() -> dict[str, type]:
    """Built-in reconstructor classes plus discovered user drop-in plugins."""
    return {**_AVAILABLE_RECONSTRUCTOR_CLASSES, **_USER_RECONSTRUCTOR_CLASSES}


def _install_user_reconstructor_classes(
    classes: dict[str, type],
) -> tuple[list[str], list]:
    """Replace the user table with ``classes``; built-in ids win a collision.

    Called by :func:`imswitch.improcess.plugins.load_user_plugins` with what
    one scan of the folder defined. Returns ``(installed_ids, errors)``.
    """
    from imswitch.improcess.plugins.user_plugins import PluginLoadError

    _USER_RECONSTRUCTOR_CLASSES.clear()
    installed: list[str] = []
    errors: list = []
    for plugin_id, plugin_cls in classes.items():
        if plugin_id in _AVAILABLE_RECONSTRUCTOR_CLASSES:
            errors.append(
                PluginLoadError(
                    path=getattr(plugin_cls, "__module__", plugin_id),
                    message=(
                        f"Plugin reconstructor id {plugin_id!r} collides with a "
                        f"built-in; the built-in is kept."
                    ),
                )
            )
            continue
        _USER_RECONSTRUCTOR_CLASSES[plugin_id] = plugin_cls
        installed.append(plugin_id)
    return installed, errors


def clear_user_reconstructors() -> None:
    """Forget all discovered user reconstructors (test/reset helper)."""
    _USER_RECONSTRUCTOR_CLASSES.clear()


def builtin_reconstructor_ids() -> list[str]:
    """Return the ids of the shipped reconstructors only."""
    return sorted(_AVAILABLE_RECONSTRUCTOR_CLASSES)


def available_reconstructor_ids() -> list[str]:
    """Return reconstructor IDs (built-in + user plugins) accepted by config."""
    return sorted(_all_reconstructor_classes())


def register_reconstructor_by_id(registry: PluginRegistry, plugin_id: str) -> Reconstructor:
    """Instantiate and register one reconstructor (built-in or user plugin) by id."""
    try:
        plugin_cls = _all_reconstructor_classes()[plugin_id]
    except KeyError as exc:
        raise KeyError(f"Unknown reconstructor id: {plugin_id!r}") from exc
    plugin = plugin_cls()
    registry.register_reconstructor(plugin)
    return plugin


def register_default_reconstructors(
    registry: PluginRegistry,
    filter_ids: list[str] | None = None
) -> None:
    """
    Register built-in reconstructors plus all discovered user drop-in plugins.

    Built-ins are gated by config (``filter_ids``); user plugins are gated by
    their presence on disk, so a dropped-in reconstructor is always active.

    Args:
        registry: The plugin registry to populate
        filter_ids: Optional list of built-in reconstructor ids to register.
            None registers all built-ins.
    """
    if filter_ids is None:
        to_register = list(_AVAILABLE_RECONSTRUCTOR_CLASSES.items())
    else:
        all_classes = _all_reconstructor_classes()
        unknown = [pid for pid in filter_ids if pid not in all_classes]
        if unknown:
            raise KeyError(
                f"Unknown reconstructor id(s): {unknown}. "
                f"Available reconstructors: {available_reconstructor_ids()}"
            )
        to_register = [(pid, all_classes[pid]) for pid in filter_ids]

    registered_ids = set()
    for plugin_id, plugin_class in to_register:
        registry.register_reconstructor(plugin_class())
        registered_ids.add(plugin_id)

    # User drop-in plugins: active whenever present, regardless of the built-in
    # filter (unless already registered via an explicit filter id).
    for plugin_id, plugin_class in _USER_RECONSTRUCTOR_CLASSES.items():
        if plugin_id in registered_ids:
            continue
        registry.register_reconstructor(plugin_class())


__all__ = [
    "Reconstructor",
    "PluginRegistry",
    "get_registry",
    "available_reconstructor_ids",
    "builtin_reconstructor_ids",
    "clear_user_reconstructors",
    "register_default_reconstructors",
    "register_reconstructor_by_id",
]


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
