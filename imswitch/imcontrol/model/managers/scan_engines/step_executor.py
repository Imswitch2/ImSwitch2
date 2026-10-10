"""A step board's executor (docs/design/plans/scan-engines-and-expert-scan.md
§2.7, §5.2, M1, M5).

It runs an uploaded plan -- loops, affine drives, an event table, waits, all
in ticks of the board's grid -- the way step firmware does: it walks the loop
counters itself, writes each DAC value when the motion says to, switches
lines, waits for its trigger input and honours STOP at the next step, or at
once while waiting. It never calls the scan-program interpreter: conformance
compares what it did with the interpreter's schedule, and that comparison
only means something if the two were written apart.

The simulated step board (``SimulatedStepEngine``) and the simulated board
behind the step-engine protocol (``step_board.SimulatedStepBoard``) both run
this executor; what they report goes to a listener.

Qt-free.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class StepPlan:
    """What the board is uploaded: everything in ticks. Positions are in the
    plan's own unit (µm on the simulated step board, volts on the wire).

    A drive whose ``output`` is false is timing only (M6): the board moves
    it as it would a DAC -- waits for its move, settle and return -- and
    writes nothing. A leader runs another engine's devices that way."""
    loops: List[Tuple[str, int, str, Tuple[str, ...]]]
    phase_loop: Optional[str]
    drives: List[dict]
    step_ticks: int
    return_ticks: int
    events: List[dict]
    waits: List[dict]
    preparation: Dict[str, int]
    captured_um: Dict[str, float]
    park_um: Dict[str, float]


@dataclass
class RunLog:
    """What the board did, in ticks since the run started (``wall``) and
    since its segment started (``tick``)."""
    entries: List[tuple] = field(default_factory=list)

    def add(self, *entry):
        self.entries.append(entry)

    def of(self, kind):
        return [entry for entry in self.entries if entry[0] == kind]


class Stopped(Exception):
    pass


class Failed(Exception):
    pass


class Listener:
    """What the executor reports, as it happens."""

    def started(self):
        pass

    def released(self, segment: int, wall: int):
        pass

    def prepared(self, segment: int, wall: int):
        """The preparation after a release is over; the segment starts."""
        pass

    def progressed(self, step: int, steps: int):
        pass

    def done(self, wall: int):
        pass

    def stopped(self, step: int):
        pass

    def failed(self, message: str):
        pass


