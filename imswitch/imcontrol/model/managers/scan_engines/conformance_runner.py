"""Put a board that speaks the step-engine protocol through the conformance
programs (docs/design/plans/scan-engines-and-expert-scan.md §7.8;
``tools/step_board_conformance.py`` is the command).

Every program is run on the board's own channels -- its first DAC and its
first lines, from ``*CAPS?`` -- and the log the board returns (``LOG?``) is
compared with what the scan-program interpreter says it means, write by
write and edge by edge: the comparison the conformance tests make with the
simulated board, against firmware. A board that declares its DACs' bits is
compared within half a code. Every edge's level is compared with the level
the host sent: one line is active low, and on a board with a second DAC that
takes 0 V a program plays a power level on it -- so a firmware that assumes
low is off, or ignores a power level's volts, fails.

A program with trigger waits needs pulses on the board's trigger input,
which this cannot send: it is run only when asked (``trigger=True``), with
something wired to the input. The execution after each release is still
compared exactly; when the releases came is not (the arrivals are unknown
here).

A board that never reports the end of a program fails it rather than
holding the runner: each program has until its own duration plus the link's
reply time -- and ``trigger_timeout_s`` per trigger wait, which says how
long a pulse may take to come -- and is then stopped.

Qt-free apart from the engine's signals, which it does not use.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, level_differences, log_differences,
)
from imswitch.imcontrol.model.scan_program.engine import DeviceChannels, ScanEngineRefused
from .StepProtocolScanEngine import NS_PER_S, StepBoardError, StepProtocolScanEngine

#: The engine's name on the board: its channels are ``board/dac<i>`` and so on.
BOARD = 'board'
#: The lines the programs switch (the board's first ones).
LINES = 3


@dataclass
class Result:
    program: str
    passed: bool
    problems: List[str]


def _programs(grid_s: float, trigger: bool, power: Optional[float] = None
              ) -> Dict[str, Callable[[], ScanProgram]]:
    """The conformance programs, every time a whole number of ``grid_s``
    ticks (they are written in ticks of the board's own grid)."""
    t = lambda ticks: ticks * grid_s  # noqa: E731
    jump = Motion(transition='jump', move_s=t(100), settle_s=t(100), return_s=t(200),
                  origin='absolute', restore='first')

    def raster():
        return ScanProgram(
            loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2), Loop('x', 'scan_x', 3)),
            drives=(DeviceDrive('D', weights_um={'x': 0.5}, motion=jump),),
            step_s=t(1000),
            timeline=(TimelineEvent('L0', 'ttl', 0.0, t(400), phases=frozenset({0}),
                                    during='any'),
                      TimelineEvent('L1', 'ttl', t(200), t(600), phases=frozenset({1}),
                                    during='any'),
                      TimelineEvent('L2', 'detector', 0.0, t(800), during='any')),
            timing_policy='strict-v1')

    def interleave():
        return ScanProgram(
            loops=(Loop('cycle', 'cycle', 2), Loop('plane', 'plane', 3)),
            drives=(DeviceDrive('D', weights_um={'cycle': 0.25, 'plane': 0.5}, motion=jump),),
            step_s=t(2000),
            timeline=(TimelineEvent('L0', 'ttl', 0.0, t(200), during='any'),
                      TimelineEvent('L1', 'ttl', t(300), t(800), during='any'),
                      TimelineEvent('L2', 'detector', t(900), t(1500), during='any')),
            timing_policy='strict-v1')

    def serpentine():
        return ScanProgram(
            loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2),
                   Loop('x', 'scan_x', 3, traversal='serpentine', parity_loops=('y', 'phase'))),
            drives=(DeviceDrive('D', weights_um={'x': 0.5}, motion=jump),),
            step_s=t(1000),
            timeline=(TimelineEvent('L2', 'detector', 0.0, t(800), during='any'),),
            timing_policy='strict-v1')

    def anchors_and_time_waits():
        return ScanProgram(
            loops=(Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 3)),
            drives=(DeviceDrive('D', table_loop='x', table_um=(0.0, 1.0, 0.5), motion=jump),),
            step_s=t(1000),
            timeline=(TimelineEvent('L0', 'ttl', 0.0, t(100), anchor='pass-start:x',
                                    during='any'),
                      TimelineEvent('L1', 'ttl', t(-300), t(-100), anchor='pass-end:x',
                                    during='any')),
            waits=(Wait('y', 'time', interval_s=t(500)),),
            timing_policy='strict-v1')

    programs = {'raster with line steps': raster, 'interleave': interleave,
                'serpentine on two loops': serpentine,
                'pass anchors, a table and time waits': anchors_and_time_waits}
    if power is not None:
        def power_level():
            return ScanProgram(
                loops=(Loop('x', 'scan_x', 3),),
                drives=(DeviceDrive('D', weights_um={'x': 0.5}, motion=jump),),
                step_s=t(1000),
                timeline=(TimelineEvent('P', 'level', t(200), t(600), value=power,
                                        during='any'),
                          TimelineEvent('L1', 'ttl', 0.0, t(400), during='any')),
                timing_policy='strict-v1')
        programs['a power level on the second DAC'] = power_level
    if trigger:
        def trigger_waits():
            return ScanProgram(
                loops=(Loop('y', 'scan_y', 3), Loop('x', 'scan_x', 2)),
                drives=(DeviceDrive('D', weights_um={'x': 0.5}, motion=jump),),
                step_s=t(1000),
                timeline=(TimelineEvent('L2', 'detector', 0.0, t(800), during='any'),),
                waits=(Wait('y', 'external'),), timing_policy='strict-v1')
        programs['trigger waits (pulse the trigger input)'] = trigger_waits
    return programs


