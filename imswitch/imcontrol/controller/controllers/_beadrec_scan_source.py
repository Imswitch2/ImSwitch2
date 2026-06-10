"""
BeadRec scan source capability protocol.

Defines a runtime-checkable protocol any scan controller can implement to expose
the metadata BeadRec needs: scan dimensions, step sizes, line-step count, and
frames-per-pixel.

Protocol methods are prefixed with "BeadRec" to avoid collision with the existing
list-returning getScanStepSizes() method on scan controllers.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class BeadRecScanSource(Protocol):
    """Protocol for controllers that can provide scan metadata to BeadRec.
    
    Controllers implementing this protocol expose the scan dimensions, step sizes,
    line-step count, and frames-per-pixel needed for bead reconstruction workflows.
    """

    def getBeadRecScanDims(self) -> tuple[int, int]:
        """Return (x_pixels, y_pixels) for the scan."""
        ...

    def getBeadRecStepSizes(self) -> tuple[float, float]:
        """Return (x_step_um, y_step_um) for the scan."""
        ...

    def getNumLineSteps(self) -> int:
        """Return the number of line-steps in the scan."""
        ...

    def getFramesPerScanPixel(self) -> int:
        """Return the number of detector frames acquired per scan pixel."""
        ...

    def isBeadRecCompatible(self) -> bool:
        """Return True if this controller's scan is compatible with BeadRec."""
        ...


class BeadRecScanSourceMixin:
    """Mixin providing default implementations for BeadRecScanSource protocol.
    
    Controllers inheriting this mixin must still implement getBeadRecScanDims()
    and getBeadRecStepSizes() themselves. The mixin provides sensible defaults
    for the other methods.
    """

    def getNumLineSteps(self) -> int:
        """Default: single-pass scan with no line-stepping."""
        return 1

    def getFramesPerScanPixel(self) -> int:
        """Default: one detector frame per scan pixel."""
        return 1

    def isBeadRecCompatible(self) -> bool:
        """Default: assume compatible unless overridden."""
        return True


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
