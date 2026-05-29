"""
ImProcess processor plugins.

Processors operate on ProcessingResults and are stackable. They are registered
in the global registry alongside reconstructors.
"""

from .base import Processor
from .drift_correct import DriftCorrectProcessor


def register_default_processors(registry) -> None:
    """
    Register built-in processors.
    
    Called at module startup. Individual processors are instantiated and
    registered here as they're implemented (Agent 2's scope).
    """
    registry.register_processor(DriftCorrectProcessor())


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