def _channels(dac, lines, power_dac=None):
    channels = {'D': DeviceChannels('D', analog=f'{BOARD}/dac{dac}', kind='positioner')}
    for index, line in enumerate(lines):
        channels[f'L{index}'] = DeviceChannels(f'L{index}', digital=f'{BOARD}/ttl{line}',
                                               kind='laser')
    if power_dac is not None:
        channels['P'] = DeviceChannels('P', analog=f'{BOARD}/dac{power_dac}', kind='laser')
    return channels


#: The line that is active low: a board that assumes low is off fails.
ACTIVE_LOW = 'L1'


def _setup(power_range=None):
    """1 µm per volt (the board is sent the programs' µm as volts); one line
    active low; ``P``, a laser whose power is the second DAC's volts (no
    calibration, so the value is the voltage), within that DAC's range."""
    lasers = {f'L{i}': SimpleNamespace(ttlActiveLevel='low' if f'L{i}' == ACTIVE_LOW
                                       else 'high') for i in range(LINES)}
    if power_range is not None:
        lasers['P'] = SimpleNamespace(ttlActiveLevel='high', valueRangeMin=power_range[0],
                                      valueRangeMax=power_range[1], managerProperties={})
    return SimpleNamespace(
        positioners={'D': SimpleNamespace(managerProperties={'conversionFactor': 1.0})},
        lasers=lasers, detectors={})


def _deadline(schedule, rate_hz, transport, trigger_timeout_s):
    """How long a run may take before it counts as hung: the program's
    realized duration, the link's reply time, and ``trigger_timeout_s`` per
    trigger wait. ``None`` for an in-process board, which runs at the
    computer's pace and always reports an end."""
    if not transport.reply_timeout:
        return None
    ticks = sum(segment.ticks for segment in schedule.segments)
    waits = len(schedule.segments) - 1
    return ticks / rate_hz + float(transport.reply_timeout) + waits * float(trigger_timeout_s or 0)


