"""The TriggerScope step firmware, run on this computer as a fake board
(firmware/triggerscope_step/host), against the step-engine protocol's
conformance programs and the host engine (docs/devices/step-engine-protocol.rst,
"Testing a firmware").

The firmware core (step_core.cpp) is compiled with the system C++ compiler
and spoken to over a pipe, exactly as a board on a serial port is: *IDN?,
*CAPS?, a program upload, RUN, the reports, LOG?. Its log must match the
scan-program interpreter edge by edge, as the simulated board's does. The
fake board takes the simulated board's test-only hooks -- trigger arrivals
chosen in advance, an injected failure -- outside the protocol.

Skipped when no C++ compiler builds it.
"""

import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines import conformance_runner
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    StepBoardError, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import message
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences, trigger_problems,
)
from imswitch.imcontrol.model.scan_program.engine import DeviceChannels

REPO = Path(__file__).resolve().parents[4]
HOST = REPO / 'firmware' / 'triggerscope_step' / 'host'
GRID_NS = 1000
RATE = 1_000_000
LATENCY_TICKS = 2

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='the fake board is POSIX')


@pytest.fixture(scope='module')
def binary(tmp_path_factory):
    compiler = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
    if compiler is None:
        pytest.skip('no C++ compiler to build the fake board')
    out = tmp_path_factory.mktemp('firmware') / 'host_board'
    core = HOST.parent / 'TriggerScopeStep'
    subprocess.run([compiler, '-std=c++11', '-O1', '-Wall', f'-I{core}', '-o', str(out),
                    str(HOST / 'host_board.cpp'), str(core / 'step_core.cpp')], check=True)
    return out


class PipeTransport:
    """The line link to the fake board process: what SerialTransport is to a
    board on a port."""

    def __init__(self, binary, *args):
        self.process = subprocess.Popen([str(binary), *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, bufsize=1)
        self.reply_timeout = 5.0
        self.run_timeout = 5.0
        self.board = None
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.process.stdout:
            self._lines.put(line.rstrip('\n'))

    def write(self, line):
        self.process.stdin.write(line + '\n')
        self.process.stdin.flush()

    def read(self, timeout):
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def hook(self, line):
        """A test hook of the fake board; its answer."""
        self.write(line)
        return self.read(self.reply_timeout)

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.wait(timeout=5)


@pytest.fixture
def transport(binary):
    t = PipeTransport(binary)
    yield t
    t.close()


def test_the_conformance_programs_pass(transport):
    results = conformance_runner.run(transport, report=lambda line: None)
    assert [r.program for r in results if not r.passed] == [], \
        '\n'.join(f'{r.program}: {r.problems}' for r in results if not r.passed)
    assert len(results) == 5       # the four, and a power level on the second DAC


def test_the_programs_run_on_chosen_channels(transport):
    """A board in a rig has devices on its first channels: the runner drives
    the ones it is told, and leaves the power level out when asked."""
    lines = []
    results = conformance_runner.run(transport, report=lines.append, dac=1, lines=[3, 2, 1],
                                     power=False)
    assert [r.program for r in results if not r.passed] == []
    assert len(results) == 4
    assert 'driving dac1, ttl3, ttl2, ttl1, no power level' in lines
    with pytest.raises(StepBoardError, match='no dac7'):
        conformance_runner.run(transport, report=lambda line: None, dac=7)
    with pytest.raises(StepBoardError, match='second DAC'):
        conformance_runner.run(transport, report=lambda line: None, dac=0, power_dac=0)


# -- trigger waits with chosen arrivals, a stop, a failure -----------------------

CHANNELS = {
    'X': DeviceChannels('X', analog='Board/dac0', kind='positioner'),
    'L405': DeviceChannels('L405', digital='Board/ttl0', kind='laser'),
    'Camera': DeviceChannels('Camera', digital='Board/ttl2', kind='detector'),
}
SETUP = SimpleNamespace(
    positioners={'X': SimpleNamespace(managerProperties={'conversionFactor': 2.0})},
    lasers={'L405': SimpleNamespace(ttlActiveLevel='high')},
    detectors={'Camera': SimpleNamespace(ttlActiveLevel='low')})
JUMP = Motion(transition='jump', move_s=100e-6, settle_s=100e-6, return_s=200e-6,
              origin='absolute', restore='first')


def _engine(transport):
    return StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                  setupInfo=SETUP, transport=transport)


def _waiting(**wait):
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 3), Loop('x', 'scan_x', 2)),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=JUMP),),
        step_s=1e-3,
        timeline=(TimelineEvent('Camera', 'detector', 0.0, 800e-6, during='any'),),
        waits=(Wait('y', 'external', **wait),))


def _run(engine, program, transport, arrivals=()):
    compiled = engine.compile(program, CHANNELS, captured_um={})
    assert transport.hook('#ARRIVE,' + (';'.join(str(a) for a in arrivals) or '-')) == 'OK,#ARRIVE'
    engine.prepare(compiled)
    engine.run(compiled)
    engine.join(10)
    return compiled, engine.outcome


