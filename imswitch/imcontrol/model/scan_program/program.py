"""The scan program: one hardware-agnostic description of a scan
(docs/design/plans/scan-engines-and-expert-scan.md §2).

A program is nested loops, outermost first, that drive devices. A device's
position is an affine function of the loop counters, or a table indexed by
one loop's counter. Inside every innermost step runs a timeline of events.
Positions are micrometres and times seconds; volts, sample rates and table
formats belong to the engines that run a program, never to the program.

The program holds what was *requested*. What will run is the realized
schedule (``schedule.py``), derived from the program, an engine's timing
grid and the program's timing policy. Neither overwrites the other.

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from .motion import TRANSITIONS

#: Loop kinds, as the acquisition layout names them.
LOOP_KINDS = ('scan_x', 'scan_y', 'scan_z', 'cycle', 'plane', 'time', 'condition',
              'repeat', 'tile', 'position')
LOOP_MODES = ('stepped', 'swept')
TRAVERSALS = ('forward', 'reverse', 'serpentine')
PLACEMENTS = ('in-step', 'between-steps')
ORIGINS = ('captured', 'absolute')
RESTORES = ('captured', 'first', 'park', 'hold')
EVENT_KINDS = ('ttl', 'level', 'offset', 'detector', 'marker')
EVENT_SHAPES = ('hold', 'ramp')
DURING = ('stationary', 'any')
ASSEMBLIES = ('stream', 'image')
WINDOWS = ('event', 'step')
TIMEOUT_ACTIONS = ('fail', 'continue')
WAIT_HOLDS = ('idle', 'hold')
EDGES = ('start', 'end')


class ProgramError(ValueError):
    """A program that is not well formed; the message lists every problem."""


@dataclass(frozen=True)
class Loop:
    """One loop, a chronological counter. ``kind`` is its acquisition-layout
    kind; a ``condition`` loop is the phase loop (line steps at any level)."""
    id: str
    kind: str
    count: int
    mode: str = 'stepped'
    traversal: str = 'forward'
    parity_loops: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Motion:
    """How a device gets from one position to the next (§2.2).

    ``placement`` 'in-step' puts the transition (``move_s``) and the settle
    (``settle_s``) at the end of the step, inside its dwell, as
    BetaScanDesigner does; 'between-steps' adds them between steps instead.
    ``return_s`` is the interval at the end of every pass of the innermost
    loop: the device returns to its first position, and every outer drive
    that changes there makes its transition during it.
    """
    transition: str = 'smooth'
    move_s: float = 0.0
    settle_s: float = 0.0
    placement: str = 'in-step'
    return_s: float = 0.0
    origin: str = 'captured'
    restore: str = 'captured'


@dataclass(frozen=True)
class DeviceDrive:
    """A device's position, in µm: ``offset_um`` plus the weighted loop
    counters, plus -- for an explicit order -- ``table_um[counter of
    table_loop]``. With ``origin: captured`` the result is relative to the
    position captured before the scan."""
    device: str
    offset_um: float = 0.0
    weights_um: Mapping[str, float] = field(default_factory=dict)
    table_loop: Optional[str] = None
    table_um: Optional[Tuple[float, ...]] = None
    motion: Motion = Motion()


@dataclass(frozen=True)
class TimelineEvent:
    """Something a device does at a time.

    ``anchor`` says what the times are relative to: 'step' (every innermost
    step, 0 to ``step_s``), 'pass-start:<loop>' (the first step of every pass
    of that loop) or 'pass-end:<loop>' (the end of its last step; times are
    then <= 0). ``phases`` limits the event to those phase indices.
    """
    device: str
    kind: str
    start_s: float
    end_s: float
    value: Optional[float] = None
    shape: str = 'hold'
    phases: Optional[frozenset] = None
    during: str = 'stationary'
    still: Optional[Tuple[str, ...]] = None
    id: Optional[str] = None
    anchor: str = 'step'


@dataclass(frozen=True)
class Separation:
    """A critical delay between two edges, held relative to its reference."""
    after: Tuple[str, str]
    before: Tuple[str, str]
    tolerance_s: Optional[float] = None


@dataclass(frozen=True)
class Wait:
    """A barrier: before every iteration of loop ``at`` ('step' for every
    step, 'start' for the run), until ``trigger`` arrives ('time' waits
    ``interval_s`` instead). ``hold`` is the outputs' state while waiting."""
    at: str
    trigger: str
    interval_s: Optional[float] = None
    timeout_s: Optional[float] = None
    on_timeout: str = 'fail'
    hold: str = 'idle'


