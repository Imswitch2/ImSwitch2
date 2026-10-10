"""A leader and the engines that follow it step by step, as one engine
(docs/design/plans/scan-engines-and-expert-scan.md §5.4, M6).

The setup wires a follower's trigger input to a line the leader drives
(``scanEngines.<name>.triggerInput``); the engine the panel runs on then
leads. A program that uses only the leader's devices is the leader's alone:
assessed, compiled and run by it, exactly as without followers. A program
that uses a follower's devices is split (``scan_program.split``):

- the leader runs the whole program -- the followers' devices as timing
  only -- and pulses each follower's line at the start of every step;
- each follower waits for its pulse before every step and runs its devices'
  part, shifted by its trigger latency;
- the report has every engine's verdicts, the pulse, and each follower's
  drift between two pulses against half a tick;
- one run, one outcome: Stop stops all, the first failure stops the rest.

Between simulated engines the wire is simulated: the leader runs first and
the follower's trigger arrivals are its pulses, on the follower's ticks.

The run's lifecycle signals are the leader's: a single-engine run emits them
itself, a multi-engine run through this engine.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from imswitch.imcontrol.model.scan_program.engine import (
    EngineCapabilities, FeasibilityReport, FeatureVerdict, IMPOSSIBLE, NATIVE, UNKNOWN,
    ScanEngine, ScanEngineRefused,
)
from imswitch.imcontrol.model.scan_program.program import ProgramError
from imswitch.imcontrol.model.scan_program.split import (
    Wire, drift, follower_program, leader_program, longest_pulse_interval, pulse_seconds,
    shares,
)
from .SimulatedStepEngine import SimulatedStepEngine, StepEngineBase
from .StepProtocolScanEngine import StepProtocolScanEngine

#: A tick count from a ratio of two grids takes a division of two rounded
#: values: within this of a whole number it IS that number.
_RATIO_RELATIVE_ERROR = 1.5 * sys.float_info.epsilon


def simulated(engine) -> bool:
    """Whether an engine is a simulation (its wires can only be simulated)."""
    if isinstance(engine, SimulatedStepEngine):
        return True
    return isinstance(engine, StepProtocolScanEngine) and engine.board is not None


@dataclass
class _Split:
    report: FeasibilityReport
    followers: List[str] = field(default_factory=list)
    timing_only: frozenset = frozenset()
    leader_program: object = None
    follower_programs: Dict[str, object] = field(default_factory=dict)
    wires: Dict[str, Wire] = field(default_factory=dict)


@dataclass
class CompositeCompiled:
    """A program compiled across engines: the program as written (what the
    panel realizes, records and reports on), and each engine's compile."""
    program: object
    report: FeasibilityReport
    leader: object
    followers: Dict[str, object]
    wires: Dict[str, Wire]


