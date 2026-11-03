"""Coordinates that mean something in a named frame of reference.

A ``PointsTableResult`` is a table that happens to hold coordinates. A
``PointsResult`` is the same table with a claim attached: these are positions
in ``frame`` -- ``"detector:Camera"`` pixels, ``"stage:XYStage"`` micrometres
-- measured on source frame ``frame_index``. The claim is what a smart
pipeline checks before it lets a point drive hardware (``kind = "points"``,
see :data:`~.result.RESULT_KINDS`): camera pixels never reach a stage move
without a transform step that says it maps that frame to this one.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from imswitch.improcess.model.points_table_result import PointsTableResult

#: Frame tag of points whose frame of reference nobody has claimed.
UNKNOWN_FRAME = "unknown"


class PointsResult(PointsTableResult):
    """``(N, D)`` coordinates in a named frame, plus per-point properties."""

    kind = "points"
    publishes_table_rows = True

    def __init__(
        self,
        name: str,
        coordinates: np.ndarray | Any,
        *,
        frame: str = UNKNOWN_FRAME,
        frame_index: int | None = None,
        axis_names: list[str] | None = None,
        properties: dict[str, Any] | None = None,
        coordinate_scale: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict[str, Any] | None = None,
    ):
        coords = np.asarray(coordinates, dtype=np.float64)
        if coords.ndim == 1 and coords.size in (2, 3):
            coords = coords.reshape(1, -1)  # one point, given flat
        if coords.ndim == 1 and coords.size == 0:
            # No detection is a normal outcome, not an error: an empty list
            # is an empty set of 2-D points (an empty (0, 3) stays as given).
            coords = coords.reshape(0, 2)
        if coords.ndim != 2 or coords.shape[1] not in (2, 3):
            raise ValueError(
                f"points are (N, 2) or (N, 3) coordinates, got shape {coords.shape}"
            )
        meta = dict(metadata or {})
        meta["frame"] = str(frame or UNKNOWN_FRAME)
        if frame_index is not None:
            meta["frame_index"] = int(frame_index)
        super().__init__(
            name,
            coords,
            axis_names=axis_names,
            properties=properties,
            coordinate_scale=coordinate_scale,
            scale_unit=scale_unit,
            metadata=meta,
        )

    @property
    def frame(self) -> str:
        """The frame of reference the coordinates are measured in."""
        return str(self.metadata.get("frame", UNKNOWN_FRAME))

    @property
    def frame_index(self) -> int | None:
        """Index of the source frame the points were measured on, if known."""
        value = self.metadata.get("frame_index")
        return None if value is None else int(value)

    def in_frame(self, frame: str, coordinates: np.ndarray | Any, *,
                 name: str | None = None) -> "PointsResult":
        """The same points expressed in another frame: what a transform step
        returns. Properties and the source frame index are carried over."""
        return PointsResult(
            name or self.name,
            coordinates,
            frame=frame,
            frame_index=self.frame_index,
            axis_names=self.axis_names,
            properties=dict(self.properties),
            metadata={k: v for k, v in self.metadata.items() if k not in ("frame", "frame_index")},
        )

    def __repr__(self) -> str:
        return f"PointsResult({self.name!r}, {self.count} points in {self.frame!r})"


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
