"""A realized schedule as sample arrays, for streaming engines
(docs/design/plans/scan-engines-and-expert-scan.md §2.7, §5.1).

One sample per grid step. Drives come out in µm (the engine converts to its
own units, e.g. volts through ``conversionFactor``); TTL and marker events
come out as logical on/off, which an engine maps to electrical levels
(§2.6a); a power level inside a step comes out as the laser's value over the
event and 0 (off) elsewhere, which the engine converts to its volts. The indexing within a span is the Beta designer's: an in-step
transition fills ``[n - move - settle, n - settle)`` and the settle holds
the new position to the end of the step; a return fills its whole span.

Qt-free.
"""

from __future__ import annotations

from typing import Dict

import numpy as np

from .motion import transition
from .schedule import RealizedSchedule


class DensifyError(ValueError):
    pass


def densify(schedule: RealizedSchedule, segment: int = 0) -> Dict[str, Dict[str, np.ndarray]]:
    """``{'analog': {device: µm[n]}, 'digital': {device: bool[n]},
    'level': {laser: value[n]}}`` for one segment of the schedule."""
    if not 0 <= segment < len(schedule.segments):
        raise DensifyError(f'No segment {segment}; the schedule has {len(schedule.segments)}.')
    swept = [loop.id for loop in schedule.expansion.program.loops if loop.mode == 'swept']
    if swept:
        raise DensifyError(
            f'Loop(s) {swept} are swept: a swept scan compiles through GalvoScanDesigner '
            '(plan §2.7), not through the interpreter.')
    total = schedule.segments[segment].ticks
    spans = [s for s in schedule.spans if s.segment == segment]
    devices = [d.device for d in schedule.expansion.program.drives]
    analog = {name: np.zeros(total) for name in devices}

    for realized in spans:
        a, n = realized.start_tick, realized.ticks
        if n == 0:
            continue
        for name, piece in realized.span.pieces.items():
            out = analog[name]
            if piece.where == 'hold':
                out[a:a + n] = piece.start_um
            elif piece.where == 'whole':
                out[a:a + n] = transition(piece.shape, piece.start_um, piece.end_um, n)
            elif piece.where == 'end':
                move = realized.move_ticks[name]
                settle = realized.settle_ticks[name]
                out[a:a + n] = piece.start_um
                end = a + n
                out[end - move - settle:end - settle] = transition(
                    piece.shape, piece.start_um, piece.end_um, move)
                out[end - settle:end] = piece.end_um
            elif piece.where == 'start':
                move = min(realized.move_ticks[name], n)
                out[a:a + move] = transition(piece.shape, piece.start_um, piece.end_um, move)
                out[a + move:a + n] = piece.end_um

    program = schedule.expansion.program
    motion = {d.device: d.motion for d in program.drives}
    for offset in schedule.offsets:
        span = next(s for s in spans if s.span.step_index == offset.step_index)
        out = analog[offset.device]
        delta = np.zeros(span.ticks)
        base = span.start_tick
        s, e = offset.start_tick - base, offset.end_tick - base
        m, st = offset.move_ticks, offset.settle_ticks
        shape = motion[offset.device].transition
        if offset.shape == 'hold':
            delta[s - m - st:s - st] = transition(shape, 0.0, offset.value_um, m)
            delta[s - st:e] = offset.value_um
        else:
            delta[s:e] = np.linspace(0.0, offset.value_um, num=e - s, endpoint=True)
        delta[e:e + m] = transition(shape, offset.value_um, 0.0, m)
        out[base:base + span.ticks] += delta

    digital = {}
    for event in schedule.events:
        if event.segment != segment or event.kind not in ('ttl', 'marker', 'detector'):
            continue
        line = digital.setdefault(event.device, np.zeros(total, dtype=bool))
        line[event.start_tick:event.end_tick] = True
    level = {}
    for event in schedule.events:
        if event.segment != segment or event.kind != 'level':
            continue
        values = level.setdefault(event.device, np.zeros(total))
        values[event.start_tick:event.end_tick] = float(event.value)
    # A wait that holds the outputs (Wait.hold 'hold') keeps every line and
    # level where the span before it left them; one that idles leaves them
    # at zero, as the events alone give.
    for realized in spans:
        wait = getattr(realized.span, 'wait', None)
        a, n = realized.start_tick, realized.ticks
        if realized.span.kind != 'wait' or wait is None or wait.hold != 'hold' or n == 0 \
                or a == 0:
            continue
        for out in list(digital.values()) + list(level.values()):
            out[a:a + n] = out[a - 1]
    return {'analog': analog, 'digital': digital, 'level': level}


__all__ = ['DensifyError', 'densify']


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