class CompositeScanEngine(ScanEngine):
    """``leader`` and ``followers`` (``{name: (engine, line)}``)."""

    _SIGNALS = ('sigScanBuilt', 'sigScanStarted', 'sigScanDone', 'sigScanBuildFailed',
                'sigBarrierReleased', 'sigScanStopped', 'sigScanFailed', 'sigScanProgress')

    def __init__(self, leader, followers):
        self.leader = leader
        self.followers = {name: (engine, str(line)) for name, (engine, line) in followers.items()}
        self.name = leader.name
        self._lock = threading.Lock()
        self._run = None          # the multi-engine run in progress
        self._thread = None

    def __getattr__(self, name):
        # The run's signals are the leader's.
        if name in CompositeScanEngine._SIGNALS:
            return getattr(self.leader, name)
        raise AttributeError(name)

    # -- what it can do ---------------------------------------------------------

    def capabilities(self) -> EngineCapabilities:
        caps = self.leader.capabilities()
        analog, digital = dict(caps.analog), list(caps.digital)
        for engine, _ in self.followers.values():
            other = engine.capabilities()
            analog.update(other.analog)
            digital += [line for line in other.digital if line not in digital]
        return dataclasses.replace(caps, analog=analog, digital=tuple(digital))

    def owns(self, channel: str) -> bool:
        return self.leader.owns(channel) or any(e.owns(channel)
                                                for e, _ in self.followers.values())

    def _owner_of(self, channels):
        engines = [(self.leader.name, self.leader)] + [(name, e) for name, (e, _)
                                                       in self.followers.items()]

        def owner(line):
            return next((name for name, engine in engines if engine.owns(line)), None)

        out = {}
        for device, entry in channels.items():
            lines = [line for line in (entry.analog, entry.digital) if line]
            out[device] = next((owner(line) for line in lines if owner(line)), None)
        return out

    def _split(self, program, channels) -> _Split:
        owner_of = self._owner_of(channels)
        # A marker names its line itself.
        for event in program.timeline:
            if event.kind == 'marker' and event.device not in owner_of:
                owner_of[event.device] = next(
                    (n for n, (e, _) in self.followers.items() if e.owns(event.device)),
                    self.leader.name if self.leader.owns(event.device) else None)
        by_engine = shares(program, owner_of)
        used = [name for name in self.followers if name in by_engine]
        report = FeasibilityReport(self.name)
        split = _Split(report, used)
        if not used:
            return split
        split.timing_only = frozenset().union(*(by_engine[name] for name in used))
        if getattr(program, 'trajectory', None) is not None:
            report.add('followers', IMPOSSIBLE, 'A trajectory is followed by one streaming '
                       f'engine; {", ".join(used)} would have to follow it step by step.')
            return split
        if not isinstance(self.leader, StepEngineBase):
            report.add('lead', IMPOSSIBLE,
                       f'{self.leader.name} cannot pulse the line that steps '
                       f'{", ".join(used)}: its scan drives only devices\' lines, and that line '
                       'is no device\'s.', 'Run the program on a step engine that leads.')
            return split
        lead = self.leader.capabilities()
        for name in used:
            engine, line = self.followers[name]
            caps = engine.capabilities()
            wire = Wire(name, line, caps.trigger_latency_s, caps.trigger_min_pulse_s,
                        caps.timing_grid_s, caps.clock_accuracy_ppm)
            split.wires[name] = wire
            if 'step-trigger' not in caps.waits:
                report.add(f'follow {name}', IMPOSSIBLE,
                           f'{name} cannot wait for a pulse before every step.')
            elif wire.latency_s is None:
                report.add(f'follow {name}', UNKNOWN,
                           f'{name} does not declare its trigger latency, so where it runs in '
                           'the step is unknown.')
            elif simulated(engine) != simulated(self.leader):
                report.add(f'follow {name}', IMPOSSIBLE,
                           f'{"A simulated" if simulated(self.leader) else "A real"} '
                           f'{self.leader.name} cannot pulse {"a real" if simulated(self.leader) else "a simulated"} '
                           f'{name}.')
            else:
                report.add(f'follow {name}', NATIVE,
                           f'{name} follows {self.leader.name} step by step: its trigger input '
                           f'is wired to {line}, and it starts {wire.latency_s:g} s after each '
                           'pulse.')
        if not report.runnable:
            return split
        wires = [split.wires[name] for name in used]
        pulse_s, why = pulse_seconds(wires, lead.timing_grid_s)
        if pulse_s is None:
            report.add('pulse', UNKNOWN, why)
            return split
        report.add('pulse', NATIVE, f'{pulse_s:g} s at the start of every step, on '
                                    f'{", ".join(w.line for w in wires)}.')
        interval = longest_pulse_interval(program)
        for wire in wires:
            verdict, reason = drift(self.leader.name, lead.timing_grid_s,
                                    lead.clock_accuracy_ppm, wire, interval)
            report.add(f'drift {wire.engine}', verdict, reason)
        try:
            split.leader_program = leader_program(program, wires, pulse_s)
        except ProgramError as error:
            report.add('lead', IMPOSSIBLE, str(error))
            return split
        self._merge(report, self.leader.name, self.leader.assess(
            split.leader_program, channels, timing_only=split.timing_only))
        for wire in wires:
            engine, _ = self.followers[wire.engine]
            try:
                share, refusals = follower_program(program, by_engine[wire.engine], wire)
            except ProgramError as error:
                share, refusals = None, [str(error)]
            if refusals:
                report.add(f'{wire.engine}: timing', IMPOSSIBLE, ' '.join(refusals))
                continue
            split.follower_programs[wire.engine] = share
            self._merge(report, wire.engine, engine.assess(share, channels))
            if simulated(engine):
                self._check_simulated_wire(report, lead.timing_grid_s, wire)
        return split

    @staticmethod
    def _merge(report, name, other):
        for verdict in other.verdicts:
            report.verdicts.append(FeatureVerdict(f'{name}: {verdict.feature}', verdict.verdict,
                                                  verdict.reason, verdict.remedy, verdict.cost))

    @staticmethod
    def _check_simulated_wire(report, leader_grid_s, wire):
        ratio = leader_grid_s / wire.grid_s
        if abs(ratio - round(ratio)) > _RATIO_RELATIVE_ERROR * max(1.0, ratio):
            report.add(f'wire {wire.engine}', IMPOSSIBLE,
                       f'A simulated wire carries the pulses onto {wire.engine}\'s ticks: the '
                       f'leader\'s {leader_grid_s:g} s tick is not a whole number of its '
                       f'{wire.grid_s:g} s ticks.')

    def assess(self, program, channels) -> FeasibilityReport:
        split = self._split(program, channels)
        return split.report if split.followers else self.leader.assess(program, channels)

    # -- compile ------------------------------------------------------------------

    def compile(self, program, channels, captured_um=None, devices=None):
        split = self._split(program, channels)
        if not split.followers:
            return self.leader.compile(program, channels, captured_um=captured_um,
                                       devices=devices)
        if not split.report.runnable:
            raise ScanEngineRefused(split.report)
        leader = self.leader.compile(split.leader_program, channels, captured_um=captured_um,
                                     devices=devices, timing_only=split.timing_only)
        followers = {}
        for name, share in split.follower_programs.items():
            engine, _ = self.followers[name]
            # A follower has no preparation stage: the leader waits for every
            # device's preparation before it pulses (§5.4).
            followers[name] = engine.compile(share, channels, captured_um=captured_um,
                                             devices={})
        return CompositeCompiled(program, split.report, leader, followers,
                                 {name: split.wires[name] for name in followers})

    # -- a run ----------------------------------------------------------------------

    def prepare(self, compiled):
        if not isinstance(compiled, CompositeCompiled):
            return self.leader.prepare(compiled)
        self.leader.prepare(compiled.leader, quiet=True)
        devices = set(self._devices(compiled.leader))
        for name, part in compiled.followers.items():
            self.followers[name][0].prepare(part, quiet=True)
            devices |= set(self._devices(part))
        self.leader.sigScanBuilt.emit({'engine': self.name,
                                       'followers': sorted(compiled.followers)},
                                      compiled.program, sorted(devices))

    @staticmethod
    def _devices(compiled):
        plan = compiled.plan
        return ({e['device'] for e in plan.events if e.get('kind') != 'marker'}
                | {d['device'] for d in plan.drives if d.get('output', True)})

    def ready(self) -> bool:
        return self.leader.ready()

    def run(self, compiled=None, *, arrivals=(), blocking=False, fail_at_step=None):
        """Run a compile. ``arrivals`` and ``fail_at_step`` go to the
        leader (a simulated one's trigger input, an injected failure)."""
        if not isinstance(compiled, CompositeCompiled):
            return self.leader.run(compiled, arrivals=arrivals, blocking=blocking,
                                   fail_at_step=fail_at_step)
        parts = {self.leader.name: self.leader}
        parts.update({name: self.followers[name][0] for name in compiled.followers})
        with self._lock:
            self._run = {'parts': parts, 'ended': {}, 'stopping': False, 'finished': False}
        self.leader.sigScanStarted.emit()
        if simulated(self.leader):
            work = (lambda: self._runSimulated(compiled, arrivals, fail_at_step))
        else:
            work = (lambda: self._runConnected(compiled, arrivals, fail_at_step))
        if blocking:
            work()
        else:
            self._thread = threading.Thread(target=work, daemon=True,
                                            name=f'{self.name} composite run')
            self._thread.start()

    def _runSimulated(self, compiled, arrivals, fail_at_step):
        """The leader first, then each follower on the pulses it sent. The
        leader runs as on its own (a wait nothing releases holds it until it
        is stopped); a follower is handed every pulse there will be, so a
        pulse it misses fails it rather than holding it."""
        self.leader.run(compiled.leader, arrivals=arrivals, blocking=False,
                        fail_at_step=fail_at_step, quiet=True, on_end=self._ended)
        self.leader.join()
        led = self.leader.outcome or ('failed', 'the leader did not end')
        for name, part in compiled.followers.items():
            engine = self.followers[name][0]
            if led[0] == 'failed':
                self._ended(engine, ('stopped', ''))
                continue
            pulses = self._pulses(self.leader.log, compiled.wires[name])
            if led[0] == 'done':
                engine.run(part, arrivals=pulses, blocking=True, quiet=True,
                           on_end=self._ended)
            else:
                # Stopped: it takes the pulses that came, then the stop at its
                # next wait.
                engine.run(part, arrivals=pulses, blocking=False, quiet=True,
                           on_end=self._ended)
                engine.abort()
                engine.join()

    def _runConnected(self, compiled, arrivals, fail_at_step):
        """Real boards: every follower armed, then the leader."""
        for name, part in compiled.followers.items():
            self.followers[name][0].run(part, blocking=False, quiet=True, on_end=self._ended)
        self.leader.run(compiled.leader, arrivals=arrivals, blocking=False,
                        fail_at_step=fail_at_step, quiet=True, on_end=self._ended)

    def _pulses(self, log, wire):
        """The leader's pulses on ``wire.line``, on the follower's ticks."""
        starts = {0: 0}
        for _, segment, wall, _arrival in log.of('release'):
            starts[segment] = wall
        ratio = round(self.leader.capabilities().timing_grid_s / wire.grid_s)
        return [(starts[segment] + tick) * ratio
                for kind, segment, tick, device in
                (entry for entry in log.entries if entry[0] == 'on')
                if device == wire.line]

    def _ended(self, engine, outcome=None):
        """A part's run ended: the first failure stops the rest; the last to
        end reports the run's outcome."""
        outcome = outcome or engine.outcome
        with self._lock:
            run = self._run
            if run is None or run['finished']:
                return
            name = next((n for n, e in run['parts'].items() if e is engine), engine.name)
            run['ended'][name] = outcome
            stopOthers = outcome[0] == 'failed' and not run['stopping']
            if stopOthers:
                run['stopping'] = True
            others = [e for n, e in run['parts'].items() if n not in run['ended']]
            finished = not others
            if finished:
                run['finished'] = True
        if stopOthers:
            for other in others:
                other.abort()
        if finished:
            self._report(run['ended'])

    def _report(self, ended):
        failed = [(name, reason) for name, (kind, reason) in ended.items() if kind == 'failed']
        if failed:
            name, reason = failed[0]
            self.leader.sigScanFailed.emit(f'{name}: {reason}')
            self.leader.sigScanBuildFailed.emit()
        elif any(kind == 'stopped' for kind, _ in ended.values()):
            self.leader.sigScanStopped.emit()
            self.leader.sigScanDone.emit()
        else:
            self.leader.sigScanDone.emit()

    def partsOutcome(self) -> Optional[Dict[str, Tuple[str, str]]]:
        """How each part of the last multi-engine run ended."""
        run = self._run
        return None if run is None else dict(run['ended'])

    def abort(self) -> str:
        run = self._run
        if run is None or run['finished']:
            return self.leader.abort()
        with self._lock:
            run['stopping'] = True
        for engine in run['parts'].values():
            engine.abort()
        return 'step-boundary'

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        for engine in [self.leader] + [e for e, _ in self.followers.values()]:
            join = getattr(engine, 'join', None)
            if callable(join):
                join(timeout)

    def finalize(self):
        complete = True
        for engine in [self.leader] + [e for e, _ in self.followers.values()]:
            finalize = getattr(engine, 'finalize', None)
            if callable(finalize) and finalize() is False:
                complete = False
        return complete


__all__ = ['CompositeCompiled', 'CompositeScanEngine', 'simulated']


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
