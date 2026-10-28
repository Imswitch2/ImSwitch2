"""One schedule decides (review round 1, batch 2; docs/design/plans/
scan-engines-and-expert-scan.md §7.9).

Every engine refuses what the interpreter's schedule refuses, on its own
grid; positions are held through waits and reached before the first step
(the approach, which the Advanced adapter leaves out as Beta does); a move
at the end of a pass that outlasts the return is still a move in the next
step; times are summed and parsed as the decimals they were written as.
"""

import dataclasses
import json
import random
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.scan_engines.CompositeScanEngine import (
    CompositeScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.managers.scan_engines.SimulatedStepEngine import (
    SimulatedStepEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SimulatedTransport, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import BoardCaps
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, DeviceFacts, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait,
    expand, presets, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.engine import (
    DeviceChannels, IMPOSSIBLE, ScanEngineRefused, UNKNOWN, setup_channels,
)
from imswitch.imcontrol.model.scan_program.program import ProgramError, validate
from imswitch.imcontrol.model.scan_program.scan_info import RASTER_KEY, scan_info_for
from imswitch.imcontrol.model.scan_program.schedule import (
    ScheduleRefused, approach_span, grid_ticks,
)
from imswitch.imcontrol.model.scan_program.serialize import program_from_dict, program_to_dict
from imswitch.imcontrol.view.widgets.ScanWidgetExpert import MS, stored

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
EXPERT_MOCK = SETUPS + 'expert_mock_scan_setup.json'
RATE = 100_000
STAGE = Motion(transition='jump', move_s=1e-3, settle_s=1e-3, return_s=2e-3,
               origin='absolute', restore='first')


def _ms_resolft(**changes):
    params = dict(device='Z', cycles=2, planes=3, cycle_step_um=0.25, start_um=1.0,
                  pulses=[['405 (ON)', 1e-3, 1e-3], ['488 (EXC)', 2e-3, 1e-3]],
                  camera='Camera', exposure_pulse=2, timepoints=2, pause_s=0.0,
                  move_s=5e-4, settle_s=5e-4, transition='jump')
    params.update(changes)
    return presets.make('ms-resolft', params)


def _expert_setup():
    return SetupInfo.from_json(open(EXPERT_MOCK).read(), infer_missing=True)


# --- a time wait holds every position --------------------------------------------

def test_a_time_wait_holds_every_drive_where_it_is_on_the_nidaq():
    """It drove every positioner to 0 V for the pause and back."""
    setup = _expert_setup()
    program = _ms_resolft(pause_s=10e-3, start_um=5.0)
    compiled = NidaqScanEngine(None, nidaqManager=None, setupInfo=setup).compile(
        program, setup_channels(setup), captured_um={'Z': 5.0})
    schedule = compiled.schedule
    z = densify(schedule)['analog']['Z']
    waits = [s for s in schedule.spans if s.span.kind == 'wait']
    assert waits
    for wait in waits:
        before = z[wait.start_tick - 1] if wait.start_tick else z[0]
        assert set(z[wait.start_tick:wait.start_tick + wait.ticks]) == {before}
    assert z.min() >= 5.0            # never towards 0 µm


# --- the approach ----------------------------------------------------------------------

def _raster(**changes):
    return ScanProgram(**{**dict(
        loops=(Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 3)),
        drives=(DeviceDrive('X', weights_um={'x': 1.0}, motion=STAGE),
                DeviceDrive('Y', offset_um=2.0, weights_um={'y': 1.0}, motion=STAGE)),
        step_s=10e-3, timeline=(TimelineEvent('Cam', 'detector', 3e-3, 7e-3),),
        timing_policy='strict-v1'), **changes})


def test_the_run_starts_with_the_drives_reaching_their_first_positions():
    schedule = realize(expand(_raster(), RunContext(captured_um={'X': 7.0, 'Y': 2.0})), RATE)
    approach = approach_span(schedule)
    assert approach.ticks == 200                       # X's 1 ms move and 1 ms settle
    assert approach.span.pieces['X'].start_um == 7.0   # from where it was captured
    assert approach.span.pieces['Y'].where == 'hold'   # already there
    assert schedule.step_spans()[0].start_tick == 200
    x = densify(schedule)['analog']['X']
    assert x[0] == 0.0 and x[199] == 0.0               # a jump: there at once, then settles


def test_no_approach_when_every_drive_is_already_there():
    schedule = realize(expand(_raster(), RunContext(captured_um={'X': 0.0, 'Y': 2.0})), RATE)
    assert approach_span(schedule) is None


def test_a_drive_whose_position_is_unknown_is_given_its_move_and_settle():
    schedule = realize(expand(_raster(), RunContext()), RATE)
    assert approach_span(schedule).ticks == 200


def test_the_advanced_adapter_has_no_approach_and_files_keep_the_choice():
    program = _raster(approach=False)
    assert approach_span(realize(expand(program, RunContext()), RATE)) is None
    assert program_from_dict(program_to_dict(program)).approach is False
    with pytest.raises(ProgramError, match="approach is true or false, not 'yes'"):
        validate(_raster(approach='yes'))


def test_an_advanced_scan_still_has_no_approach():
    from imswitch.imcontrol.model.scan_program.advanced import program_from_advanced
    from . import test_beta_signal_goldens as goldens
    program, _context = program_from_advanced(goldens.CASES['xy_centred'], None,
                                              goldens._setup())
    assert program.approach is False


def test_point_detectors_throw_the_approach_away():
    setup = _expert_setup()
    program = presets.make('raster', dict(
        x_device='X', y_device='Y', x_pixels=4, y_pixels=3, x_step_um=0.5, y_step_um=0.5,
        x_start_um=0.0, y_start_um=0.0, pulses=[['405 (ON)', 1e-3, 0.0]], camera='',
        exposure_pulse=1, move_s=5e-4, settle_s=5e-4, return_s=1e-3, traversal='forward'))
    compiled = NidaqScanEngine(None, nidaqManager=None, setupInfo=setup).compile(
        program, setup_channels(setup), captured_um={'X': 3.0, 'Y': 3.0})
    info = compiled.scanInfoDict
    assert info[RASTER_KEY]
    assert info['scan_throw_startzero'] == approach_span(compiled.schedule).ticks == 100


def test_a_step_board_waits_out_the_approach_as_the_interpreter_means_it():
    caps = BoardCaps('B', 'f', grid_ns=10_000, dacs={0: (-10.0, 10.0), 1: (-10.0, 10.0)},
                     ttl_lines=3)
    from .test_scan_engine_conformance import CHANNELS, SETUP
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP,
                                    transport=SimulatedTransport(SimulatedStepBoard(caps)))
    program = ScanProgram(
        loops=(Loop('x', 'scan_x', 3),),
        drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=STAGE),),
        step_s=10e-3, timeline=(TimelineEvent('L405', 'ttl', 0.0, 4e-3, during='any'),))
    compiled = engine.compile(program, CHANNELS, captured_um={'X': 9.0})
    assert compiled.lines.index('WAIT,start,time,200,-,fail') >= 0
    engine.prepare(compiled)
    engine.run(compiled, blocking=True)
    expected = expected_log(realize(expand(program, RunContext(captured_um={'X': 9.0})), RATE))
    assert log_differences(engine_log(engine.log), expected) == []
    assert engine.log.of('on')[0][2] == 200            # the first pulse after the approach


