"""A simulated step board: an independent executor of scan programs
(docs/design/plans/scan-engines-and-expert-scan.md §2.7, §5.2, M1).

It does what step firmware will do, the way firmware will do it: compile a
program into its own plan -- loops, affine drives and event tables in grid
ticks -- then walk the loop counters itself, write each DAC value when the
motion says to, switch lines, wait for its trigger input and honour STOP.
It never calls the scan-program interpreter: conformance compares this
engine's log with the interpreter's timeline, and that comparison only means
something if the two were written apart.

Its properties are the board's: what a real board would report through
``*CAPS?``. Nothing it needs is defaulted -- an undeclared trigger latency
makes trigger waits unknown, as the feasibility report says.

``managerProperties``:

- ``timingGridS`` (required): seconds per tick;
- ``analogChannels``: ``{channel: [minV, maxV]}`` it drives;
- ``digitalLines``: lines it drives;
- ``triggerLatencyS``: from a trigger's arrival to the release (optional);
- ``maxSteps``: steps it can hold (optional).
"""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EngineCapabilities, FeasibilityReport, IMPOSSIBLE, ScanEngine,
    ScanEngineRefused, assess_program,
)

#: ``seconds / grid`` takes three roundings -- the decimal time, the grid,
#: the quotient -- each at most half an ULP: 1.5 machine epsilon together. A
#: quotient within it of a whole number IS that many ticks.
_QUOTIENT_RELATIVE_ERROR = 1.5 * sys.float_info.epsilon


@dataclass
class StepPlan:
    """What the board is uploaded: everything in ticks."""
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
class Compiled:
    program: object
    plan: StepPlan
    report: FeasibilityReport


@dataclass
class RunLog:
    """What the board did, in ticks since the run started (``wall``) and
    since its segment started (``tick``)."""
    entries: List[tuple] = field(default_factory=list)

    def add(self, *entry):
        self.entries.append(entry)

    def of(self, kind):
        return [entry for entry in self.entries if entry[0] == kind]


class _Stopped(Exception):
    pass


