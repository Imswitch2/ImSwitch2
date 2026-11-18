"""Model of a timelapse run: the plan, the cadence clock and per-point metadata.

The Timelapse module owns *when* each timepoint starts; the Recording
controller (and tiling) own *what is written* at each one. This module is the
Qt-free, hardware-free half of that split: what one run is (a validated
:class:`TimelapsePlan`), when each timepoint starts (:class:`CadenceClock`),
what each timepoint records about itself (:func:`point_attributes`) and where
tiling timepoints are saved (:func:`timelapse_folder`, :func:`timepoint_folder`).

No numeric tolerance is invented here. Every number comes from the operator's
plan or from a documented setup default (``TimelapseInfo``).

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

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Written for ``illumination_settle_ms`` when the setup declares none. The
#: metadata states the absence rather than a guessed number.
NONE_DECLARED = 'none declared'


class TimelapseKind(str, Enum):
    """What each timepoint does."""

    CAMERA = 'camera'
    SCAN = 'scan'
    TILING = 'tiling'


class IntervalMode(str, Enum):
    """What the interval is measured between."""

    START_TO_START = 'start_to_start'
    END_TO_START = 'end_to_start'


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


@dataclass(frozen=True)
class TimelapsePlan:
    """One validated timelapse run, exactly as the operator entered it."""

    kind: TimelapseKind
    timepoints: int
    interval_s: float
    interval_mode: IntervalMode
    detectors: Tuple[str, ...] = ()
    #: ``(name, power)`` pairs. Power is in the laser's own units; ``None`` means
    #: a binary laser or "leave the setpoint as it is".
    lasers: Tuple[Tuple[str, Optional[float]], ...] = ()
    frames_per_point: int = 1
    single_file: bool = True
    #: ``None`` = none declared (never guessed); see ``TimelapseInfo``.
    illumination_settle_ms: Optional[float] = None
    scan_source: str = ''

    def lasers_dict(self) -> Dict[str, Optional[float]]:
        return {name: power for name, power in self.lasers}

    def problems(self) -> List[str]:
        """Human-readable reasons the plan cannot run; empty when valid."""
        out: List[str] = []
        try:
            kind = TimelapseKind(self.kind)
        except ValueError:
            kind = None
            out.append(f'Unknown timelapse kind {self.kind!r}.')
        try:
            IntervalMode(self.interval_mode)
        except ValueError:
            out.append(f'Unknown interval mode {self.interval_mode!r}.')

        tp = self.timepoints
        if isinstance(tp, float) and tp.is_integer():
            tp_ok = False  # integral floats are still floats: refuse, as for bools
        else:
            tp_ok = _is_int(tp) and tp >= 1
        if not tp_ok:
            out.append(f'Timepoints must be a whole number of at least 1 (got {tp!r}).')

        if not _is_finite_number(self.interval_s) or self.interval_s < 0:
            out.append(f'Interval must be a finite number of seconds, 0 or more (got {self.interval_s!r}).')

        fpp = self.frames_per_point
        if not _is_int(fpp) or fpp < 1:
            out.append(f'Frames per timepoint must be a whole number of at least 1 (got {fpp!r}).')
        elif fpp > 1 and kind is not TimelapseKind.CAMERA:
            out.append('More than one frame per timepoint is only available for camera timelapses; '
                       'a scan or tiling timepoint is one scan or one tile set.')

        if kind in (TimelapseKind.CAMERA, TimelapseKind.SCAN) and not self.detectors:
            out.append('Select at least one detector to record.')

        if kind is TimelapseKind.SCAN and self.lasers:
            out.append('A scan timelapse takes no lasers of its own: the scan program decides '
                       'which lasers fire (the widget shows them read-only).')

        names = [name for name, _ in self.lasers]
        if any((not isinstance(n, str)) or not n for n in names):
            out.append('Laser names must be non-empty.')
        if len(set(names)) != len(names):
            out.append('Each laser may be listed only once.')
        for name, power in self.lasers:
            if power is not None and (not _is_finite_number(power) or power < 0):
                out.append(f'Laser {name!r} power must be empty or a finite number, 0 or more (got {power!r}).')

        settle = self.illumination_settle_ms
        if settle is not None and (not _is_finite_number(settle) or settle < 0):
            out.append(f'Illumination settle time must be empty or a finite number of ms, 0 or more (got {settle!r}).')
        return out

    def validate(self) -> None:
        problems = self.problems()
        if problems:
            raise ValueError('; '.join(problems))


@dataclass(frozen=True)
class ScheduledStart:
    """When a timepoint starts, and by how much it missed its target."""

    #: Monotonic seconds, same clock as the ``*_s`` arguments of the clock.
    at_s: float
    #: How late the start is against its target, seconds, never negative.
    slip_s: float


class CadenceClock:
    """Decides when each timepoint starts. Pure arithmetic; the caller sleeps.

    Each start is computed from the *previous actual* start (or end), not from
    a fixed grid anchored at the run start. After an overrun the next point
    therefore starts immediately, once, and the cadence resumes from there:
    there is never a burst of back-to-back catch-up points trying to rejoin the
    old grid. The slip is reported rather than hidden.

    The Recording controller's camera lapse makes the opposite choice after an
    overrun: it waits a full interval. The timelapse starts immediately
    instead, because the operator asked for N timepoints and an overrun point
    should not cost an extra interval on top of the time it already lost.
    """

    def __init__(self, interval_s: float, mode: IntervalMode):
        self.interval_s = interval_s
        self.mode = IntervalMode(mode)

    def next_start(self, index: int, *, run_start_s: float,
                   previous_start_s: Optional[float] = None,
                   previous_end_s: Optional[float] = None,
                   now_s: float) -> ScheduledStart:
        if index == 0:
            return ScheduledStart(run_start_s, max(0.0, now_s - run_start_s))
        if self.mode is IntervalMode.START_TO_START:
            if previous_start_s is None:
                raise ValueError('previous_start_s is required for index > 0 in start_to_start mode')
            target = previous_start_s + self.interval_s
        else:
            if previous_end_s is None:
                raise ValueError('previous_end_s is required for index > 0 in end_to_start mode')
            target = previous_end_s + self.interval_s
        if target < now_s:
            return ScheduledStart(now_s, now_s - target)
        return ScheduledStart(target, 0.0)


def point_attributes(plan: TimelapsePlan, index: int, *, scheduled_s_since_start: float,
                     slip_s: float, lasers_applied: Dict[str, Optional[float]],
                     settle_ms_applied: Optional[float]) -> Dict[str, object]:
    """Per-timepoint metadata, restricted to HDF5-attribute-safe scalars."""
    return {
        'timelapse:kind': TimelapseKind(plan.kind).value,
        'timelapse:index': int(index),
        'timelapse:total': int(plan.timepoints),
        'timelapse:interval_s': float(plan.interval_s),
        'timelapse:interval_mode': IntervalMode(plan.interval_mode).value,
        'timelapse:scheduled_offset_s': float(scheduled_s_since_start),
        'timelapse:slip_s': float(slip_s),
        'timelapse:lasers': json.dumps(dict(lasers_applied), sort_keys=True),
        'timelapse:illumination_settle_ms': (
            NONE_DECLARED if settle_ms_applied is None else float(settle_ms_applied)),
    }


def timelapse_folder(base: Optional[Path | str] = None) -> Path:
    """One folder per run: ``<base>/timelapse_<YYYYMMDD_HHMMSS>``.

    Mirrors ``tiling_folder``: ``base`` is the operator's output folder, and
    the headless measurements root is used only when none is given.
    """
    from datetime import datetime

    from imswitch.imcontrol.model.workflows.paths import resolve_measurements_root

    root = Path(base).expanduser() if base else resolve_measurements_root(None)
    return Path(root) / f'timelapse_{datetime.now().strftime("%Y%m%d_%H%M%S")}'


def timepoint_folder(root: Path | str, index: int, total: int) -> Path:
    """``<root>/t<index>`` zero-padded to ``len(str(total))`` digits.

    Same width rule as the Recording controller's camera-lapse save names, so
    the folders sort in time order.
    """
    return Path(root) / f't{index:0{len(str(total))}d}'
