"""Free trajectories and their gridded image (docs/design/plans/
scan-engines-and-expert-scan.md §1.5, §6, M4): the patterns, the path on an
engine's clock, the limits, and the gridding by what a detector measures.
"""
import json

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.scan_frame import FRAME_GEOMETRY_KEY
from imswitch.imcontrol.model.scan_program import ProgramError, ScanProgram, TimelineEvent
from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol.model.scan_program.program import Trajectory, validate
from imswitch.imcontrol.model.scan_program.scan_info import TRAJECTORY_KEY
from imswitch.imcontrol.model.scan_program.schedule import ScheduleRefused
from imswitch.imcontrol.model.scan_program.serialize import program_from_dict, program_to_dict
from imswitch.imcontrol.model.scan_program.trajectory import (
    Grid, GridAccumulator, PATTERNS, limits_refusals, path_positions, realize_trajectory,
)

RATE = 100_000
SETUP_FILE = 'imswitch/_data/user_defaults/imcontrol_setups/expert_mock_scan_setup.json'
SETUP_DICT = json.load(open(SETUP_FILE))
SETUP = SetupInfo.from_json(json.dumps(SETUP_DICT), infer_missing=True)
NIDAQ = NidaqScanEngine(None, nidaqManager=None, setupInfo=SETUP)
LASER = list(SETUP_DICT['lasers'])[1]


def _spiral(**changes):
    params = dict(x_device='X', y_device='Y', center_um=[1.0, -2.0], radius_um=10.0, turns=20,
                  speed='angular', duration_s=0.5, window_s=1e-4, approach_s=5e-3,
                  pixel_um=0.5, laser=LASER)
    params.update(changes)
    return presets.make('trajectory-spiral', params)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

def test_a_circle_stays_on_its_radius_and_closes():
    trajectory = Trajectory(('X', 'Y'), 'circle', {'center_um': [2.0, 3.0], 'radius_um': 4.0,
                                                   'turns': 3}, 0.3, 0.0, 0.5)
    t = np.linspace(0, 0.3, 1001)
    x, y = path_positions(trajectory, t)
    assert np.allclose(np.hypot(x - 2.0, y - 3.0), 4.0)
    assert np.allclose([x[0], y[0]], [x[-1], y[-1]])


def test_a_spiral_grows_from_its_centre():
    trajectory = Trajectory(('X', 'Y'), 'spiral', {'center_um': [0.0, 0.0], 'radius_um': 10.0,
                                                   'turns': 5, 'speed': 'angular'}, 1.0, 0.0, 0.5)
    x, y = path_positions(trajectory, np.array([0.0, 0.5, 1.0]))
    assert np.allclose(np.hypot(x, y), [0.0, 5.0, 10.0])


def test_points_are_visited_at_equal_times_and_a_script_draws_its_own_path():
    points = Trajectory(('X', 'Y'), 'points', {'points_um': [[0, 0], [2, 0], [2, 4]]},
                        2.0, 0.0, 0.5)
    assert path_positions(points, np.array([0.0, 0.5, 1.0, 2.0])).T.tolist() == \
        [[0, 0], [1, 0], [2, 0], [2, 4]]
    script = Trajectory(('X', 'Y'), 'script', {
        'code': 'def path(t, duration):\n    return 3 * t / duration, -t\n'}, 1.0, 0.0, 0.5)
    assert path_positions(script, np.array([0.5])).ravel().tolist() == [1.5, -0.5]


def test_a_script_without_a_path_says_so():
    trajectory = Trajectory(('X', 'Y'), 'script', {'code': 'x = 1'}, 1.0, 0.0, 0.5)
    with pytest.raises(ProgramError, match=r'defines no path\(t, duration\)'):
        path_positions(trajectory, np.array([0.0]))


@pytest.mark.parametrize('change, message', [
    (dict(pattern='hexagon'), 'Unknown pattern'),
    (dict(devices=('X',)), 'moves 2 devices, not 1'),
    (dict(pixel_um=0.0), 'pixel_um must be positive'),
    (dict(record_measurements='yes'), "record_measurements is true or false, not 'yes'"),
])
def test_a_trajectory_is_validated(change, message):
    program = _spiral()
    trajectory = Trajectory(**{**program.trajectory.__dict__, **change})
    with pytest.raises(ProgramError, match=message):
        validate(ScanProgram(loops=(), drives=(), step_s=1e-4, trajectory=trajectory))


def test_a_trajectory_switches_lines_on_the_path_only():
    program = _spiral()
    with pytest.raises(ProgramError, match="placed on the path"):
        validate(ScanProgram(loops=(), drives=(), step_s=1e-4, trajectory=program.trajectory,
                             timeline=(TimelineEvent(LASER, 'ttl', 0.0, 0.1),)))


def test_a_trajectory_program_reads_back_from_its_json():
    for program in (_spiral(), _spiral(record_measurements=True)):
        assert program_from_dict(json.loads(json.dumps(program_to_dict(program)))) == program