@dataclass(frozen=True)
class DetectorBinding:
    """Which detector events a detector takes, and how it assembles them.

    ``events`` is an event id, or the device whose events trigger the
    detector (a camera's trigger line takes every event on it)."""
    detector: str
    events: str
    per_step: int = 1
    phases: Optional[frozenset] = None
    window: str = 'event'
    assembly: str = 'stream'


@dataclass(frozen=True)
class ScanProgram:
    loops: Tuple[Loop, ...]
    drives: Tuple[DeviceDrive, ...]
    step_s: float
    timeline: Tuple[TimelineEvent, ...] = ()
    waits: Tuple[Wait, ...] = ()
    separations: Tuple[Separation, ...] = ()
    detectors: Tuple[DetectorBinding, ...] = ()
    park: Mapping[str, float] = field(default_factory=dict)
    timing_policy: str = 'strict-v1'
    #: The request in the format it was made, when a panel made it (e.g.
    #: ``{'format': 'advanced-scan/1', 'analog': ..., 'digital': ...}``).
    #: Saving writes it back unchanged: requested values are never rewritten.
    requested_as: Optional[Mapping[str, object]] = None

    def loop(self, loop_id) -> Loop:
        for loop in self.loops:
            if loop.id == loop_id:
                return loop
        raise KeyError(loop_id)

    @property
    def phase_loop(self) -> Optional[Loop]:
        phases = [loop for loop in self.loops if loop.kind == 'condition']
        return phases[0] if phases else None

    @property
    def phase_count(self) -> int:
        phase = self.phase_loop
        return phase.count if phase else 1

    @property
    def innermost(self) -> Loop:
        return self.loops[-1]


def _check_choice(problems, where, value, choices):
    if value not in choices:
        problems.append(f'{where}: {value!r} is not one of {", ".join(choices)}')


def _anchor_loop(anchor):
    if anchor == 'step':
        return None
    for prefix in ('pass-start:', 'pass-end:'):
        if anchor.startswith(prefix):
            return anchor[len(prefix):]
    return ''


