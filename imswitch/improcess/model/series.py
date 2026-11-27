"""A series: the same measurement repeated, one source per timepoint.

A timelapse is stored in one of these ways today:

``array``
    One array with a T axis in front. Not a series here: auto-indexing
    (:mod:`~imswitch.improcess.model.iteration`) loops it.
``recording-lapse``, one file per timepoint
    The Recording controller's camera/scan lapse: ``<name>_time07_Camera.h5``,
    ``<name>_scan0007_APD.zarr``. Every file says which lapse it is part of
    (``recording:lapse_index``, ``recording:num_timepoints``).
``recording-lapse``, one group per timepoint
    A scan lapse in one file: ``scan0/Camera``, ``scan1/Camera``...
    (``recording:single_lapse_file``).
``tiling-timelapse``
    The Timelapse panel's tiling kind: ``timelapse_<date>/timelapse.json`` and
    one tiling dataset folder ``t0/ t1/ ...`` per timepoint.

The last three are *series*: several sources, one per timepoint. This module
is the one place that recognises them, whatever will read them. It is Qt-free
and knows no reconstructor:

* :func:`discover_series` -- given any opened source (a path, a ``DataObj``),
  is it one timepoint of a series? Returns the ordered members with time
  stamps and completeness, or ``None``. The recording lapses are recognised by
  :mod:`~imswitch.improcess.model.lapse_source` (which the Time lapse
  reconstructor also uses), the tiling timelapse by
  :mod:`~imswitch.imcommon.algorithms.tile_timelapse`; nothing is
  re-implemented here.
* :class:`SeriesRequest` -- what the user asked for: this timepoint, all, or a
  range; what to do about a missing one; what to do when the outputs differ in
  size. Carried as reserved ``_series*`` step parameters, like ``_iterate``.
* :func:`stack_series` -- the default way to put per-timepoint results
  together: one result with a leading ``T`` axis, the time stamps in its
  metadata, every timepoint on one canvas.

How a reconstruction runs over a series is
:mod:`imswitch.improcess.reconstructors.series_run`.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from imswitch.improcess.model.param_spec import ParamField

# ----------------------------------------------------------------------
# Reserved parameters
# ----------------------------------------------------------------------

#: ``auto`` | ``this`` | ``all`` | ``range``. ``auto`` is what a reconstructor
#: declares as its ``series_default``, or ``all`` when the series itself (its
#: manifest) was opened rather than one timepoint of it.
SERIES_PARAM = "_series"
SERIES_FIRST_PARAM = "_series_first"
SERIES_LAST_PARAM = "_series_last"
SERIES_STEP_PARAM = "_series_step"
#: What to do about a timepoint that is missing or incomplete: ``skip`` it
#: (T is shorter), or keep T regular with a ``zeros`` / ``nan`` plane.
SERIES_MISSING_PARAM = "_series_missing"
#: What to do when the per-timepoint outputs differ in size: ``refuse`` (name
#: the sizes), or pad to the common canvas anchored at the ``pad_origin``
#: (top-left) or ``pad_center``.
SERIES_SHAPES_PARAM = "_series_shapes"

SERIES_PARAMS = (
    SERIES_PARAM, SERIES_FIRST_PARAM, SERIES_LAST_PARAM, SERIES_STEP_PARAM,
    SERIES_MISSING_PARAM, SERIES_SHAPES_PARAM,
)

MODES = ("auto", "this", "all", "range")
MISSING_POLICIES = ("skip", "zeros", "nan")
SHAPE_POLICIES = ("refuse", "pad_origin", "pad_center")

KIND_RECORDING_LAPSE = "recording-lapse"
KIND_TILING_TIMELAPSE = "tiling-timelapse"

STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_MISSING = "missing"
#: A member listed from a directory listing whose header was not read.
STATE_UNKNOWN = "unknown"

#: Key of the series description in a result's ``metadata``.
METADATA_KEY = "series"


class SeriesError(RuntimeError):
    """A series could not be put together; the message says what to change."""


def series_param_fields() -> tuple[ParamField, ...]:
    """The reserved parameters as fields, for forms and the workflow editor.

    They are *not* part of a plugin's ``default_params()`` (a plugin that has
    never heard of series must keep passing the parameter-contract tests);
    hosts that build forms append these for every plugin that supports series.
    """
    group = "Series"
    return (
        ParamField(
            SERIES_PARAM, "select", "auto", label="Timepoints", group=group,
            options=MODES,
            option_labels=("Default", "This timepoint only", "All timepoints", "Range"),
            help="For a source that is one timepoint of a series. Default: what the "
                 "reconstructor declares, or all when the series itself was opened. "
                 "Ignored for a source that is not part of a series.",
        ),
        ParamField(SERIES_FIRST_PARAM, "int", 0, label="First timepoint", group=group, min=0,
                   help="Range: first timepoint (0-based, as numbered by the acquisition)."),
        ParamField(SERIES_LAST_PARAM, "int", None, label="Last timepoint", group=group, min=0,
                   nullable=True, help="Range: last timepoint, inclusive; empty for the last one."),
        ParamField(SERIES_STEP_PARAM, "int", 1, label="Every n-th", group=group, min=1,
                   help="Range: use every n-th timepoint."),
        ParamField(
            SERIES_MISSING_PARAM, "select", "skip", label="Missing timepoints", group=group,
            options=MISSING_POLICIES, option_labels=("Skip", "Fill with zeros", "Fill with NaN"),
            help="A timepoint that is missing or incomplete is reported either way. "
                 "Skip leaves it out; zeros/NaN keep T regular so index n stays timepoint n.",
        ),
        ParamField(
            SERIES_SHAPES_PARAM, "select", "refuse", label="Different sizes", group=group,
            options=SHAPE_POLICIES,
            option_labels=("Refuse", "Pad (top-left)", "Pad (centred)"),
            help="When the timepoints' outputs differ in size. Refuse names the sizes; "
                 "padding puts every timepoint on the largest canvas, anchored at the "
                 "top-left or the centre, with zeros (NaN if missing planes are NaN).",
        ),
    )


@dataclass(frozen=True)
class SeriesRequest:
    """What was asked of a series."""

    mode: str = "auto"
    first: int = 0
    last: int | None = None
    step: int = 1
    missing: str = "skip"
    shapes: str = "refuse"

    def __post_init__(self):
        if self.mode not in MODES:
            raise SeriesError(f"{SERIES_PARAM}: {self.mode!r} is not one of {list(MODES)}")
        if self.missing not in MISSING_POLICIES:
            raise SeriesError(f"{SERIES_MISSING_PARAM}: {self.missing!r} is not one of {list(MISSING_POLICIES)}")
        if self.shapes not in SHAPE_POLICIES:
            raise SeriesError(f"{SERIES_SHAPES_PARAM}: {self.shapes!r} is not one of {list(SHAPE_POLICIES)}")
        if int(self.step) < 1:
            raise SeriesError(f"{SERIES_STEP_PARAM} must be at least 1, got {self.step!r}")

    def effective_mode(self, default: str = "this", *, opened_as_series: bool = False) -> str:
        """``this`` / ``all`` / ``range`` after resolving ``auto``."""
        if self.mode != "auto":
            return self.mode
        return "all" if opened_as_series else (default if default in ("this", "all") else "this")

    def to_params(self) -> dict:
        """The reserved parameters that reproduce this request."""
        return {
            SERIES_PARAM: self.mode, SERIES_FIRST_PARAM: int(self.first),
            SERIES_LAST_PARAM: None if self.last is None else int(self.last),
            SERIES_STEP_PARAM: int(self.step), SERIES_MISSING_PARAM: self.missing,
            SERIES_SHAPES_PARAM: self.shapes,
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> "SeriesRequest | None":
        """The request in ``params``; ``None`` when no reserved key is present."""
        params = dict(params or {})
        if not any(key in params for key in SERIES_PARAMS):
            return None
        last = params.get(SERIES_LAST_PARAM)
        return cls(
            mode=str(params.get(SERIES_PARAM, "auto")),
            first=int(params.get(SERIES_FIRST_PARAM) or 0),
            last=None if last in (None, "") else int(last),
            step=int(params.get(SERIES_STEP_PARAM) or 1),
            missing=str(params.get(SERIES_MISSING_PARAM, "skip")),
            shapes=str(params.get(SERIES_SHAPES_PARAM, "refuse")),
        )


def without_series_params(params: Mapping[str, Any] | None) -> dict:
    """``params`` without the reserved series keys."""
    return {key: value for key, value in dict(params or {}).items() if key not in SERIES_PARAMS}


# ----------------------------------------------------------------------
# What a series is
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesMember:
    """One timepoint of a series."""

    #: Place in the series, 0-based: the lapse index the acquisition assigned.
    index: int
    state: str
    #: File (or tiling ``tiles.json``) to open; ``""`` for a missing member.
    path: str = ""
    dataset: str | None = None
    reason: str = ""
    #: Seconds since the first timepoint (recorded start, else planned).
    time_s: float | None = None

    @property
    def usable(self) -> bool:
        return self.state in (STATE_COMPLETE, STATE_UNKNOWN) and bool(self.path)


@dataclass(frozen=True)
class SeriesInfo:
    """A discovered series, ordered by timepoint index."""

    kind: str
    name: str
    members: tuple[SeriesMember, ...]
    #: Which member the opened source is (index into ``members``), or ``None``.
    anchor: int | None
    planned: int
    interval_s: float | None = None
    #: The series manifest (``timelapse.json``) when there is one.
    manifest: str | None = None
    #: ``image`` or ``tiling-manifest``: what each member opens as.
    member_kind: str = "image"
    detector: str | None = None
    #: The user opened the series itself (its manifest), not one timepoint.
    opened_as_series: bool = False
    #: Problems that make some or all of the series unusable as one stack.
    issues: tuple[str, ...] = ()
    #: Members' headers were read (``False``: listed from a directory only).
    checked: bool = True

    def counts(self) -> dict[str, int]:
        out = {STATE_COMPLETE: 0, STATE_INCOMPLETE: 0, STATE_MISSING: 0}
        for member in self.members:
            out[member.state] = out.get(member.state, 0) + 1
        return out

    def describe(self) -> str:
        counts = self.counts()
        parts = [f"{len(self.members)} timepoint(s)"]
        for state in (STATE_COMPLETE, STATE_INCOMPLETE, STATE_MISSING, STATE_UNKNOWN):
            if counts.get(state):
                parts.append(f"{counts[state]} {state}")
        return f"{self.name}: " + ", ".join(parts)

    def times_s(self) -> list[float | None]:
        return [member.time_s for member in self.members]


@dataclass(frozen=True)
class SeriesSelection:
    """The members one run covers, in order, and what happens to the others."""

    info: SeriesInfo
    request: SeriesRequest
    #: Indices into ``info.members`` that are processed.
    process: tuple[int, ...]
    #: Indices into ``info.members`` that are left out, with the reason.
    skipped: tuple[tuple[int, str], ...] = ()
    #: Indices that stay in T as a placeholder plane (``missing`` != ``skip``).
    filled: tuple[int, ...] = ()
    #: ``this`` / ``all`` / ``range``, after ``auto`` was resolved.
    mode: str = "all"

    @property
    def positions(self) -> tuple[int, ...]:
        """Every member index that appears along T, ascending."""
        return tuple(sorted({*self.process, *self.filled}))

    def times_s(self) -> list[float | None]:
        return [self.info.members[position].time_s for position in self.positions]


def select_members(info: SeriesInfo, request: SeriesRequest, *, mode: str | None = None) -> SeriesSelection:
    """Which members a request covers, and which unusable ones are skipped or filled.

    ``mode`` overrides the request's (an ``auto`` already resolved by the
    caller). ``this`` selects the anchor alone.
    """
    mode = mode or request.mode
    members = info.members
    if mode == "this":
        if info.anchor is None:
            raise SeriesError(f"{info.name}: no timepoint is the opened one; choose all or a range")
        wanted = [info.anchor]
    elif mode == "range":
        first = max(0, int(request.first))
        last = len(members) - 1 if request.last is None else min(int(request.last), len(members) - 1)
        if last < first:
            raise SeriesError(
                f"{info.name}: the range {first}..{request.last} holds no timepoint "
                f"(the series has {len(members)}, numbered 0..{len(members) - 1})"
            )
        wanted = list(range(first, last + 1, int(request.step)))
    else:
        wanted = list(range(len(members)))
    process: list[int] = []
    skipped: list[tuple[int, str]] = []
    filled: list[int] = []
    for position in wanted:
        member = members[position]
        if member.usable:
            process.append(position)
            continue
        why = f"{member.state}" + (f": {member.reason}" if member.reason else "")
        if request.missing == "skip":
            skipped.append((position, why))
        else:
            filled.append(position)
            skipped.append((position, why + f" (filled with {request.missing})"))
    if not process:
        raise SeriesError(
            f"{info.name}: none of the selected timepoints is usable "
            f"({'; '.join(f'{p}: {r}' for p, r in skipped) or 'nothing selected'})"
        )
    return SeriesSelection(info, request, tuple(process), tuple(skipped), tuple(filled), mode)


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


def _source_path_and_dataset(source) -> tuple[str | None, str | None, bool]:
    """``(path, dataset, opened_as_series)`` of a path or a ``DataObj``."""
    if isinstance(source, (str, Path)):
        path = str(source)
        return path, None, _is_series_manifest_path(path)
    if getattr(source, "provenance_parent", None) is not None:
        return None, None, False
    path = getattr(source, "dataPath", None)
    if not path:
        return None, None, False
    kind = str(getattr(source, "sourceKind", "image"))
    dataset = getattr(source, "_datasetName", None) if kind == "image" else None
    original = getattr(source, "sourceOriginalPath", None)
    return str(path), dataset, _is_series_manifest_path(original) if original else False


def _is_series_manifest_path(path) -> bool:
    from imswitch.imcommon.algorithms.tile_timelapse import TIMELAPSE_MANIFEST_NAME

    p = Path(str(path))
    return p.name == TIMELAPSE_MANIFEST_NAME or (p.is_dir() and (p / TIMELAPSE_MANIFEST_NAME).is_file())


def discover_series(source, dataset: str | None = None, *, deep: bool = True) -> SeriesInfo | None:
    """Whether ``source`` is one timepoint of a series, and the whole series if so.

    ``source`` is a path or a ``DataObj``. Returns ``None`` for everything
    else -- a single recording, a result, a plain tiling, an unreadable file --
    and never raises for them: asking must be safe for any source.

    ``deep=False`` lists a recording lapse's items without opening them (cheap
    enough for the GUI thread); their state is ``unknown``. ``deep=True`` reads
    every header, for a worker. A tiling timelapse reads only small manifests
    either way.
    """
    path, own_dataset, as_series = _source_path_and_dataset(source)
    if not path:
        return None
    try:
        info = _discover_tiling_timelapse(path, as_series)
        if info is not None:
            return info
    except Exception:  # noqa: BLE001 - a foreign file is simply not a series
        pass
    # A tiling run that is not a timelapse point is never a recording lapse
    # (lapse_source says so itself); do not even open it.
    try:
        return _discover_recording_lapse(path, dataset or own_dataset, deep=deep)
    except Exception:  # noqa: BLE001
        return None


def _discover_tiling_timelapse(path: str, opened_as_series: bool) -> SeriesInfo | None:
    from imswitch.imcommon.algorithms.tile_timelapse import (
        STATE_COMPLETE as T_COMPLETE,
        STATE_INCOMPLETE as T_INCOMPLETE,
        read_timelapse,
    )

    series = read_timelapse(path)
    if series is None:
        return None
    states = {T_COMPLETE: STATE_COMPLETE, T_INCOMPLETE: STATE_INCOMPLETE}
    members = tuple(
        SeriesMember(
            index=point.index,
            state=states.get(point.state, STATE_MISSING),
            path=str(point.manifest) if point.manifest is not None else "",
            reason=point.reason,
            time_s=point.time_s,
        )
        for point in series.points
    )
    anchor = None
    here = Path(path)
    folder = here if here.is_dir() else here.parent
    for point in series.points:
        if point.folder == folder or point.folder in folder.parents:
            anchor = point.index
    return SeriesInfo(
        kind=KIND_TILING_TIMELAPSE, name=series.root.name, members=members,
        anchor=anchor, planned=series.planned, interval_s=series.interval_s,
        manifest=str(series.manifest), member_kind="tiling-manifest",
        opened_as_series=bool(opened_as_series or anchor is None),
    )


def _discover_recording_lapse(path: str, dataset: str | None, *, deep: bool) -> SeriesInfo | None:
    from imswitch.improcess.model import lapse_source as ls

    index = ls.discover_time_lapse(path, dataset)
    channel = index.channel()
    if not deep:
        members = tuple(
            SeriesMember(
                index=ref.ordinal, state=STATE_UNKNOWN, path=str(ref.path), dataset=ref.dataset,
            )
            for ref in sorted(channel.candidates, key=lambda ref: ref.ordinal)
        )
        anchor = next((i for i, m in enumerate(members) if m.index == index.anchor_index), None)
        return SeriesInfo(
            kind=KIND_RECORDING_LAPSE, name=index.name, members=members, anchor=anchor,
            planned=index.planned_count, interval_s=index.interval_s,
            detector=index.anchor_detector, checked=False,
        )
    headers = ls.read_lapse_headers(index)
    belonging, _excluded = ls._members_of_this_lapse(index, headers)
    slots = {header.lapse_index: ls._slot_for(header) for header in belonging}
    by_index = {header.lapse_index: header for header in belonging}
    if not slots:
        return None
    last = max(slots)
    actual = [by_index[i].started_at if i in by_index else None for i in range(last + 1)]
    planned = [by_index[i].planned_start if i in by_index else None for i in range(last + 1)]

    def seconds(stamps):
        origin = next((s for s in stamps if s is not None), None)
        if origin is None:
            return [None] * len(stamps)
        return [None if s is None else (s - origin).total_seconds() for s in stamps]

    actual_s, planned_s = seconds(actual), seconds(planned)
    members = []
    shapes = {tuple(h.shape) for h in belonging if slots[h.lapse_index].state == ls.TIMEPOINT_COMPLETE}
    issues = ()
    if len(shapes) > 1:
        issues = (f"the complete items differ in shape: {sorted(shapes)}",)
    for position in range(last + 1):
        slot = slots.get(position)
        if slot is None or slot.ref is None:
            members.append(SeriesMember(position, STATE_MISSING, reason=(slot.reason if slot else "not recorded"),
                                        time_s=_fallback_time(position, actual_s, planned_s, index.interval_s)))
            continue
        members.append(SeriesMember(
            index=position, state=slot.state, path=str(slot.ref.path), dataset=slot.dataset or slot.ref.dataset,
            reason=slot.reason, time_s=_fallback_time(position, actual_s, planned_s, index.interval_s),
        ))
    anchor = next((i for i, m in enumerate(members) if m.index == index.anchor_index), None)
    return SeriesInfo(
        kind=KIND_RECORDING_LAPSE, name=index.name, members=tuple(members), anchor=anchor,
        planned=index.planned_count, interval_s=index.interval_s,
        detector=headers.detector, issues=issues,
    )


def _fallback_time(position, actual_s, planned_s, interval):
    if actual_s[position] is not None:
        return actual_s[position]
    if planned_s[position] is not None:
        return planned_s[position]
    return None if interval is None else position * float(interval)


def open_member(info: SeriesInfo, member: SeriesMember):
    """A ``DataObj`` for one member, opened the way a workflow source is."""
    from imswitch.improcess.workflows.sources import SourceSpec, open_source

    if not member.usable:
        raise SeriesError(f"{info.name}: timepoint {member.index} is {member.state} and cannot be opened")
    kind = "tiling-manifest" if info.member_kind == "tiling-manifest" else "auto"
    return open_source(SourceSpec(path=member.path, dataset=member.dataset, source_kind=kind))


# ----------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------


def _fingerprint(path: str) -> dict:
    p = Path(path)
    try:
        if p.suffix.lower() == ".json":
            return {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        stat = p.stat()
        return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
    except OSError:
        return {}


def describe_selection(selection: SeriesSelection, *, extra: Mapping[str, Any] | None = None) -> dict:
    """JSON-safe record of a run over a series: manifest, every member, the request.

    ``request`` holds the reserved parameters with the mode resolved, so a
    replay does the same thing whatever the defaults become.
    """
    info = selection.info
    request = SeriesRequest(
        mode=selection.mode, first=selection.request.first, last=selection.request.last, step=selection.request.step,
        missing=selection.request.missing, shapes=selection.request.shapes,
    )
    skipped = dict(selection.skipped)
    members = []
    for position in selection.positions:
        member = info.members[position]
        members.append({
            "index": member.index, "state": member.state, "path": member.path or None,
            "dataset": member.dataset, "time_s": member.time_s,
            "fingerprint": _fingerprint(member.path) if member.path else {},
            "used": position in selection.process,
            "note": skipped.get(position),
        })
    out = {
        "kind": info.kind, "name": info.name, "planned": info.planned,
        "manifest": info.manifest,
        "manifest_fingerprint": _fingerprint(info.manifest) if info.manifest else None,
        "request": request.to_params(), "members": members,
        "left_out": [{"index": info.members[p].index, "why": why}
                     for p, why in selection.skipped if p not in selection.filled],
    }
    out.update(dict(extra or {}))
    return out


# ----------------------------------------------------------------------
# Stacking
# ----------------------------------------------------------------------


def time_axis(times: Sequence[float | None], positions: Sequence[int],
              interval_s: float | None = None) -> dict:
    """The T scale for members at ``positions`` with recorded ``times``.

    ``scale`` is the typical step per timepoint in seconds: the median of
    ``(t_j - t_i) / (position_j - position_i)`` between neighbours, so a skipped
    member does not stretch it. ``regular`` is whether every step is within 5 %
    of it (a lapse's recorded starts jitter by a little, never by 5 %).
    ``basis`` says where it came from: ``recorded`` stamps, the planned
    ``interval`` or a plain ``index`` (scale 1, no unit).
    """
    known = [(p, t) for p, t in zip(positions, times) if t is not None]
    steps = [
        (t2 - t1) / (p2 - p1)
        for (p1, t1), (p2, t2) in zip(known, known[1:]) if p2 > p1
    ]
    steps = [s for s in steps if s > 0]
    if steps:
        scale = float(np.median(steps))
        regular = all(abs(s - scale) <= 0.05 * scale for s in steps)
        return {"scale": scale, "basis": "recorded", "regular": bool(regular)}
    if interval_s:
        return {"scale": float(interval_s), "basis": "interval", "regular": True}
    return {"scale": 1.0, "basis": "index", "regular": True}


def _pad_to(array: np.ndarray, shape: tuple[int, ...], how: str, fill) -> np.ndarray:
    out = np.full(shape, fill, dtype=array.dtype)
    if how == "pad_center":
        starts = [(big - small) // 2 for big, small in zip(shape, array.shape)]
    else:
        starts = [0] * len(shape)
    window = tuple(slice(s, s + n) for s, n in zip(starts, array.shape))
    out[window] = array
    return out


def stack_series(results: Sequence[Any | None], selection: SeriesSelection, *, name: str,
                 stack_hook: Callable | None = None, extra_metadata: Mapping[str, Any] | None = None):
    """One result from the per-timepoint ``results``, with a leading ``T`` axis.

    ``results`` has one entry per ``selection.positions``; ``None`` for a
    filled (missing) timepoint. Results must be image or label results with the
    same axis labels. When their sizes differ the request decides: ``refuse``
    names every size, ``pad_*`` puts each on the largest canvas (a size is never
    cropped). The T scale, the real time stamps, the members and what was left
    out go in ``result.metadata["series"]``.
    """
    from imswitch.improcess.model import iteration
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.core_dims import CoreMatch
    from imswitch.improcess.model.labels_result import LabelsResult
    from imswitch.improcess.model.result import result_kind

    info, request = selection.info, selection.request
    positions = selection.positions
    if len(results) != len(positions):
        raise SeriesError("internal: one result per timepoint is required")
    real = [(p, r) for p, r in zip(positions, results) if r is not None]
    if not real:
        raise SeriesError(f"{info.name}: no timepoint produced a result")
    first = real[0][1]
    kind = result_kind(first)
    if kind not in ("image", "labels"):
        raise SeriesError(
            f"the default series run stacks image or label results, but this reconstructor "
            f"produced a {kind!r} result; it needs its own series handling "
            "(Reconstructor.series_policy = 'own')"
        )
    labels = [str(item) for item in (getattr(first, "axis_labels", None) or [])]
    arrays = {p: np.asarray(r.data) for p, r in real}
    for p, r in real:
        other = [str(item) for item in (getattr(r, "axis_labels", None) or [])]
        if other != labels or arrays[p].ndim != arrays[real[0][0]].ndim:
            raise SeriesError(
                f"timepoint {info.members[p].index} produced axes {other}, the first {labels}; "
                "the outputs of a series must have the same axes"
            )
    shapes = {p: tuple(a.shape) for p, a in arrays.items()}
    distinct = sorted(set(shapes.values()))
    padded_from: dict[str, Any] | None = None
    if len(distinct) > 1:
        if request.shapes == "refuse":
            sizes = "; ".join(
                f"{shape}: timepoint(s) {[info.members[p].index for p, s in shapes.items() if s == shape]}"
                for shape in distinct
            )
            raise SeriesError(
                f"{info.name}: the timepoints' outputs differ in size ({sizes}). Nothing is cropped. "
                f"Set {SERIES_SHAPES_PARAM} to 'pad_origin' or 'pad_center' to put them on the "
                "largest canvas, or use a reconstructor that places every timepoint on a "
                "common canvas itself (the tiling mosaic does)."
            )
        canvas = tuple(max(s[axis] for s in distinct) for axis in range(len(distinct[0])))
        padded_from = {"how": request.shapes, "canvas": list(canvas),
                       "sizes": {str(info.members[p].index): list(s) for p, s in shapes.items()}}
    else:
        canvas = distinct[0]
    dtype = np.result_type(*[a.dtype for a in arrays.values()])
    fill_value = 0
    if request.missing == "nan" and selection.filled:
        if not np.issubdtype(dtype, np.floating):
            dtype = np.dtype(np.float32)
        fill_value = np.nan
    planes = []
    for position, result in zip(positions, results):
        if result is None:
            planes.append(np.full(canvas, fill_value, dtype=dtype))
            continue
        array = arrays[position].astype(dtype, copy=False)
        if tuple(array.shape) != canvas:
            array = _pad_to(array, canvas, request.shapes, 0)
        planes.append(array)
    template = first
    scales = list(getattr(template, "axis_scales", None) or [])
    if len(scales) != len(labels):
        scales = [1.0] * len(labels)
    unit = getattr(template, "scale_unit", "px")
    stamps = selection.times_s()
    axis = time_axis(stamps, positions, info.interval_s)
    match = CoreMatch(loop_labels=("T",), loop_shape=(len(planes),), core_labels=tuple(labels))
    stacks = [
        _Plane(name=str(getattr(template, "name", name)), data=plane, axis_labels=labels,
               axis_scales=scales, scale_unit=unit,
               metadata=dict(getattr(template, "metadata", None) or {}), attrs=dict(getattr(r, "attrs", None) or {}),
               kind=kind)
        for plane, r in zip(planes, [r if r is not None else template for r in results])
    ]
    if stack_hook is not None:
        stacked = stack_hook(stacks, match, loop_scales=[axis["scale"]])
    else:
        stacked = iteration.stack_results(stacks, match, loop_scales=[axis["scale"]])
    stacked.name = name
    record = series_record(selection, axis, extra={
        "padded": padded_from, **dict(extra_metadata or {})})
    metadata = dict(getattr(stacked, "metadata", None) or {})
    metadata[METADATA_KEY] = record
    stacked.metadata = metadata
    return stacked


def series_record(selection: SeriesSelection, axis: Mapping[str, Any], *,
                  extra: Mapping[str, Any] | None = None) -> dict:
    """What a stacked result records about its T axis, in ``metadata["series"]``."""
    info, request = selection.info, selection.request
    positions = selection.positions
    record = {
        "kind": info.kind, "series": info.name, "manifest": info.manifest,
        "timepoints": [info.members[p].index for p in positions],
        "times_s": selection.times_s(), "t_scale_s": axis["scale"], "t_basis": axis["basis"],
        "t_regular": axis["regular"],
        "filled": [info.members[p].index for p in selection.filled],
        "left_out": [{"index": info.members[p].index, "why": why}
                     for p, why in selection.skipped if p not in selection.filled],
        "filled_with": request.missing if selection.filled else None,
    }
    record.update(dict(extra or {}))
    return record


@dataclass
class _Plane:
    """The few attributes :func:`iteration.stack_results` reads from a result."""

    name: str
    data: Any
    axis_labels: list
    axis_scales: list
    scale_unit: str
    metadata: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    kind: str = "image"


def series_times_s(result) -> list[float] | None:
    """One time in seconds per index of ``result``'s T axis, if it carries them."""
    record = (getattr(result, "metadata", None) or {}).get(METADATA_KEY)
    if not isinstance(record, dict):
        return None
    times = record.get("times_s")
    if not times or any(t is None for t in times):
        return None
    return [float(t) for t in times]


__all__ = [
    "KIND_RECORDING_LAPSE", "KIND_TILING_TIMELAPSE", "METADATA_KEY", "MISSING_POLICIES",
    "SERIES_FIRST_PARAM", "SERIES_LAST_PARAM", "SERIES_MISSING_PARAM", "SERIES_PARAM",
    "SERIES_PARAMS", "SERIES_SHAPES_PARAM", "SERIES_STEP_PARAM", "SHAPE_POLICIES",
    "SeriesError", "SeriesInfo", "SeriesMember", "SeriesRequest", "SeriesSelection",
    "describe_selection", "discover_series", "open_member", "select_members", "series_param_fields",
    "series_record", "series_times_s", "stack_series", "time_axis", "without_series_params",
]