def test_measurements_are_recorded_only_when_asked():
    """Off by default (plan §6): a program saved before M6 reads back
    without them, the preset's box unticked makes none."""
    saved = program_to_dict(_spiral())
    del saved['trajectory']['record_measurements']
    assert program_from_dict(saved).trajectory.record_measurements is False
    assert _spiral(record_measurements=None).trajectory.record_measurements is False
    assert _spiral(record_measurements=True).trajectory.record_measurements is True


# ---------------------------------------------------------------------------
# The path on the clock
# ---------------------------------------------------------------------------

def test_windows_tile_the_path_and_the_last_may_be_shorter():
    realized = realize_trajectory(_spiral(duration_s=0.01, window_s=3e-4), RATE,
                                  captured_um={'X': 0.0, 'Y': 0.0})
    ticks = realized.window_ticks
    assert ticks.sum() == realized.path_ticks == 1000
    assert set(ticks[:-1]) == {30} and ticks[-1] == 1000 - 30 * 33
    assert np.all(np.diff(realized.window_start) == ticks[:-1])
    assert realized.window_start[0] == realized.approach_ticks == 500


def test_the_approach_joins_the_path_without_a_jump_in_speed():
    realized = realize_trajectory(_spiral(), RATE, captured_um={'X': 3.0, 'Y': 2.0})
    a, n = realized.approach_ticks, realized.path_ticks
    for device, start in (('X', 3.0), ('Y', 2.0)):
        p = realized.positions_um[device]
        assert p[0] == pytest.approx(start) and p[-1] == pytest.approx(start)
        # Velocity continuous where the approach and the way back join the
        # path. A jump in speed would make the second difference there a
        # spike of about v * dt; with continuous velocity it is of the size
        # of the accelerations on either side (each side's acceleration may
        # differ: the join is continuous in velocity, not acceleration).
        accel = np.abs(np.diff(p, 2))
        speed = np.abs(np.diff(p)).max()
        for join in (a - 1, a + n - 1):
            here = accel[join - 1:join + 2].max()
            around = np.concatenate([accel[join - 50:join - 1], accel[join + 2:join + 50]]).max()
            assert here <= 2 * around
            assert here < speed / 10          # nowhere near a jump
        assert len(p) == realized.total_ticks


def test_a_path_that_starts_elsewhere_needs_an_approach():
    with pytest.raises(ScheduleRefused, match='give the trajectory an approach time'):
        realize_trajectory(_spiral(approach_s=0.0), RATE, captured_um={'X': 5.0, 'Y': 0.0})


def test_a_window_off_the_clock_is_refused_with_its_neighbours():
    with pytest.raises(ScheduleRefused, match='measurement window: 15 µs is not on the 10 µs '
                                              'grid'):
        realize_trajectory(_spiral(window_s=1.5e-5), RATE, captured_um={'X': 0, 'Y': 0})


def test_limits_are_checked_on_what_the_stage_does_and_unknown_ones_refuse():
    realized = realize_trajectory(_spiral(speed='linear'), RATE, captured_um={'X': 0, 'Y': 0})
    refusals = limits_refusals(realized, {'X': {'vel_max': 0.1, 'acc_max': 1e-4},
                                          'Y': {'vel_max': 0.1}})
    assert any('X would reach' in r and 'above its acc_max of 0.0001' in r for r in refusals)
    assert any('Y declares no acc_max' in r for r in refusals)


# ---------------------------------------------------------------------------
# Gridding by what the detector measures
# ---------------------------------------------------------------------------

def _grid():
    return Grid(('X', 'Y'), (0.0, 0.0), 1.0, (3, 2))


def test_unequal_windows_give_the_same_count_rate():
    accumulator = GridAccumulator(_grid(), 'counts')
    # one bin, a constant 1000 counts/s, windows of 1, 2 and 7 ms
    seconds = np.array([1e-3, 2e-3, 7e-3])
    accumulator.add(np.zeros(3, dtype=int), 1000.0 * seconds, seconds)
    assert accumulator.image()[0, 0] == pytest.approx(1000.0)
    assert accumulator.coverage()[0, 0] == pytest.approx(0.01)


def test_the_mean_is_weighted_by_integration_time():
    accumulator = GridAccumulator(_grid(), 'mean')
    accumulator.add(np.array([1, 1]), np.array([2.0, 5.0]), np.array([3e-3, 1e-3]))
    assert accumulator.image()[0, 1] == pytest.approx((2 * 3 + 5 * 1) / 4)
    assert accumulator.unit == 'V'


def test_unvisited_is_nan_and_measured_dark_is_zero():
    accumulator = GridAccumulator(_grid(), 'counts')
    accumulator.add(np.array([4]), np.array([0.0]), np.array([1e-3]))
    image = accumulator.image()
    assert image[1, 1] == 0.0
    assert np.isnan(image[0, 0]) and np.isnan(image).sum() == 5
    assert accumulator.coverage()[0, 0] == 0.0


