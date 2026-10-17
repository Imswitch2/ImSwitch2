"""Any pulse generator as a scan engine, for the digital part of the model
(docs/design/plans/scan-engines-and-expert-scan.md §5.3, M3).

A ``PulseGeneratorManager`` -- the Teensy today, the PulseStreamer, any
pulse-generator plugin -- plays a table of steps, each a duration and the
state of every channel, repeated ``n_reps`` times. This adapter compiles a
scan program into that table and refuses, with the feature and the limit,
everything a table of digital states cannot say:

- a drive or an offset (a position needs an analog output);
- a level event (a power level is analog);
- a wait for a trigger before every step, and a start trigger the board
  does not take;
- more distinct steps than the board holds, more repeats than it counts;
- a time off the board's grid (the program's timing policy decides, on the
  board's own resolution);
- a line active when low, on a board whose stop drives every channel low.

The program is realized on the board's grid; the run's line states are
searched for the shortest period that repeats them exactly, and that period
becomes the table, the rest the repeat count. Nothing the board does not
declare is assumed: an undeclared capacity, resolution or repeat limit makes
the scan unknown, and it is refused as such.

``managerProperties``:

- ``pulseGenerator``: the generator it drives -- a ``pulseGenerators``
  entry's name; omitted, the one the setup's ``teensyPulse`` builds
  (``pulseGeneratorManager``);
- ``channels``: the generator's channels it owns, by index (default: all of
  them). A device is wired to one by its ``digitalLine`` (the channel index,
  as ``PulseGeneratorLaserManager`` reads it).
"""

from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from qtpy import QtCore

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager import PulseStep
from imswitch.imcontrol.model.managers.pulsegen.loader import pulse_generator_named
from imswitch.imcontrol.model.scan_program.engine import (
    EngineCapabilities, FeasibilityReport, IMPOSSIBLE, NATIVE, UNKNOWN, ScanEngine,
    ScanEngineRefused, SetupError,
)
from imswitch.imcontrol.model.scan_program.interpreter import RunContext, expand
from imswitch.imcontrol.model.scan_program.schedule import ScheduleRefused, format_seconds, realize

NS_PER_S = 1_000_000_000


@dataclass
class CompiledSequence:
    program: object
    schedule: object
    steps: List[PulseStep]
    n_reps: int
    trigger: str
    report: FeasibilityReport
    #: ``{channel: device}`` for every line the program switches.
    lines: Dict[int, str]


