"""A realized schedule as traces for a preview, from its writes and edges
rather than from one sample per tick.

``densify`` gives one sample per grid step: on a step board's 1 µs grid a
run of a few seconds is millions of samples per trace, and a time-lapse of
minutes is more than a computer holds; the Expert panel froze drawing it.
``sparse_traces`` gives the same picture from what actually happens -- each
drive's writes and transitions, each line's edges, each level and offset --
evaluated at the union of those moments: thousands of points however long
the run, exact wherever the signal holds or jumps, and a straight line
through a smooth transition (its shape is not drawn).

Qt-free.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from .schedule import RealizedSchedule


class PreviewError(ValueError):
    pass


def sparse_traces(schedule: RealizedSchedule, segment: int = 0
                  ) -> Tuple[np.ndarray, Dict[str, Dict[str, np.ndarray]]]:
    """``(time_s, {'analog': {device: µm}, 'digital': {device: 0/1},
    'level': {laser: value}})`` for one segment, every array on the one
    ``time_s`` axis: every breakpoint twice, the value just before it and
    just after, so a step draws vertical."""
    if not 0 <= segment < len(schedule.segments):
        raise PreviewError(f'No segment {segment}; the schedule has {len(schedule.segments)}.')
    total = int(schedule.segments[segment].ticks)
    spans = [s for s in schedule.spans if s.segment == segment]
    program = schedule.expansion.program
    traces: Dict[str, _Trace] = {}

    def trace(name, initial=0.0):
        if name not in traces:
            traces[name] = _Trace(initial)
        return traces[name]

    # Drives: a piece per span, as densify fills its samples.
    for realized in spans:
        a, n = realized.start_tick, realized.ticks
        for name, piece in realized.span.pieces.items():
            out = trace(name, piece.start_um)
            if n == 0:
                continue
            if piece.where == 'hold':
                out.step(a, piece.start_um)
            elif piece.where == 'whole':
                out.ramp(a, a + n, piece.shape, piece.start_um, piece.end_um)
            elif piece.where == 'end':
                move = realized.move_ticks.get(name, 0)
                settle = realized.settle_ticks.get(name, 0)
                out.step(a, piece.start_um)
                out.ramp(a + n - move - settle, a + n - settle, piece.shape, piece.start_um,
                         piece.end_um)
            elif piece.where == 'start':
                move = min(realized.move_ticks.get(name, 0), n)
                out.ramp(a, a + move, piece.shape, piece.start_um, piece.end_um)

    # Intra-step offsets: a delta over the device's position.
    motion = {d.device: d.motion for d in program.drives}
    deltas: Dict[str, _Trace] = {}
    steps = {s.span.step_index: s for s in spans if s.span.kind == 'step'}
    for offset in schedule.offsets:
        if offset.step_index not in steps:
            continue
        delta = deltas.setdefault(offset.device, _Trace(0.0))
        s, e = offset.start_tick, offset.end_tick
        m, st = offset.move_ticks, offset.settle_ticks
        shape = motion[offset.device].transition
        if offset.shape == 'hold':
            delta.ramp(s - m - st, s - st, shape, 0.0, float(offset.value_um))
        else:
            delta.ramp(s, e, 'linear', 0.0, float(offset.value_um))
        delta.ramp(e, e + m, shape, float(offset.value_um), 0.0)

    digital: Dict[str, _Trace] = {}
    level: Dict[str, _Trace] = {}
    for event in schedule.events:
        if event.segment != segment:
            continue
        if event.kind in ('ttl', 'marker', 'detector'):
            line = digital.setdefault(event.device, _Trace(0.0))
            line.step(event.start_tick, 1.0)
            line.step(event.end_tick, 0.0)
        elif event.kind == 'level':
            values = level.setdefault(event.device, _Trace(0.0))
            values.step(event.start_tick, float(event.value))
            values.step(event.end_tick, 0.0)

    everything = list(traces.values()) + list(deltas.values()) + list(digital.values()) \
        + list(level.values())
    breaks = {0, total}
    for item in everything:
        breaks.update(item.ticks())
    breaks = np.array(sorted(b for b in breaks if 0 <= b <= total), dtype=float)
    time_ticks = np.repeat(breaks, 2)
    before, after = breaks - 0.5, breaks + 0.5

    def evaluate(item):
        values = np.empty(time_ticks.size)
        values[0::2] = item.at(before)
        values[1::2] = item.at(after)
        return values

    analog = {}
    for name, item in traces.items():
        values = evaluate(item)
        if name in deltas:
            values = values + evaluate(deltas[name])
        analog[name] = values
    return time_ticks / float(schedule.rate_hz), {
        'analog': analog,
        'digital': {name: evaluate(item) for name, item in digital.items()},
        'level': {name: evaluate(item) for name, item in level.items()},
    }


class _Trace:
    """A piecewise-linear signal with steps, as nodes (tick, value) where a
    step is two nodes at one tick."""

    def __init__(self, initial: float):
        self._nodes: List[Tuple[float, float]] = [(0.0, float(initial))]
        self._last = float(initial)
        self._sorted = True

    def step(self, tick, value):
        """Hold the last value up to ``tick``, then ``value``."""
        self._nodes.append((float(tick), self._last))
        self._nodes.append((float(tick), float(value)))
        self._last = float(value)
        self._sorted = False

    def ramp(self, start, end, shape, v0, v1):
        """Hold up to ``start``, then a straight line from ``v0`` at
        ``start`` to ``v1`` at ``end``; a jump (or no time) is a step."""
        if end <= start or shape == 'jump':
            self.step(start if shape == 'jump' else end, v1)
            return
        self.step(start, v0)
        self._nodes.append((float(end), float(v1)))
        self._last = float(v1)
        self._sorted = False

    def ticks(self):
        return [t for t, _ in self._nodes]

    def at(self, ticks):
        """The signal at ``ticks``: between two nodes a straight line, before
        the first the first value, after the last the last; at a node with
        two values, the earlier one just before and the later one after."""
        if not self._sorted:
            # A stable sort keeps a step's two nodes in the order written.
            self._nodes.sort(key=lambda node: node[0])
            self._sorted = True
        xp = np.array([t for t, _ in self._nodes])
        fp = np.array([v for _, v in self._nodes])
        # Nudge repeated ticks forward so the abscissa is increasing; the
        # queries sit half a tick away from every node, so the nudge never
        # changes an answer.
        if xp.size > 1:
            xp = xp + np.arange(xp.size) * 1e-6
        return np.interp(np.asarray(ticks, dtype=float), xp, fp)


__all__ = ['PreviewError', 'sparse_traces']


# Copyright (C) 2026 ImSwitch developers
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
