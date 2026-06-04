"""
ImProcess reconstructor plugins.

Reconstructors are populated in the global registry at startup based on
config (or standalone defaults). Controllers retrieve plugins via the registry,
never import them directly.

For now, plugins are hard-coded imports here. Entry-point loading can come later.
"""

from .base import Reconstructor
from .registry import PluginRegistry, get_registry

# Plugin imports
from .monalisa import MonalisaReconstructor
from .snouty import SnoutyReconstructor
from .view_only import ViewOnlyReconstructor
from .snouty_projections import SnoutyProjectionsReconstructor


_AVAILABLE_RECONSTRUCTOR_CLASSES = {
    'monalisa': MonalisaReconstructor,
    'snouty': SnoutyReconstructor,
    'view-only': ViewOnlyReconstructor,
    'snouty-projections': SnoutyProjectionsReconstructor,
}


def available_reconstructor_ids() -> list[str]:
    """Return built-in reconstructor IDs accepted by setup processing config."""
    return sorted(_AVAILABLE_RECONSTRUCTOR_CLASSES)


def register_default_reconstructors(
    registry: PluginRegistry,
    filter_ids: list[str] | None = None
) -> None:
    """
    Register built-in reconstructors.
    
    Called at module startup or when ImProcess launches standalone without
    a setup file. Individual plugins are instantiated and registered here.
    
    Args:
        registry: The plugin registry to populate
        filter_ids: Optional list of plugin IDs to register. If None, all are registered.
    """
    # Register requested plugins
    if filter_ids is None:
        # Register all
        to_register = _AVAILABLE_RECONSTRUCTOR_CLASSES.items()
    else:
        # Register only the filtered ones
        to_register = [
            (pid, cls)
            for pid, cls in _AVAILABLE_RECONSTRUCTOR_CLASSES.items()
            if pid in filter_ids
        ]
    
    for plugin_id, plugin_class in to_register:
        registry.register_reconstructor(plugin_class())


__all__ = [
    "Reconstructor",
    "PluginRegistry",
    "get_registry",
    "available_reconstructor_ids",
    "register_default_reconstructors",
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
