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
# from .view_only import ViewOnlyReconstructor  # TODO: future plugins


def register_default_reconstructors(registry: PluginRegistry) -> None:
    """
    Register built-in reconstructors.
    
    Called at module startup or when ImProcess launches standalone without
    a setup file. Individual plugins are instantiated and registered here.
    """
    registry.register_reconstructor(MonalisaReconstructor())


__all__ = [
    "Reconstructor",
    "PluginRegistry",
    "get_registry",
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