def test_the_grid_covers_the_windows_centred_on_its_bins():
    centres = np.array([[0.2, 1.0], [2.9, 1.6], [1.4, 0.4]])
    grid = Grid.covering(('X', 'Y'), centres, 0.5)
    assert grid.first_um == (0.2, 0.4)
    assert grid.shape == (6, 3)          # 2.7 µm and 1.2 µm at 0.5 µm, bins centred
    assert grid.flat_index(centres).tolist() == [0 + 1 * 6, 5 + 2 * 6, 2 + 0 * 6]


# ---------------------------------------------------------------------------
# On the NI-DAQ
# ---------------------------------------------------------------------------

def test_the_nidaq_plays_a_spiral_and_tells_the_detectors_its_windows():
    compiled = NIDAQ.compile(_spiral(), setup_channels(SETUP), captured_um={'X': 0, 'Y': 0})
    analog = compiled.signalDict['scanSignalsDict']
    conversion = SETUP.positioners['X'].managerProperties['conversionFactor']
    realized = compiled.schedule
    assert np.allclose(analog['X'] * conversion, realized.positions_um['X'])
    assert compiled.signalDict['TTLCycleSignalsDict'][LASER].sum() == realized.path_ticks
    info = compiled.scanInfoDict
    assert info['scan_samples_total'] == realized.total_ticks == len(analog['X'])
    assert info['img_dims'] == list(realized.grid.shape)
    measured = info[TRAJECTORY_KEY]
    assert len(measured['bin']) == len(realized.window_ticks) == 5000
    assert measured['provenance_counts']['unit'] == 'counts/s'
    assert measured['provenance_mean']['weighting'].startswith('Σ (value')
    geometry = info[FRAME_GEOMETRY_KEY]['axes']
    assert [axis['device'] for axis in geometry] == ['X', 'Y']
    assert geometry[0]['first_um'] == pytest.approx(realized.grid.first_um[0])
    # what a recorded measurement is at: the window's centre, on the grid
    assert measured['record_measurements'] is False
    assert measured['centres_um'].shape == (5000, 2) and measured['devices'] == ['X', 'Y']
    assert np.array_equal(realized.grid.flat_index(measured['centres_um']), measured['bin'])
    recording = NIDAQ.compile(_spiral(record_measurements=True), setup_channels(SETUP),
                              captured_um={'X': 0, 'Y': 0}).scanInfoDict[TRAJECTORY_KEY]
    assert recording['record_measurements'] is True
    assert recording['provenance_counts']['raw_measurements_stored'] is True


def test_the_nidaq_refuses_a_path_its_stage_cannot_follow():
    with pytest.raises(ScanEngineRefused, match='above its acc_max'):
        NIDAQ.compile(_spiral(speed='linear'), setup_channels(SETUP),
                      captured_um={'X': 0, 'Y': 0})


def test_a_scan_that_completes_before_its_live_images_arrive_keeps_its_own():
    """The live images arrive queued; a teardown on another thread can
    complete the scan first. Its raw frame is finished from the measurement
    itself, and a live image delivered late does not overwrite it."""
    from types import SimpleNamespace
    from imswitch.imcontrol.model.managers.detectors._trajectory_measure import (
        TrajectoryDetectorMixin,
    )
    grid = Grid(('X', 'Y'), (0.0, 0.0), 1.0, (2, 2))
    accumulator = GridAccumulator(grid, 'counts')
    accumulator.add(np.array([0, 3]), np.array([5.0, 7.0]), np.array([1.0, 2.0]))
    measurement = SimpleNamespace(accumulator=accumulator, values=np.array([5.0, 7.0]),
                                  provenance={'reduction': 'gridded from a trajectory'})

    class Manager(TrajectoryDetectorMixin):
        _scanGeneration = 1
        _image = np.zeros((2, 2), np.float32)

        def _publishTrajectoryImage(self):
            pass

    manager = Manager()
    manager._trackTrajectory(1, measurement)
    manager._finishTrajectory(1)             # no live image has arrived yet
    final = accumulator.image().astype(np.float32)
    np.testing.assert_array_equal(manager._image, final)
    assert manager._rawFrameAuxiliary(1)[0]['measurements'].tolist() == [5.0, 7.0]
    partial = np.full((2, 2), np.nan)
    manager._onTrajectoryImage(partial, partial, 'counts/s', {}, 1, measurement)
    np.testing.assert_array_equal(manager._image, final)
    assert manager._trajectoryState['coverage'].sum() == 3.0


def test_every_pattern_has_a_preset_with_its_parameters():
    for pattern in PATTERNS:
        preset = presets.PRESETS[f'trajectory-{pattern}']
        names = {field.name for field in preset.fields}
        assert {p[0] for p in PATTERNS[pattern].parameters} <= names
        assert {'duration_s', 'window_s', 'approach_s', 'pixel_um', 'laser'} <= names


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
