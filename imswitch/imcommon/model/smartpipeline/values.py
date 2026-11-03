"""What flows through a pipeline when ImProcess is not there to make results.

The engine's own outputs -- a gate's ``fired`` event, a transform's points
when its input is a bare array -- are these small carriers. ImProcess's
``PointsResult`` / ``ValueResult`` / ``EventResult`` flow through unchanged
(a transform keeps the class of what it was given, see
:func:`.transforms.apply_transform`); the readers in :mod:`.frames` accept
both. :class:`Frame` is the envelope a source hands the engine per tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from imswitch.imcommon.model.smartpipeline.frames import UNKNOWN_FRAME, FrameIdentity


@dataclass
class Points:
    """``(N, D)`` coordinates in a named frame."""

    coordinates: Any
    frame: str = UNKNOWN_FRAME
    frame_index: int | None = None
    properties: dict = field(default_factory=dict)
    name: str = "points"

    kind = "points"

    def __post_init__(self):
        coords = np.asarray(self.coordinates, dtype=np.float64)
        if coords.ndim == 1 and coords.size in (2, 3):
            coords = coords.reshape(1, -1)
        if coords.ndim == 1 and coords.size == 0:
            coords = coords.reshape(0, 2)
        if coords.ndim != 2 or coords.shape[1] not in (2, 3):
            raise ValueError(f"points are (N, 2) or (N, 3) coordinates, got shape {coords.shape}")
        self.coordinates = coords
        self.frame = str(self.frame or UNKNOWN_FRAME)

    @property
    def count(self) -> int:
        return int(self.coordinates.shape[0])

    def in_frame(self, frame: str, coordinates, *, name: str | None = None) -> "Points":
        return Points(coordinates, frame=frame, frame_index=self.frame_index,
                      properties=dict(self.properties), name=name or self.name)


@dataclass
class Value:
    """One number with a unit."""

    value: float
    unit: str = ""
    name: str = "value"

    kind = "value"

    def __post_init__(self):
        self.value = float(self.value)


@dataclass
class Event:
    """A yes/no with a payload."""

    fired: bool
    payload: dict = field(default_factory=dict)
    name: str = "event"

    kind = "event"

    def __post_init__(self):
        self.fired = bool(self.fired)

    def __bool__(self) -> bool:
        return self.fired


@dataclass
class Frame:
    """One tick's input: an image and where, when and how it was taken.

    ``sequence`` counts the detector's frames (never repeats within a run,
    grows with time); ``t`` is the source's clock in seconds; ``identity``
    is the pixel grid (:class:`~.frames.FrameIdentity`), derived from the
    image when the source does not say. ``frame_index`` is the index in the
    recording for a replay source.
    """

    image: Any
    detector: str
    sequence: int
    t: float
    exposure_s: float | None = None
    identity: FrameIdentity | None = None
    scale: tuple[float, ...] = ()
    unit: str = "px"
    frame_index: int | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.sequence = int(self.sequence)
        self.t = float(self.t)
        if self.identity is None:
            self.identity = FrameIdentity.of_image(self.image)

    @property
    def frame_tag(self) -> str:
        """The frame tag of points measured on this image."""
        return f"detector:{self.detector}"


__all__ = ["Event", "Frame", "Points", "Value"]


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
