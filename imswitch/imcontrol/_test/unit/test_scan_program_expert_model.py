"""The model under the Expert panel (docs/design/plans/
scan-engines-and-expert-scan.md §2.5, §2.6, §4, M3): programs as JSON,
presets, the NI-DAQ's compile, what the detectors are told, and the
recording layouts read from a program.
"""
import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_ASSEMBLED_IMAGE,
    recorded_frame_coordinates,
    recorded_frame_count,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_program_layouts,
)
from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.scan_program import (
    DetectorBinding, Loop, ProgramError, ScanProgram, TimelineEvent, Wait, visits,
)
from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.advanced import program_from_advanced
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol.model.scan_program.presets import PresetError
from imswitch.imcontrol.model.scan_program.scan_info import RASTER_KEY, RASTER_REASON_KEY
from imswitch.imcontrol.model.scan_program.serialize import (
    program_from_dict, program_to_dict,
)
from imswitch.improcess.reconstructors.snouty.metadata import recorded_snouty_geometry
from imswitch.improcess.reconstructors.snouty.restack import restack_interleaved

from . import test_beta_signal_goldens as goldens

CONVERSION = 2.0      # µm per volt on every positioner of this rig
RATE = 100_000


def _positioner(channel):
    return {'managerName': 'NidaqPositionerManager',
            'managerProperties': {'conversionFactor': CONVERSION, 'minVolt': -10, 'maxVolt': 10},
            'analogChannel': f'Dev1/ao{channel}', 'forScanning': True, 'forPositioning': True,
            'axes': ['X']}


def _laser(line):
    return {'managerName': 'NidaqLaserManager', 'managerProperties': {},
            'digitalLine': line, 'analogChannel': None, 'wavelength': 488,
            'valueRangeMin': 0, 'valueRangeMax': 1}


SETUP = SetupInfo.from_json(json.dumps({
    'positioners': {'RO': _positioner(0), 'X': _positioner(1), 'Y': _positioner(2)},
    'lasers': {'L405': _laser('Dev1/port0/line0'), 'L488': _laser('Dev1/port0/line1'),
               'Teensy405': _laser('3')},
    'detectors': {
        'Camera': {'managerName': 'HamamatsuManager',
                   'managerProperties': {'cameraListIndex': 'mock', 'hamamatsu': {}},
                   'digitalLine': 'Dev1/port0/line2', 'analogChannel': None,
                   'forAcquisition': True},
        'APD': {'managerName': 'APDManager',
                'managerProperties': {'ctrInputLine': 'Dev1/ctr0', 'terminal': 'PFI0'},
                'digitalLine': None, 'analogChannel': None, 'forAcquisition': True}},
    'scan': {'scanWidgetType': 'Expert', 'sampleRate': RATE, 'scanDesigner': None,
             'scanDesignerParams': {}, 'TTLCycleDesigner': None, 'TTLCycleDesignerParams': {}},
    'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
}), infer_missing=True)
CHANNELS = setup_channels(SETUP)
NIDAQ = NidaqScanEngine(None, nidaqManager=None, setupInfo=SETUP)

PULSES = [['L405', 1e-3, 1e-3], ['L488', 2e-3, 1e-3], ['L488', 1e-3, 0.0]]
MS_RESOLFT = dict(device='RO', cycles=2, planes=3, cycle_step_um=0.25, start_um=0.5,
                  pulses=PULSES, camera='Camera', exposure_pulse=3, timepoints=2, pause_s=0.0,
                  move_s=5e-4, settle_s=5e-4, transition='jump')
RASTER = dict(x_device='X', y_device='Y', x_pixels=4, y_pixels=3, x_step_um=0.5,
              y_step_um=0.25, x_start_um=-1.0, y_start_um=0.0, pulses=[['L488', 2e-3, 0.0]],
              camera='', exposure_pulse=1, move_s=5e-4, settle_s=5e-4, return_s=1e-3,
              traversal='forward')


# ---------------------------------------------------------------------------
# Programs as JSON
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('preset, params', [
    ('ms-resolft', MS_RESOLFT), ('raster', RASTER),
    ('pulse-sequence', dict(pulses=PULSES, camera='Camera', exposure_pulse=3, frames=5,
                            pause_s=0.01)),
])
def test_a_program_reads_back_from_its_json_exactly(preset, params):
    program = presets.make(preset, params)
    text = json.dumps(program_to_dict(program))
    assert program_from_dict(json.loads(text)) == program


def test_an_advanced_scan_keeps_its_request_through_json():
    stage = goldens.CASES[sorted(goldens.CASES)[0]]
    program, _context = program_from_advanced(stage, None, goldens._setup())
    again = program_from_dict(json.loads(json.dumps(program_to_dict(program))))
    assert again == program and again.requested_as['format'] == 'advanced-scan/1'


