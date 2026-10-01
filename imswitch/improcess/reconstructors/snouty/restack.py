"""
MS-RESOLFT plane de-interlacing helper.

Vendored from Mini_Recon (https://github.com/khoj00/Mini_Recon).
"""

import numpy as np


def restack_interleaved(
    stack: np.ndarray,
    cycles: int,
    planes_in_cycle: int,
    *,
    timepoints: int | None = None,
) -> np.ndarray:
    """
    De-interlace MS-RESOLFT planes so all cycles of plane-0 come first, then plane-1, …

    Every timepoint is restacked independently. Restacking the whole stream as
    one block and slicing to ``cycles * planes_in_cycle`` silently discarded
    every timepoint after the first, and a stream shorter than one timepoint
    was returned still interleaved, which reads as a successful reconstruction
    of scrambled planes.

    Args:
        stack: 3D array (planes, cam_y, cam_x), one or more timepoints.
        cycles: Number of MS-RESOLFT cycles (slow axis groups).
        planes_in_cycle: Planes acquired per cycle.
        timepoints: Expected timepoint count. When given, a stream that holds a
            different number is an error rather than a silent reinterpretation.

    Returns:
        Restacked 3D array with planes grouped by cycle within each timepoint.
    """
    if cycles < 1 or planes_in_cycle < 1:
        raise ValueError(
            f'MS-RESOLFT restacking needs positive cycles and planes/cycle, '
            f'got {cycles} and {planes_in_cycle}'
        )
    frames_per_timepoint = cycles * planes_in_cycle
    total = stack.shape[0]
    if total % frames_per_timepoint:
        raise ValueError(
            f'MS-RESOLFT restacking needs a whole number of timepoints: '
            f'{total} frames is not a multiple of {cycles} cycles x '
            f'{planes_in_cycle} planes/cycle ({frames_per_timepoint} frames '
            f'per timepoint)'
        )
    observed = total // frames_per_timepoint
    if timepoints is not None and timepoints != observed:
        raise ValueError(
            f'MS-RESOLFT restacking expected {timepoints} timepoint(s) '
            f'({timepoints * frames_per_timepoint} frames) but this source has '
            f'{total} frames ({observed} timepoint(s))'
        )

    restacked = np.empty_like(stack)
    for timepoint in range(observed):
        start = timepoint * frames_per_timepoint
        block = stack[start:start + frames_per_timepoint]
        for plane in range(planes_in_cycle):
            target = start + plane * cycles
            restacked[target:target + cycles] = block[plane::planes_in_cycle]
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
