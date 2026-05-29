"""
ImProcess processor plugins.

Processors operate on ProcessingResults and are stackable. They are registered
in the global registry alongside reconstructors.
"""

from .base import Processor
from .drift_correct import DriftCorrectProcessor


def register_default_processors(registry, filter_ids: list[str] | None = None) -> None:
    """
    Register built-in processors.

    Args:
        registry: Plugin registry to populate.
        filter_ids: Optional list of processor ids to register. None registers all.
    """
    available_plugins = {
        'drift-correct': DriftCorrectProcessor,
    }
    if filter_ids is None:
        to_register = available_plugins.items()
    else:
        to_register = [(pid, cls) for pid, cls in available_plugins.items() if pid in filter_ids]
    for _pid, plugin_cls in to_register:
        registry.register_processor(plugin_cls())


__all__ = [
    "Processor",
    "DriftCorrectProcessor",
    "register_default_processors",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version).
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
