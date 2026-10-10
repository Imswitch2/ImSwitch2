"""Engine conformance on the simulated step board (docs/design/plans/
scan-engines-and-expert-scan.md §2.4, §2.7, §5.0, M1).

The simulated board executes programs independently of the interpreter; its
log must match the interpreter's realized schedule. Trigger handling is
checked against arrival traces the test chooses, with mutants that release
early or ignore the trigger. Stop, failure, preparation, readiness and the
feasibility report are exercised too.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines import SimulatedStepEngine
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
JUMP = Motion(transition='jump', move_s=1e-3, settle_s=1e-3, return_s=2e-3,
              origin='absolute', restore='first')

CHANNELS = {
    'X': DeviceChannels('X', analog='Sim/DAC0', kind='positioner'),
    'RO': DeviceChannels('RO', analog='Sim/DAC1', kind='positioner'),
    'L405': DeviceChannels('L405', digital='Sim/TTL0', kind='laser'),
    'L488': DeviceChannels('L488', digital='Sim/TTL1', kind='laser'),
    'Camera': DeviceChannels('Camera', digital='Sim/TTL2', kind='detector'),
    'Elsewhere': DeviceChannels('Elsewhere', digital='Dev1/port0/line3', kind='laser'),
}


def _board(cls=SimulatedStepEngine, **props):
    props = dict({'timingGridS': GRID_S,
                  'analogChannels': {'Sim/DAC0': [-10, 10], 'Sim/DAC1': [-10, 10]},
                  'digitalLines': ['Sim/TTL0', 'Sim/TTL1', 'Sim/TTL2'],
                  'triggerLatencyS': 2e-5}, **props)
    return cls(SimpleNamespace(managerProperties=props), name='Board')


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

@pytest.mark.parametrize('make', [_raster, _interleaved], ids=['raster', 'interleaved'])
def test_the_board_does_what_the_program_means(make):
    program = make()
    board = _run(_board(), program)
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def test_the_interleave_visits_planes_in_its_order():
    board = _run(_board(), _interleaved())
    positions = [value for _, _, _, device, value in board.log.of('dac') if device == 'RO']
    assert positions[:6] == [0.0, 0.5, 1.0, 0.25, 0.75, 1.25]


def test_a_mock_camera_counts_the_frames_the_layout_plans():
    """Readiness: a camera triggered by the board's TTL gets one frame per
    step (2 lines x 2 phases x 3 pixels)."""
    board = _run(_board(), _raster())
    rising = [e for e in board.log.entries if e[0] == 'on' and e[3] == 'Camera']
    assert len(rising) == 12


def test_prepare_publishes_the_devices_before_the_run():
    board = _board()
    built = []
    board.sigScanBuilt.connect(lambda info, plan, devices: built.append(devices))
    compiled = board.compile(_raster(), CHANNELS)
    assert not board.ready()
    board.prepare(compiled)
    assert board.ready() and built == [['Camera', 'L405', 'L488', 'X']]


def test_the_lifecycle_signals_come_in_order():
    board = _board()
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


def test_execution_after_each_release_matches():
    program = _waiting()
    board = _run(_board(), program, arrivals=[5, 7000, 20000])
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def test_releases_follow_their_triggers_within_the_declared_latency():
    program = _waiting()
    arrivals = [5, 7000, 20000]
    board = _run(_board(), program, arrivals=arrivals)
    starts = _wait_starts(program, arrivals, latency=2)
    assert trigger_problems(board.log.of('release'), arrivals, starts, latency_ticks=2) == []


class _EarlyRelease(SimulatedStepEngine):
    def _release(self, wait_start, arrivals, timeout):
        return wait_start, None


class _IgnoresTrigger(SimulatedStepEngine):
    def _release(self, wait_start, arrivals, timeout):
        later = [a for a in arrivals if a > wait_start]
        return (later[1], later[1]) if len(later) > 1 else super()._release(
            wait_start, arrivals, timeout)


@pytest.mark.parametrize('mutant, symptom', [(_EarlyRelease, 'before its trigger'),
                                              (_IgnoresTrigger, 'later than')])
def test_the_trigger_check_catches_engines_that_get_triggers_wrong(mutant, symptom):
    program = _waiting()
    arrivals = [5, 7000, 9000, 20000, 30000, 40000, 50000]
    board = _run(_board(mutant), program, arrivals=arrivals)
    starts = _wait_starts(program, arrivals, latency=2)
    problems = trigger_problems(board.log.of('release'), arrivals, starts, latency_ticks=2)
    assert any(symptom in p for p in problems), problems
    # ... while replaying its own releases would not have noticed.
    assert log_differences(engine_log(board.log), _oracle(program)) == []


def test_a_timeout_fails_or_continues_as_asked():
    failing = _run(_board(), _waiting(timeout_s=1e-3), arrivals=[])
    assert failing.log.of('failed')
    program = _waiting(timeout_s=1e-3, on_timeout='continue')
    board = _run(_board(), program, arrivals=[])
    releases = board.log.of('release')
    assert len(releases) == 3 and all(arrival is None for *_, arrival in releases)
    starts = _wait_starts_with_timeouts(program, timeout=100)
    assert trigger_problems(releases, [], starts, latency_ticks=2, timeout_ticks=100,
                            on_timeout='continue') == []


def _wait_starts_with_timeouts(program, timeout):
    schedule = realize(expand(program), RATE)
    starts, wall = [], 0
    for segment in schedule.segments[:-1]:
        wall += segment.ticks
        starts.append(wall)
        wall += timeout
    return starts


def test_preparation_after_a_release_comes_from_the_device_and_moves_no_event():
    devices = {'Camera': DeviceFacts(1e-3, 'Camera.preparation.durationS')}
    program = _waiting()
    board = _run(_board(), program, arrivals=[5, 7000, 20000], devices=devices)
    assert log_differences(engine_log(board.log), _oracle(program, devices)) == []


def test_an_unknown_preparation_refuses_a_waiting_program():
    devices = {'Camera': DeviceFacts('unknown', 'Camera: no preparation key')}
    with pytest.raises(ScanEngineRefused, match='preparation time is\\s+unknown'):
        _board().compile(_waiting(), CHANNELS, devices=devices)


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


def test_stop_during_an_unbounded_wait_is_honoured_at_once(qtbot):
    board = _board()
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

def test_a_line_another_engine_owns_is_refused_with_the_remedy():
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('Elsewhere', 'ttl', 0.0, 1e-3,
                                                  during='any'),))
    report = _board().assess(program, CHANNELS)
    verdict = report.verdict('ttl Elsewhere')
    assert verdict.verdict == IMPOSSIBLE and 'Dev1/port0/line3' in verdict.reason
    assert 'Wire it to this engine' in verdict.remedy


def test_the_board_cannot_sweep_and_says_so():
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2, mode='swept'),),
                          drives=(DeviceDrive('X', weights_um={'x': 1.0}, motion=JUMP),),
                          step_s=1e-3)
    verdict = _board().assess(program, CHANNELS).verdict('swept loop')
    assert verdict.verdict == IMPOSSIBLE and 'Make the axis stepped' in verdict.remedy


def test_an_undeclared_trigger_latency_is_unknown_not_guessed():
    board = _board(triggerLatencyS=None)
    verdict = board.assess(_waiting(), CHANNELS).verdict('trigger wait at y')
    assert verdict.verdict == UNKNOWN and 'does not declare its trigger latency' in verdict.reason
    with pytest.raises(ScanEngineRefused):
        board.compile(_waiting(), CHANNELS)


def test_times_off_the_boards_grid_are_refused():
    program = _raster()
    program = ScanProgram(**{**program.__dict__, 'step_s': 10.005e-3})
    with pytest.raises(ScanEngineRefused, match='not a whole number'):
        _board().compile(program, CHANNELS)


def test_a_board_without_a_declared_grid_does_not_start():
    with pytest.raises(ValueError, match='timingGridS is required'):
        SimulatedStepEngine(SimpleNamespace(managerProperties={}))


def test_channel_ownership_must_be_unambiguous():
    a, b = _board(), _board()
    b.name = 'Other'
    with pytest.raises(SetupError, match='Sim/TTL0 \\(L405\\) is claimed by Board and Other'):
        assign_owners([a, b], CHANNELS)
    owners = assign_owners([a], CHANNELS)
    assert owners['Sim/TTL0'] == 'Board' and owners['Dev1/port0/line3'] is None


def test_what_the_board_runs_is_native():
    report = _board().assess(_raster(), CHANNELS)
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
