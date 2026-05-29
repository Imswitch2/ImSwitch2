"""
MS-RESOLFT plane de-interlacing helper.

Vendored from Mini_Recon (https://github.com/khoj00/Mini_Recon).
"""

import numpy as np


def restack_interleaved(stack: np.ndarray, cycles: int, planes_in_cycle: int) -> np.ndarray:
    """
    De-interlace MS-RESOLFT planes so all cycles of plane-0 come first, then plane-1, …
    
    Args:
        stack: 3D array (planes, cam_y, cam_x)
        cycles: Number of MS-RESOLFT cycles (slow axis groups)
        planes_in_cycle: Planes acquired per cycle
    
    Returns:
        Restacked 3D array with planes grouped by cycle
    """
    expected = cycles * planes_in_cycle
    if stack.shape[0] < expected:
        return stack
    tp_data = stack[:expected]
    restacked = np.empty_like(tp_data)
    for i in range(planes_in_cycle):
        restacked[i * cycles:(i + 1) * cycles] = tp_data[i::planes_in_cycle]
    return restacked


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
