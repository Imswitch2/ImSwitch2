"""Frames of reference, and the coordinate contract a hardware action enforces.

A ``points`` result carries a *frame tag*: ``detector:Camera`` (pixels of
that detector's frames), ``stage:XYStage`` (micrometres of that positioner),
``scanner:galvo`` (volts), or ``unknown``. The tag alone does not pin down a
detector's pixel grid -- a crop, a binning or a different axis order changes
the coordinates without changing the detector's name -- so a live frame also
carries a :class:`FrameIdentity`, and the engine refuses points measured on a
grid other than the one the pipeline was armed on.

The helpers at the end read coordinates, numbers and yes/no values from
whatever carries them (ImProcess's ``PointsResult`` / ``ValueResult`` /
``EventResult``, this package's :mod:`.values`, a bare array or number),
so the engine and the gates never import a result class.
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from typing import Any

import numpy as np

FRAME_KINDS = ("detector", "stage", "scanner", "unknown")
UNKNOWN_FRAME = "unknown"


class CoordinateError(ValueError):
    """Points that cannot be handed to an action as they are."""


@dataclass(frozen=True)
class FrameTag:
    """``kind:name`` -- what a coordinate is measured in."""

    kind: str
    name: str = ""

    @classmethod
    def parse(cls, text) -> "FrameTag":
        if isinstance(text, FrameTag):
            return text
        text = str(text or UNKNOWN_FRAME).strip()
        if ":" not in text:
            if text != UNKNOWN_FRAME:
                raise CoordinateError(
                    f"bad frame tag {text!r}: expected 'kind:name' with kind one of {FRAME_KINDS[:-1]}, or 'unknown'"
                )
            return cls(UNKNOWN_FRAME)
        kind, name = text.split(":", 1)
        kind, name = kind.strip(), name.strip()
        if kind not in FRAME_KINDS or kind == UNKNOWN_FRAME:
            raise CoordinateError(f"bad frame tag {text!r}: kind must be one of {FRAME_KINDS[:-1]}")
        if not name:
            raise CoordinateError(f"bad frame tag {text!r}: the {kind} needs a name")
        return cls(kind, name)

    @property
    def known(self) -> bool:
        return self.kind != UNKNOWN_FRAME

    def __str__(self) -> str:
        return self.kind if not self.name else f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class FrameIdentity:
    """The pixel grid a detector's frames are on.

    ``shape`` is the frame's (rows, columns), ``binning`` the camera binning
    per axis, ``offset`` the ROI's origin on the full chip, ``axis_order``
    the order of the coordinate columns a points result in this frame uses.
    Two identities that differ in any of these are two coordinate systems
    with the same name.
    """

    shape: tuple[int, ...]
    binning: tuple[int, ...] = (1, 1)
    offset: tuple[int, ...] = (0, 0)
    axis_order: str = "yx"

    def differs_from(self, other: "FrameIdentity") -> list[str]:
        """What is different about ``other``, as readable reasons."""
        reasons = []
        for name in ("shape", "binning", "offset", "axis_order"):
            mine, theirs = getattr(self, name), getattr(other, name)
            if name != "axis_order":
                mine, theirs = tuple(mine), tuple(theirs)
            if mine != theirs:
                reasons.append(f"{name} {mine!r} -> {theirs!r}")
        return reasons

    def to_dict(self) -> dict:
        return {"shape": list(self.shape), "binning": list(self.binning), "offset": list(self.offset),
                "axis_order": self.axis_order}

    @classmethod
    def from_dict(cls, payload: dict) -> "FrameIdentity":
        return cls(
            shape=tuple(int(v) for v in payload.get("shape", ())),
            binning=tuple(int(v) for v in payload.get("binning", (1, 1))),
            offset=tuple(int(v) for v in payload.get("offset", (0, 0))),
            axis_order=str(payload.get("axis_order", "yx")),
        )

    @classmethod
    def of_image(cls, image) -> "FrameIdentity":
        """The identity of a plain array: its shape, unbinned, uncropped."""
        shape = tuple(int(s) for s in np.shape(image)[-2:])
        return cls(shape=shape)


# --------------------------------------------------------------------------
# duck-typed readers
# --------------------------------------------------------------------------

def frame_of(obj) -> str:
    """The frame tag ``obj`` claims, ``"unknown"`` when it claims none."""
    frame = getattr(obj, "frame", None)
    if frame is None:
        metadata = getattr(obj, "metadata", None)
        if isinstance(metadata, dict):
            frame = metadata.get("frame")
    return str(frame or UNKNOWN_FRAME)


def points_of(obj) -> np.ndarray | None:
    """``(N, D)`` coordinates of ``obj``, or ``None`` when it carries none."""
    coordinates = getattr(obj, "coordinates", None)
    if coordinates is None and isinstance(obj, (np.ndarray, list, tuple)):
        coordinates = obj
    if coordinates is None:
        return None
    array = np.asarray(coordinates, dtype=np.float64)
    if array.ndim == 1 and array.size in (2, 3):
        array = array.reshape(1, -1)
    if array.ndim == 1 and array.size == 0:
        array = array.reshape(0, 2)
    if array.ndim != 2:
        return None
    return array


def value_of(obj) -> float | None:
    """The number ``obj`` carries, or ``None``."""
    if isinstance(obj, bool):
        return None
    if isinstance(obj, numbers.Real):
        return float(obj)
    value = getattr(obj, "value", None)
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, np.ndarray) and value.ndim == 0:
        return float(value)
    return None


def fired_of(obj) -> bool | None:
    """The yes/no ``obj`` carries, or ``None`` when it carries none."""
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    fired = getattr(obj, "fired", None)
    if fired is None:
        return None
    return bool(fired)


def kind_of(obj) -> str:
    """The port kind ``obj`` counts as: its declared ``kind``, else guessed
    from what it is (an array of coordinates, a number, a yes/no)."""
    declared = getattr(obj, "kind", None)
    if isinstance(declared, str) and declared:
        return declared
    if fired_of(obj) is not None:
        return "event"
    if value_of(obj) is not None:
        return "value"
    if points_of(obj) is not None:
        return "points"
    return "unknown"


# --------------------------------------------------------------------------
# the coordinate contract
# --------------------------------------------------------------------------

def check_points(obj, *, frame=None, dims=(2, 3), bounds=None, identity=None, expected_identity=None,
                 what: str = "points") -> np.ndarray:
    """The coordinates of ``obj`` as an action may use them, or
    :class:`CoordinateError` saying why not.

    Checked: that there are coordinates, with 2 or 3 columns (``dims``), all
    finite; that their frame tag is exactly ``frame`` when one is expected
    (a tag is not a number: ``detector:Camera`` into a stage move is
    refused, whatever the values); that each column lies within ``bounds``
    (``((lo, hi), ...)`` per column, ``None`` for no limit) when given; and
    that the grid the points were measured on (``identity``) is the one the
    pipeline was armed on (``expected_identity``). An empty set of points
    passes: no detection is a normal tick, and it is the action's caller who
    decides that nothing is done.
    """
    coordinates = points_of(obj)
    if coordinates is None:
        raise CoordinateError(f"{what}: no coordinates (got {type(obj).__name__})")
    if coordinates.shape[1] not in tuple(dims):
        raise CoordinateError(f"{what}: {coordinates.shape[1]} coordinate columns; expected {' or '.join(map(str, dims))}")
    if coordinates.size and not np.all(np.isfinite(coordinates)):
        raise CoordinateError(f"{what}: a coordinate is NaN or infinite")
    if frame is not None:
        have = frame_of(obj)
        want = str(FrameTag.parse(frame))
        if have != want:
            raise CoordinateError(f"{what} are in frame {have!r}; the action takes {want!r}")
    if bounds is not None and coordinates.size:
        for column, limits in enumerate(bounds):
            if limits is None or column >= coordinates.shape[1]:
                continue
            low, high = limits
            values = coordinates[:, column]
            if (low is not None and float(values.min()) < low) or (high is not None and float(values.max()) > high):
                raise CoordinateError(
                    f"{what}: column {column} outside [{low}, {high}] "
                    f"(have {float(values.min()):g}..{float(values.max()):g})"
                )
    if identity is not None and expected_identity is not None:
        reasons = expected_identity.differs_from(identity)
        if reasons:
            raise CoordinateError(f"{what} were measured on a different grid than the pipeline was armed on: "
                                  + "; ".join(reasons))
    return coordinates


def finite_value(obj, *, what: str = "value") -> float:
    """The number ``obj`` carries, finite, or :class:`CoordinateError`."""
    value = value_of(obj)
    if value is None:
        raise CoordinateError(f"{what}: not a number (got {type(obj).__name__})")
    if not math.isfinite(value):
        raise CoordinateError(f"{what}: {value!r} is not finite")
    return value


__all__ = [
    "FRAME_KINDS",
    "UNKNOWN_FRAME",
    "CoordinateError",
    "FrameIdentity",
    "FrameTag",
    "check_points",
    "finite_value",
    "fired_of",
    "frame_of",
    "kind_of",
    "points_of",
    "value_of",
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
