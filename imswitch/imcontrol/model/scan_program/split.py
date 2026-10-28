"""A scan program across engines: one leads, the others follow step by step
(docs/design/plans/scan-engines-and-expert-scan.md §5.4, M6).

The leader runs the whole program -- the followers' devices as timing only --
and pulses a wired line at the start of every step. A follower waits for that
pulse before every step and runs its devices' part of the step, shifted by its
trigger latency so that it happens when the program says.

This module is the part that is pure model: which engine drives which device,
each engine's share of the program, the pulse, and the drift between two
clocks that agree once per step. The composite engine does the rest.

Qt-free.
"""

from __future__ import annotations

import dataclasses
import math
import sys
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Set, Tuple

from .interpreter import inner_drives
from .program import ScanProgram, TimelineEvent, Wait, validate

#: The trigger a follower waits for before every step: its leader's pulse.
LEADER_PULSE = 'leader pulse'

#: A difference of two times in seconds carries at most one rounding of each
#: and one of its own: a result within this of zero, relative to the larger
#: time, IS zero.
_DIFFERENCE_RELATIVE_ERROR = 1.5 * sys.float_info.epsilon


@dataclass(frozen=True)
class Wire:
    """A follower and how it follows: the leader's line its trigger input is
    wired to, and what it declares about that input and its clock. ``None``:
    not declared."""
    engine: str
    line: str
    latency_s: Optional[float]
    min_pulse_s: Optional[float]
    grid_s: Optional[float]
    clock_ppm: Optional[float]


def devices_of(program: ScanProgram) -> Set[str]:
    """Every device the program drives or switches."""
    return {d.device for d in program.drives} | {e.device for e in program.timeline}


def shares(program: ScanProgram, owner_of: Mapping[str, Optional[str]]
           ) -> Dict[Optional[str], Set[str]]:
    """``{engine: devices}``: who drives what. ``owner_of(device)`` names the
    engine whose channel the device is on (``None``: no engine's)."""
    out: Dict[Optional[str], Set[str]] = {}
    for device in devices_of(program):
        out.setdefault(owner_of.get(device), set()).add(device)
    return out


def _shifted(seconds, latency_s):
    """``seconds - latency_s``, exactly zero when within float error of it;
    ``None`` when it is negative."""
    value = float(seconds) - float(latency_s)
    if abs(value) <= _DIFFERENCE_RELATIVE_ERROR * max(abs(seconds), abs(latency_s)):
        return 0.0
    return value if value > 0 else None


def follower_program(program: ScanProgram, devices: Set[str], wire: Wire
                     ) -> Tuple[Optional[ScanProgram], List[str]]:
    """What follower ``wire.engine`` runs: its devices, its steps
    ``latency`` shorter after a wait for the leader's pulse, its step-anchored
    events ``latency`` earlier (pass-end events and moves count from the
    step's end, which leader and follower share). Refusals name what cannot
    happen on it."""
    refusals = []
    latency = float(wire.latency_s)
    step = _shifted(program.step_s, latency)
    if not step:
        refusals.append(f'{wire.engine} is released {latency:g} s after each pulse; a step of '
                        f'{program.step_s:g} s leaves it no time.')
    events = []
    for event in program.timeline:
        if event.device not in devices:
            continue
        if event.anchor.startswith('pass-end'):
            events.append(event)
            continue
        start, end = _shifted(event.start_s, latency), _shifted(event.end_s, latency)
        if start is None or end is None:
            refusals.append(
                f'{event.device} starts {event.start_s:g} s into the step; {wire.engine} '
                f'starts {latency:g} s after the pulse that begins it. Start it at least '
                f'{latency:g} s in, or drive it from {wire.line.split("/", 1)[0]}.')
            continue
        events.append(dataclasses.replace(event, start_s=start, end_s=end))
    own = {e.id for e in events if e.id}
    for separation in program.separations:
        edges = {separation.after[0], separation.before[0]}
        if edges & own and not edges <= own:
            refusals.append(f'The separation between {separation.after[0]} and '
                            f'{separation.before[0]} spans two engines; it is not held '
                            'across them.')
    if refusals:
        return None, refusals
    share = dataclasses.replace(
        program,
        drives=tuple(d for d in program.drives if d.device in devices),
        timeline=tuple(events), step_s=step,
        waits=(Wait('step', LEADER_PULSE),),
        separations=tuple(s for s in program.separations
                          if {s.after[0], s.before[0]} <= own),
        detectors=(), park={k: v for k, v in program.park.items() if k in devices},
        requested_as=None,
        # The leader's approach covers its followers' drives (it times them):
        # a follower sets its first positions and waits for the first pulse.
        approach=False)
    return validate(share), []


