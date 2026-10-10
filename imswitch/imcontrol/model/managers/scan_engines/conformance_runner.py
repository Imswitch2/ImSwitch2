"""Put a board that speaks the step-engine protocol through the conformance
programs (docs/design/plans/scan-engines-and-expert-scan.md §7.8;
``tools/step_board_conformance.py`` is the command).

Every program is run on the board's own channels -- its first DAC and its
first lines, from ``*CAPS?`` -- and the log the board returns (``LOG?``) is
compared with what the scan-program interpreter says it means, write by
write and edge by edge: the comparison the conformance tests make with the
simulated board, against firmware. A board that declares its DACs' bits is
compared within half a code.

A program with trigger waits needs pulses on the board's trigger input,
which this cannot send: it is run only when asked (``trigger=True``), with
something wired to the input. The execution after each release is still
compared exactly; when the releases came is not (the arrivals are unknown
here).

Qt-free apart from the engine's signals, which it does not use.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Dict, List

from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences,
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


def _programs(grid_s: float, trigger: bool) -> Dict[str, Callable[[], ScanProgram]]:
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


def _channels(dac, lines):
    channels = {'D': DeviceChannels('D', analog=f'{BOARD}/dac{dac}', kind='positioner')}
    for index, line in enumerate(lines):
        channels[f'L{index}'] = DeviceChannels(f'L{index}', digital=f'{BOARD}/ttl{line}',
                                               kind='laser')
    return channels


#: 1 µm per volt: the board is sent the programs' µm as volts.
_SETUP = SimpleNamespace(
    positioners={'D': SimpleNamespace(managerProperties={'conversionFactor': 1.0})},
    lasers={f'L{i}': SimpleNamespace(ttlActiveLevel='high') for i in range(LINES)},
    detectors={})


def run(transport, *, trigger=False, report: Callable[[str], None] = print) -> List[Result]:
    """Every conformance program on the board behind ``transport``."""
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name=BOARD,
                                    setupInfo=_SETUP, transport=transport)
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
    dac = min(caps.dacs)
    channels = _channels(dac, range(LINES))
    grid_s = caps.grid_ns / NS_PER_S
    rate_hz = NS_PER_S / caps.grid_ns      # exact for a grid that divides a second
    step = caps.dac_step(dac)
    resolution = {'D': step} if step is not None else None
    results = []
    for name, make in _programs(grid_s, trigger).items():
        program = make()
        try:
            compiled = engine.compile(program, channels, captured_um={})
            engine.prepare(compiled)
            engine.run(compiled, blocking=True)
            outcome = engine.outcome
            if outcome is None or outcome[0] != 'done':
                problems = [f'the run ended {outcome}']
            else:
                # The segments restart at each release, so the execution
                # after it is compared whenever it came.
                expected = expected_log(realize(expand(program, RunContext()), rate_hz))
                problems = log_differences(engine_log(engine.log), expected, resolution)
        except (ScanEngineRefused, StepBoardError) as error:
            problems = [str(error)]
        results.append(Result(name, not problems, problems))
        report(f'{"PASS" if not problems else "FAIL"}  {name}'
               + ''.join(f'\n      {p}' for p in problems))
    return results


def simulated_transport(grid_ns, *, dac_bits=None):
    """A simulated board to try the runner on (``--simulated``)."""
    from .StepProtocolScanEngine import SimulatedTransport, simulated_board
    return SimulatedTransport(simulated_board({
        'gridNs': grid_ns, 'dacs': {'0': [-10, 10]}, 'ttlLines': LINES + 1,
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