def run(transport, *, trigger=False, trigger_timeout_s=None,
        report: Callable[[str], None] = print, dac=None, power_dac=None, lines=None,
        power=True) -> List[Result]:
    """Every conformance program on the board behind ``transport``.
    ``trigger_timeout_s``: how long a pulse on the trigger input may take to
    come, per trigger wait (needed with ``trigger`` on a real board).

    The programs drive the board's first DAC, its first three lines and, for
    the power level, the next DAC whose range takes 0 V -- unless told which:
    ``dac``, ``lines`` (three line indices) and ``power_dac`` choose channels
    nothing is wired to on a board in a rig, and ``power=False`` leaves the
    power-level program out when no second DAC is free."""
    if trigger and trigger_timeout_s is None and transport.reply_timeout:
        raise ValueError('Trigger waits on a real board need trigger_timeout_s: how long a '
                         'pulse may take to come.')
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name=BOARD,
                                    setupInfo=_setup(), transport=transport)
    caps = engine._caps
    if caps is None:
        raise StepBoardError(f'The board does not answer as a step board: '
                             f'{engine._unreachable}')
    if not caps.dacs or caps.ttl_lines < LINES or caps.grid_ns is None:
        raise StepBoardError(f'The programs need a DAC, {LINES} lines and a declared grid; '
                             f'{caps.board} declares {len(caps.dacs)} DACs, {caps.ttl_lines} '
                             'lines.')
    report(f'{caps.board} ({caps.firmware}): grid {caps.grid_ns} ns, '
           f'{len(caps.dacs)} DACs, {caps.ttl_lines} lines')
    dac = min(caps.dacs) if dac is None else int(dac)
    if dac not in caps.dacs:
        raise StepBoardError(f'{caps.board} has no dac{dac}; it declares '
                             f'{", ".join(f"dac{i}" for i in sorted(caps.dacs))}.')
    lines = list(range(LINES)) if lines is None else [int(line) for line in lines]
    if len(lines) != LINES or len(set(lines)) != LINES:
        raise StepBoardError(f'The programs need {LINES} distinct lines, not {lines}.')
    for line in lines:
        if not 0 <= line < caps.ttl_lines:
            raise StepBoardError(f'{caps.board} has no ttl{line}; it has ttl0 to '
                                 f'ttl{caps.ttl_lines - 1}.')
    if not power:
        power_dac = None
    elif power_dac is None:
        # A power level needs a second DAC whose range takes the laser's off, 0 V.
        others = [index for index in sorted(caps.dacs) if index != dac
                  and caps.dacs[index][0] <= 0.0 <= caps.dacs[index][1]]
        power_dac = others[0] if others else None
    else:
        power_dac = int(power_dac)
        if power_dac not in caps.dacs or power_dac == dac:
            raise StepBoardError(f'The power level needs a second DAC of {caps.board}, not '
                                 f'dac{power_dac}.')
        low, high = caps.dacs[power_dac]
        if not low <= 0.0 <= high:
            raise StepBoardError(f'dac{power_dac} gives {low:g} to {high:g} V and cannot '
                                 'play a power level that is off at 0 V.')
    power_range = None if power_dac is None else caps.dacs[power_dac]
    engine._setupInfo = _setup(power_range)
    channels = _channels(dac, lines, power_dac)
    report(f'driving dac{dac}, ttl{lines[0]}, ttl{lines[1]}, ttl{lines[2]}'
           + (f', a power level on dac{power_dac}' if power_dac is not None else
              ', no power level'))
    grid_s = caps.grid_ns / NS_PER_S
    rate_hz = NS_PER_S / caps.grid_ns      # exact for a grid that divides a second
    step = caps.dac_step(dac)
    resolution = {'D': step} if step is not None else None
    results = []
    level = None if power_range is None else float(power_range[1])   # its top: in range
    for name, make in _programs(grid_s, trigger, level).items():
        program = make()
        try:
            compiled = engine.compile(program, channels, captured_um={})
            schedule = realize(expand(program, RunContext()), rate_hz)
            deadline = _deadline(schedule, rate_hz, transport, trigger_timeout_s)
            engine.prepare(compiled)
            engine.run(compiled, blocking=deadline is None)
            if deadline is not None:
                engine.join(deadline)
            outcome = engine.outcome
            if outcome is None:
                engine.abort()
                engine.join(engine._stop_limit(transport))
                problems = [f'no end of the run reported within {deadline:g} s (the program\'s '
                            'duration and the link\'s reply time); it was told STOP']
            elif outcome[0] != 'done':
                problems = [f'the run ended {outcome}']
            else:
                # The segments restart at each release, so the execution
                # after it is compared whenever it came.
                problems = (log_differences(engine_log(engine.log), expected_log(schedule),
                                            resolution)
                            + level_differences(engine.log, _levels(compiled)))
        except (ScanEngineRefused, StepBoardError) as error:
            problems = [str(error)]
        results.append(Result(name, not problems, problems))
        report(f'{"PASS" if not problems else "FAIL"}  {name}'
               + ''.join(f'\n      {p}' for p in problems))
    return results


def _levels(compiled):
    """``{device: (on, off)}``: the levels the host sent for each event's
    channel (its EVENT lines), in the program's device names."""
    return {compiled.back.get(e['device'], (e['device'], None))[0]: (e['on'], e['off'])
            for e in compiled.wire.events}


def simulated_transport(grid_ns, *, dac_bits=None):
    """A simulated board to try the runner on (``--simulated``)."""
    from .StepProtocolScanEngine import SimulatedTransport, simulated_board
    return SimulatedTransport(simulated_board({
        'gridNs': grid_ns, 'dacs': {'0': [-10, 10], '1': [0, 10]}, 'ttlLines': LINES + 1,
        'triggerIn': True, 'triggerLatencyTicks': 2,
        'dacBits': dict(dac_bits or {})}))


__all__ = ['Result', 'run', 'simulated_transport']


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
