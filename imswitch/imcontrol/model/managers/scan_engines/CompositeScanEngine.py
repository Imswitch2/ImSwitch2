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


def can_lead(engine) -> bool:
    """Whether an engine can lead followers: pulse a line that belongs to no
    device at every step. A step engine can; the NI-DAQ cannot yet -- its
    scan drives only devices' lines (plan §5.4)."""
    return isinstance(engine, StepEngineBase)


def lead_refusal(leader, followers) -> str:
    """Why ``leader`` cannot step ``followers`` (one wording: the report
    and the setup dialog)."""
    return (f'{leader.name} cannot pulse the line that steps {", ".join(followers)}: its '
            'scan drives only devices\' lines, and that line is no device\'s.')


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
    #: The leader's schedule: every step, wait and approach of the run on the
    #: leader's grid (the followers' events are on theirs, each checked by
    #: its own compile).
    schedule: object = None


class CompositeScanEngine(ScanEngine):
    """``leader`` and ``followers`` (``{name: (engine, line)}``)."""

    _SIGNALS = ('sigScanBuilt', 'sigScanStarted', 'sigScanDone', 'sigScanBuildFailed',
                'sigBarrierReleased', 'sigScanStopped', 'sigScanFailed', 'sigScanProgress')
    #: What a streaming leader's panel calls to run a compile of its own (the
    #: NI-DAQ's): a program on the leader's devices alone is the leader's.
    _LEADER_CALLS = ('run_signals', 'resolveScanTTLDevices', 'rate_hz', 'manager')

    def __init__(self, leader, followers):
        self.leader = leader
        self.followers = {name: (engine, str(line)) for name, (engine, line) in followers.items()}
        self.name = leader.name
        self._lock = threading.Lock()
        self._run = None          # the multi-engine run in progress
        self._thread = None

    def __getattr__(self, name):
        # The run's signals are the leader's, and so is how the leader alone
        # runs (a NI-DAQ leader's waveforms).
        if name in CompositeScanEngine._SIGNALS or name in CompositeScanEngine._LEADER_CALLS:
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
        if not can_lead(self.leader):
            report.add('lead', IMPOSSIBLE, lead_refusal(self.leader, used),
                       'Run the program on a step engine that leads.')
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
            elif wire.grid_s is None:
                report.add(f'follow {name}', UNKNOWN,
                           f'{name} declares no timing grid, so its drift between pulses is '
                           'unknown.')
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
        if lead.timing_grid_s is None:
            report.add('lead', UNKNOWN, f'{self.leader.name} declares no timing grid, so '
                       'where its pulses fall is unknown.')
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
                                 {name: split.wires[name] for name in followers},
                                 schedule=getattr(leader, 'schedule', None))

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
            run = (lambda: self._runSimulated(compiled, arrivals, fail_at_step))
        else:
            run = (lambda: self._runConnected(compiled, arrivals, fail_at_step))
        work = (lambda: self._guarded(run))
        if blocking:
            work()
        else:
            self._thread = threading.Thread(target=work, daemon=True,
                                            name=f'{self.name} composite run')
            self._thread.start()

    def _guarded(self, run):
        """A run that raises is a failed run: every part that has not ended
        ends failed with the reason (the first of them stops the rest), so
        the run always reports one outcome."""
        try:
            run()
        except Exception as error:
            reason = f'the run failed: {type(error).__name__}: {error}'
            with self._lock:
                current = self._run
                pending = ([] if current is None or current['finished'] else
                           [e for n, e in current['parts'].items()
                            if n not in current['ended']])
            for engine in pending:
                self._ended(engine, ('failed', reason))

    def _start(self, engine, part, **kwargs):
        """Start a part. One that raises has failed -- that ends the run as
        any failure does -- and ``False`` says so."""
        try:
            engine.run(part, quiet=True, on_end=self._ended, **kwargs)
            return True
        except Exception as error:
            self._ended(engine, ('failed', f'it did not start: {type(error).__name__}: '
                                           f'{error}'))
            return False

    def _notStarted(self, engines, why):
        for engine in engines:
            self._ended(engine, ('stopped', f'not started: {why}'))

    def _runSimulated(self, compiled, arrivals, fail_at_step):
        """The leader first, then each follower on the pulses it sent. The
        leader runs as on its own (a wait nothing releases holds it until it
        is stopped); a follower is handed every pulse there will be, so a
        pulse it misses fails it rather than holding it."""
        followers = [(self.followers[name][0], name, part)
                     for name, part in compiled.followers.items()]
        if not self._start(self.leader, compiled.leader, arrivals=arrivals, blocking=False,
                           fail_at_step=fail_at_step):
            self._notStarted([e for e, _, _ in followers], f'{self.leader.name} failed')
            return
        self.leader.join()
        led = self.leader.outcome or ('failed', 'the leader did not end')
        for engine, name, part in followers:
            if led[0] == 'failed':
                self._ended(engine, ('stopped', ''))
                continue
            try:
                pulses = self._pulses(self.leader.log, compiled.wires[name])
            except Exception as error:
                self._ended(engine, ('failed', f'the leader\'s pulses could not be read: '
                                               f'{type(error).__name__}: {error}'))
                continue
            if led[0] == 'done':
                self._start(engine, part, arrivals=pulses, blocking=True)
            elif self._start(engine, part, arrivals=pulses, blocking=False):
                # Stopped: it takes the pulses that came, then the stop at its
                # next wait.
                engine.abort()
                engine.join()

    def _runConnected(self, compiled, arrivals, fail_at_step):
        """Real boards: every follower armed, then the leader. A part that
        does not start fails the run; the parts after it are not started."""
        order = [(self.followers[name][0], part, {})
                 for name, part in compiled.followers.items()]
        order.append((self.leader, compiled.leader,
                      {'arrivals': arrivals, 'fail_at_step': fail_at_step}))
        for index, (engine, part, kwargs) in enumerate(order):
            if not self._start(engine, part, blocking=False, **kwargs):
                self._notStarted([e for e, _, _ in order[index + 1:]],
                                 f'{engine.name} did not start')
                return

    def _pulses(self, log, wire):
        """The leader's pulses on ``wire.line``, on the follower's ticks."""
        starts = {0: 0}
        for _, segment, wall, _arrival in log.of('release'):
            starts[segment] = wall
        ratio = round(self.leader.capabilities().timing_grid_s / wire.grid_s)
        return [(starts[segment] + tick) * ratio
                for _, segment, tick, device, _level in log.of('on')
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


__all__ = ['CompositeCompiled', 'CompositeScanEngine', 'can_lead', 'lead_refusal', 'simulated']


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