def leader_program(program: ScanProgram, wires: List[Wire], pulse_s: float) -> ScanProgram:
    """What the leader runs: the whole program, and a pulse on every wired
    line at the start of every step."""
    pulses = tuple(TimelineEvent(w.line, 'marker', 0.0, float(pulse_s), during='any',
                                 id=f'pulse:{w.engine}') for w in wires)
    return validate(dataclasses.replace(program, timeline=tuple(program.timeline) + pulses))


def pulse_seconds(wires: List[Wire], leader_grid_s: float) -> Tuple[Optional[float], str]:
    """The pulse: as long as the longest minimum the followers declare, on
    the leader's grid -- at least that long, so rounded up. ``(None, why)``
    when one does not declare its minimum."""
    unknown = [w.engine for w in wires if w.min_pulse_s is None]
    if unknown:
        return None, (f'{", ".join(unknown)} declare{"s" if len(unknown) == 1 else ""} no '
                      'minimum trigger pulse.')
    longest = max(float(w.min_pulse_s) for w in wires)
    quotient = longest / leader_grid_s
    ticks = round(quotient)
    if abs(quotient - ticks) > _DIFFERENCE_RELATIVE_ERROR * max(1.0, quotient):
        ticks = math.ceil(quotient)
    return max(1, ticks) * leader_grid_s, ''


def longest_pulse_interval(program: ScanProgram) -> float:
    """The longest time between two pulses: a step, then the longest of the
    return after a pass and a between-step move (a wait does not count: the
    clocks agree again at the pulse after it)."""
    extra = [d.motion.return_s for d in inner_drives(program)]
    extra += [d.motion.move_s + d.motion.settle_s for d in program.drives
              if d.motion.placement == 'between-steps']
    return float(program.step_s) + max(extra, default=0.0)


def max_pulse_interval_s(leader_grid_s: Optional[float], leader_ppm: Optional[float],
                         wire: Wire) -> Optional[float]:
    """The longest two pulses may be apart for the clocks' drift between them
    to stay under half the finer tick (``drift``'s rule): half that tick over
    the two accuracies summed. ``math.inf`` for clocks both declared exact;
    ``None`` when a tick or an accuracy is not declared."""
    if None in (leader_grid_s, leader_ppm, wire.grid_s, wire.clock_ppm):
        return None
    ppm = float(leader_ppm) + float(wire.clock_ppm)
    limit = 0.5 * min(float(leader_grid_s), float(wire.grid_s))
    return math.inf if ppm <= 0 else limit / (ppm * 1e-6)


def drift(leader: str, leader_grid_s: float, leader_ppm: Optional[float], wire: Wire,
          interval_s: float) -> Tuple[str, str]:
    """``(verdict, reason)`` for two clocks that agree at every pulse: their
    drift over the longest pulse interval must stay under half the finer tick
    -- where an edge still lands on its tick (the strict policy's edge rule;
    ``max_pulse_interval_s`` is the same rule as a longest interval)."""
    if leader_ppm is None or wire.clock_ppm is None:
        missing = [name for name, ppm in ((leader, leader_ppm), (wire.engine, wire.clock_ppm))
                   if ppm is None]
        return 'unknown', (f'{" and ".join(missing)} declare{"s" if len(missing) == 1 else ""} '
                           'no clock accuracy, so the drift between pulses is unknown.')
    bound = (float(leader_ppm) + float(wire.clock_ppm)) * 1e-6 * interval_s
    limit = 0.5 * min(leader_grid_s, float(wire.grid_s))
    numbers = (f'{bound * 1e6:.3g} µs over the longest {interval_s * 1e3:.3g} ms between '
               f'pulses ({leader_ppm:g} + {wire.clock_ppm:g} ppm), against half a tick, '
               f'{limit * 1e6:.3g} µs')
    if interval_s < max_pulse_interval_s(leader_grid_s, leader_ppm, wire):
        return 'native', f'{wire.engine} drifts at most {numbers}.'
    return 'impossible', (f'{wire.engine} would drift {numbers}: an edge would leave its tick. '
                          'Shorter steps, or clocks that agree better.')


__all__ = ['LEADER_PULSE', 'Wire', 'devices_of', 'drift', 'follower_program', 'leader_program',
           'longest_pulse_interval', 'max_pulse_interval_s', 'pulse_seconds', 'shares']


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
