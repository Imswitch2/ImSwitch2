"""The step-engine protocol, IMSWITCH_STEP/1 (docs/design/plans/
scan-engines-and-expert-scan.md §5.2, M5; docs/devices/step-engine-protocol.rst).

The codec, the simulated board's answers to what is not a program it can
run, and the host engine's side: what it reads from the board, what it
refuses before uploading (no conversion, out of range, too wide for the
protocol, a power level), the polarity and safe levels it sends, a board that
speaks another version or does not answer, a stop before the run, the serial
link. Conformance of the executed runs is in test_scan_engine_conformance.py.
"""

import sys
import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SerialTransport, SimulatedTransport, StepBoardError, StepProtocolScanEngine,
    simulated_board,
)
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import (
    BoardCaps, CAPACITY, PROTOCOL, ProtocolError, UINT32_MAX, lines_to_plan, parse_caps,
    parse_log, log_lines, plan_to_lines,
)
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait,
)
from imswitch.imcontrol.model.scan_program.engine import IMPOSSIBLE, UNKNOWN, ScanEngineRefused

from .test_scan_engine_conformance import (
    CHANNELS, GRID_NS, JUMP, SETUP, _interleaved, _raster, _waiting, _wire,
)

CAPTURED = Motion(transition='jump', move_s=1e-3, settle_s=1e-3, return_s=2e-3,
                  origin='captured', restore='park')


def _everything():
    """A program with every kind of line the protocol has: a table, a
    captured origin, a park, a serpentine, pass anchors, phases, waits."""
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2),
               Loop('x', 'scan_x', 3, traversal='serpentine', parity_loops=('y', 'phase'))),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=CAPTURED),
                DeviceDrive('RO', table_loop='y', table_um=(0.25, 0.75), motion=JUMP)),
        step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 4e-3, phases=frozenset({0}), during='any'),
                  TimelineEvent('L488', 'ttl', 0.0, 1e-3, anchor='pass-start:x', during='any'),
                  TimelineEvent('Camera', 'detector', -2e-3, 0.0, anchor='pass-end:x',
                                during='any')),
        waits=(Wait('y', 'cameraReady', timeout_s=5e-3, on_timeout='continue'),
               Wait('step', 'time', interval_s=1e-4)),
        park={'X': 1.0},
    )


def _compiled(engine=None, program=None):
    engine = engine or _wire()
    return engine, engine.compile(program or _everything(), CHANNELS, captured_um={'X': 2.0})


# ---------------------------------------------------------------------------
# The codec
# ---------------------------------------------------------------------------

def test_a_decoded_program_encodes_to_the_same_lines():
    _, compiled = _compiled()
    lines = compiled.lines
    assert lines[0] == 'PROGRAM,BEGIN' and lines[-1] == 'PROGRAM,END'
    assert plan_to_lines(lines_to_plan(lines)) == lines
    kinds = {line.split(',')[0] for line in lines}
    assert kinds >= {'LOOP', 'PHASE', 'STEP', 'DRIVE', 'WEIGHT', 'TABLE', 'EVENT', 'WAIT',
                     'CAPTURED', 'PARK', 'SAFE'}


def test_positions_go_as_volts_and_lines_as_their_levels():
    _, compiled = _compiled()
    lines = compiled.lines
    # X: 2 µm per volt; RO: 0.5 µm per volt
    assert 'WEIGHT,dac0,2,2.5' in lines and 'TABLE,dac1,0,0.5;1.5' in lines
    assert 'CAPTURED,dac0,1.0' in lines and 'PARK,dac0,0.5' in lines
    # the camera is active low: on is 0, and its safe level is 1
    camera = next(line for line in lines if line.startswith('EVENT,ttl2'))
    assert camera.endswith(',0,1') and 'SAFE,ttl2,1' in lines
    assert next(line for line in lines if line.startswith('EVENT,ttl0')).endswith(',1,0')
    assert 'SAFE,ttl0,0' in lines


def test_caps_and_logs_read_back():
    caps = BoardCaps('B', 'fw 2', grid_ns=1000, dacs={0: (-5.0, 5.0), 3: (0.0, 10.0)},
                     ttl_lines=16, max_steps=4096, trigger_in=True, trigger_latency_ticks=3)
    assert parse_caps('B', 'fw 2', caps.lines()) == caps
    assert caps.has('dac3') and not caps.has('dac1') and caps.has('ttl15')
    assert not caps.has('ttl16') and not caps.has('X')
    engine = _wire()
    engine.run(engine.compile(_raster(), CHANNELS), blocking=True, fail_at_step=3)
    wire_log = engine.board.log
    assert parse_log(log_lines(wire_log)).entries == wire_log.entries
    assert wire_log.of('failed')


