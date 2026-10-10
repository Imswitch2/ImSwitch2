"""The realized schedule: a program's spans on an engine's timing grid
(docs/design/plans/scan-engines-and-expert-scan.md §2.6).

``realize(expansion, rate_hz, policy)`` maps every requested time onto the
grid the way the program's timing policy says, and returns what will run:
every span, transition and event in grid steps ("ticks"), every change from
the request, the run-wide timing error, and where each number came from.
Preview, acquisition and recorded metadata all use this one schedule.

A time that the policy does not let onto the grid is refused, naming the
quantity and its representable neighbours. Under ``strict-v1`` that is any
time that is not a whole number of steps.

Times are converted as ``seconds * rate_hz``, the same arithmetic today's
designers use, so the compatibility policy reproduces them exactly.

Qt-free.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .interpreter import Expansion, Span
from .policy import TimingPolicy, resolve_policy

#: The relative error of ``seconds * rate`` for a decimal time stored in
#: binary: one rounding converting the decimal, one in the product, each at
#: most half an ULP -- one machine epsilon together. A product within it of a
#: whole number IS that whole number; nothing coarser is accepted as exact.
_PRODUCT_RELATIVE_ERROR = sys.float_info.epsilon


class ScheduleRefused(ValueError):
    """The program cannot run on this grid under its policy; the message
    names every quantity that is refused and what would be representable."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__('The scan cannot run as requested:\n- ' + '\n- '.join(self.reasons))


@dataclass
class Change:
    """A time that runs differently from the request (never under strict)."""
    quantity: str
    where: str
    requested_s: float
    realized_s: float


@dataclass
class RealizedEvent:
    device: str
    kind: str
    start_tick: int
    end_tick: int
    value: Optional[float]
    event_id: Optional[str]
    step_index: Optional[int]
    segment: int


@dataclass
class RealizedOffset:
    device: str
    value_um: float
    shape: str
    start_tick: int          # the offset is held (or ramps) from here ...
    end_tick: int            # ... to here
    move_ticks: int
    settle_ticks: int
    step_index: int


@dataclass
class RealizedSpan:
    span: Span
    segment: int
    start_tick: int          # within its segment
    ticks: int
    move_ticks: Dict[str, int] = field(default_factory=dict)
    settle_ticks: Dict[str, int] = field(default_factory=dict)


@dataclass
class Segment:
    """A stretch with one time base: the run start, or a barrier's release."""
    index: int
    barrier: Optional[object]
    ticks: int = 0


@dataclass
class TimingError:
    """Realized minus requested start times over the run, in seconds."""
    max_deviation_s: float
    end_deviation_s: float


@dataclass
class RealizedSchedule:
    expansion: Expansion
    rate_hz: float
    policy: TimingPolicy
    spans: List[RealizedSpan]
    events: List[RealizedEvent]
    offsets: List[RealizedOffset]
    segments: List[Segment]
    changes: List[Change]
    error: TimingError
    sources: List[Dict[str, object]]

    @property
    def grid_s(self) -> float:
        return 1.0 / self.rate_hz

    def step_spans(self):
        return [s for s in self.spans if s.span.kind == 'step']

    def summary(self) -> Dict[str, object]:
        """What the acquisition records about its timing."""
        return {
            'policy': {'id': self.policy.id, 'contents': dict(self.policy.document)},
            'grid_s': self.grid_s,
            'changes': [vars(change) for change in self.changes],
            'error': vars(self.error),
            'sources': list(self.sources),
        }


class _Converter:
    def __init__(self, rate_hz, policy):
        self.rate = rate_hz
        self.policy = policy
        self.changes: List[Change] = []
        self.refusals: List[str] = []

    def ticks(self, seconds, quantity, where):
        mode = self.policy.rounding[quantity]
        product = float(seconds) * self.rate
        if mode == 'ceil':
            # Today's designers' arithmetic, unchanged: np.ceil on the product.
            ticks = int(np.ceil(product))
        elif mode == 'nearest-even':
            ticks = int(round(product))
        else:
            ticks = int(round(product))
            if abs(product - ticks) > _PRODUCT_RELATIVE_ERROR * max(1.0, abs(product)):
                low, high = math.floor(product), math.ceil(product)
                self.refusals.append(
                    f'{where}: {_fmt(seconds)} is not on the {_fmt(1 / self.rate)} grid '
                    f'(policy {self.policy.id}): {_fmt(low / self.rate)} or '
                    f'{_fmt(high / self.rate)}'
                )
                return ticks
        if ticks / self.rate != float(seconds) and not math.isclose(
                product, ticks, rel_tol=0, abs_tol=_PRODUCT_RELATIVE_ERROR * max(1.0, abs(product))):
            self.changes.append(Change(quantity, where, float(seconds), ticks / self.rate))
        return ticks


def _fmt(seconds):
    seconds = float(seconds)
    if seconds == 0:
        return '0 s'
    for unit, factor in (('s', 1.0), ('ms', 1e-3), ('µs', 1e-6), ('ns', 1e-9)):
        if abs(seconds) >= factor or unit == 'ns':
            return f'{seconds / factor:.6g} {unit}'