@pytest.mark.parametrize('change, message', [
    (lambda d: d.pop('timing_policy'), 'names no timing policy'),
    (lambda d: d.update(timing_policy='strict-v9'), 'strict-v9'),
    (lambda d: d.update(format='scan-program/0'), 'not a scan program'),
    (lambda d: d.update(colour='blue'), 'unknown keys colour'),
    (lambda d: d['loops'][0].update(speed=3), r"program.loops\[0\]: unknown keys speed"),
    (lambda d: d['loops'][0].update(count=0), 'count must be at least 1'),
])
def test_a_program_file_is_read_strictly(change, message):
    data = program_to_dict(presets.make('ms-resolft', MS_RESOLFT))
    change(data)
    with pytest.raises(ProgramError, match=message):
        program_from_dict(data)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def test_ms_resolft_visits_cycle_by_cycle():
    program = presets.make('ms-resolft', MS_RESOLFT)
    drive = program.drives[0]
    positions = [drive.offset_um + v['cycle'] * 0.25 + v['plane'] * 0.5
                 for v in visits(program)][:6]
    assert positions == [0.5, 1.0, 1.5, 0.75, 1.25, 1.75]
    # the pulses, then the move and settle to the next position
    assert program.step_s == pytest.approx(6e-3 + 1e-3)
    assert presets.preset_of(program) == ('ms-resolft', MS_RESOLFT)


def test_a_preset_says_what_is_missing():
    params = dict(MS_RESOLFT, cycles=None, move_s='')
    with pytest.raises(PresetError, match='fill in Cycles, Move time'):
        presets.make('ms-resolft', params)


def test_the_camera_exposes_during_a_pulse_the_sequence_has():
    with pytest.raises(PresetError, match='during pulse 4; the sequence has 3'):
        presets.make('ms-resolft', dict(MS_RESOLFT, exposure_pulse=4))


def test_a_pause_waits_before_every_timepoint():
    program = presets.make('ms-resolft', dict(MS_RESOLFT, pause_s=0.02))
    assert program.waits == (Wait(at='time', trigger='time', interval_s=0.02),)


# ---------------------------------------------------------------------------
# The NI-DAQ plays it, and tells the detectors what it is
# ---------------------------------------------------------------------------

def test_the_nidaq_plays_positions_in_volts_and_lines_as_logic():
    compiled = NIDAQ.compile(presets.make('ms-resolft', MS_RESOLFT), CHANNELS)
    analog = compiled.signalDict['scanSignalsDict']
    digital = compiled.signalDict['TTLCycleSignalsDict']
    assert sorted(analog) == ['RO'] and sorted(digital) == ['Camera', 'L405', 'L488']
    assert analog['RO'].max() == pytest.approx(1.75 / CONVERSION)
    assert compiled.scanInfoDict['scan_samples_total'] == analog['RO'].size
    # one camera frame per position: 2 timepoints x 2 cycles x 3 planes
    camera = digital['Camera']
    assert int(np.count_nonzero(camera[1:] & ~camera[:-1]) + camera[0]) == 12


def test_a_regular_raster_is_described_like_a_beta_scan():
    compiled = NIDAQ.compile(presets.make('raster', RASTER), CHANNELS)
    info = compiled.scanInfoDict
    assert info[RASTER_KEY] and info[RASTER_REASON_KEY] == ''
    step = int(round(3e-3 * RATE))
    assert info['img_dims'] == [4, 3]
    assert info['samples_per_pixel'] == step
    assert info['scan_samples_d2_period'] == 4 * step + int(round(1e-3 * RATE))
    assert info['scan_samples'] == [step, 4 * step, 3 * info['scan_samples_d2_period']]
    assert info['pixel_sizes'] == [0.5, 0.25]
    assert 'axis_visit_order' not in info


def test_a_serpentine_raster_tells_point_detectors_its_order():
    compiled = NIDAQ.compile(presets.make('raster', dict(RASTER, traversal='serpentine')),
                             CHANNELS)
    assert compiled.scanInfoDict['axis_visit_order'] == [{'serpentine': 'line'}, None]


def test_a_pause_is_not_a_raster_and_says_so():
    compiled = NIDAQ.compile(presets.make('ms-resolft', dict(MS_RESOLFT, pause_s=0.01)),
                             CHANNELS)
    assert compiled.scanInfoDict[RASTER_KEY] is False
    assert 'wait intervals between its steps' in compiled.scanInfoDict[RASTER_REASON_KEY]


def test_the_nidaq_refuses_what_it_cannot_play_with_every_reason():
    program = presets.make('pulse-sequence', dict(
        pulses=[['Teensy405', 1e-3, 1e-3]], camera='Camera', exposure_pulse=1, frames=2,
        pause_s=0.0))
    program = ScanProgram(loops=program.loops, drives=(), step_s=program.step_s,
                          timeline=program.timeline, detectors=program.detectors,
                          waits=(Wait(at='frame', trigger='external'),),
                          timing_policy=program.timing_policy)
    with pytest.raises(ScanEngineRefused) as caught:
        NIDAQ.compile(program, CHANNELS)
    reasons = ' '.join(caught.value.report.reasons())
    assert 'Teensy405 is wired to 3, which nidaq does not drive' in reasons
    assert 'cannot wait for an external trigger' in reasons


