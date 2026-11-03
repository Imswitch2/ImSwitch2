"""The transform slot: points from one frame of reference into another.

A :class:`CoordinateTransform` knows the frame it reads (``source_frame``)
and the one it writes (``target_frame``) and maps ``(N, D)`` coordinates
between them. Two are shipped: :class:`Identity` (the same frame, for a
pipeline whose detection already measures in the frame the action takes --
it is *one click away*, never implicit: a points result reaches an action
only through a ``transform`` step) and :class:`ScaleFlip` (pixels into
micrometres with a pixel size, an optional swap of the two axes, inversions
and an offset: what ``EtSTEDInfo`` holds). Calibrated transforms -- the
polynomial of EtSTED, an affine from a click calibration -- implement the
same protocol and are registered by id; the file format
(:func:`transform_from_dict`) keeps ``type`` for the class and the rest for
its fields, so a calibration file is one of these dicts.

Column order: a points result's columns are in the order its frame's
``axis_order`` says (``yx`` for an image: row, column). A transform keeps the
column order of its source frame unless it says otherwise (``ScaleFlip`` with
``swap_xy``); the action's axis map then says which column drives which
positioner axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

import numpy as np

from imswitch.imcommon.model.smartpipeline.frames import CoordinateError, FrameTag, frame_of, points_of


@runtime_checkable
class CoordinateTransform(Protocol):
    id: str
    source_frame: str
    target_frame: str

    def apply(self, points: np.ndarray, context: Mapping[str, Any]) -> np.ndarray:
        """``(N, D)`` in the source frame -> ``(N, D)`` in the target frame.
        ``context`` carries what the tick knows (the frame envelope, the
        step's params); a transform that needs nothing ignores it."""

    def to_dict(self) -> dict: ...


@dataclass(frozen=True)
class Identity:
    """The same coordinates, declared to be in the frame they already are in."""

    id: str
    source_frame: str
    target_frame: str = ""

    def __post_init__(self):
        object.__setattr__(self, "source_frame", str(FrameTag.parse(self.source_frame)))
        target = self.target_frame or self.source_frame
        if str(FrameTag.parse(target)) != self.source_frame:
            raise CoordinateError(
                f"identity transform {self.id!r} cannot relabel {self.source_frame!r} as {target!r}: "
                "a change of frame needs a calibrated transform"
            )
        object.__setattr__(self, "target_frame", self.source_frame)

    def apply(self, points, context=None) -> np.ndarray:
        return np.array(points, dtype=np.float64, copy=True)

    def to_dict(self) -> dict:
        return {"type": "identity", "id": self.id, "source_frame": self.source_frame,
                "target_frame": self.target_frame}

    @classmethod
    def from_dict(cls, payload: dict) -> "Identity":
        return cls(id=str(payload.get("id") or "identity"), source_frame=payload["source_frame"],
                   target_frame=payload.get("target_frame", ""))


@dataclass(frozen=True)
class ScaleFlip:
    """Pixels of a detector into the units of a stage or scanner.

    ``out = (p - origin) * pixel_size`` per column, then the two columns
    swapped when ``swap_xy``, then each column negated when ``invert``
    says so, then ``offset`` added. ``origin`` is in source pixels
    (``(0, 0)`` for the corner; the frame centre for a displacement from
    where the field is), ``pixel_size`` in target units per source pixel
    per column, ``offset`` in target units. With ``relative`` true the result
    is a *displacement* (the action moves by it); otherwise it is an absolute
    position in the target frame (the action moves to it).
    """

    id: str
    source_frame: str
    target_frame: str
    pixel_size: tuple[float, ...] = (1.0, 1.0)
    origin: tuple[float, ...] = (0.0, 0.0)
    swap_xy: bool = False
    invert: tuple[bool, ...] = (False, False)
    offset: tuple[float, ...] = (0.0, 0.0)
    relative: bool = False
    calibrated_on: dict | None = None

    def __post_init__(self):
        object.__setattr__(self, "source_frame", str(FrameTag.parse(self.source_frame)))
        object.__setattr__(self, "target_frame", str(FrameTag.parse(self.target_frame)))
        for name in ("pixel_size", "origin", "offset"):
            object.__setattr__(self, name, tuple(float(v) for v in getattr(self, name)))
        object.__setattr__(self, "invert", tuple(bool(v) for v in self.invert))
        if any(v == 0 for v in self.pixel_size):
            raise CoordinateError(f"transform {self.id!r}: a pixel size of 0")

    def apply(self, points, context=None) -> np.ndarray:
        array = np.array(points, dtype=np.float64, copy=True)
        if array.size == 0:
            return array
        columns = array.shape[1]
        origin = np.asarray(_padded(self.origin, columns, 0.0))
        scale = np.asarray(_padded(self.pixel_size, columns, 1.0))
        out = (array - origin) * scale
        if self.swap_xy and columns >= 2:
            out[:, [0, 1]] = out[:, [1, 0]]
        for column, flip in enumerate(_padded(self.invert, columns, False)):
            if flip:
                out[:, column] = -out[:, column]
        out += np.asarray(_padded(self.offset, columns, 0.0))
        return out

    def to_dict(self) -> dict:
        payload = {"type": "scale-flip", "id": self.id, "source_frame": self.source_frame,
                   "target_frame": self.target_frame, "pixel_size": list(self.pixel_size),
                   "origin": list(self.origin), "swap_xy": self.swap_xy, "invert": list(self.invert),
                   "offset": list(self.offset), "relative": self.relative}
        if self.calibrated_on:
            payload["calibrated_on"] = dict(self.calibrated_on)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ScaleFlip":
        return cls(
            id=str(payload["id"]), source_frame=payload["source_frame"], target_frame=payload["target_frame"],
            pixel_size=tuple(payload.get("pixel_size", (1.0, 1.0))), origin=tuple(payload.get("origin", (0.0, 0.0))),
            swap_xy=bool(payload.get("swap_xy", False)), invert=tuple(payload.get("invert", (False, False))),
            offset=tuple(payload.get("offset", (0.0, 0.0))), relative=bool(payload.get("relative", False)),
            calibrated_on=payload.get("calibrated_on"),
        )


def _padded(values, length: int, fill):
    values = list(values)
    return (values + [fill] * length)[:length]


#: The 20 coefficients of EtSTED's third-order polynomial, in its order.
POLYNOMIAL3_COEFFICIENTS = 20


def polynomial3_apply(coefficients, c1, c2):
    """EtSTED's third-order polynomial: ``(c1, c2) -> (out1, out2)``, vectorised."""
    a = coefficients
    out1 = (a[0] * c1**3 + a[1] * c2**3 + a[2] * c2 * c1**2 + a[3] * c1 * c2**2
            + a[4] * c1**2 + a[5] * c2**2 + a[6] * c1 * c2 + a[7] * c1 + a[8] * c2 + a[9])
    out2 = (a[10] * c1**3 + a[11] * c2**3 + a[12] * c2 * c1**2 + a[13] * c1 * c2**2
            + a[14] * c1**2 + a[15] * c2**2 + a[16] * c1 * c2 + a[17] * c1 + a[18] * c2 + a[19])
    return out1, out2


@dataclass(frozen=True)
class Polynomial3:
    """EtSTED's calibrated transform: a third-order polynomial from one
    detector's pixels to the scan (or stage) coordinates, with the setup's
    ``swapXY`` / ``invertX`` / ``invertY`` applied before it, as
    ``EtSTEDTransformService.prepare_input_coords`` does.

    The polynomial reads a point as ``(c1, c2)`` -- EtSTED's ``(x, y)`` --
    while a points result's columns are ``(row, col)`` = ``(y, x)``; the
    transform takes the columns in the order ``columns`` says (``"yx"``:
    column 1 is x) and writes its two outputs back in the same places.
    ``calibrated_on`` is the pixel grid the calibration was made on
    (:class:`~.frames.FrameIdentity` as a dict); a run on another grid is
    refused at arm.
    """

    id: str
    source_frame: str
    target_frame: str
    coefficients: tuple[float, ...]
    swap_xy: bool = False
    invert_x: bool = False
    invert_y: bool = False
    columns: str = "yx"
    calibrated_on: dict | None = None

    def __post_init__(self):
        object.__setattr__(self, "source_frame", str(FrameTag.parse(self.source_frame)))
        object.__setattr__(self, "target_frame", str(FrameTag.parse(self.target_frame)))
        coefficients = tuple(float(c) for c in np.asarray(self.coefficients, dtype=float).reshape(-1))
        if len(coefficients) != POLYNOMIAL3_COEFFICIENTS:
            raise CoordinateError(f"transform {self.id!r}: a third-order polynomial has "
                                  f"{POLYNOMIAL3_COEFFICIENTS} coefficients, got {len(coefficients)}")
        object.__setattr__(self, "coefficients", coefficients)
        if self.columns not in ("yx", "xy"):
            raise CoordinateError(f"transform {self.id!r}: columns must be 'yx' or 'xy'")

    def apply(self, points, context=None) -> np.ndarray:
        array = np.array(points, dtype=np.float64, copy=True)
        if array.size == 0:
            return array
        if array.shape[1] < 2:
            raise CoordinateError(f"transform {self.id!r} needs two coordinate columns")
        x_col, y_col = (1, 0) if self.columns == "yx" else (0, 1)
        x, y = array[:, x_col].copy(), array[:, y_col].copy()
        if self.swap_xy:
            x, y = y, x
        if self.invert_x:
            x = -x
        if self.invert_y:
            y = -y
        out1, out2 = polynomial3_apply(self.coefficients, x, y)
        out = array.copy()
        out[:, x_col], out[:, y_col] = out1, out2
        return out

    def to_dict(self) -> dict:
        payload = {"type": "polynomial3", "id": self.id, "source_frame": self.source_frame,
                   "target_frame": self.target_frame, "coefficients": list(self.coefficients),
                   "swap_xy": self.swap_xy, "invert_x": self.invert_x, "invert_y": self.invert_y,
                   "columns": self.columns}
        if self.calibrated_on:
            payload["calibrated_on"] = dict(self.calibrated_on)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "Polynomial3":
        return cls(
            id=str(payload["id"]), source_frame=payload["source_frame"], target_frame=payload["target_frame"],
            coefficients=tuple(payload["coefficients"]), swap_xy=bool(payload.get("swap_xy", False)),
            invert_x=bool(payload.get("invert_x", False)), invert_y=bool(payload.get("invert_y", False)),
            columns=str(payload.get("columns", "yx")), calibrated_on=payload.get("calibrated_on"),
        )


_TYPES = {"identity": Identity, "scale-flip": ScaleFlip, "polynomial3": Polynomial3}


def transform_from_dict(payload: dict):
    """A transform from its ``to_dict`` form (``type`` picks the class)."""
    if not isinstance(payload, dict):
        raise CoordinateError(f"a transform must be an object, got {payload!r}")
    kind = str(payload.get("type") or "")
    cls = _TYPES.get(kind)
    if cls is None:
        raise CoordinateError(f"unknown transform type {kind!r} (known: {', '.join(_TYPES)})")
    try:
        return cls.from_dict(payload)
    except KeyError as exc:
        raise CoordinateError(f"transform of type {kind!r} needs {exc.args[0]!r}") from None


class TransformRegistry:
    """The transforms a pipeline may name, by id."""

    def __init__(self, transforms=()):
        self._by_id: dict[str, Any] = {}
        for transform in transforms:
            self.register(transform)

    def register(self, transform) -> None:
        self._by_id[str(transform.id)] = transform

    def get(self, transform_id: str):
        return self._by_id.get(str(transform_id))

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def __contains__(self, transform_id) -> bool:
        return str(transform_id) in self._by_id

    @classmethod
    def from_dicts(cls, payloads) -> "TransformRegistry":
        return cls(transform_from_dict(payload) for payload in payloads)

    def to_dicts(self) -> list[dict]:
        return [transform.to_dict() for transform in self._by_id.values()]


def apply_transform(obj, transform, context: Mapping[str, Any] | None = None, *, name: str | None = None):
    """``obj``'s points in ``transform.target_frame``, keeping the class of
    ``obj`` when it has ``in_frame`` (ImProcess's ``PointsResult``), else as
    :class:`~.values.Points`. The frame tag of ``obj`` must be the one the
    transform reads."""
    from imswitch.imcommon.model.smartpipeline.values import Points

    coordinates = points_of(obj)
    if coordinates is None:
        raise CoordinateError(f"transform {transform.id!r}: no coordinates in {type(obj).__name__}")
    have = frame_of(obj)
    if have != transform.source_frame:
        raise CoordinateError(
            f"transform {transform.id!r} reads {transform.source_frame!r}, but the points are in {have!r}"
        )
    moved = np.asarray(transform.apply(coordinates, dict(context or {})), dtype=np.float64)
    if moved.shape != coordinates.shape:
        raise CoordinateError(f"transform {transform.id!r} returned shape {moved.shape} for {coordinates.shape}")
    in_frame = getattr(obj, "in_frame", None)
    if callable(in_frame):
        return in_frame(transform.target_frame, moved, name=name)
    return Points(moved, frame=transform.target_frame, frame_index=getattr(obj, "frame_index", None),
                  name=name or getattr(obj, "name", "points"))


__all__ = [
    "POLYNOMIAL3_COEFFICIENTS",
    "CoordinateTransform",
    "Identity",
    "Polynomial3",
    "ScaleFlip",
    "polynomial3_apply",
    "TransformRegistry",
    "apply_transform",
    "transform_from_dict",
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