# --- a move at the end of a pass that outlasts the return --------------------------

def test_an_outer_move_longer_than_the_return_is_a_move_in_the_next_step():
    program = _raster(drives=(
        DeviceDrive('X', weights_um={'x': 1.0}, motion=dataclasses.replace(STAGE, return_s=0.0)),
        DeviceDrive('Y', offset_um=2.0, weights_um={'y': 1.0},
                    motion=dataclasses.replace(STAGE, move_s=2e-3, settle_s=2e-3))),
        timeline=(TimelineEvent('Cam', 'detector', 1e-3, 5e-3),))
    with pytest.raises(ScheduleRefused, match=r'step 3, Cam detector \(stationary\) overlaps Y '
                                              r'moving from 0 s to 4 ms'):
        realize(expand(program, RunContext(captured_um={'X': 0.0, 'Y': 2.0})), RATE)
    # time enough in the return: accepted
    roomy = dataclasses.replace(program, drives=(
        dataclasses.replace(program.drives[0],
                            motion=dataclasses.replace(STAGE, return_s=4e-3)),
        program.drives[1]))
    realize(expand(roomy, RunContext(captured_um={'X': 0.0, 'Y': 2.0})), RATE)
    # and an event that may run while moving is never refused for it
    anytime = dataclasses.replace(program, timeline=(
        TimelineEvent('Cam', 'detector', 1e-3, 5e-3, during='any'),))
    realize(expand(anytime, RunContext(captured_um={'X': 0.0, 'Y': 2.0})), RATE)


