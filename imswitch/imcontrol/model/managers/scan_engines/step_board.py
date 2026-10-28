"""The simulated board behind the step-engine protocol (docs/design/plans/
scan-engines-and-expert-scan.md §5.2, M5).

It is the board end of IMSWITCH_STEP/1, in process: it answers ``*IDN?``
and ``*CAPS?`` with what it was built with, decodes an uploaded program the
way firmware does, runs it on the M1 executor in its own thread -- the
interpreter is never called -- and reports what a board reports: progress,
releases, preparations, the end, a stop, a failure, and its log on
``LOG?``. Until the TriggerScope firmware exists (the lab writes it, later),
this is the board the protocol engine, the Expert panel and the conformance
tests run against; a firmware is conformant when it does what this does.

What the tests choose, a board cannot be told over the wire: the arrivals on
its trigger input (``arrivals``, in run-clock ticks) and an injected failure
(``fail_at_step``).

Qt-free.
"""

from __future__ import annotations

import math
import queue
import threading
from typing import List, Optional

from .step_executor import Listener, RunLog, StepExecutor

#: Nanoseconds per second: the grid is declared in ns.
NS_PER_S = 1_000_000_000
from .step_protocol import (
    BoardCaps, CAPACITY, CHANNEL, ProtocolError, RANGE, STATE, SYNTAX, channel, level_of,
    lines_to_plan, log_lines, text,
)