def test_a_number_the_protocol_cannot_hold_is_refused_by_the_board():
    board = _wire().board
    for line in ['PROGRAM,BEGIN', f'LOOP,{UINT32_MAX + 1},forward,-', 'STEP,10,0',
                 'PROGRAM,END']:
        board.write(line)
    assert board.read(0).startswith('ERR,range,line 2 (LOOP): 4294967296 does not fit')
    with pytest.raises(ProtocolError) as error:
        lines_to_plan(['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'STEP,10,0',
                       'EVENT,ttl0,0,5,step,-,-,2,0', 'PROGRAM,END'])
    assert error.value.code == 'range' and 'a line level (0 or 1)' in str(error.value)


@pytest.mark.parametrize('lines, answer', [
    (['HELLO'], 'ERR,syntax,not a command of the protocol: HELLO'),
    (['RUN'], 'ERR,state,no program to run'),
    (['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'STEP,10,0', 'DRIVE,dac7,0,0,0,in-step,absolute,'
      'hold', 'PROGRAM,END'], 'ERR,channel,dac7: this board has no such channel'),
    (['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'STEP,10,0', 'EVENT,dac0,0,5,step,-,-,1.5,0',
      'PROGRAM,END'], 'ERR,channel,dac0: only a line takes an event'),
    (['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'STEP,10,0', 'SAFE,dac0,0', 'PROGRAM,END'],
     'ERR,channel,dac0: only a line has a safe level (a DAC holds)'),
    (['PROGRAM,BEGIN', 'LOOP,2,sideways,-', 'STEP,10,0', 'PROGRAM,END'],
     "ERR,syntax,line 2 (LOOP): traversal 'sideways' is not one of forward, reverse, "
     'serpentine'),
    (['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'PROGRAM,END'],
     'ERR,syntax,The program has no STEP line.'),
])
def test_the_board_answers_what_it_cannot_take_with_the_reason(lines, answer):
    board = _wire().board
    for line in lines:
        board.write(line)
    assert board.read(0) == answer


def test_a_board_refuses_more_steps_than_it_holds():
    board = _wire(max_steps=4).board
    for line in ['PROGRAM,BEGIN', 'LOOP,5,forward,-', 'STEP,10,0', 'PROGRAM,END']:
        board.write(line)
    assert board.read(0) == 'ERR,capacity,5 steps; this board holds 4'


# ---------------------------------------------------------------------------
# The host: what the board says it can do
# ---------------------------------------------------------------------------

def test_the_engine_is_what_the_board_says():
    engine = _wire(ttl_lines=2, dacs={0: (-1.0, 1.0)}, max_steps=99)
    caps = engine.capabilities()
    assert caps.timing_grid_s == GRID_NS / 1e9 and caps.max_steps == 99
    assert caps.analog['Board/dac0'].max_v == 1.0 and caps.digital == ('Board/ttl0',
                                                                         'Board/ttl1')
    assert caps.sources['timing_grid_s'] == 'Board *CAPS? grid_ns'
    assert engine.owns('Board/ttl1') and not engine.owns('Board/ttl2')
    assert not engine.owns('Board/dac1') and not engine.owns('Other/ttl0')
    # a device on a channel the board does not have: no engine drives it
    report = engine.assess(_raster(), CHANNELS)
    assert 'Board/ttl2, which Board does not drive' in report.verdict('detector Camera').reason


class _OtherVersion(SimulatedStepBoard):
    def write(self, line):
        if line == '*IDN?':
            self._reply('IMSWITCH_STEP/2,Future,9.0')
        else:
            super().write(line)


class _Silent(SimulatedStepBoard):
    def write(self, line):
        pass


@pytest.mark.parametrize('board, reason', [
    (_OtherVersion, f"answers 'IMSWITCH_STEP/2,Future,9.0'; this ImSwitch speaks {PROTOCOL}"),
    (_Silent, 'did not answer *IDN?'),
])
def test_a_board_that_does_not_answer_as_a_step_board_is_unknown(board, reason):
    engine = _wire(board)
    assert reason in engine.assess(_raster(), CHANNELS).verdict('board').reason
    with pytest.raises(ScanEngineRefused, match='has not answered'):
        engine.compile(_raster(), CHANNELS)
    # its channels stay its own while it is away
    assert engine.owns('Board/ttl7') and not engine.owns('Board/X')


def test_a_board_that_answers_later_is_asked_again_at_compile():
    engine = _wire(_Silent)
    assert engine.assess(_raster(), CHANNELS).verdict('board').verdict == UNKNOWN
    engine.board.__class__ = SimulatedStepBoard          # plugged in
    assert engine.compile(_raster(), CHANNELS).report.runnable


# ---------------------------------------------------------------------------
# The host: what it refuses before uploading
# ---------------------------------------------------------------------------

def test_a_positioner_without_its_conversion_is_unknown(monkeypatch):
    monkeypatch.setitem(SETUP.positioners, 'X', SimpleNamespace(managerProperties={}))
    with pytest.raises(ScanEngineRefused) as error:
        _wire().compile(_raster(), CHANNELS)
    verdict = error.value.report.verdict('conversion')
    assert verdict.verdict == UNKNOWN and 'X declares no conversionFactor' in verdict.reason


def test_positions_beyond_the_dac_or_the_positioner_are_refused_with_the_numbers(monkeypatch):
    with pytest.raises(ScanEngineRefused, match='X needs 0 to 5 V; Board dac0 gives -1 to 1 V'):
        _wire(dacs={0: (-1.0, 1.0), 1: (-10.0, 10.0)}).compile(_raster(), CHANNELS)
    monkeypatch.setitem(SETUP.positioners, 'X', SimpleNamespace(
        managerProperties={'conversionFactor': 2.0, 'minVolt': 0, 'maxVolt': 4}))
    with pytest.raises(ScanEngineRefused, match='outside scanner range'):
        _wire().compile(_raster(), CHANNELS)


def test_a_power_level_is_refused_with_the_reason():
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('L405', 'level', 0.0, 1e-3, value=0.5,
                                                  during='any'),))
    channels = dict(CHANNELS, L405=CHANNELS['X'].__class__('L405', analog='Board/dac1',
                                                           kind='laser'))
    verdict = _wire().assess(program, channels).verdict('level L405')
    assert verdict.verdict == IMPOSSIBLE and 'power-to-volt conversion' in verdict.reason