# --- step engines refuse what the schedule refuses ---------------------------------

def _simulated_board():
    return SimulatedStepEngine(SimpleNamespace(managerProperties={
        'timingGridS': 1e-6, 'analogChannels': {'S/dac0': [-10, 10]},
        'digitalLines': ['S/ttl0']}), name='S')


CHANNELS = {'Z': DeviceChannels('Z', analog='S/dac0', kind='positioner'),
            'L': DeviceChannels('L', digital='S/ttl0', kind='laser')}


@pytest.mark.parametrize('motion, timeline, reason', [
    (Motion(transition='jump', move_s=0.5e-3, settle_s=0.5e-3, origin='absolute',
            restore='first'),
     (TimelineEvent('L', 'ttl', 0.0, 0.2e-3, during='any'),), 'leaves no stationary time'),
    (Motion(transition='jump', move_s=0.2e-3, settle_s=0.2e-3, origin='absolute',
            restore='first'),
     (TimelineEvent('L', 'ttl', 0.0, 0.9e-3),), r'L ttl \(stationary\) overlaps Z moving'),
])
def test_a_step_engine_refuses_what_the_schedule_refuses(motion, timeline, reason):
    program = ScanProgram(loops=(Loop('z', 'plane', 3),),
                          drives=(DeviceDrive('Z', weights_um={'z': 1.0}, motion=motion),),
                          step_s=1e-3, timeline=timeline)
    with pytest.raises(ScanEngineRefused, match=reason):
        _simulated_board().compile(program, CHANNELS, captured_um={'Z': 0.0})


def test_a_restore_it_cannot_know_is_refused_at_compile_not_in_the_board():
    program = ScanProgram(
        loops=(Loop('z', 'plane', 2),),
        drives=(DeviceDrive('Z', weights_um={'z': 1.0}, motion=Motion(
            transition='jump', origin='absolute', restore='park')),),
        step_s=1e-3, timeline=(TimelineEvent('L', 'ttl', 0.0, 0.2e-3, during='any'),))
    with pytest.raises(ScanEngineRefused, match='parks it nowhere'):
        _simulated_board().compile(program, CHANNELS, captured_um={'Z': 0.0})


def test_the_expert_report_says_why_instead_of_raising(qtbot):
    from .test_scan_controller_expert import ExpertRig
    raw = json.load(open(SETUPS + 'expert_step_boards_mock_setup.json'))
    rig = ExpertRig(setup=raw)
    try:
        program = _ms_resolft(pulses=[['405 (ON)', 1e-3, 1e-3]], exposure_pulse=1)
        # the camera stationary through Z's move at the end of the step
        timeline = tuple(dataclasses.replace(e, end_s=program.step_s) if e.kind == 'detector'
                         else e for e in program.timeline)
        rig.scan.setProgram(dataclasses.replace(program, timeline=timeline))
        report = rig.widget.reportText()
        assert report.startswith('Not runnable on triggerscope.')
        assert 'overlaps Z moving' in report
    finally:
        rig.close()


def test_a_follower_event_on_its_own_finer_grid_is_its_own_engines_to_check():
    from .test_scan_engines_multi import CHANNELS as MULTI, _composite, _resolft, \
        _simulated_follower
    from imswitch.imcontrol.model.scan_program.program import DetectorBinding
    composite = _composite(_simulated_follower())
    # the follower's camera bound to its readout: the binding goes with its
    # events when the leader realizes its share
    program = dataclasses.replace(_resolft(), detectors=(DetectorBinding('Camera', 'ro'),))
    timeline = tuple(dataclasses.replace(e, start_s=3.005e-3) if e.id == 'off' else e
                     for e in program.timeline)
    compiled = composite.compile(dataclasses.replace(program, timeline=timeline), MULTI)
    assert compiled.report.runnable
    assert compiled.schedule is compiled.leader.schedule     # the panel shows the leader's