class SimulatedStepBoard:
    """A board that speaks IMSWITCH_STEP/1. ``latency_ticks`` is its real
    trigger latency, which it declares only if ``caps`` does."""

    def __init__(self, caps: BoardCaps, *, latency_ticks: Optional[int] = None):
        if caps.trigger_in and latency_ticks is None and caps.trigger_latency_ticks is None:
            raise ValueError('A simulated board with a trigger input needs its latency, '
                             'declared or not.')
        self.caps = caps
        self.latency_ticks = (caps.trigger_latency_ticks if latency_ticks is None
                              else int(latency_ticks))
        self.arrivals = ()
        self.fail_at_step = None
        #: The arrivals are all there will be: a wait none releases fails
        #: instead of waiting for a stop (as a blocking simulated run does).
        self.arrivals_final = False
        self.log = RunLog()
        #: Every channel's value once a run is over: the lines at their safe
        #: level, the DACs where they stopped.
        self.outputs = {}
        self._replies = queue.Queue()
        self._receiving: Optional[List[str]] = None
        self._plan = None
        self._stop = threading.Event()
        self._thread = None
        self._executor = None
        self._lock = threading.Lock()

    # -- the line interface -----------------------------------------------------

    def write(self, line: str):
        line = line.strip()
        with self._lock:
            if self._receiving is not None:
                self._receiving.append(line)
                if line == 'PROGRAM,END':
                    self._program(self._receiving)
                    self._receiving = None
                return
            if line == '*IDN?':
                self._reply(self.caps.idn())
            elif line == '*CAPS?':
                for caps_line in self.caps.lines():
                    self._reply(caps_line)
            elif line == 'PROGRAM,BEGIN':
                if self.running:
                    self._error(STATE, 'a program cannot be uploaded while one runs')
                else:
                    self._receiving = [line]
            elif line == 'RUN':
                self._run()
            elif line == 'STOP':
                self._stop.set()
                self._reply('OK,STOP')
            elif line == 'LOG?':
                for log_line in log_lines(self.log):
                    self._reply(log_line)
            elif line.startswith('SET,'):
                self._set(line)
            else:
                self._error(SYNTAX, f'not a command of the protocol: {line}')

    def read(self, timeout: Optional[float] = None) -> Optional[str]:
        try:
            return self._replies.get(timeout=timeout) if timeout != 0 else \
                self._replies.get_nowait()
        except queue.Empty:
            return None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _reply(self, line):
        self._replies.put(line)

    def _error(self, code, reason):
        self._reply(f'ERR,{code},{text(reason)}')

    def _set(self, line):
        """``SET,<channel>,<value>``: an output by hand, between runs."""
        fields = line.split(',')
        if len(fields) != 3:
            self._error(SYNTAX, f'SET takes a channel and a value: {line}')
            return
        if self.running:
            self._error(STATE, 'an output is not set by hand while a program runs')
            return
        try:
            kind, index = channel(fields[1])
            if kind == 'time' or not self.caps.has(fields[1]):
                raise ProtocolError(f'{fields[1]}: this board has no such output', CHANNEL)
            value = level_of(fields[1], fields[2], 'SET')
            if kind == 'dac':
                low, high = self.caps.dacs[index]
                if not low <= value <= high:
                    raise ProtocolError(f'{fields[1]} gives {low:g} to {high:g} V, not '
                                        f'{value:g}', RANGE)
        except ProtocolError as error:
            self._error(error.code, str(error))
            return
        self.outputs[fields[1]] = self._settable(fields[1], value)
        self._reply('OK,SET')

    # -- a program --------------------------------------------------------------

    def _program(self, lines):
        try:
            plan = lines_to_plan(lines)
            self._check(plan)
        except ProtocolError as error:
            self._error(error.code, str(error))
            return
        self._plan = plan
        self._reply(f'OK,PROGRAM,{math.prod(count for _, count, _, _ in plan.loops)}')

    def _check(self, plan):
        """What firmware checks before it accepts a program: its channels,
        its capacity, its trigger input."""
        names = ([d['device'] for d in plan.drives] + [e['device'] for e in plan.events]
                 + list(plan.safe) + list(plan.preparation) + list(plan.captured_um)
                 + list(plan.park_um))
        for name in names:
            if not self.caps.has(name):
                raise ProtocolError(f'{name}: this board has no such channel', CHANNEL)
        for kinds, what, names in (
                (('dac', 'time'), 'a DAC or a time channel is driven',
                 [d['device'] for d in plan.drives]),
                (('dac', 'time'), 'a DAC or a time channel has a captured or park position',
                 list(plan.captured_um) + list(plan.park_um)),
                (('ttl', 'dac'), 'a line or a DAC takes an event',
                 [e['device'] for e in plan.events]),
                (('ttl', 'dac'), 'a line or a DAC has a safe level', list(plan.safe))):
            for name in names:
                if channel(name)[0] not in kinds:
                    raise ProtocolError(f'{name}: only {what}', CHANNEL)
        steps = math.prod(count for _, count, _, _ in plan.loops)
        if self.caps.max_steps is not None and steps > self.caps.max_steps:
            raise ProtocolError(f'{steps} steps; this board holds {self.caps.max_steps}',
                                CAPACITY)
        if any(w['trigger'] != 'time' for w in plan.waits) and not self.caps.trigger_in:
            raise ProtocolError('a trigger wait, and this board has no trigger input',
                                CAPACITY)

    # -- a run ------------------------------------------------------------------

    def _run(self):
        if self._plan is None:
            self._error(STATE, 'no program to run')
            return
        if self.running:
            self._error(STATE, 'already running')
            return
        self._stop.clear()
        self.log = RunLog()
        self._reply('OK,RUN')
        self._thread = threading.Thread(target=self._execute, daemon=True,
                                        name='SimulatedStepBoard run')
        self._thread.start()

    def _release(self, wait_start, arrivals, timeout):
        """When a trigger wait releases (the conformance mutants override)."""
        return self._executor.standard_release(wait_start, arrivals, timeout)

    def _execute(self):
        plan = self._plan
        heartbeat = (self.caps.alive_ticks * self.caps.grid_ns / NS_PER_S
                     if self.caps.alive_ticks and self.caps.grid_ns else None)
        self._executor = StepExecutor(plan, latency_ticks=self.latency_ticks or 0,
                                      stop=self._stop, blocking=self.arrivals_final,
                                      listener=_Wire(self), log=self.log,
                                      release=self._release, settable=self._settable,
                                      heartbeat_s=heartbeat)
        self._executor.run(self.arrivals, self.fail_at_step)

    def _settable(self, name, value):
        """What an output really takes: a DAC its nearest code."""
        kind, index = channel(name)
        return self.caps.dac_value(index, value) if kind == 'dac' else value

    def _settle(self):
        """Where a run leaves the outputs: every line at its safe level, every
        DAC holding where it is."""
        for _, _, _, name, value in self.log.of('dac'):
            self.outputs[name] = value
        self.outputs.update(self._plan.safe)


class _Wire(Listener):
    """The executor's reports as the board's messages."""

    def __init__(self, board):
        self.board = board

    def started(self):
        self.board._reply('STARTED')

    def progressed(self, step, steps):
        self.board._reply(f'STEP,{step},{steps}')

    def released(self, segment, wall):
        self.board._reply(f'RELEASE,{segment},{wall}')

    def prepared(self, segment, wall):
        self.board._reply(f'PREPARED,{segment},{wall}')

    def waiting(self, segment):
        self.board._reply(f'WAITING,{segment}')

    def done(self, wall):
        self.board._settle()
        self.board._reply(f'DONE,{wall}')

    def stopped(self, step):
        self.board._settle()
        self.board._reply(f'STOPPED,{step},{self.board._executor._wall}')

    def failed(self, message):
        self.board._settle()
        self.board._reply(f'FAILED,{self.board._executor._step},{text(message)}')


__all__ = ['SimulatedStepBoard']


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