def _divisors(n: int) -> List[int]:
    small, large = [], []
    i = 1
    while i * i <= n:
        if n % i == 0:
            small.append(i)
            if i != n // i:
                large.append(n // i)
        i += 1
    return small + large[::-1]


class _States:
    """A run's line states: ``starts[i]`` is when ``masks[i]`` begins, up to
    the next start (the last up to ``total``). Adjacent masks differ."""

    def __init__(self, starts, masks, total):
        self.starts, self.masks, self.total = starts, masks, total

    def window(self, lo, hi):
        """``[(offset, mask)]`` over ``[lo, hi)``, the first at offset 0."""
        i = bisect.bisect_right(self.starts, lo) - 1
        out = [(0, self.masks[i])]
        for j in range(i + 1, len(self.starts)):
            if self.starts[j] >= hi:
                break
            out.append((self.starts[j] - lo, self.masks[j]))
        return out

    def period(self) -> Tuple[List[Tuple[int, int]], int]:
        """The shortest unit that repeats into the whole run: ``(unit,
        repeats)``, the unit as ``[(offset, mask)]``."""
        for period in _divisors(self.total):
            unit = self.window(0, period)
            repeats = self.total // period
            if all(self.window(k * period, (k + 1) * period) == unit
                   for k in range(1, repeats)):
                return unit, repeats
        return self.window(0, self.total), 1


class PulseGeneratorScanEngine(SignalInterface, ScanEngine):
    sigScanBuilt = Signal(object, object, object)
    sigScanStarted = Signal()
    sigScanDone = Signal()
    sigScanBuildFailed = Signal()
    sigScanStopped = Signal()
    sigScanFailed = Signal(str)

    def __init__(self, engineInfo, name='PulseGeneratorScanEngine', **lowLevelManagers):
        super().__init__()
        props = dict(getattr(engineInfo, 'managerProperties', None) or {})
        self.name = name
        self.__logger = initLogger(self, instanceName=name)
        key = props.get('pulseGenerator')
        self._generator, available = pulse_generator_named(lowLevelManagers, key)
        if self._generator is None:
            has = (f'it has {", ".join(available)}' if available else
                   'it has none (pulseGenerators, or a teensyPulse section)')
            raise SetupError(f'{name}: the setup has no pulse generator '
                             f'{key or "pulseGeneratorManager"!r}; {has}.')
        self._setupInfo = lowLevelManagers.get('setupInfo')
        count = int(self._generator.n_digital_channels)
        channels = props.get('channels')
        channels = range(count) if channels is None else [int(ch) for ch in channels]
        outside = [ch for ch in channels if not 0 <= ch < count]
        if outside:
            raise SetupError(f'{name}: channels {outside} are outside the generator\'s '
                             f'0..{count - 1}.')
        self._lines = {str(ch): ch for ch in channels}
        self._prepared: Optional[CompiledSequence] = None
        self._stopping = False
        self._failure: Optional[str] = None
        #: ``{channel: error}``: lines whose off level the generator did not
        #: take, so their state is unknown; tried again at the next stop, run
        #: or close until it takes.
        self._unknownLines: Dict[int, str] = {}
        self._thread: Optional[threading.Thread] = None
        # The engine plays the sequence in its own thread with the generator
        # blocking, and decides there how it ended; the generator's failure
        # is caught where it is emitted, whatever thread that is.
        self._generator.sigSequenceFailed.connect(self._recordFailure,
                                                  QtCore.Qt.DirectConnection)

    # -- capabilities -----------------------------------------------------------

    def capabilities(self) -> EngineCapabilities:
        g = self._generator
        resolution = g.timing_resolution_ns
        waits = {'time'} | ({'start-trigger'} if 'start' in g.run_triggers else set())
        kind = type(g).__name__
        return EngineCapabilities(
            name=self.name, digital=tuple(self._lines), streaming=False, stepping='none',
            timing_grid_s=None if resolution is None else resolution / NS_PER_S,
            min_pulse_s=g.min_pulse_width_ns / NS_PER_S, max_steps=g.max_sequence_steps,
            deadlines=False, waits=frozenset(waits),
            abort=g.stop_granularity or 'unknown',
            safe_state='every line at its off level, after a run and after a stop',
            sources={'timing_grid_s': f'{kind}.timing_resolution_ns',
                     'min_pulse_s': f'{kind}.min_pulse_width_ns',
                     'max_steps': f'{kind}.max_sequence_steps'},
        )

    def owns(self, channel: str) -> bool:
        return str(channel) in self._lines

    def _device_info(self, device):
        for section in ('lasers', 'detectors'):
            devices = getattr(self._setupInfo, section, None) or {}
            if device in devices:
                return devices[device]
        return None

    def _active_low(self, device) -> bool:
        info = self._device_info(device)
        return str(getattr(info, 'ttlActiveLevel', 'high') or 'high').lower() == 'low'

    # -- compile -------------------------------------------------------------------

    def _assess(self, program, channels) -> Tuple[FeasibilityReport, Dict[int, str]]:
        g = self._generator
        report = FeasibilityReport(self.name)
        lines: Dict[int, str] = {}
        for drive in program.drives:
            report.add(f'drive {drive.device}', IMPOSSIBLE,
                       f'{drive.device} needs an analog output; {self.name} has none.',
                       'Run the scan on an engine that drives it.')
        seen = set()
        for event in program.timeline:
            key = (event.device, event.kind)
            if key in seen:
                continue
            seen.add(key)
            feature = f'{event.kind} {event.device}'
            if event.kind == 'level':
                report.add(feature, IMPOSSIBLE,
                           f'The {event.device} power level is an analog event; pulse '
                           'generators play digital states only.')
                continue
            if event.kind == 'offset':
                report.add(feature, IMPOSSIBLE,
                           f'An offset of {event.device} is a move; {self.name} has no analog '
                           'output.')
                continue
            entry = channels.get(event.device)
            line = entry.digital if entry is not None else None
            if line is None or not self.owns(line):
                where = 'has no digital line' if line is None else (
                    f'is wired to {line}, which {self.name} does not drive')
                report.add(feature, IMPOSSIBLE, f'{event.device} {where}.')
                continue
            channel = self._lines[str(line)]
            if lines.setdefault(channel, event.device) != event.device:
                report.add(feature, IMPOSSIBLE,
                           f'{event.device} and {lines[channel]} share channel {channel}.')
                continue
            if self._active_low(event.device):
                if g.stop_drives_low is None:
                    report.add(f'active-low {event.device}', UNKNOWN,
                               f'{self.name} does not say what a stop does to its outputs, '
                               f'and {event.device} is on when its line is low.')
                    continue
                if g.stop_drives_low:
                    report.add(f'active-low {event.device}', IMPOSSIBLE,
                               f'A stop drives every {self.name} channel low, which switches '
                               f'{event.device} (active low) on.',
                               'Wire it active high, or to another engine.')
                    continue
            report.add(feature, NATIVE)
        for wait in program.waits:
            if wait.trigger == 'time':
                report.add('time wait', NATIVE)
            elif wait.at != 'start':
                which = 'step' if wait.at == 'step' else f'{wait.at} iteration'
                report.add(f'trigger wait at {wait.at}', IMPOSSIBLE,
                           f'Waiting before every {which} needs a per-{which} trigger; '
                           f'{self.name} takes none.')
            elif 'start' not in g.run_triggers:
                report.add('start trigger', IMPOSSIBLE,
                           f'{self.name} takes no start trigger; it starts when told to.')
            else:
                report.add('start trigger', NATIVE)
        if g.timing_resolution_ns is None:
            report.add('timing', UNKNOWN, f'{self.name} does not declare its timing resolution.')
        return report, lines

    def compile(self, program, channels, captured_um=None, devices=None) -> CompiledSequence:
        """The step table for ``program``, or :class:`ScanEngineRefused`
        with every reason at once."""
        report, lines = self._assess(program, channels)
        if not report.runnable:
            raise ScanEngineRefused(report)
        g = self._generator
        resolution = int(g.timing_resolution_ns)
        try:
            schedule = realize(
                expand(program, RunContext(captured_um=dict(captured_um or {}),
                                           devices=dict(devices or {}))),
                NS_PER_S / resolution, rate_source=f'{type(g).__name__}.timing_resolution_ns')
        except ScheduleRefused as error:
            report.add('timing', IMPOSSIBLE, str(error))
            raise ScanEngineRefused(report) from error
        states = self._states(schedule, lines)
        unit, repeats = states.period()
        durations = [(unit[i + 1][0] if i + 1 < len(unit) else states.total // repeats)
                     - unit[i][0] for i in range(len(unit))]
        steps = [PulseStep(duration_ns=d * resolution,
                           channel_states={ch: bool(mask >> ch & 1) for ch in self._lines.values()})
                 for d, (_offset, mask) in zip(durations, unit)]

        capacity = g.max_sequence_steps
        if capacity is None:
            report.add('steps', UNKNOWN, f'{self.name} does not declare how many steps it holds.')
        elif len(steps) > capacity:
            report.add('steps', IMPOSSIBLE,
                       f'This timeline needs {len(steps)} distinct steps; {self.name} holds '
                       f'{capacity}.')
        if repeats > 1:
            limit = g.max_repeats
            if limit is None:
                report.add('repeats', UNKNOWN,
                           f'{self.name} does not declare how many times it repeats a sequence '
                           f'(this one plays {repeats} times).')
            elif repeats > limit:
                report.add('repeats', IMPOSSIBLE,
                           f'The sequence repeats {repeats} times; {self.name} counts to '
                           f'{limit}.')
        shortest = min(step.duration_ns for step in steps)
        if shortest < g.min_pulse_width_ns:
            report.add('pulse width', IMPOSSIBLE,
                       f'A {format_seconds(shortest / NS_PER_S)} step is shorter than the '
                       f'{format_seconds(g.min_pulse_width_ns / NS_PER_S)} {self.name} '
                       'resolves.')
        if not report.runnable:
            raise ScanEngineRefused(report)
        trigger = 'start' if any(w.trigger != 'time' and w.at == 'start'
                                 for w in program.waits) else 'none'
        return CompiledSequence(program, schedule, steps, repeats, trigger, report, lines)

    def _line_owners(self) -> Dict[int, str]:
        """``{channel: device}`` for the owned lines a laser or detector is on."""
        owners = {}
        for section in ('lasers', 'detectors'):
            for name, info in (getattr(self._setupInfo, section, None) or {}).items():
                line = getattr(info, 'digitalLine', None)
                if line is not None and self.owns(str(line)):
                    owners[self._lines[str(line)]] = name
        return owners

    def _off_mask(self) -> int:
        """Every owned line at its off level: high for an active-low device."""
        mask = 0
        for channel, device in self._line_owners().items():
            if self._active_low(device):
                mask |= 1 << channel
        return mask

    def _states(self, schedule, lines) -> _States:
        segments = {e.segment for e in schedule.events} | {s.segment for s in schedule.spans}
        if len(segments) > 1:
            # Only a start trigger is a barrier this engine takes, and it
            # comes before the first step: one segment is played.
            raise ScanEngineRefused(self._refusal('waits', 'Only one wait for a trigger, before '
                                                  'the run, can be played.'))
        total = max(s.start_tick + s.ticks for s in schedule.spans)
        device_channel = {device: channel for channel, device in lines.items()}
        changes: Dict[int, List[Tuple[int, int]]] = {}
        for event in schedule.events:
            channel = device_channel.get(event.device)
            if channel is None or event.end_tick <= event.start_tick:
                continue
            changes.setdefault(event.start_tick, []).append((channel, +1))
            changes.setdefault(event.end_tick, []).append((channel, -1))
        off = self._off_mask()
        active_low = {channel: self._active_low(device) for channel, device in lines.items()}
        depth = {channel: 0 for channel in lines}

        def mask_now():
            mask = off
            for channel, count in depth.items():
                on = count > 0
                high = (not on) if active_low[channel] else on
                mask = (mask | (1 << channel)) if high else (mask & ~(1 << channel))
            return mask

        starts, masks = [], []
        for tick in sorted(set(changes) | {0}):
            if tick >= total:      # what ends with the run
                break
            for channel, delta in changes.get(tick, ()):
                depth[channel] += delta
            mask = mask_now()
            if not masks or masks[-1] != mask:
                starts.append(tick)
                masks.append(mask)
        return _States(starts, masks, total)

    def _refusal(self, feature, reason):
        report = FeasibilityReport(self.name)
        report.add(feature, IMPOSSIBLE, reason)
        return report

    # -- run -------------------------------------------------------------------------

    def prepare(self, compiled: CompiledSequence):
        self._generator.program_sequence(compiled.steps)
        self._prepared = compiled
        self.sigScanBuilt.emit(compiled.program, compiled.schedule,
                               sorted(compiled.lines.values()))

    def run(self, compiled: Optional[CompiledSequence] = None, blocking=False):
        if compiled is not None and compiled is not self._prepared:
            raise RuntimeError(f'{self.name}: that program is not the one prepared.')
        if self._prepared is None:
            raise RuntimeError(f'{self.name}: nothing prepared to run.')
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f'{self.name} is running.')
        if self._unknownLines and self._safeState():
            # The last run's end, or a stop, could not turn them off; a new
            # run would start from outputs nobody knows.
            raise RuntimeError(f'{self._unknownReason()} Not starting a run.')
        self._stopping = False
        self._failure = None
        if blocking:
            self._play()
        else:
            self._thread = threading.Thread(target=self._play, name=f'{self.name} run',
                                            daemon=True)
            self._thread.start()

    def _play(self):
        compiled = self._prepared
        kwargs = {'trigger': 'start'} if compiled.trigger == 'start' else {}
        self.sigScanStarted.emit()
        try:
            self._generator.run(n_reps=compiled.n_reps, blocking=True, **kwargs)
        except Exception as error:
            self._failure = str(error)
        # The board leaves its last step's state on the wire when it is done;
        # a line that does not take its off level fails the run: it may still
        # be on (a laser emitting), and saying "done" would hide that.
        if self._safeState():
            unknown = self._unknownReason()
            self._failure = unknown if self._failure is None else f'{self._failure}; {unknown}'
        # The run's end, as every engine signals it (ScanEngine).
        if self._failure is not None:
            self.sigScanFailed.emit(self._failure)
            self.sigScanBuildFailed.emit()
        elif self._stopping:
            self.sigScanStopped.emit()
            self.sigScanDone.emit()
        else:
            self.sigScanDone.emit()

    def join(self, timeout=None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def abort(self) -> str:
        self._stopping = True
        self._generator.stop()
        running = self._thread is not None and self._thread.is_alive()
        if self._safeState() and not running:
            # During a run a refused write is expected (the board is still
            # playing) and the run's own end tries again and reports; with no
            # run, nothing else will.
            self.__logger.error(self._unknownReason())
        return self._generator.stop_granularity or 'unknown'

    def finalize(self):
        """At close: stop a run and put every line at its off level. False
        while a run is still going or a line's state is unknown, so that the
        close tries again."""
        if self._thread is not None and self._thread.is_alive():
            self.abort()
        running = self._thread is not None and self._thread.is_alive()
        if running:
            self.__logger.error(f'{self.name} is still running at close; its run ends at '
                                'the next step boundary.')
        if self._safeState():
            self.__logger.error(self._unknownReason())
            return False
        return not running

    def _safeState(self) -> Dict[int, str]:
        """Every owned line to its off level, each one tried. Returns the
        lines the generator did not take, ``{channel: error}``, and keeps them
        as the lines whose state is unknown."""
        off = self._off_mask()
        unknown = {}
        for channel in self._lines.values():
            try:
                self._generator.setDigital(channel, bool(off >> channel & 1))
            except Exception as error:
                unknown[channel] = str(error) or type(error).__name__
        self._unknownLines = unknown
        return unknown

    def unknownLines(self) -> Dict[int, str]:
        """The lines whose state is unknown since a refused off-level write."""
        return dict(self._unknownLines)

    def _unknownReason(self) -> str:
        owners = self._line_owners()
        lines = ', '.join(
            f'line {channel}' + (f' ({owners[channel]})' if channel in owners else '')
            + f': {error}' for channel, error in sorted(self._unknownLines.items()))
        return (f'{self.name} did not take the off level of {lines}; their state is '
                'unknown (a laser may still be on).')

    def _recordFailure(self, reason):
        self._failure = str(reason)


__all__ = ['CompiledSequence', 'PulseGeneratorScanEngine']


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