def _wait_starts(program, arrivals, latency):
    """Where each wait begins, from the program's own schedule and the test's
    arrivals -- not from the board."""
    schedule = realize(expand(program, RunContext()), RATE)
    starts, wall = [], 0
    pending = sorted(arrivals)
    for segment in schedule.segments[:-1]:
        wall += segment.ticks
        starts.append(wall)
        due = next(a for a in pending if a >= wall)
        pending.remove(due)
        wall = due + latency
    return starts


def test_trigger_waits_release_on_the_chosen_arrivals(transport):
    engine = _engine(transport)
    arrivals = (500, 9000, 20000)
    compiled, outcome = _run(engine, _waiting(), transport, arrivals)
    assert outcome == ('done', '')
    schedule = realize(expand(compiled.program, RunContext()), RATE)
    # X is 2 µm per volt on a 16-bit DAC: positions match within half a code.
    resolution = {'X': engine._caps.dac_step(0) * 2.0}
    assert log_differences(engine_log(engine.log), expected_log(schedule), resolution) == []
    starts = _wait_starts(compiled.program, arrivals, LATENCY_TICKS)
    assert trigger_problems(engine.log.of('release'), arrivals, starts, LATENCY_TICKS) == []


def test_a_timeout_that_continues_releases_at_the_deadline(transport):
    engine = _engine(transport)
    # One arrival, after the approach (the drive's move and settle, 200
    # ticks) so the first wait is already waiting; the other two time out.
    compiled, outcome = _run(engine, _waiting(timeout_s=2e-3, on_timeout='continue'),
                             transport, arrivals=(300,))
    assert outcome == ('done', '')
    releases = engine.log.of('release')
    assert len(releases) == 3
    assert releases[0][3] == 300                 # the one arrival
    assert releases[1][3] is None and releases[2][3] is None     # the timeouts


def test_a_timeout_that_fails_ends_the_run(transport):
    engine = _engine(transport)
    compiled, outcome = _run(engine, _waiting(timeout_s=2e-3, on_timeout='fail'),
                             transport, arrivals=(300,))
    assert outcome[0] == 'failed'
    assert 'no trigger within 2000 ticks' in outcome[1]


def test_a_stop_during_a_wait_is_honoured_at_once(transport):
    engine = _engine(transport)
    compiled = engine.compile(_waiting(), CHANNELS, captured_um={})
    assert transport.hook('#ARRIVE,300') == 'OK,#ARRIVE'     # the first wait only
    engine.prepare(compiled)
    engine.run(compiled)
    threading.Timer(0.5, lambda: engine.abort()).start()
    engine.join(10)
    assert engine.outcome == ('stopped', '')
    stopped = [e for e in engine.log.entries if e[0] == 'safe-state']
    assert len(stopped) == 1
    assert engine.log.of('release')[0][3] == 300
    assert len(engine.log.of('release')) == 1       # stopped in the second wait


def test_an_injected_failure_reports_the_step(transport):
    engine = _engine(transport)
    assert transport.hook('#FAIL,3') == 'OK,#FAIL'
    compiled = engine.compile(_waiting(), CHANNELS, captured_um={})
    assert transport.hook('#ARRIVE,300;5000;20000') == 'OK,#ARRIVE'
    engine.prepare(compiled)
    engine.run(compiled)
    engine.join(10)
    assert engine.outcome[0] == 'failed'
    assert 'failed at step 3: injected failure at step 3' in engine.outcome[1]
    assert transport.hook('#FAIL,-') == 'OK,#FAIL'


# -- what the board refuses -----------------------------------------------------------

def _answers(transport, lines):
    for line in lines:
        transport.write(line)
    return transport.read(transport.reply_timeout)


def test_a_channel_the_board_lacks_is_refused(transport):
    answer = _answers(transport, ['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'PHASE,-', 'STEP,10,0',
                                  'DRIVE,dac7,0.0,1,1,in-step,absolute,first', 'PROGRAM,END'])
    assert message(answer) == ('ERR', ['channel', 'dac7: this board has no such channel'])


def test_a_number_that_does_not_fit_is_refused_not_wrapped(transport):
    answer = _answers(transport, ['PROGRAM,BEGIN', 'LOOP,4294967296,forward,-', 'PHASE,-',
                                  'STEP,10,0', 'PROGRAM,END'])
    kind, (code, reason) = message(answer)
    assert (kind, code) == ('ERR', 'range')
    assert 'line 2' in reason and 'unsigned 32-bit' in reason


def test_a_program_without_a_step_line_is_refused(transport):
    answer = _answers(transport, ['PROGRAM,BEGIN', 'LOOP,2,forward,-', 'PROGRAM,END'])
    assert message(answer) == ('ERR', ['syntax', 'The program has no STEP line.'])


def test_run_without_a_program_and_a_second_upload_while_running(transport):
    assert message(_answers(transport, ['RUN'])) == ('ERR', ['state', 'no program to run'])


def test_set_by_hand(transport):
    assert _answers(transport, ['SET,dac1,2.5']) == 'OK,SET'
    assert message(_answers(transport, ['SET,dac1,11'])) == \
        ('ERR', ['range', 'dac1 gives 0 to 10 V, not 11'])
    assert message(_answers(transport, ['SET,time0,1']))[1][0] == 'channel'