def test_a_position_outside_the_scanners_range_is_refused():
    with pytest.raises(ScanEngineRefused, match='RO'):
        NIDAQ.compile(presets.make('ms-resolft', dict(MS_RESOLFT, start_um=30.0)), CHANNELS)


# ---------------------------------------------------------------------------
# Recording layouts from the program
# ---------------------------------------------------------------------------

def _camera_layout(program):
    compiled = NIDAQ.compile(program, CHANNELS)
    return compiled, build_program_layouts(program, compiled.schedule, ['Camera'],
                                           scan_source='ScanControllerExpert')['Camera']


def test_an_ms_resolft_recording_is_read_by_snouty_at_its_positions():
    program = presets.make('ms-resolft', MS_RESOLFT)
    compiled, layout = _camera_layout(program)
    assert [(loop.kind, loop.count, loop.step, loop.device) for loop in layout.event_loops] \
        == [('time', 2, None, None), ('cycle', 2, 0.25, 'RO'), ('plane', 3, 0.5, 'RO')]
    resolved = SimpleNamespace(layout=layout, confidence='high', is_authoritative=True)
    assert recorded_snouty_geometry(SimpleNamespace(acquisition_layout=resolved)) == (2, 3, 2)

    # Frame f is the camera's f-th trigger: the readout held the position
    # the layout gives it.
    ro = compiled.signalDict['scanSignalsDict']['RO'] * CONVERSION
    camera = compiled.signalDict['TTLCycleSignalsDict']['Camera']
    edges = np.flatnonzero(camera & ~np.concatenate(([False], camera[:-1])))
    assert len(edges) == recorded_frame_count(layout) == 12
    true_index = []
    for frame, edge in enumerate(edges):
        c = recorded_frame_coordinates(layout, frame)
        assert ro[edge] == pytest.approx(0.5 + 0.25 * c['cycle'] + 0.5 * c['plane'])
        true_index.append(c['cycle'] + 2 * c['plane'])
    stack = np.asarray(true_index[:6], dtype=float)[:, None, None]
    assert restack_interleaved(stack, 2, 3)[:, 0, 0].tolist() == [0, 1, 2, 3, 4, 5]


def test_a_camera_that_takes_frames_in_some_phases_has_spans():
    program = presets.make('raster', dict(RASTER, camera='Camera'))
    program = ScanProgram(
        loops=(program.loops[0], Loop('phase', 'condition', 2), program.loops[1]),
        drives=program.drives, step_s=program.step_s,
        timeline=tuple(e if e.device != 'Camera' else
                       TimelineEvent('Camera', 'detector', e.start_s, e.end_s,
                                     phases=frozenset({1}), id='exposure')
                       for e in program.timeline),
        # bound as its line fires: phase 1 only (§2.5)
        detectors=(DetectorBinding('Camera', events='exposure', phases=frozenset({1})),),
        timing_policy=program.timing_policy)
    _compiled, layout = _camera_layout(program)
    assert [loop.kind for loop in layout.event_loops] == ['scan_y', 'condition', 'scan_x']
    assert recorded_frame_count(layout) == 3 * 4
    assert {recorded_frame_coordinates(layout, f)['phase'] for f in range(12)} == {1}


def test_a_point_detector_gets_an_assembled_image_of_a_raster_only():
    raster = presets.make('raster', RASTER)
    compiled = NIDAQ.compile(raster, CHANNELS)
    layout = build_program_layouts(raster, compiled.schedule, ['APD'], scan_source='x',
                                   scan_driven_detectors=['APD'])['APD']
    assert layout.payload_kind == PAYLOAD_ASSEMBLED_IMAGE
    assert layout.storage_axes == ('frame', 'scan_y', 'scan_x')

    paused = presets.make('ms-resolft', dict(MS_RESOLFT, pause_s=0.01))
    compiled = NIDAQ.compile(paused, CHANNELS)
    with pytest.raises(ValueError, match='APD cannot assemble this scan: The scan has wait'):
        build_program_layouts(paused, compiled.schedule, ['APD'], scan_source='x',
                              scan_driven_detectors=['APD'],
                              raster_refusal=compiled.scanInfoDict[RASTER_REASON_KEY])


def test_a_table_order_is_refused_for_a_frame_stream_with_the_remedy():
    stage = copy.deepcopy(goldens.CASES[sorted(goldens.CASES)[0]])
    stage['axis_order'] = ['forward', 'forward', 'forward']
    program, context = program_from_advanced(stage, None, goldens._setup())
    drive = program.drives[0]
    from dataclasses import replace
    loop = drive.weights_um and next(iter(drive.weights_um))
    count = program.loop(loop).count
    tabled = replace(program, drives=(replace(drive, weights_um={}, table_loop=loop,
                                              table_um=tuple(float(i) for i in range(count))),)
                     + program.drives[1:], timeline=(
                         TimelineEvent('Camera', 'detector', 0.0, 1e-4, during='any'),))
    from imswitch.imcontrol.model.scan_program import expand, realize
    schedule = realize(expand(tabled, context), goldens._setup().scan.sampleRate)
    with pytest.raises(ValueError, match='write the order as loops'):
        build_program_layouts(tabled, schedule, ['Camera'], scan_source='x')


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
