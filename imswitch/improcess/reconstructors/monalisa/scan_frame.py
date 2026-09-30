"""The scan of a MoNaLISA recording: where the sample was in every frame.

A raster scan is recorded line by line: the fast axis runs through its steps
once per step of the slow axis. Which camera axis is the fast one and in
which direction each axis runs depends on how the stage is mounted, and the
recording's metadata may or may not say. There are eight possibilities, and
the recording says which one it is: reassembled with the right one, the image
is smooth across the borders of the cells; with any other, it is not.

This module holds the scan in camera pixels. Reading it from the resolved
acquisition layout is the next step (``docs/monalisa_optimal_reconstruction.md``,
section 4.2).
"""

from __future__ import annotations

from typing import Callable

import numpy as np

ORIENTATIONS = (
    "+x+y", "+x-y", "-x+y", "-x-y",
    "+y+x", "+y-x", "-y+x", "-y-x",
)


def raster_scan_index(
    num_fast: int, num_slow: int, orientation: str = "+x+y"
) -> np.ndarray:
    """Integer scan positions ``(K, 2)`` of a raster scan, in steps along x and y.

    ``orientation`` names the fast axis first: ``"-x+y"`` is a scan whose fast
    axis is the camera's x, running backwards, and whose slow axis is y,
    running forwards. Frame ``k`` is step ``k % num_fast`` of the fast axis
    in line ``k // num_fast``.
    """
    if orientation not in ORIENTATIONS:
        raise ValueError(
            f"Unknown orientation {orientation!r}; expected one of {ORIENTATIONS}"
        )
    sign = {"+": 1, "-": -1}
    frame = np.arange(int(num_fast) * int(num_slow))
    fast = sign[orientation[0]] * (frame % int(num_fast))
    slow = sign[orientation[2]] * (frame // int(num_fast))
    if orientation[1] == "x":
        return np.column_stack([fast, slow])
    return np.column_stack([slow, fast])


def roughness(image: np.ndarray) -> float:
    """Mean squared difference of neighbouring pixels over twice the variance.

    One minus the correlation of neighbouring pixels: 0 for a smooth image, 1
    for white noise. Reassembling the same samples in another order changes
    neither the variance nor the noise's share of the differences, so two
    orders differ by the specimen alone; the sum of absolute differences,
    which is what a total variation is, buries that under the noise.
    """
    image = np.asarray(image, dtype=float)
    variance = np.nanvar(image)
    if not np.isfinite(variance) or variance <= 0:
        return float("nan")
    across = np.nanmean(np.diff(image, axis=1) ** 2)
    down = np.nanmean(np.diff(image, axis=0) ** 2)
    return float((across + down) / (4.0 * variance))


def choose_orientation(
    assemble: Callable[[np.ndarray], np.ndarray],
    num_fast: int,
    num_slow: int,
    orientations=ORIENTATIONS,
) -> tuple[str, dict[str, float]]:
    """The orientation whose image is the smoothest.

    Args:
        assemble: Returns the image for the scan positions it is given.
        num_fast, num_slow: Steps of the fast and of the slow axis.

    Returns:
        ``(orientation, scores)``: the winner and the roughness of every
        candidate. A winner that is not clearly ahead of the second means the
        recording does not decide: too little specimen, or a scan that does
        not cover the cell.
    """
    scores = {
        name: roughness(assemble(raster_scan_index(num_fast, num_slow, name)))
        for name in orientations
    }
    finite = {name: score for name, score in scores.items() if np.isfinite(score)}
    if not finite:
        raise ValueError("No orientation produced an image")
    return min(finite, key=finite.get), scores


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
