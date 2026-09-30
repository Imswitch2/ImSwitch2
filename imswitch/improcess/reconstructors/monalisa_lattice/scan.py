"""The raster scan of a recording, from what the recording says about itself.

The lattice pipeline needs three numbers: the steps of the fast and of the
slow axis, and the size of a step. It reads the rest from the frames. The
numbers come from the resolved acquisition layout when the recording has one
and from the ``ScanStage`` attributes otherwise; a recording whose attributes
do not add up to its number of frames is taken to be a square scan, which is
what the older recordings are.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RecordedScan:
    """The scan of one recording.

    Attributes:
        num_fast, num_slow: Steps of the fast and of the slow axis.
        num_stacks: Scans of the cell in the recording (timepoints, planes).
        step_nm: Size of a step, or ``None`` when the recording does not say.
        source: Where the numbers come from, for the log.
    """

    num_fast: int
    num_slow: int
    num_stacks: int
    step_nm: float | None
    source: str

    @property
    def frames_per_stack(self) -> int:
        return self.num_fast * self.num_slow

    def describe(self) -> str:
        step = "unknown step" if self.step_nm is None else f"{self.step_nm:g} nm"
        stacks = "" if self.num_stacks == 1 else f", {self.num_stacks} stacks"
        return (
            f"{self.num_fast} x {self.num_slow} steps of {step}{stacks} "
            f"({self.source})"
        )


def _step_nm(attrs) -> float | None:
    try:
        step = np.asarray(attrs["ScanStage:axis_step_size"], dtype=float).ravel()
    except (KeyError, TypeError, ValueError):
        return None
    if step.size == 0 or not np.isfinite(step[0]) or step[0] <= 0:
        return None
    return float(step[0] * 1000.0)     # the attribute is in micrometres


def _line_steps(attrs) -> int:
    try:
        value = np.asarray(attrs["ScanTTL:n_linesteps"]).ravel()
        return max(1, int(value[0]))
    except (KeyError, TypeError, ValueError, IndexError):
        return 1


def _counts_from_attributes(attrs, num_frames: int):
    """Step counts of the first two scan axes that divide the frame count."""
    try:
        count_x = int(np.asarray(attrs["ScanTTL:Nx"]).ravel()[0])
        count_y = int(np.asarray(attrs["ScanTTL:Ny"]).ravel()[0])
        if count_x > 0 and count_y > 0 and num_frames % (count_x * count_y) == 0:
            return count_x, count_y, "ScanTTL:Nx/Ny"
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    try:
        length = np.asarray(attrs["ScanStage:axis_length"], dtype=float).ravel()
        step = np.asarray(attrs["ScanStage:axis_step_size"], dtype=float).ravel()
    except (KeyError, TypeError, ValueError):
        return None
    if length.size < 2 or step.size < 2 or np.any(step[:2] <= 0):
        return None
    ratio = np.abs(length[:2] / step[:2])
    # A scan of a length has one position more than it has steps; older
    # metadata counted the steps. Take the reading the frames agree with.
    readings = (
        np.floor(ratio + 1e-6).astype(int) + 1,
        np.rint(ratio).astype(int),
        np.ceil(ratio - 1e-6).astype(int),
    )
    for counts in readings:
        if np.all(counts > 0) and num_frames % int(counts[0] * counts[1]) == 0:
            return int(counts[0]), int(counts[1]), "ScanStage attributes"
    return None


def recorded_scan(
    attrs,
    num_frames: int,
    num_fast: int = 0,
    num_slow: int = 0,
    step_nm: float = 0.0,
) -> RecordedScan:
    """The scan of a recording of ``num_frames`` frames.

    ``num_fast``, ``num_slow`` and ``step_nm`` are what the user has set; 0
    leaves the value to the recording.

    Raises:
        ValueError: If the numbers cannot be made to agree with the number
            of frames, or the recording holds line-step conditions, which the
            lattice pipeline does not take apart.
    """
    attrs = attrs or {}
    num_frames = int(num_frames)
    if _line_steps(attrs) > 1:
        raise ValueError(
            "This recording holds line-step conditions "
            f"(ScanTTL:n_linesteps = {_line_steps(attrs)}); the lattice "
            "reconstructor takes one condition per scan"
        )
    step = float(step_nm) if step_nm and step_nm > 0 else _step_nm(attrs)

    if num_fast > 0 and num_slow > 0:
        counts = (int(num_fast), int(num_slow), "parameters")
        if num_frames % (counts[0] * counts[1]) != 0:
            raise ValueError(
                f"{num_frames} frames are not a number of scans of "
                f"{counts[0]} x {counts[1]} steps"
            )
    elif num_fast > 0 or num_slow > 0:
        raise ValueError("Set the steps of both scan axes, or of neither")
    else:
        counts = _counts_from_attributes(attrs, num_frames)
        if counts is None:
            side = int(round(np.sqrt(num_frames)))
            if side * side != num_frames:
                raise ValueError(
                    f"The recording does not say how its {num_frames} frames "
                    "make a scan, and they are not a square one: set the "
                    "steps of the two scan axes"
                )
            counts = (side, side, "square scan assumed")
    return RecordedScan(
        num_fast=counts[0],
        num_slow=counts[1],
        num_stacks=num_frames // (counts[0] * counts[1]),
        step_nm=step,
        source=counts[2],
    )


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