def test_a_time_wait_needs_no_preparation_on_a_step_engine():
    program = ScanProgram(
        loops=(Loop('t', 'time', 2), Loop('z', 'plane', 2)),
        drives=(DeviceDrive('Z', weights_um={'z': 1.0}, motion=Motion(
            transition='jump', origin='absolute', restore='hold')),),
        step_s=1e-3, timeline=(TimelineEvent('L', 'ttl', 0.0, 0.2e-3, during='any'),),
        waits=(Wait('t', 'time', interval_s=1e-3),))
    devices = {'L': DeviceFacts('unknown', 'L: no preparation key')}
    _simulated_board().compile(program, CHANNELS, captured_um={'Z': 0.0}, devices=devices)


def test_an_oversized_program_is_refused_before_it_is_expanded():
    board = SimulatedStepEngine(SimpleNamespace(managerProperties={
        'timingGridS': 1e-6, 'digitalLines': ['S/ttl0'], 'maxSteps': 1000}), name='S')
    program = ScanProgram(loops=(Loop('t', 'time', 10 ** 12),), drives=(), step_s=1e-3,
                          timeline=(TimelineEvent('L', 'ttl', 0.0, 0.2e-3, during='any'),))
    with pytest.raises(ScanEngineRefused, match='1000000000000 steps; this board holds 1000'):
        board.compile(program, CHANNELS)


# --- the leader and its followers ---------------------------------------------------

def test_a_leader_carries_its_followers_preparation_on_a_channel_of_its_own():
    from .test_scan_engines_multi import CHANNELS as MULTI, SETUP, _resolft, \
        _simulated_follower
    caps = BoardCaps('Lead', 'f', grid_ns=10_000, dacs={0: (-10.0, 10.0)}, ttl_lines=4,
                     trigger_in=True, trigger_latency_ticks=2, clock_ppm=20.0)
    leader = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Lead',
                                    setupInfo=SETUP, transport=SimulatedTransport(
                                        SimulatedStepBoard(caps)))
    composite = CompositeScanEngine(leader, {'Follow': (_simulated_follower(), 'Lead/ttl3')})
    program = _resolft(waits=(Wait('start', 'external'),))
    devices = {'L488': DeviceFacts(5e-3, 'L488 (follower)'),
               'L405': DeviceFacts(1e-3, 'L405 (leader)')}
    compiled = composite.compile(program, MULTI, captured_um={}, devices=devices)
    prepare = [line for line in compiled.leader.lines if line.startswith('PREPARE')]
    timing = sum(1 for d in compiled.leader.plan.drives if not d.get('output', True))
    assert sorted(prepare) == sorted(['PREPARE,ttl0,100', f'PREPARE,time{timing},500'])


class _Real:
    """A link with a reply time and no in-process board, as a serial one;
    ``inner``: the board behind it, or none (unplugged)."""
    reply_timeout = 0.01
    run_timeout = 0.01
    board = None

    def __init__(self, inner=None):
        self.inner = inner

    def write(self, line):
        if self.inner is not None:
            self.inner.write(line)

    def read(self, timeout):
        return None if self.inner is None else self.inner.read(timeout)

    def close(self):
        pass


def test_a_leader_that_has_not_answered_is_unknown_not_a_crash():
    from .test_scan_engines_multi import CHANNELS as MULTI, SETUP, _resolft
    leader = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Lead',
                                    setupInfo=SETUP, transport=_Real())
    caps = BoardCaps('F', 'f', grid_ns=5000, dacs={0: (-10.0, 10.0)}, ttl_lines=2,
                     trigger_in=True, trigger_latency_ticks=3, trigger_min_ticks=4,
                     clock_ppm=20.0)
    follower = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Follow',
                                      setupInfo=SETUP, transport=_Real(SimulatedStepBoard(caps)))
    assert leader.unanswered and follower.unanswered is None
    report = CompositeScanEngine(leader, {'Follow': (follower, 'Lead/ttl3')}).assess(
        _resolft(), MULTI)               # was TypeError: float / None
    verdict = report.verdict('lead')
    assert verdict.verdict == UNKNOWN and 'declares no timing grid' in verdict.reason