class SimulatedStepEngine(SignalInterface, ScanEngine):
    sigScanBuilt = Signal(object, object, object)
    sigScanStarted = Signal()
    sigScanDone = Signal()
    sigScanBuildFailed = Signal()
    sigBarrierReleased = Signal(int, int)
    sigScanStopped = Signal()

    def __init__(self, engineInfo, name='SimulatedStepEngine', **_lowLevelManagers):
        super().__init__()
        props = dict(getattr(engineInfo, 'managerProperties', None) or {})
        if 'timingGridS' not in props:
            raise ValueError(f'{name}: managerProperties.timingGridS is required '
                             '(the board\'s timing grid, in seconds).')
        self.name = name
        self._grid = float(props['timingGridS'])
        self._analog = {str(ch): AnalogOut(str(ch), *(list(r) + [None, None])[:2])
                        for ch, r in (props.get('analogChannels') or {}).items()}
        self._digital = tuple(str(line) for line in props.get('digitalLines') or ())
        self._latency = props.get('triggerLatencyS')
        self._maxSteps = props.get('maxSteps')
        self._stop = threading.Event()
        self._blocking = False
        self._prepared = None
        self._thread = None
        self.log = RunLog()

    # -- capabilities ---------------------------------------------------------

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name=self.name, analog=dict(self._analog), digital=self._digital,
            streaming=False, stepping='native', transitions=frozenset({'jump'}),
            timing_grid_s=self._grid, min_pulse_s=self._grid,
            max_steps=self._maxSteps,
            trigger_latency_s=None if self._latency is None else float(self._latency),
            deadlines=False, waits=frozenset({'time', 'start-trigger', 'step-trigger'}),
            abort='step-boundary',
            sources={'timing_grid_s': f'{self.name}.managerProperties.timingGridS'},
        )

    def owns(self, channel: str) -> bool:
        return channel in self._analog or channel in self._digital

    # -- compile ----------------------------------------------------------------

    def _ticks(self, seconds, what, refusals):
        product = float(seconds) / self._grid
        ticks = int(round(product))
        if abs(product - ticks) > _QUOTIENT_RELATIVE_ERROR * max(1.0, abs(product)):
            refusals.append(f'{what}: {seconds:g} s is not a whole number of the '
                            f'{self._grid:g} s ticks this board runs '
                            f'({math.floor(product)} or {math.ceil(product)} ticks).')
        return ticks

    def compile(self, program, channels, captured_um=None, devices=None) -> Compiled:
        """``devices``: ``{name: DeviceFacts}`` from the setup (preparation)."""
        report = assess_program(program, self.capabilities(), self.owns, channels)
        used = {e.device for e in program.timeline}
        for name, facts in (devices or {}).items():
            if name in used and facts.preparation == 'unknown' and program.waits:
                report.add(f'preparation {name}', 'unknown',
                           f'{name} needs preparation after a wait; its preparation time is '
                           f'unknown ({facts.source or "not declared"}).')
        if not report.runnable:
            raise ScanEngineRefused(report)
        refusals = []
        ticks = lambda s, what: self._ticks(s, what, refusals)  # noqa: E731
        inner = program.loops[-1].id
        inner_drives = [d for d in program.drives
                        if inner in d.weights_um or d.table_loop == inner]
        drives = []
        for d in program.drives:
            drives.append(dict(
                device=d.device, channel=channels[d.device].analog, offset=float(d.offset_um),
                weights={k: float(v) for k, v in d.weights_um.items()},
                table_loop=d.table_loop, table=None if d.table_um is None else list(d.table_um),
                move=ticks(d.motion.move_s, f'{d.device} move'),
                settle=ticks(d.motion.settle_s, f'{d.device} settle'),
                placement=d.motion.placement, origin=d.motion.origin, restore=d.motion.restore,
            ))
        events = []
        for e in program.timeline:
            anchor_loop = None if e.anchor == 'step' else e.anchor.split(':', 1)[1]
            events.append(dict(
                device=e.device, kind=e.kind,
                start=ticks(e.start_s, f'{e.device} start'), end=ticks(e.end_s, f'{e.device} end'),
                phases=None if e.phases is None else set(e.phases),
                anchor=e.anchor.split(':', 1)[0], anchor_loop=anchor_loop, value=e.value,
            ))
        waits = []
        for w in program.waits:
            waits.append(dict(
                at=w.at, trigger=w.trigger,
                interval=None if w.interval_s is None else ticks(w.interval_s, 'wait interval'),
                timeout=None if w.timeout_s is None else ticks(w.timeout_s, 'wait timeout'),
                on_timeout=w.on_timeout,
            ))
        plan = StepPlan(
            loops=[(lp.id, int(lp.count), lp.traversal, tuple(lp.parity_loops))
                   for lp in program.loops],
            phase_loop=next((lp.id for lp in program.loops if lp.kind == 'condition'), None),
            drives=drives, step_ticks=ticks(program.step_s, 'step'),
            return_ticks=max((ticks(d.motion.return_s, f'{d.device} return')
                              for d in inner_drives), default=0),
            events=events, waits=waits,
            preparation={name: ticks(facts.preparation, f'{name} preparation')
                         for name, facts in (devices or {}).items()
                         if name in used and facts.preparation not in ('notRequired', 'unknown',
                                                                       None)},
            captured_um=dict(captured_um or {}), park_um=dict(program.park),
        )
        total = math.prod(count for _, count, _, _ in plan.loops)
        if self._maxSteps is not None and total > int(self._maxSteps):
            refusals.append(f'{total} steps; this board holds {self._maxSteps}.')
        if refusals:
            report.add('timing', IMPOSSIBLE, ' '.join(refusals))
            raise ScanEngineRefused(report)
        return Compiled(program, plan, report)

    # -- run --------------------------------------------------------------------

    def prepare(self, compiled: Compiled):
        self._prepared = compiled
        self._stop.clear()
        self.log = RunLog()
        devices = sorted({e['device'] for e in compiled.plan.events}
                         | {d['device'] for d in compiled.plan.drives})
        self.sigScanBuilt.emit({'engine': self.name}, compiled.plan, devices)

    def ready(self) -> bool:
        return self._prepared is not None

    def run(self, compiled: Compiled = None, *, arrivals=(), blocking=False, fail_at_step=None):
        compiled = compiled or self._prepared
        if compiled is None:
            raise RuntimeError(f'{self.name}: nothing prepared to run.')
        args = (compiled.plan, sorted(int(a) for a in arrivals), fail_at_step)
        self._blocking = bool(blocking)
        if blocking:
            self._execute(*args)
        else:
            self._thread = threading.Thread(target=self._execute, args=args, daemon=True,
                                            name=f'{self.name} run')
            self._thread.start()

    def abort(self) -> str:
        self._stop.set()
        return 'step-boundary'

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the executor -------------------------------------------------------------

    def _release(self, wait_start, arrivals, timeout):
        """When a trigger wait that began at ``wait_start`` (wall ticks)
        releases, and on which arrival: ``(release, arrival)`` or ``None``
        on timeout. Overridden only by the conformance mutants."""
        latency = self._ticks(self._latency, 'trigger latency', []) if self._latency else 0
        for arrival in arrivals:
            if arrival < wait_start:
                continue
            if timeout is not None and arrival > wait_start + timeout:
                break
            return arrival + latency, arrival
        return None

    def _execute(self, plan: StepPlan, arrivals, fail_at_step):
        try:
            self.sigScanStarted.emit()
            self._walk(plan, arrivals, fail_at_step)
        except _Stopped:
            self.log.add('safe-state', self._wall)
            self.sigScanStopped.emit()
            self.sigScanDone.emit()
            return
        except _Failed as failure:
            self.log.add('failed', self._wall, str(failure))
            self.sigScanBuildFailed.emit()
            return
        self.sigScanDone.emit()

    def _position(self, drive, counters, plan):
        value = drive['offset']
        for loop_id in drive['weights']:
            value += drive['weights'][loop_id] * counters[loop_id]
        if drive['table'] is not None:
            value += drive['table'][counters[drive['table_loop']]]
        if drive['origin'] == 'captured':
            value += plan.captured_um[drive['device']]
        return value

    def _visits(self, plan):
        order = []

        def walk(depth, counters, ordinals):
            if depth == len(plan.loops):
                order.append(dict(counters))
                return
            loop_id, count, traversal, parity = plan.loops[depth]
            values = list(range(count))
            if traversal == 'reverse' or (traversal == 'serpentine'
                                          and sum(ordinals[p] for p in parity) % 2):
                values.reverse()
            for ordinal, value in enumerate(values):
                counters[loop_id], ordinals[loop_id] = value, ordinal
                walk(depth + 1, counters, ordinals)

        walk(0, {}, {})
        return order

    def _walk(self, plan, arrivals, fail_at_step):
        visits = self._visits(plan)
        ids = [loop[0] for loop in plan.loops]
        self._wall = 0
        self._tick = 0
        self._segment = 0
        held = {}
        positions = [{d['device']: self._position(d, v, plan) for d in plan.drives}
                     for v in visits]
        first = positions[0]

        def write(device, value, tick=None):
            # A write at a time inside the current span names its tick; it
            # never moves the board's clocks.
            if held.get(device) != value:
                held[device] = value
                self.log.add('dac', self._segment, self._tick if tick is None else tick,
                             device, value)

        for d in plan.drives:
            write(d['device'], first[d['device']])

        for index, counters in enumerate(visits):
            if fail_at_step is not None and index == fail_at_step:
                raise _Failed(f'injected failure at step {index}')
            if self._stop.is_set():
                raise _Stopped()
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
        self.log.add('end', self._segment, self._tick, self._wall)

    def _advance(self, tick):
        self._wall += tick - self._tick
        self._tick = tick

    def _edge(self, event, start, end):
        self.log.add('on', self._segment, start, event['device'])
        self.log.add('off', self._segment, end, event['device'])

    def _restore(self, drive, first, here, plan):
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
        if self._stop.is_set():
            raise _Stopped()
        released = self._release(start, arrivals, wait['timeout'])
        if released is None and wait['timeout'] is None:
            if self._blocking:
                # Run in the caller's thread, nothing can stop it: blocking
                # here would deadlock the caller instead of simulating a board.
                raise _Failed('a wait with no trigger coming and no timeout would block a '
                              'blocking run forever')
            # No trigger is coming and none is waited out: the board blocks
            # until it is stopped, which it honours at once.
            self._stop.wait()
            raise _Stopped()
        if released is None:
            if wait['on_timeout'] == 'fail':
                raise _Failed(f'no trigger within {wait["timeout"]} ticks')
            release, arrival = start + (wait['timeout'] or 0), None
        else:
            release, arrival = released
        self._wall = release
        self._segment += 1
        self._tick = 0
        self.log.add('release', self._segment, release, arrival)
        self.sigBarrierReleased.emit(self._segment, release)
        # Preparation: only what needs it. Positioners whose position differs
        # from what they hold move; nothing timed happens here.
        movers = [d for d in plan.drives if held.get(d['device']) != here[d['device']]]
        for d in movers:
            write(d['device'], here[d['device']])
        need = [d['move'] + d['settle'] for d in movers] + list(plan.preparation.values())
        self._advance(max(need, default=0))


class _Failed(Exception):
    pass


__all__ = ['Compiled', 'RunLog', 'SimulatedStepEngine', 'StepPlan']


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