def test_what_does_not_fit_the_protocol_is_refused_on_the_host():
    program = ScanProgram(loops=(Loop('t', 'time', UINT32_MAX + 1),), drives=(),
                          step_s=1e-3, timeline=(TimelineEvent('L405', 'ttl', 0.0, 5e-4,
                                                               during='any'),))
    with pytest.raises(ScanEngineRefused, match="loop 0 count: 4294967296 does not fit"):
        _wire().compile(program, CHANNELS)


def test_two_devices_on_one_line_are_refused():
    channels = dict(CHANNELS, L488=CHANNELS['L405'].__class__('L488', digital='Board/ttl0',
                                                              kind='laser'))
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('L405', 'ttl', 0.0, 5e-4, during='any'),
                                    TimelineEvent('L488', 'ttl', 5e-4, 1e-3, during='any')))
    with pytest.raises(ScanEngineRefused, match='ttl0 carries L405 and L488'):
        _wire().compile(program, channels)


def test_a_polarity_that_is_neither_high_nor_low_is_refused(monkeypatch):
    monkeypatch.setitem(SETUP.lasers, 'L405', SimpleNamespace(ttlActiveLevel='inverted'))
    with pytest.raises(ScanEngineRefused, match='L405: ttlActiveLevel must be "high" or "low", '
                                                "not 'inverted'"):
        _wire().compile(_raster(), CHANNELS)


def test_a_wait_that_does_not_idle_is_refused():
    program = ScanProgram(**{**_waiting().__dict__,
                             'waits': (Wait('y', 'cameraReady', hold='last'),)})
    verdict = _wire().assess(program, CHANNELS).verdict('wait hold at y')
    assert verdict.verdict == IMPOSSIBLE


# ---------------------------------------------------------------------------
# The host: upload, run, stop, failure
# ---------------------------------------------------------------------------

class _SmallerThanItSays(SimulatedStepBoard):
    def _check(self, plan):
        raise ProtocolError('only 4 steps fit after all', CAPACITY)


def test_a_program_the_board_refuses_fails_the_upload_with_its_reason():
    engine = _wire(_SmallerThanItSays)
    compiled = engine.compile(_raster(), CHANNELS)
    with pytest.raises(StepBoardError,
                       match='refused PROGRAM: capacity: only 4 steps fit after all'):
        engine.prepare(compiled)
    assert not engine.ready()


def test_a_stop_and_a_finished_run_leave_every_line_at_its_safe_level(qtbot):
    engine = _wire()
    _run_and_stop(engine, qtbot)
    outputs = engine.board.outputs
    assert outputs['ttl2'] == 1                 # the active-low camera: off is high
    assert outputs['dac0'] == 0.0               # X holds where it is
    engine.run(engine.compile(_raster(), CHANNELS), blocking=True)
    assert engine.board.outputs['ttl0'] == 0 and engine.board.outputs['ttl2'] == 1


def _run_and_stop(engine, qtbot):
    compiled = engine.compile(_waiting(), CHANNELS)
    stopped = threading.Event()
    engine.sigScanStopped.connect(stopped.set)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=[], blocking=False)
    qtbot.wait(50)
    assert not stopped.is_set()
    with pytest.raises(StepBoardError, match='is running; it cannot upload a program'):
        engine.prepare(compiled)
    engine.abort()
    engine.join(timeout=2)
    qtbot.waitUntil(stopped.is_set, timeout=2000)
    assert engine.log.of('safe-state')


