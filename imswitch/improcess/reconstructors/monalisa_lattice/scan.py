"""The scan of a recording, from what the recording says about itself.

Two readings, in order of preference:

* :func:`layout_scan` reads the resolved acquisition layout, the one
  description of a recording every reconstructor shares. It places every
  stored frame: which Z plane and timepoint it belongs to, and where in the
  camera plane the sample was, with the loop directions applied by the one
  helper in ``imcommon`` that interprets them. Either camera axis may be
  scanned or not: a recording of Y and Z steps alone is an image per Z
  plane whose X axis is one pixel per lattice cell.
* :func:`recorded_scan` reads the ``ScanStage``/``ScanTTL`` attributes for
  recordings without a usable layout, as one raster of ``num_fast x
  num_slow`` steps per stack; a recording whose attributes do not add up to
  its number of frames is taken to be a square scan, which is what the
  older recordings are.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    UnconsumedLoopError,
    iter_physical_coordinates,
    iter_recorded_coordinates,
    select_loops,
)


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


def recorded_axis_step_nm(attrs, axis: str) -> float | None:
    """Read the individual stage-axis pitch (the old reader used X only)."""
    index = "xyz".index(axis)
    try:
        values = np.asarray(attrs["ScanStage:axis_step_size"], dtype=float).ravel()
        value = float(values[index])
        unit = attrs.get("ScanStage:axis_step_size_unit", "um")
        if isinstance(unit, bytes):
            unit = unit.decode("utf-8")
        factor = _UNITS_TO_NM.get(str(unit).lower() if unit is not None else None)
        if factor is None or not np.isfinite(value) or value <= 0:
            return None
        return value * factor
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def override_layout_z(scan: "LayoutScan", num_planes: int) -> "LayoutScan":
    """Regroup whole contiguous raster stacks into manually declared Z planes.

    A different Z count cannot reinterpret interleaved event loops safely.
    Refuse those recordings rather than silently assigning frames to the
    wrong plane. The user-specified count assumes Z is inside time.
    """
    if num_planes <= 0 or num_planes == scan.num_planes:
        return scan
    if scan.num_conditions != 1:
        raise ValueError("Manual Z-plane count is not supported for condition-interleaved scans")
    if scan.num_stacks % num_planes:
        raise ValueError(
            f"The {scan.num_stacks} recorded raster stacks cannot form "
            f"{num_planes} Z planes per timepoint"
        )
    for index in range(scan.num_stacks):
        positions = scan.frames_of(index)
        expected = np.arange(index * scan.frames_per_stack,
                             (index + 1) * scan.frames_per_stack)
        if not np.array_equal(positions, expected):
            raise ValueError(
                "Cannot override Z count: acquisition layout interleaves "
                "raster stacks. The frame-to-plane order would be ambiguous."
            )
    return replace(
        scan, num_planes=num_planes, num_time=scan.num_stacks // num_planes,
        stack=np.repeat(np.arange(scan.num_stacks), scan.frames_per_stack),
        source="acquisition layout, Z count from the panel",
    )


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


# ------------------------------------------------------------------ layout
_UNITS_TO_NM = {None: 1000.0, "um": 1000.0, "µm": 1000.0, "nm": 1.0, "mm": 1e6, "m": 1e9}


def _loop_step_nm(loop) -> float | None:
    """The pitch of a loop in nanometres, or ``None`` when it has none."""
    if loop is None or not loop.step:
        return None
    unit = str(loop.unit).lower() if loop.unit is not None else None
    if unit not in _UNITS_TO_NM:
        raise ValueError(
            f"Loop {loop.id!r} records its step in {loop.unit!r}, a unit of "
            "length this reconstructor does not know"
        )
    return float(loop.step) * _UNITS_TO_NM[unit]


@dataclass(frozen=True)
class LayoutScan:
    """Every stored frame of a recording placed by its acquisition layout.

    Attributes:
        index: ``(frames, 2)`` integer position of the sample in the camera
            plane for every frame, ``(x, y)`` in steps of the scan, increasing
            with physical position. An axis the recording does not scan is 0
            throughout.
        logical: ``(frames, 2)`` the same positions before the loop
            directions were applied: the order the positions were visited in
            along each axis, for what depends on chronology (the gain of the
            frames).
        stack: ``(frames,)`` the stack every frame belongs to, one stack per
            timepoint, condition and Z plane, in that order:
            ``(t * num_conditions + c) * num_planes + z``.
        num_time, num_planes: Timepoints and Z planes.
        num_conditions, condition_labels: Line-step conditions (a colour
            each, say) and their names.
        counts: ``(nx, ny)`` positions along the camera axes; 1 for an axis
            that is not scanned.
        step_nm: ``(x, y)`` size of a step along each camera axis; ``None``
            for an axis that is not scanned, or whose step the recording does
            not say.
        plane_step_nm: Distance between Z planes, or ``None``.
        fast_axis: ``"x"`` or ``"y"``, the scanned axis stepped innermost.
        source: Where the numbers come from, for the log.
    """

    index: np.ndarray
    logical: np.ndarray
    stack: np.ndarray
    num_time: int
    num_planes: int
    num_conditions: int
    condition_labels: tuple[str, ...]
    counts: tuple[int, int]
    step_nm: tuple[float | None, float | None]
    plane_step_nm: float | None
    fast_axis: str
    source: str = "acquisition layout"

    @property
    def num_stacks(self) -> int:
        return int(self.num_time * self.num_conditions * self.num_planes)

    @property
    def stack_shape(self) -> tuple[int, int, int]:
        """``(timepoints, conditions, planes)``: how the stacks are numbered."""
        return (int(self.num_time), int(self.num_conditions), int(self.num_planes))

    @property
    def frames_per_stack(self) -> int:
        return int(self.counts[0] * self.counts[1])

    @property
    def scanned_axes(self) -> tuple[str, ...]:
        return tuple(axis for axis, count in zip("xy", self.counts) if count > 1)

    @property
    def in_plane_shape(self) -> tuple[int, int]:
        """``(num_fast, num_slow)`` of the in-plane raster of one stack."""
        nx, ny = self.counts
        return (nx, ny) if self.fast_axis == "x" else (ny, nx)

    def frames_of(self, stack: int) -> np.ndarray:
        """Indices of the frames of one stack, in storage order."""
        return np.flatnonzero(self.stack == int(stack))

    def gain_positions(self, stack: int) -> tuple[np.ndarray, np.ndarray]:
        """``(fast, slow)`` chronological positions of the frames of a stack."""
        logical = self.logical[self.frames_of(stack)]
        fast, slow = (0, 1) if self.fast_axis == "x" else (1, 0)
        return logical[:, fast], logical[:, slow]

    def describe(self) -> str:
        parts = []
        for axis, count, step in zip("xy", self.counts, self.step_nm):
            if count > 1:
                size = "unknown step" if step is None else f"{step:g} nm"
                parts.append(f"{axis}: {count} steps of {size}")
            else:
                parts.append(f"{axis}: not scanned")
        if self.num_planes > 1:
            size = "" if self.plane_step_nm is None else f" of {self.plane_step_nm:g} nm"
            parts.append(f"{self.num_planes} Z planes{size}")
        if self.num_conditions > 1:
            names = ", ".join(self.condition_labels)
            parts.append(f"{self.num_conditions} conditions ({names})")
        if self.num_time > 1:
            parts.append(f"{self.num_time} timepoints")
        return ", ".join(parts) + f" ({self.source})"


def layout_scan(resolved, num_frames: int) -> LayoutScan | None:
    """The scan of a recording as its resolved acquisition layout records it.

    Returns ``None`` when the recording has no usable layout (the caller
    falls back to :func:`recorded_scan`), or when an inferred layout holds
    a loop this reconstructor cannot place: an inference must not fail a
    file. A declared layout that cannot be placed raises instead, with the
    loop named.

    Line-step conditions (one colour each, interleaved per line or recorded
    one image per condition) make a ``C`` axis: every condition is a scan of
    its own, placed by its coordinates.

    Raises:
        ValueError: A declared layout with a loop that cannot be placed; a
            recording that scans neither camera axis; a layout whose frame
            count is not the recording's.
    """
    layout = getattr(resolved, "layout", None)
    if layout is None or not getattr(resolved, "is_usable", False):
        return None
    if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return None
    try:
        selected = select_loops(
            layout,
            consumer="MoNaLISA lattice reconstructor",
            roles={
                "x": "scan_x", "y": "scan_y", "z": "scan_z",
                "time": "time", "condition": "condition",
            },
        )
    except UnconsumedLoopError as error:
        if getattr(resolved, "is_authoritative", False):
            raise ValueError(str(error)) from error
        return None
    condition = selected["condition"]
    num_conditions = int(condition.count) if condition is not None else 1
    condition_labels: tuple[str, ...] = ()
    if condition is not None:
        condition_labels = tuple(condition.labels) or tuple(
            f"condition_{index}" for index in range(num_conditions)
        )
    x_loop, y_loop, z_loop, t_loop = (
        selected["x"], selected["y"], selected["z"], selected["time"]
    )
    counts = (
        int(x_loop.count) if x_loop is not None else 1,
        int(y_loop.count) if y_loop is not None else 1,
    )
    if max(counts) < 2:
        raise ValueError(
            "The acquisition layout scans neither camera axis: there is no "
            "scan of the lattice cell to reconstruct"
        )
    num_planes = int(z_loop.count) if z_loop is not None else 1
    num_time = int(t_loop.count) if t_loop is not None else 1

    # The innermost scanned in-plane loop is the fast axis.
    fast_axis = "x"
    for loop in layout.event_loops:
        if loop is x_loop and counts[0] > 1:
            fast_axis = "x"
        elif loop is y_loop and counts[1] > 1:
            fast_axis = "y"

    def coordinate(coordinates, loop):
        return int(coordinates[loop.id]) if loop is not None else 0

    index, logical, stack = [], [], []
    for physical, recorded in zip(
        iter_physical_coordinates(layout), iter_recorded_coordinates(layout)
    ):
        index.append((coordinate(physical, x_loop), coordinate(physical, y_loop)))
        logical.append((coordinate(recorded, x_loop), coordinate(recorded, y_loop)))
        stack.append(
            (coordinate(physical, t_loop) * num_conditions + coordinate(physical, condition))
            * num_planes + coordinate(physical, z_loop)
        )
    if len(index) != int(num_frames):
        raise ValueError(
            f"The acquisition layout records {len(index)} frames but this "
            f"source has {num_frames}. Frames are never dropped or padded to "
            "fit a scan shape."
        )
    return LayoutScan(
        index=np.asarray(index, dtype=int).reshape(-1, 2),
        logical=np.asarray(logical, dtype=int).reshape(-1, 2),
        stack=np.asarray(stack, dtype=int),
        num_time=num_time,
        num_planes=num_planes,
        num_conditions=num_conditions,
        condition_labels=condition_labels,
        counts=counts,
        step_nm=(
            _loop_step_nm(x_loop) if counts[0] > 1 else None,
            _loop_step_nm(y_loop) if counts[1] > 1 else None,
        ),
        plane_step_nm=_loop_step_nm(z_loop) if num_planes > 1 else None,
        fast_axis=fast_axis,
    )


def recorded_fast_axis(resolved) -> str:
    """Fast physical axis for displaying historical fast/slow overrides."""
    layout = getattr(resolved, "layout", None)
    if layout is not None and getattr(resolved, "is_usable", False):
        for loop in reversed(layout.event_loops):
            if loop.kind in ("scan_x", "scan_y") and loop.count > 1:
                return loop.kind[-1]
    return "x"  # Legacy recordings conventionally step stage X first.


def has_stage_count_override(params: dict) -> bool:
    return any(int(params.get(f"scan_steps_{axis}", 0) or 0) > 0 for axis in "xy")


def stage_count_scan(resolved, attrs, num_frames: int, params: dict) -> LayoutScan:
    """Resolve explicit stage-axis counts without conflating order and orientation.

    Reinterpret a complete recorded event layout with updated loop counts,
    preserving its loop order, traversal and directions. Do not guess how to
    insert an absent scanned axis or reinterpret partial event selections.
    Without usable layout metadata, use the legacy X-fast contiguous raster
    convention, but return stage coordinates so camera orientation remains
    independent of acquisition order.
    """
    from math import prod

    counts = {axis: int(params.get(f"scan_steps_{axis}", 0) or 0) for axis in "xyz"}
    layout = getattr(resolved, "layout", None)
    if layout is not None and getattr(resolved, "is_usable", False):
        loops = {loop.kind: loop for loop in layout.event_loops}
        lateral_changed = False
        for axis in "xy":
            loop = loops.get(f"scan_{axis}")
            recorded = loop.count if loop is not None else 1
            lateral_changed |= counts[axis] > 0 and counts[axis] != recorded
        if not lateral_changed:
            scan = layout_scan(resolved, num_frames)
            if scan is not None:
                return override_layout_z(scan, counts["z"])
        else:
            if layout.recorded_event_spans is not None:
                raise ValueError("Cannot change X/Y counts for a partial event layout")
            for axis, count in counts.items():
                if count > 1 and f"scan_{axis}" not in loops:
                    raise ValueError(f"Cannot add a scanned {axis.upper()} axis: its acquisition order is not recorded")
            updated = tuple(
                replace(loop, count=counts[loop.kind[-1]])
                if loop.kind in ("scan_x", "scan_y", "scan_z") and counts[loop.kind[-1]] > 0
                else loop for loop in layout.event_loops
            )
            # Retain all non-spatial loops. A count correction that does not
            # fit the stored events is rejected, not silently treated as time.
            if prod(loop.count for loop in updated) != num_frames:
                raise ValueError("Manual XYZ positions do not match the recorded frame count and other acquisition loops")
            corrected = replace(resolved, layout=replace(layout, event_loops=updated))
            scan = layout_scan(corrected, num_frames)
            if scan is None:
                raise ValueError("Cannot apply XYZ counts to this acquisition layout")
            return replace(scan, source="acquisition layout, positions from the panel")

    attrs = attrs or {}
    inferred = _counts_from_attributes(attrs, num_frames)
    for index, axis in enumerate("xy"):
        if counts[axis] <= 0:
            try:
                counts[axis] = int(np.asarray(attrs[f"ScanTTL:N{axis}"]).ravel()[0])
            except (KeyError, TypeError, ValueError, IndexError):
                counts[axis] = inferred[index] if inferred else 0
    if min(counts["x"], counts["y"]) <= 0:
        raise ValueError("Set both X and Y positions when recording counts are unavailable")
    nx, ny = counts["x"], counts["y"]
    raster = recorded_scan(attrs, num_frames, nx, ny)
    if max(nx, ny) < 2:
        raise ValueError("At least one lateral axis must be scanned")
    nz = counts["z"]
    if nz <= 0:
        try:
            nz = int(np.asarray(attrs.get("ScanTTL:Nz", 1)).ravel()[0])
        except (TypeError, ValueError, IndexError):
            nz = 1
    nz = max(1, nz)
    if raster.num_stacks % nz:
        raise ValueError(f"The {raster.num_stacks} raster stacks cannot form {nz} Z planes per timepoint")
    k = np.arange(raster.frames_per_stack)
    index = np.tile(np.column_stack((k % nx, k // nx)), (raster.num_stacks, 1))
    return LayoutScan(
        index=index, logical=index.copy(),
        stack=np.repeat(np.arange(raster.num_stacks), raster.frames_per_stack),
        num_time=raster.num_stacks // nz, num_planes=nz, num_conditions=1,
        condition_labels=(), counts=(nx, ny),
        step_nm=tuple(recorded_axis_step_nm(attrs, axis) if count > 1 else None
                      for axis, count in zip("xy", (nx, ny))),
        plane_step_nm=recorded_axis_step_nm(attrs, "z") if nz > 1 else None,
        fast_axis="x" if nx > 1 else "y",
        source="manual stage positions, contiguous X-fast raster assumed",
    )


def describe_recording(attrs, data_obj) -> str:
    """One line on the scan of a recording, for the panel: the layout's
    reading when the recording has one, the attributes' otherwise."""
    try:
        num_frames = int(getattr(data_obj, "numFrames", 0) or 0)
    except Exception:
        num_frames = 0
    if num_frames <= 0:
        return "no recording"
    try:
        scan = layout_scan(getattr(data_obj, "acquisition_layout", None), num_frames)
        if scan is not None:
            return scan.describe()
        return recorded_scan(attrs or {}, num_frames).describe()
    except ValueError as exc:
        return str(exc)


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