class StepExecutor:
    """Runs one plan. ``release(wait_start, arrivals, timeout)`` decides when
    a trigger wait releases (the conformance mutants replace it)."""

    def __init__(self, plan: StepPlan, *, latency_ticks: int, stop: threading.Event,
                 blocking: bool, listener: Listener, log: Optional[RunLog] = None,
                 release: Optional[Callable] = None):
        self.plan = plan
        self.latency_ticks = int(latency_ticks)
        self.stop = stop
        self.blocking = blocking
        self.listener = listener
        self.log = log if log is not None else RunLog()
        self.release = release or self.standard_release
        self._wall = self._tick = self._segment = 0
        self._step = 0

    def standard_release(self, wait_start, arrivals, timeout):
        """The first trigger at or after the wait began, plus the latency;
        ``None`` when none comes before the timeout."""
        for arrival in arrivals:
            if arrival < wait_start:
                continue
            if timeout is not None and arrival > wait_start + timeout:
                break
            return arrival + self.latency_ticks, arrival
        return None

    def run(self, arrivals, fail_at_step=None):
        try:
            self.listener.started()
            self._walk(self.plan, sorted(int(a) for a in arrivals), fail_at_step)
        except Stopped:
            self.log.add('safe-state', self._wall)
            self.listener.stopped(self._step)
            return
        except Failed as failure:
            self.log.add('failed', self._wall, str(failure))
            self.listener.failed(str(failure))
            return
        self.listener.done(self._wall)

    # -- the walk -------------------------------------------------------------

    def _position(self, drive, counters, plan):
        value = drive['offset']
        for loop_id in drive['weights']:
            value += drive['weights'][loop_id] * counters[loop_id]
        if drive['table'] is not None:
            value += drive['table'][counters[drive['table_loop']]]
        if drive['origin'] == 'captured':
            value += plan.captured_um[drive['device']]
        return value

    @staticmethod
    def visits(plan):
        """Every step's loop counters, in the order the board walks them. A
        serpentine runs back on every other pass of its parity loops: the
        parity of their flat index, outermost first (the acquisition
        layout's rule)."""
        counts = {loop_id: count for loop_id, count, _, _ in plan.loops}
        order = []

        def walk(depth, counters, ordinals):
            if depth == len(plan.loops):
                order.append(dict(counters))
                return
            loop_id, count, traversal, parity = plan.loops[depth]
            values = list(range(count))
            flat = 0
            for name in parity:
                flat = flat * counts[name] + ordinals[name]
            if traversal == 'reverse' or (traversal == 'serpentine' and flat % 2):
                values.reverse()
            for ordinal, value in enumerate(values):
                counters[loop_id], ordinals[loop_id] = value, ordinal
                walk(depth + 1, counters, ordinals)

        walk(0, {}, {})
        return order

    def _walk(self, plan, arrivals, fail_at_step):
        visits = self.visits(plan)
        ids = [loop[0] for loop in plan.loops]
        held = {}
        positions = [{d['device']: self._position(d, v, plan) for d in plan.drives}
                     for v in visits]
        first = positions[0] if positions else {}

        silent = {d['device'] for d in plan.drives if not d.get('output', True)}

        def write(device, value, tick=None):
            # A write at a time inside the current span names its tick; it
            # never moves the board's clocks.
            if held.get(device) != value:
                held[device] = value
                if device not in silent:
                    self.log.add('dac', self._segment,
                                 self._tick if tick is None else tick, device, value)

        for d in plan.drives:
            write(d['device'], first[d['device']])

        for index, counters in enumerate(visits):
            self._step = index
            if fail_at_step is not None and index == fail_at_step:
                raise Failed(f'injected failure at step {index}')
            if self.stop.is_set():
                raise Stopped()
            self.listener.progressed(index, len(visits))
            phase = counters.get(plan.phase_loop, 0) if plan.phase_loop else 0
            for wait in self._waits_here(plan, visits, index, ids):
                self._wait(wait, arrivals, plan, positions[index], held, write)
            step_start = self._tick
            last = index == len(visits) - 1
            pass_end = last or any(visits[index + 1][o] != counters[o] for o in ids[:-1])
            for event in plan.events:
                if event['phases'] is not None and phase not in event['phases']:
                    continue
                if event['anchor'] == 'step':
                    self._edge(event, step_start + event['start'], step_start + event['end'])
                elif event['anchor'] == 'pass-start' and self._starts(visits, index, ids,
                                                                       event['anchor_loop']):
                    self._edge(event, step_start + event['start'], step_start + event['end'])
                elif event['anchor'] == 'pass-end' and self._ends(visits, index, ids,
                                                                   event['anchor_loop']):
                    end = step_start + plan.step_ticks
                    self._edge(event, end + event['start'], end + event['end'])
            if not pass_end:
                for d in plan.drives:
                    nxt = positions[index + 1][d['device']]
                    if d['placement'] == 'in-step' and nxt != positions[index][d['device']]:
                        write(d['device'], nxt,
                              step_start + plan.step_ticks - d['move'] - d['settle'])
                self._advance(step_start + plan.step_ticks)
                movers = [d for d in plan.drives if d['placement'] == 'between-steps'
                          and positions[index + 1][d['device']] != positions[index][d['device']]]
                if movers:
                    for d in movers:
                        write(d['device'], positions[index + 1][d['device']])
                    self._advance(self._tick + max(d['move'] + d['settle'] for d in movers))
                continue
            self._advance(step_start + plan.step_ticks)
            for d in plan.drives:
                if last:
                    target = self._restore(d, first, positions[index], plan)
                else:
                    target = positions[index + 1][d['device']]
                write(d['device'], target)
            self._advance(self._tick + plan.return_ticks)
        self._step = len(visits)
        self.log.add('end', self._segment, self._tick, self._wall)

    def _advance(self, tick):
        self._wall += tick - self._tick
        self._tick = tick

    def _edge(self, event, start, end):
        self.log.add('on', self._segment, start, event['device'])
        self.log.add('off', self._segment, end, event['device'])

    @staticmethod
    def _restore(drive, first, here, plan):
        if drive['restore'] == 'captured':
            return plan.captured_um[drive['device']]
        if drive['restore'] == 'first':
            return first[drive['device']]
        if drive['restore'] == 'park':
            return plan.park_um[drive['device']]
        return here[drive['device']]

    @staticmethod
    def _starts(visits, index, ids, loop_id):
        outer = ids[:ids.index(loop_id)]
        return index == 0 or any(visits[index][o] != visits[index - 1][o] for o in outer)

    @staticmethod
    def _ends(visits, index, ids, loop_id):
        outer = ids[:ids.index(loop_id)]
        return index == len(visits) - 1 or any(visits[index][o] != visits[index + 1][o]
                                               for o in outer)

    @staticmethod
    def _waits_here(plan, visits, index, ids):
        out = []
        for wait in plan.waits:
            if wait['at'] == 'start':
                if index == 0:
                    out.append(wait)
            elif wait['at'] == 'step':
                out.append(wait)
            else:
                upto = ids[:ids.index(wait['at']) + 1]
                if index == 0 or any(visits[index][o] != visits[index - 1][o] for o in upto):
                    out.append(wait)
        # The board checks a run-start wait, then loop waits outermost first,
        # then a per-step wait.
        order = {'start': -1, 'step': len(ids)}
        return sorted(out, key=lambda w: order.get(w['at'], ids.index(w['at'])
                                                   if w['at'] in ids else 0))

    def _wait(self, wait, arrivals, plan, here, held, write):
        if wait['trigger'] == 'time':
            self._advance(self._tick + wait['interval'])
            return
        start = self._wall
        if self.stop.is_set():
            raise Stopped()
        released = self.release(start, arrivals, wait['timeout'])
        if released is None and wait['timeout'] is None:
            if self.blocking:
                # Run in the caller's thread, nothing can stop it: blocking
                # here would deadlock the caller instead of simulating a board.
                raise Failed('a wait with no trigger coming and no timeout would block a '
                             'blocking run forever')
            # No trigger is coming and none is waited out: the board blocks
            # until it is stopped, which it honours at once.
            self.stop.wait()
            raise Stopped()
        if released is None:
            if wait['on_timeout'] == 'fail':
                raise Failed(f'no trigger within {wait["timeout"]} ticks')
            release, arrival = start + (wait['timeout'] or 0), None
        else:
            release, arrival = released
        self._wall = release
        self._segment += 1
        self._tick = 0
        self.log.add('release', self._segment, release, arrival)
        self.listener.released(self._segment, release)
        # Preparation: only what needs it. Positioners whose position differs
        # from what they hold move; nothing timed happens here.
        movers = [d for d in plan.drives if held.get(d['device']) != here[d['device']]]
        for d in movers:
            write(d['device'], here[d['device']])
        need = [d['move'] + d['settle'] for d in movers] + list(plan.preparation.values())
        self._advance(max(need, default=0))
        self.listener.prepared(self._segment, self._wall)


__all__ = ['Failed', 'Listener', 'RunLog', 'StepExecutor', 'StepPlan', 'Stopped']


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