def realize(expansion: Expansion, rate_hz: float, policy=None, *,
            rate_source: str = 'given') -> RealizedSchedule:
    """The expansion on a ``rate_hz`` grid under ``policy`` (the program's
    own when omitted), or :class:`ScheduleRefused`."""
    program = expansion.program
    policy = resolve_policy(policy if policy is not None else program.timing_policy)
    conv = _Converter(float(rate_hz), policy)

    spans: List[RealizedSpan] = []
    events: List[RealizedEvent] = []
    offsets: List[RealizedOffset] = []
    segments = [Segment(0, None)]
    tick = 0
    requested = 0.0
    deviations = [0.0]
    motion = {d.device: d.motion for d in program.drives}

    for span in expansion.spans:
        if span.kind == 'wait' and span.duration_s is None:
            segments[-1].ticks = tick
            segments.append(Segment(len(segments), span.wait))
            tick = 0
            requested = 0.0
            continue
        quantity = {'step': 'step', 'return': 'return', 'move': 'move',
                    'wait': 'wait', 'preparation': 'preparation'}[span.kind]
        where = _where(span)
        ticks = conv.ticks(span.duration_s, quantity, where)
        if policy.scheduling == 'deadlines':
            # Start at the requested time, rounded once from the segment
            # start; the previous span must have ended by then (overrun rule).
            deadline = int(round(requested * rate_hz))
            if deadline < tick:
                conv.refusals.append(
                    f'{where}: the previous span overruns its deadline by '
                    f'{_fmt((tick - deadline) / rate_hz)}; nothing is truncated or started late.'
                )
            tick = max(tick, deadline)
        realized = RealizedSpan(span, len(segments) - 1, tick, ticks)
        for name, piece in span.pieces.items():
            if piece.where in ('end', 'start'):
                realized.move_ticks[name] = conv.ticks(piece.move_s, 'move', f'{where}, {name} move')
                realized.settle_ticks[name] = conv.ticks(piece.settle_s, 'settle',
                                                         f'{where}, {name} settle')
                if piece.where == 'end' and (realized.move_ticks[name]
                                             + realized.settle_ticks[name]) >= ticks:
                    conv.refusals.append(
                        f'{where}: {name} moves and settles for '
                        f'{_fmt((realized.move_ticks[name] + realized.settle_ticks[name]) / rate_hz)}'
                        f', which leaves no stationary time in the {_fmt(ticks / rate_hz)} step.'
                    )
        if span.kind == 'step':
            _realize_events(conv, program, policy, realized, events, offsets, motion, rate_hz)
        spans.append(realized)
        tick += ticks
        requested += float(span.duration_s)
        deviations.append(tick / rate_hz - requested)
    segments[-1].ticks = tick

    _check_separations(conv, program, events, rate_hz)
    if conv.refusals:
        raise ScheduleRefused(conv.refusals)

    error = TimingError(max(abs(d) for d in deviations), deviations[-1])
    sources = [
        {'quantity': 'timing grid', 'value': 1.0 / float(rate_hz), 'unit': 's',
         'source': rate_source},
        {'quantity': 'timing policy', 'value': policy.id, 'unit': None,
         'source': 'published policy' if policy.published else 'bundled policy'},
    ]
    return RealizedSchedule(expansion, float(rate_hz), policy, spans, events, offsets,
                            segments, conv.changes, error, sources)


def _where(span):
    if span.kind == 'step':
        return f'step {span.step_index}'
    return span.kind


def _realize_events(conv, program, policy, realized, events, offsets, motion, rate_hz):
    span = realized.span
    where = _where(span)
    step_ticks = realized.ticks
    placed = []
    for instance in span.events:
        event = instance.event
        label = f'{where}, {event.id or event.device} {event.kind}'
        start = conv.ticks(instance.start_s, 'event_edges', f'{label} start')
        end = conv.ticks(instance.end_s, 'event_edges', f'{label} end')
        if instance.from_end:
            start += step_ticks
            end += step_ticks
        if policy.clip_to_step:
            start = max(0, min(step_ticks, start))
            end = max(0, min(step_ticks, end))
        elif start < 0 or end > step_ticks:
            conv.refusals.append(f'{label}: leaves its step.')
        if end <= start:
            if policy.vanished == 'refuse':
                conv.refusals.append(f'{label}: rounds to nothing on the '
                                     f'{_fmt(1 / rate_hz)} grid.')
            else:
                conv.changes.append(Change('event', label, float(instance.end_s - instance.start_s),
                                           0.0))
            continue
        if event.kind == 'offset':
            drive_motion = motion[event.device]
            move = conv.ticks(drive_motion.move_s, 'move', f'{label} move')
            settle = conv.ticks(drive_motion.settle_s, 'settle', f'{label} settle')
            first = start - (move + settle if event.shape == 'hold' else 0)
            last = end + move + settle
            stationary_end = step_ticks - realized.move_ticks.get(event.device, 0) \
                - realized.settle_ticks.get(event.device, 0)
            if first < 0 or last > stationary_end:
                conv.refusals.append(
                    f'{label}: with its moves and settles it needs '
                    f'{_fmt(first / rate_hz)} to {_fmt(last / rate_hz)}; '
                    f'{event.device} is stationary from 0 to {_fmt(stationary_end / rate_hz)} '
                    'in this step.'
                )
            offsets.append(RealizedOffset(event.device, float(event.value), event.shape,
                                          realized.start_tick + start, realized.start_tick + end,
                                          move, settle, span.step_index))
        placed.append((event, start, end, instance))
        events.append(RealizedEvent(event.device, event.kind,
                                    realized.start_tick + start, realized.start_tick + end,
                                    event.value, event.id, span.step_index, realized.segment))
    _check_relations(conv, policy, placed, where)
    _check_stationary(conv, program, realized, placed, offsets, where, rate_hz)