def test_a_stop_before_the_run_starts_runs_nothing():
    engine = _wire()
    seen = []
    for name in ('sigScanStarted', 'sigScanStopped', 'sigScanDone'):
        getattr(engine, name).connect(lambda *a, n=name: seen.append(n))
    compiled = engine.compile(_raster(), CHANNELS)
    engine.prepare(compiled)
    assert engine.abort() == 'step-boundary'
    engine.run(compiled, blocking=True)
    assert seen == ['sigScanStopped', 'sigScanDone']
    assert engine.log.entries == [] and engine.board.log.entries == []
    # the next run is not stopped by it
    engine.prepare(compiled)
    engine.run(compiled, blocking=True)
    assert engine.log.of('end')


class _NoLog(SimulatedStepBoard):
    def write(self, line):
        if line == 'LOG?':
            self._error(CAPACITY, 'no memory for a log')
        else:
            super().write(line)


def test_a_board_that_keeps_no_log_says_so():
    engine = _wire(_NoLog)
    engine.run(engine.compile(_raster(), CHANNELS), blocking=True)
    with pytest.raises(StepBoardError, match='keeps no log: capacity: no memory for a log'):
        engine.log


def test_a_failure_reports_the_step_and_the_reason():
    engine = _wire()
    reasons = []
    engine.sigScanFailed.connect(reasons.append)
    engine.run(engine.compile(_interleaved(), CHANNELS), blocking=True, fail_at_step=2)
    assert reasons == ['failed at step 2: injected failure at step 2']


def test_trigger_arrivals_are_only_a_simulated_boards():
    class Link:
        reply_timeout = run_timeout = 0
        board = None

        def __init__(self):
            self.inner = SimulatedTransport(simulated_board({'gridNs': GRID_NS, 'ttlLines': 3,
                                                             'dacs': {0: [-10, 10],
                                                                      1: [-10, 10]}}))

        def write(self, line):
            self.inner.write(line)

        def read(self, timeout):
            return self.inner.read(timeout)

        def close(self):
            pass

    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP, transport=Link())
    compiled = engine.compile(_raster(), CHANNELS)
    with pytest.raises(ValueError, match="a real board takes its trigger input"):
        engine.run(compiled, arrivals=[5])


def test_finalize_stops_a_run_and_closes_the_link(qtbot):
    engine = _wire()
    board = engine.board
    compiled = engine.compile(_waiting(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=[], blocking=False)
    qtbot.wait(50)
    engine.finalize()
    assert not board.running and board.log.of('safe-state')
    with pytest.raises(StepBoardError, match='not connected'):
        engine.prepare(compiled)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('props, message', [
    ({}, 'managerProperties.port is required'),
    ({'port': 'COM3'}, 'baudrate and replyTimeoutS are required on a serial port'),
    ({'port': 'COM3', 'baudrate': 115200}, 'replyTimeoutS is required on a serial port'),
    ({'port': 'simulated'}, 'simulatedBoard is required'),
    ({'port': 'simulated', 'simulatedBoard': {}}, 'simulatedBoard.gridNs is required'),
    ({'port': 'simulated', 'simulatedBoard': {'gridNs': 1000, 'triggerIn': True}},
     'needs its latency'),
])
def test_what_the_engine_needs_is_required(props, message):
    with pytest.raises(ValueError, match=message):
        StepProtocolScanEngine(SimpleNamespace(managerProperties=props), name='Board')


def test_a_port_that_does_not_open_leaves_the_engine_unknown(monkeypatch):
    class Serial:
        def __init__(self, *a, **k):
            raise OSError('could not open port COM9')

    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(Serial=Serial))
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={
        'port': 'COM9', 'baudrate': 115200, 'replyTimeoutS': 0.5}), name='Board')
    assert 'could not open port COM9' in engine.assess(_raster(), CHANNELS).verdict(
        'board').reason


def test_the_serial_link_reassembles_lines_split_by_its_timeout(monkeypatch):
    chunks = [b'IMSWITCH', b'_STEP/1,B,1\n', b'', b'OK,RUN\n']

    class Serial:
        timeout = None

        def __init__(self, *a, **k):
            self.written = []

        def readline(self):
            return chunks.pop(0)

        def write(self, data):
            self.written.append(data)

    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(Serial=Serial))
    link = SerialTransport('COM9', 115200, 0.5)
    assert link.read(0.5) is None
    assert link.read(0.5) == f'{PROTOCOL},B,1'
    assert link.read(0.5) is None and link.read(0.5) == 'OK,RUN'
    link.write('RUN')
    assert link._serial.written == [b'RUN\n']


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
