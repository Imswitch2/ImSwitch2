"""Engine conformance on the step boards (docs/design/plans/
scan-engines-and-expert-scan.md §2.4, §2.7, §5.0, §5.2, M1, M5).

Two boards execute programs independently of the interpreter: the simulated
step board (M1), and a board behind the step-engine protocol (M5) -- the
same executor, reached through IMSWITCH_STEP/1: compiled to channels and
volts on the host, uploaded as lines, decoded and run by the board, its log
asked for with LOG? and read back in the program's terms. Each log must
match the interpreter's realized schedule. Trigger handling is checked
against arrival traces the test chooses, with mutants that release early or
ignore the trigger. Stop, failure, preparation, readiness and the
feasibility report are exercised too.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines import (
    SimulatedStepEngine, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SimulatedTransport,
)
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import BoardCaps
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, DeviceFacts, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait,
    expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences, trigger_problems,
)
from imswitch.imcontrol.model.scan_program.engine import (
    DeviceChannels, IMPOSSIBLE, NATIVE, ScanEngineRefused, SetupError, UNKNOWN, assign_owners,
)

RATE = 100_000             # an exact integer rate: 1 / 1e-5 is 99999.99999999999 in binary
GRID_S = 1 / RATE
GRID_NS = 10_000
LATENCY_TICKS = 2
JUMP = Motion(transition='jump', move_s=1e-3, settle_s=1e-3, return_s=2e-3,
              origin='absolute', restore='first')

CHANNELS = {
    'X': DeviceChannels('X', analog='Board/dac0', kind='positioner'),
    'RO': DeviceChannels('RO', analog='Board/dac1', kind='positioner'),
    'L405': DeviceChannels('L405', digital='Board/ttl0', kind='laser'),
    'L488': DeviceChannels('L488', digital='Board/ttl1', kind='laser'),
    'Camera': DeviceChannels('Camera', digital='Board/ttl2', kind='detector'),
    'Elsewhere': DeviceChannels('Elsewhere', digital='Dev1/port0/line3', kind='laser'),
}

#: What the protocol engine reads from the setup: µm per volt (X and RO
#: differ, so a drive's volts are not its µm), and each line's polarity.
SETUP = SimpleNamespace(
    positioners={'X': SimpleNamespace(managerProperties={'conversionFactor': 2.0}),
                 'RO': SimpleNamespace(managerProperties={'conversionFactor': 0.5})},
    lasers={'L405': SimpleNamespace(ttlActiveLevel='high'),
            'L488': SimpleNamespace(ttlActiveLevel='high')},
    detectors={'Camera': SimpleNamespace(ttlActiveLevel='low')})


def _board(cls=SimulatedStepEngine, **props):
    props = dict({'timingGridS': GRID_S,
                  'analogChannels': {'Board/dac0': [-10, 10], 'Board/dac1': [-10, 10]},
                  'digitalLines': ['Board/ttl0', 'Board/ttl1', 'Board/ttl2'],
                  'triggerLatencyS': LATENCY_TICKS * GRID_S}, **props)
    return cls(SimpleNamespace(managerProperties=props), name='Board')


def _wire(board_cls=SimulatedStepBoard, **caps):
    """The protocol engine on a simulated board that declares what the
    simulated step board is given."""
    caps = BoardCaps(**{**dict(board='Simulated', firmware='test', grid_ns=GRID_NS,
                               dacs={0: (-10.0, 10.0), 1: (-10.0, 10.0)}, ttl_lines=3,
                               trigger_in=True, trigger_latency_ticks=LATENCY_TICKS), **caps})
    board = board_cls(caps, latency_ticks=LATENCY_TICKS)
    return StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                  setupInfo=SETUP, transport=SimulatedTransport(board))


BOARDS = {'simulated': _board, 'wire': _wire}


@pytest.fixture(params=sorted(BOARDS))
def make(request):
    """Each step board, built with its defaults."""
    return BOARDS[request.param]


def _raster(**extra):
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2), Loop('x', 'scan_x', 3)),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=JUMP),),
        step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 4e-3, phases=frozenset({0}), during='any'),
                  TimelineEvent('L488', 'ttl', 2e-3, 6e-3, phases=frozenset({1}), during='any'),
                  TimelineEvent('Camera', 'detector', 0.0, 8e-3, during='any')),
        **extra,
    )


def _interleaved():
    """MS-RESOLFT, 2 cycles x 3 planes on one readout device."""
    return ScanProgram(
        loops=(Loop('cycle', 'cycle', 2), Loop('plane', 'plane', 3)),
        drives=(DeviceDrive('RO', weights_um={'cycle': 0.25, 'plane': 0.5}, motion=JUMP),),
        step_s=20e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 2e-3, during='any', id='on'),
                  TimelineEvent('L488', 'ttl', 3e-3, 8e-3, during='any', id='off'),
                  TimelineEvent('Camera', 'detector', 9e-3, 15e-3, during='any', id='ro')),
    )


def _run(engine, program, *, arrivals=(), devices=None, **kwargs):
    compiled = engine.compile(program, CHANNELS, devices=devices)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=arrivals, blocking=True, **kwargs)
    return engine


def _oracle(program, devices=None):
    return expected_log(realize(expand(program, RunContext(devices=devices or {})), RATE))


# ---------------------------------------------------------------------------
# Execution matches the program's meaning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('program', [_raster, _interleaved], ids=['raster', 'interleaved'])
def test_the_board_does_what_the_program_means(make, program):
    program = program()
    board = _run(make(), program)
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def _serpentine_with_phases():
    """x runs back on every other line played, line steps included: two
    parity loops, whose flat index -- not the sum of their counters -- says
    which way (forward, back, forward, back)."""
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2),
               Loop('x', 'scan_x', 3, traversal='serpentine', parity_loops=('y', 'phase'))),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=JUMP),),
        step_s=10e-3,
        timeline=(TimelineEvent('Camera', 'detector', 0.0, 8e-3, during='any'),),
    )


def test_a_serpentine_alternates_on_the_flat_index_of_its_parity_loops(make):
    program = _serpentine_with_phases()
    board = _run(make(), program)
    assert log_differences(engine_log(board.log), _oracle(program)) == []
    x = [value for _, _, _, device, value in board.log.of('dac') if device == 'X']
    # first positions, then each line's moves: 0 5 10 | 10 5 0 | 0 5 10 | 10 5 0
    assert x[:3] == [0.0, 5.0, 10.0] and x[3:5] == [5.0, 0.0]


def test_the_interleave_visits_planes_in_its_order(make):
    board = _run(make(), _interleaved())
    positions = [value for _, _, _, device, value in board.log.of('dac') if device == 'RO']
    assert positions[:6] == [0.0, 0.5, 1.0, 0.25, 0.75, 1.25]


def test_a_mock_camera_counts_the_frames_the_layout_plans(make):
    """Readiness: a camera triggered by the board's TTL gets one frame per
    step (2 lines x 2 phases x 3 pixels)."""
    board = _run(make(), _raster())
    rising = [e for e in board.log.entries if e[0] == 'on' and e[3] == 'Camera']
    assert len(rising) == 12


def test_prepare_publishes_the_devices_before_the_run(make):
    board = make()
    built = []
    board.sigScanBuilt.connect(lambda info, plan, devices: built.append(devices))
    compiled = board.compile(_raster(), CHANNELS)
    assert not board.ready()
    board.prepare(compiled)
    assert board.ready() and built == [['Camera', 'L405', 'L488', 'X']]


def test_the_lifecycle_signals_come_in_order(make):
    board = make()
    seen = []
    for name in ('sigScanStarted', 'sigScanDone', 'sigScanStopped', 'sigScanBuildFailed'):
        getattr(board, name).connect(lambda *a, n=name: seen.append(n))
    _run(board, _raster())
    assert seen == ['sigScanStarted', 'sigScanDone']
    seen.clear()
    _run(board, _raster(), fail_at_step=4)
    assert seen == ['sigScanStarted', 'sigScanBuildFailed']
    assert board.log.of('failed')


# ---------------------------------------------------------------------------
# Waits: execution after release, and trigger handling as output
# ---------------------------------------------------------------------------

def _waiting(**wait):
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 3), Loop('x', 'scan_x', 2)),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=JUMP),),
        step_s=10e-3,
        timeline=(TimelineEvent('Camera', 'detector', 0.0, 8e-3, during='any'),),
        waits=(Wait('y', 'cameraReady', **wait),),
    )


def _wait_starts(program, arrivals, latency):
    """Where each wait begins, from the program's own schedule and the test's
    arrivals -- not from the engine."""
    schedule = realize(expand(program), RATE)
    starts, wall = [], 0
    pending = sorted(arrivals)
    for segment in schedule.segments[:-1]:
        wall += segment.ticks
        starts.append(wall)
        due = next(a for a in pending if a >= wall)
        pending.remove(due)
        wall = due + latency
    return starts


def test_execution_after_each_release_matches(make):
    program = _waiting()
    # the first after the 200-tick approach (X's move and settle, §7.9)
    board = _run(make(), program, arrivals=[250, 7000, 20000])
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def test_releases_follow_their_triggers_within_the_declared_latency(make):
    program = _waiting()
    arrivals = [250, 7000, 20000]
    board = _run(make(), program, arrivals=arrivals)
    starts = _wait_starts(program, arrivals, latency=LATENCY_TICKS)
    assert trigger_problems(board.log.of('release'), arrivals, starts,
                            latency_ticks=LATENCY_TICKS) == []


def _early(base):
    class EarlyRelease(base):
        def _release(self, wait_start, arrivals, timeout):
            return wait_start, None
    return EarlyRelease


def _ignoring(base):
    class IgnoresTrigger(base):
        def _release(self, wait_start, arrivals, timeout):
            later = [a for a in arrivals if a > wait_start]
            return (later[1], later[1]) if len(later) > 1 else super()._release(
                wait_start, arrivals, timeout)
    return IgnoresTrigger


@pytest.mark.parametrize('board, base', [('simulated', SimulatedStepEngine),
                                         ('wire', SimulatedStepBoard)])
@pytest.mark.parametrize('mutant, symptom', [(_early, 'before its trigger'),
                                              (_ignoring, 'later than')])
def test_the_trigger_check_catches_engines_that_get_triggers_wrong(board, base, mutant,
                                                                   symptom):
    program = _waiting()
    arrivals = [250, 7000, 9000, 20000, 30000, 40000, 50000]
    board = _run(BOARDS[board](mutant(base)), program, arrivals=arrivals)
    starts = _wait_starts(program, arrivals, latency=LATENCY_TICKS)
    problems = trigger_problems(board.log.of('release'), arrivals, starts,
                                latency_ticks=LATENCY_TICKS)
    assert any(symptom in p for p in problems), problems
    # ... while replaying its own releases would not have noticed.
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def test_a_timeout_fails_or_continues_as_asked(make):
    failing = _run(make(), _waiting(timeout_s=1e-3), arrivals=[])
    assert failing.log.of('failed')
    program = _waiting(timeout_s=1e-3, on_timeout='continue')
    board = _run(make(), program, arrivals=[])
    releases = board.log.of('release')
    assert len(releases) == 3 and all(arrival is None for *_, arrival in releases)
    starts = _wait_starts_with_timeouts(program, timeout=100)
    assert trigger_problems(releases, [], starts, latency_ticks=LATENCY_TICKS,
                            timeout_ticks=100, on_timeout='continue') == []


def _wait_starts_with_timeouts(program, timeout):
    schedule = realize(expand(program), RATE)
    starts, wall = [], 0
    for segment in schedule.segments[:-1]:
        wall += segment.ticks
        starts.append(wall)
        wall += timeout
    return starts


def test_preparation_after_a_release_comes_from_the_device_and_moves_no_event(make):
    devices = {'Camera': DeviceFacts(1e-3, 'Camera.preparation.durationS')}
    program = _waiting()
    board = _run(make(), program, arrivals=[250, 7000, 20000], devices=devices)
    assert log_differences(engine_log(board.log), _oracle(program, devices)) == []


def test_an_unknown_preparation_refuses_a_waiting_program(make):
    devices = {'Camera': DeviceFacts('unknown', 'Camera: no preparation key')}
    with pytest.raises(ScanEngineRefused, match='preparation time is\\s+unknown'):
        make().compile(_waiting(), CHANNELS, devices=devices)


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def test_stop_at_a_step_boundary():
    board = _board()
    compiled = board.compile(_raster(), CHANNELS)
    board.prepare(compiled)
    board.abort()
    board.run(compiled, blocking=True)
    assert board.log.of('safe-state') and not board.log.of('end')


def test_stop_during_an_unbounded_wait_is_honoured_at_once(qtbot, make):
    board = make()
    compiled = board.compile(_waiting(), CHANNELS)
    board.prepare(compiled)
    stopped = threading.Event()
    board.sigScanStopped.connect(lambda: stopped.set())
    board.run(compiled, arrivals=[], blocking=False)
    time.sleep(0.05)
    assert not stopped.is_set()           # blocked on its trigger
    assert board.abort() == 'step-boundary'
    board.join(timeout=2)
    qtbot.waitUntil(stopped.is_set, timeout=2000)     # emitted from the board's thread
    assert board.log.of('safe-state')


# ---------------------------------------------------------------------------
# Feasibility: what the board cannot do is refused, unknowns are unknown
# ---------------------------------------------------------------------------

def test_a_line_another_engine_owns_is_refused_with_the_remedy(make):
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('Elsewhere', 'ttl', 0.0, 1e-3,
                                                  during='any'),))
    report = make().assess(program, CHANNELS)
    verdict = report.verdict('ttl Elsewhere')
    assert verdict.verdict == IMPOSSIBLE and 'Dev1/port0/line3' in verdict.reason
    assert 'Wire it to this engine' in verdict.remedy


def test_the_board_cannot_sweep_and_says_so(make):
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2, mode='swept'),),
                          drives=(DeviceDrive('X', weights_um={'x': 1.0}, motion=JUMP),),
                          step_s=1e-3)
    verdict = make().assess(program, CHANNELS).verdict('swept loop')
    assert verdict.verdict == IMPOSSIBLE and 'Make the axis stepped' in verdict.remedy


@pytest.mark.parametrize('board', [lambda: _board(triggerLatencyS=None),
                                   lambda: _wire(trigger_latency_ticks=None)],
                         ids=['simulated', 'wire'])
def test_an_undeclared_trigger_latency_is_unknown_not_guessed(board):
    board = board()
    verdict = board.assess(_waiting(), CHANNELS).verdict('trigger wait at y')
    assert verdict.verdict == UNKNOWN and 'does not declare its trigger latency' in verdict.reason
    with pytest.raises(ScanEngineRefused):
        board.compile(_waiting(), CHANNELS)


def test_times_off_the_boards_grid_are_refused(make):
    program = _raster()
    program = ScanProgram(**{**program.__dict__, 'step_s': 10.005e-3})
    # the schedule refuses it, as for every engine (one authority, §7.9)
    with pytest.raises(ScanEngineRefused, match='10.005 ms is not on the 10 µs grid'):
        make().compile(program, CHANNELS)


def test_a_board_without_a_declared_grid_does_not_start():
    with pytest.raises(ValueError, match='timingGridS is required'):
        SimulatedStepEngine(SimpleNamespace(managerProperties={}))


def test_channel_ownership_must_be_unambiguous():
    a, b = _board(), _board()
    b.name = 'Other'
    with pytest.raises(SetupError,
                       match='Board/ttl0 \\(L405\\) is claimed by Board and Other'):
        assign_owners([a, b], CHANNELS)
    owners = assign_owners([a], CHANNELS)
    assert owners['Board/ttl0'] == 'Board' and owners['Dev1/port0/line3'] is None


def test_what_the_board_runs_is_native(make):
    report = make().assess(_raster(), CHANNELS)
    assert report.runnable
    assert report.verdict('stepped loop').verdict == NATIVE


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
