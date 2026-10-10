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
from dataclasses import dataclass

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EngineCapabilities, FeasibilityReport, IMPOSSIBLE, ScanEngine,
    ScanEngineRefused,
)
from .step_executor import Listener, RunLog, StepExecutor, StepPlan

#: ``seconds / grid`` takes three roundings -- the decimal time, the grid,
#: the quotient -- each at most half an ULP: 1.5 machine epsilon together. A
#: quotient within it of a whole number IS that many ticks.
_QUOTIENT_RELATIVE_ERROR = 1.5 * sys.float_info.epsilon


@dataclass
class Compiled:
    program: object
    plan: StepPlan
    report: FeasibilityReport


class StepEngineBase(SignalInterface, ScanEngine):
    """What every step engine shares: its capabilities as fields, and the
    compile of a program into a plan in the board's ticks (host side). The
    simulated step board runs the plan in process; the protocol engine
    uploads it to a board."""
    sigScanBuilt = Signal(object, object, object)
    sigScanStarted = Signal()
    sigScanDone = Signal()
    sigScanBuildFailed = Signal()
    sigBarrierReleased = Signal(int, int)
    sigScanStopped = Signal()
    sigScanFailed = Signal(str)

    #: Where the declared capabilities came from (for the report).
    _capsSource = 'managerProperties'
    _waits = frozenset({'time', 'start-trigger', 'step-trigger'})

    def __init__(self, name):
        super().__init__()
        self.name = name
        self._grid = None
        self._analog = {}
        self._digital = ()
        self._latency = None
        self._maxSteps = None

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name=self.name, analog=dict(self._analog), digital=self._digital,
            streaming=False, stepping='native', transitions=frozenset({'jump'}),
            timing_grid_s=self._grid, min_pulse_s=self._grid,
            max_steps=self._maxSteps,
            trigger_latency_s=None if self._latency is None else float(self._latency),
            deadlines=False, waits=frozenset(self._waits),
            abort='step-boundary',
            sources={'timing_grid_s': f'{self.name}.{self._capsSource}'},
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
        report = self.assess(program, channels)
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


class SimulatedStepEngine(StepEngineBase):
    """The simulated step board, in process (M1)."""

    _capsSource = 'managerProperties.timingGridS'

    def __init__(self, engineInfo, name='SimulatedStepEngine', **_lowLevelManagers):
        super().__init__(name)
        props = dict(getattr(engineInfo, 'managerProperties', None) or {})
        if 'timingGridS' not in props:
            raise ValueError(f'{name}: managerProperties.timingGridS is required '
                             '(the board\'s timing grid, in seconds).')
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
        return self._executor.standard_release(wait_start, arrivals, timeout)

    def _execute(self, plan: StepPlan, arrivals, fail_at_step):
        latency = self._ticks(self._latency, 'trigger latency', []) if self._latency else 0
        self._executor = StepExecutor(plan, latency_ticks=latency, stop=self._stop,
                                      blocking=self._blocking, listener=_Signals(self),
                                      log=self.log, release=self._release)
        self._executor.run(arrivals, fail_at_step)


class _Signals(Listener):
    """The executor's reports as the engine's signals."""

    def __init__(self, engine):
        self.engine = engine

    def started(self):
        self.engine.sigScanStarted.emit()

    def released(self, segment, wall):
        self.engine.sigBarrierReleased.emit(segment, wall)

    def done(self, wall):
        self.engine.sigScanDone.emit()

    def stopped(self, step):
        self.engine.sigScanStopped.emit()
        self.engine.sigScanDone.emit()

    def failed(self, message):
        self.engine.sigScanFailed.emit(message)
        self.engine.sigScanBuildFailed.emit()


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