def test_a_zero_weight_on_the_inner_loop_is_no_return_on_the_board():
    from .test_scan_engine_conformance import CHANNELS as CONF, _wire
    program = ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 2)),
        # restore 'hold': a board plays one return time, and this program
        # gives its returns none (the subject here is the inner loop's)
        drives=(DeviceDrive('X', weights_um={'x': 0.0, 'y': 1.0},
                            motion=dataclasses.replace(STAGE, restore='hold')),),
        step_s=10e-3, timeline=(TimelineEvent('L405', 'ttl', 0.0, 4e-3, during='any'),))
    engine = _wire()
    compiled = engine.compile(program, CONF, captured_um={'X': 0.0})
    assert compiled.plan.return_ticks == 0
    engine.prepare(compiled)
    engine.run(compiled, blocking=True)
    expected = expected_log(realize(expand(program, RunContext(captured_um={'X': 0.0})), RATE))
    assert log_differences(engine_log(engine.log), expected) == []


# --- times as they were written -----------------------------------------------------

def test_on_grid_pulse_sequences_are_never_refused_for_their_sums():
    """13 % of them were: a float sum lands an ULP off the grid."""
    rng = random.Random(20261010)
    refused = []
    for _ in range(400):
        pulses = [[f'L{i}', stored(f'{rng.randint(1, 300) / 100:g}', MS),
                   stored(f'{rng.randint(0, 300) / 100:g}', MS)] for i in range(3)]
        program = presets.make('pulse-sequence', dict(
            pulses=pulses, camera='', exposure_pulse=1, frames=2, pause_s=0.0))
        try:
            realize(expand(program), RATE)
        except ScheduleRefused as error:
            refused.append((pulses, error.reasons[0]))
    assert refused == []


def test_a_typed_value_is_stored_as_the_decimal_it_reads_as():
    assert stored('2.3', MS) == 0.0023 == float('0.0023')
    assert grid_ticks(stored('2.3', MS), RATE) == (230, True)
    with pytest.raises(ValueError, match="'abc' is not a number"):
        stored('abc', MS)


def test_presets_sum_their_times_exactly():
    assert presets.exact_sum(1e-3, 1.2e-3, 0.1e-3) == 0.0023
    program = _ms_resolft(pulses=[['405 (ON)', 1e-3, 1.2e-3], ['488 (EXC)', 0.1e-3, 0.0]],
                          exposure_pulse=1)
    assert program.step_s == 0.0033                    # 2.3 ms of pulses, 1 ms of motion


# --- the Advanced panel's strict timing checks its TTL windows ----------------------

def test_an_advanced_scan_under_strict_timing_refuses_an_off_grid_ttl_window():
    from . import test_beta_signal_goldens as goldens
    from imswitch.imcontrol.model.errors import ScanDesignRefusedError
    from imswitch.imcontrol.model.scan_program.advanced import program_scan_signals
    setup = goldens._setup()
    setup.scan.scanDesigner = 'BetaScanDesigner'
    stage = dict(goldens.CASES['xy_centred'], sequence_time=10e-3,
                 axis_order=['serpentine', 'forward', 'forward'])
    digital = dict(goldens.TTL_CASES['ttl_off_grid_windows'][1], sequence_time=10e-3)
    # the TTL designer would round these windows; strict timing refuses them,
    # as the panel's timing note says
    with pytest.raises(ScanDesignRefusedError, match='123.456 µs is not on the 10 µs grid'):
        program_scan_signals(stage, setup, {}, {'scan_samples_total': 0}, digital=digital)


# --- a trajectory without an approach ------------------------------------------------

def test_a_trajectory_without_an_approach_ends_where_it_started():
    from imswitch.imcontrol.model.scan_program.trajectory import realize_trajectory
    from .test_scan_program_trajectory import LASER
    program = presets.make('trajectory-circle', dict(
        x_device='X', y_device='Y', center_um=[0.0, 0.0], radius_um=2.0, turns=1,
        duration_s=0.01, window_s=1e-4, approach_s=0.0, pixel_um=0.5, laser=LASER))
    realized = realize_trajectory(program, RATE, captured_um={'X': 2.0, 'Y': 0.0})
    assert realized.positions_um['X'][-1] == 2.0 and realized.positions_um['Y'][-1] == 0.0


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