def _relation(a_start, a_end, b_start, b_end):
    if a_end < b_start:
        return 'gap'
    if a_end == b_start:
        return 'touch'
    return 'overlap'


def _check_relations(conv, policy, placed, where):
    """Order and overlap between consecutive events of one device must not
    change in realization (§2.6), unless the policy only reports it."""
    by_device = {}
    for event, start, end, instance in placed:
        by_device.setdefault(event.device, []).append((instance.start_s, instance.end_s,
                                                       start, end, event))
    for device, items in by_device.items():
        items.sort(key=lambda item: (item[0], item[1]))
        for a, b in zip(items, items[1:]):
            wanted = _relation(a[0], a[1], b[0], b[1])
            got = _relation(a[2], a[3], b[2], b[3])
            if wanted != got or b[2] < a[2]:
                message = (f'{where}: {device}: {a[4].id or a[4].kind} and {b[4].id or b[4].kind} '
                           f'were requested to {wanted}, but would {got} on this grid.')
                if policy.relations == 'preserve':
                    conv.refusals.append(message)
                else:
                    conv.changes.append(Change('relation', message, 0.0, 0.0))


def _motion_intervals(realized, offsets):
    """Where each device moves in this step, in ticks relative to the step."""
    intervals = {}
    for name, piece in realized.span.pieces.items():
        if piece.where == 'end':
            used = realized.move_ticks[name] + realized.settle_ticks[name]
            intervals.setdefault(name, []).append((realized.ticks - used, realized.ticks))
    for offset in offsets:
        if offset.step_index != realized.span.step_index:
            continue
        base = realized.start_tick
        s, e = offset.start_tick - base, offset.end_tick - base
        if offset.shape == 'hold':
            intervals.setdefault(offset.device, []).append(
                (s - offset.move_ticks - offset.settle_ticks, s))
        else:
            intervals.setdefault(offset.device, []).append((s, e))
        intervals.setdefault(offset.device, []).append(
            (e, e + offset.move_ticks + offset.settle_ticks))
    return intervals


def _check_stationary(conv, program, realized, placed, offsets, where, rate_hz):
    intervals = _motion_intervals(realized, offsets)
    for event, start, end, _ in placed:
        if event.during != 'stationary' or event.kind == 'offset':
            continue
        still = event.still if event.still is not None else tuple(intervals)
        for device in still:
            for a, b in intervals.get(device, ()):
                if a < end and start < b:
                    conv.refusals.append(
                        f'{where}, {event.id or event.device} {event.kind} (stationary) '
                        f'overlaps {device} moving from {_fmt(a / rate_hz)} to '
                        f'{_fmt(b / rate_hz)}.'
                    )


def _check_separations(conv, program, events, rate_hz):
    by_step = {}
    for event in events:
        if event.event_id is not None:
            by_step.setdefault((event.step_index, event.event_id), event)
    requested = {e.id: e for e in program.timeline if e.id is not None}
    for separation in program.separations:
        (a_id, a_edge), (b_id, b_edge) = separation.after, separation.before
        want = getattr(requested[b_id], f'{b_edge}_s') - getattr(requested[a_id], f'{a_edge}_s')
        tolerance = separation.tolerance_s or 0.0
        for (step, event_id), realized in by_step.items():
            if event_id != a_id or (step, b_id) not in by_step:
                continue
            other = by_step[(step, b_id)]
            got = (getattr(other, f'{b_edge}_tick') - getattr(realized, f'{a_edge}_tick')) / rate_hz
            if abs(got - want) > tolerance + _PRODUCT_RELATIVE_ERROR * max(1.0, abs(want)):
                conv.refusals.append(
                    f'step {step}: the separation {a_id}.{a_edge} -> {b_id}.{b_edge} is '
                    f'{_fmt(got)}, requested {_fmt(want)}.'
                )
            break


__all__ = [
    'Change', 'RealizedEvent', 'RealizedOffset', 'RealizedSchedule', 'RealizedSpan',
    'ScheduleRefused', 'Segment', 'TimingError', 'realize',
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