def validate(program: ScanProgram) -> ScanProgram:
    """``program`` if it is well formed, else :class:`ProgramError` listing
    every problem. Feasibility on an engine is a separate question."""
    problems = []
    if not program.loops:
        problems.append('A program needs at least one loop.')
    ids = [loop.id for loop in program.loops]
    if len(set(ids)) != len(ids):
        problems.append(f'Loop ids must be unique: {ids}')
    for loop in program.loops:
        where = f'loop {loop.id!r}'
        _check_choice(problems, where + ' kind', loop.kind, LOOP_KINDS)
        _check_choice(problems, where + ' mode', loop.mode, LOOP_MODES)
        _check_choice(problems, where + ' traversal', loop.traversal, TRAVERSALS)
        if int(loop.count) < 1:
            problems.append(f'{where}: count must be at least 1, not {loop.count}')
        if loop.traversal == 'serpentine' and not loop.parity_loops:
            problems.append(f'{where}: a serpentine traversal names its parity loops')
        for parity in loop.parity_loops:
            if parity not in ids or ids.index(parity) >= ids.index(loop.id):
                problems.append(f'{where}: parity loop {parity!r} must be an outer loop')
        if loop.parity_loops and all(p in ids for p in loop.parity_loops):
            # The acquisition layout's rule: the loops right outside it, in
            # order, so the recording can say how the passes alternated.
            position = ids.index(loop.id)
            expected = tuple(ids[max(0, position - len(loop.parity_loops)):position])
            if tuple(loop.parity_loops) != expected:
                problems.append(f'{where}: the parity loops must be the loops right outside '
                                f'it, outermost first ({", ".join(expected)})')
    if len([loop for loop in program.loops if loop.kind == 'condition']) > 1:
        problems.append('A program has at most one phase (condition) loop.')
    if not float(program.step_s) > 0:
        problems.append(f'step_s must be positive, not {program.step_s}')

    devices = [drive.device for drive in program.drives]
    if len(set(devices)) != len(devices):
        problems.append(f'A device is driven once: {devices}')
    for drive in program.drives:
        where = f'drive {drive.device!r}'
        for loop_id in drive.weights_um:
            if loop_id not in ids:
                problems.append(f'{where}: weight on unknown loop {loop_id!r}')
        if (drive.table_um is None) != (drive.table_loop is None):
            problems.append(f'{where}: a table needs both table_loop and table_um')
        if drive.table_loop is not None:
            if drive.table_loop not in ids:
                problems.append(f'{where}: table on unknown loop {drive.table_loop!r}')
            elif len(drive.table_um) != program.loop(drive.table_loop).count:
                problems.append(f'{where}: the table has {len(drive.table_um)} entries for '
                                f'{program.loop(drive.table_loop).count} counts')
        motion = drive.motion
        _check_choice(problems, where + ' transition', motion.transition, TRANSITIONS)
        _check_choice(problems, where + ' placement', motion.placement, PLACEMENTS)
        _check_choice(problems, where + ' origin', motion.origin, ORIGINS)
        _check_choice(problems, where + ' restore', motion.restore, RESTORES)
        for name in ('move_s', 'settle_s', 'return_s'):
            if float(getattr(motion, name)) < 0:
                problems.append(f'{where}: {name} must not be negative')

    event_ids = [event.id for event in program.timeline if event.id is not None]
    if len(set(event_ids)) != len(event_ids):
        problems.append(f'Event ids must be unique: {event_ids}')
    for index, event in enumerate(program.timeline):
        where = f'event {event.id or index} ({event.device})'
        _check_choice(problems, where + ' kind', event.kind, EVENT_KINDS)
        _check_choice(problems, where + ' shape', event.shape, EVENT_SHAPES)
        _check_choice(problems, where + ' during', event.during, DURING)
        anchor_loop = _anchor_loop(event.anchor)
        if anchor_loop == '' or (anchor_loop and anchor_loop not in ids):
            problems.append(f'{where}: unknown anchor {event.anchor!r}')
        if not float(event.end_s) > float(event.start_s):
            problems.append(f'{where}: end must be after start')
        if event.anchor == 'step' and (event.start_s < 0 or event.end_s > program.step_s):
            problems.append(f'{where}: outside its step (0 to {program.step_s:g} s)')
        if event.kind in ('level', 'offset') and event.value is None:
            problems.append(f'{where}: a {event.kind} event needs a value')
        if event.phases is not None:
            outside = sorted(p for p in event.phases if not 0 <= int(p) < program.phase_count)
            if outside:
                problems.append(f'{where}: phases {outside} outside 0..{program.phase_count - 1}')
        if event.kind == 'offset' and event.device not in devices:
            problems.append(f'{where}: an offset needs a drive for {event.device!r}')

    for separation in program.separations:
        for event_id, edge in (separation.after, separation.before):
            if event_id not in event_ids:
                problems.append(f'separation: unknown event {event_id!r}')
            _check_choice(problems, 'separation edge', edge, EDGES)

    for wait in program.waits:
        where = f'wait at {wait.at!r}'
        if wait.at not in ('start', 'step') and wait.at not in ids:
            problems.append(f'{where}: unknown loop')
        _check_choice(problems, where + ' on_timeout', wait.on_timeout, TIMEOUT_ACTIONS)
        _check_choice(problems, where + ' hold', wait.hold, WAIT_HOLDS)
        if wait.trigger == 'time' and not (wait.interval_s and wait.interval_s > 0):
            problems.append(f'{where}: a time wait needs a positive interval_s')

    for binding in program.detectors:
        where = f'detector {binding.detector!r}'
        _check_choice(problems, where + ' assembly', binding.assembly, ASSEMBLIES)
        _check_choice(problems, where + ' window', binding.window, WINDOWS)
        if binding.events not in event_ids and binding.events not in {
                event.device for event in program.timeline}:
            problems.append(f'{where}: unknown events {binding.events!r}')
        if int(binding.per_step) < 1:
            problems.append(f'{where}: per_step must be at least 1')

    if problems:
        raise ProgramError('The scan program is not well formed:\n- ' + '\n- '.join(problems))
    return program


__all__ = [
    'DetectorBinding', 'DeviceDrive', 'Loop', 'Motion', 'ProgramError',
    'ScanProgram', 'Separation', 'TimelineEvent', 'Wait', 'validate',
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
