"""Advanced scans that visit an axis out of order, and intra-pixel moves
(docs/design/plans/scan-engines-and-expert-scan.md, M2).

The waveforms come from the scan-program interpreter; point detectors place
each visit at its image index (``_visit_order``); frame streams say the
order in their acquisition layout (cycle x plane for an interleave, a
serpentine traversal). The tests check positions, not pixel values alone:
a scrambled image adds up as well as a right one.
"""
import copy
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    decode_acquisition_layout,
    encode_acquisition_layout,
    recorded_frame_coordinates,
    recorded_frame_count,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
)
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.managers.detectors._visit_order import (
    VisitOrder, require_forward,
)
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ProgramError, ScanProgram, validate, visits,
)
from imswitch.imcontrol.model.scan_program.advanced import (
    AdvancedConversionError, axis_visit_orders, interleave_permutation, parse_axis_order,
    program_scan_signals, uses_program_features,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    DEFAULT_MOVE_TIME_S, DEFAULT_SETTLE_TIME_S, BetaScanDesigner,
)
from imswitch.improcess.reconstructors.snouty.metadata import recorded_snouty_geometry
from imswitch.improcess.reconstructors.snouty.restack import restack_interleaved

from . import test_beta_signal_goldens as goldens

SETUP = goldens._setup()
SETUP.scan.scanDesigner = 'BetaScanDesigner'
RATE = SETUP.scan.sampleRate
CONVERSION = {name: info.managerProperties['conversionFactor']
              for name, info in SETUP.positioners.items()}
TARGETS = ['StageX', 'StageY', 'StageZ']


#: Long enough for a stationary part: Beta moves and settles at the end of
#: every dwell (DEFAULT_MOVE_TIME_S + DEFAULT_SETTLE_TIME_S).
DWELL_S = 10e-3


def _stage(lengths, steps, *, orders=None, dwell=DWELL_S, linesteps=1):
    """A Beta stage request on the goldens' rig, in µm."""
    stage = {
        'target_device': list(TARGETS),
        'axis_length': list(lengths),
        'axis_step_size': list(steps),
        'axis_centerpos': [0.0, 0.0, 0.0],
        'axis_startpos': [[0.0], [0.0], [0.0]],
        'sequence_time': dwell,
        'return_time': 1e-3,
        'n_linesteps': linesteps,
    }
    if orders is not None:
        stage['axis_order'] = list(orders)
    return stage


def _built(stage):
    """The Beta designer's scan, then the program's waveforms on it."""
    designed, _positions, info = BetaScanDesigner().make_signal(copy.deepcopy(stage), SETUP)
    return designed, program_scan_signals(stage, SETUP, designed, info)


def _dwell_midpoints(info, signals, n_linesteps=1):
    """Each step's sample index in the middle of its stationary part, which
    comes first: Beta moves and settles at the end of the dwell."""
    per_pixel = int(info['samples_per_pixel'])
    per_line = int(info['scan_samples_d2_period'])   # the line with its return
    dims = list(info['img_dims']) + [1] * (3 - len(info['img_dims']))
    lines = dims[1] * dims[2] * n_linesteps
    still_s = float(info['dwell_time']) - DEFAULT_MOVE_TIME_S - DEFAULT_SETTLE_TIME_S
    assert still_s > 0
    stationary = int(still_s / 2 * RATE)
    return [[line * per_line + pixel * per_pixel + stationary for pixel in range(dims[0])]
            for line in range(lines)]


def _sample(x_um, y_um):
    """A smooth, asymmetric test object: no two positions agree."""
    return 1000.0 + 37.0 * x_um + 11.0 * y_um + 3.0 * x_um * y_um


def _point_detector_image(info, signals, n_linesteps=1):
    """What a point detector assembles: lines read in time order from the
    sample under the beam, placed through the scan's visit order."""
    dims = list(info['img_dims']) + [1] * (3 - len(info['img_dims']))
    order = VisitOrder.from_scan_info(info)
    image = np.full((dims[2], n_linesteps, dims[1], dims[0]), np.nan)
    x = signals[TARGETS[0]] * CONVERSION[TARGETS[0]]
    y = signals[TARGETS[1]] * CONVERSION[TARGETS[1]]
    for line, samples in enumerate(_dwell_midpoints(info, signals, n_linesteps)):
        z, within = divmod(line, dims[1] * n_linesteps)
        y_visit, phase = divmod(within, n_linesteps)
        pixels = _sample(x[samples], y[samples])
        outer = (z,)
        if order is not None:
            pixels, y_visit, outer = order.place(pixels, y_visit, outer, line=within)
        image[outer[0], phase, y_visit, :] = pixels
    return image


def _expected_image(stage, info, n_linesteps=1):
    dims = list(info['img_dims']) + [1] * (3 - len(info['img_dims']))
    first = [0.0 - (dims[i] - 1) * stage['axis_step_size'][i] / 2.0 for i in range(2)]
    xs = first[0] + np.arange(dims[0]) * stage['axis_step_size'][0]
    ys = first[1] + np.arange(dims[1]) * stage['axis_step_size'][1]
    plane = _sample(*np.meshgrid(xs, ys))
    return np.broadcast_to(plane, (dims[2], n_linesteps, dims[1], dims[0]))


# ---------------------------------------------------------------------------
# The order vocabulary
# ---------------------------------------------------------------------------

def test_the_orders_parse_and_an_unknown_one_says_what_exists():
    assert parse_axis_order(None) == ('forward', None)
    assert parse_axis_order('Serpentine') == ('serpentine', None)
    assert parse_axis_order('interleave:3') == ('interleave', 3)
    with pytest.raises(AdvancedConversionError, match='forward, serpentine or interleave'):
        parse_axis_order('spiral')


def test_an_interleave_is_cycles_of_every_kth_position():
    # Lenny's MS-RESOLFT example: 1, 3, 5, 2, 4, 6 (zero-based here)
    assert interleave_permutation(6, 2) == [0, 2, 4, 1, 3, 5]
    assert interleave_permutation(9, 3) == [0, 3, 6, 1, 4, 7, 2, 5, 8]


@pytest.mark.parametrize('count, k', [(6, 4), (6, 1), (6, 6), (5, 2)])
def test_an_interleave_needs_a_count_it_divides(count, k):
    with pytest.raises(AdvancedConversionError, match='divides'):
        interleave_permutation(count, k)


def test_a_forward_scan_without_moves_keeps_the_designers_waveforms():
    assert not uses_program_features(_stage([2, 2, 0], [0.5, 0.5, 1]), None)
    assert not uses_program_features(
        _stage([2, 2, 0], [0.5, 0.5, 1], orders=['forward'] * 3), None)
    assert uses_program_features(
        _stage([2, 2, 0], [0.5, 0.5, 1], orders=['serpentine', 'forward', 'forward']), None)


# ---------------------------------------------------------------------------
# Serpentine parity: every pass alternates, as the acquisition layout reads it
# ---------------------------------------------------------------------------

def _serpentine_program(parity):
    return validate(ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('phase', 'condition', 2),
               Loop('x', 'scan_x', 3, traversal='serpentine', parity_loops=parity)),
        drives=(DeviceDrive('X', weights_um={'x': 1.0}, motion=Motion()),),
        step_s=1e-3, timing_policy='strict-v1'))


def test_a_serpentine_runs_back_on_every_other_pass_of_its_parity_loops():
    played = [visit['x'] for visit in visits(_serpentine_program(('y', 'phase')))]
    passes = [played[i:i + 3] for i in range(0, len(played), 3)]
    # forward, back, forward, back -- not forward, back, back, forward
    assert passes == [[0, 1, 2], [2, 1, 0], [0, 1, 2], [2, 1, 0]]


def test_the_parity_loops_are_the_loops_right_outside():
    with pytest.raises(ProgramError, match='right outside'):
        _serpentine_program(('y',))


# ---------------------------------------------------------------------------
# Every pixel at its own position: a point detector on the program's waveforms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('orders', [
    ['interleave:2', 'forward', 'forward'],
    ['forward', 'interleave:3', 'forward'],
    ['serpentine', 'forward', 'forward'],
    ['serpentine', 'interleave:2', 'forward'],
    ['interleave:3', 'serpentine', 'forward'],
])
def test_a_point_detector_images_the_sample_at_every_pixels_position(orders):
    stage = _stage([3.0, 3.0, 1.0], [0.5, 0.5, 1.0], orders=orders)
    designed, (signals, info) = _built(stage)
    assert [len(signals[name]) for name in signals] == \
        [len(designed[name]) for name in designed]
    assert info['timing_policy'] == 'strict-v1'
    np.testing.assert_allclose(_point_detector_image(info, signals),
                               _expected_image(stage, info), rtol=0, atol=1e-6)


def test_a_serpentine_alternates_with_the_line_steps_too():
    stage = _stage([2.5, 1.5, 0.0], [0.5, 0.5, 1.0],
                   orders=['serpentine', 'forward', 'forward'], linesteps=2)
    designed, (signals, info) = _built(stage)
    image = _point_detector_image(info, signals, n_linesteps=2)
    np.testing.assert_allclose(image, _expected_image(stage, info, 2), atol=1e-6)
    # ...and it was played back on every other line, line steps included
    x = signals['StageX'] * CONVERSION['StageX']
    starts = [x[line[0]] for line in _dwell_midpoints(info, signals, 2)]
    assert starts[0] < starts[1] and starts[1] > starts[2] and starts[2] < starts[3]


def test_without_the_visit_order_an_interleaved_image_is_scrambled():
    """The check above can fail: the same lines placed in time order are not
    the sample."""
    stage = _stage([3.0, 3.0, 1.0], [0.5, 0.5, 1.0], orders=['forward', 'interleave:3', 'forward'])
    _designed, (signals, info) = _built(stage)
    scrambled = _point_detector_image(dict(info, axis_visit_order=None), signals)
    assert not np.allclose(scrambled, _expected_image(stage, info), atol=1e-6)


def test_a_y_serpentine_alternates_with_the_slices():
    stage = _stage([1.5, 1.5, 2.0], [0.5, 0.5, 1.0], orders=['forward', 'serpentine', 'forward'])
    _designed, (signals, info) = _built(stage)
    image = _point_detector_image(info, signals)
    np.testing.assert_allclose(image, _expected_image(stage, info), atol=1e-6)


# ---------------------------------------------------------------------------
# Refusals with their reasons
# ---------------------------------------------------------------------------

def test_a_swept_designer_refuses_orders_and_says_why():
    galvo = copy.deepcopy(SETUP)
    galvo.scan.scanDesigner = 'GalvoScanDesigner'
    stage = _stage([2.5, 2.5, 0.0], [0.5, 0.5, 1.0], orders=['serpentine', 'forward', 'forward'])
    designed, _positions, info = BetaScanDesigner().make_signal(copy.deepcopy(stage), SETUP)
    with pytest.raises(ScanDesignRefusedError, match='GalvoScanDesigner, which sweeps'):
        program_scan_signals(stage, galvo, designed, info)


def test_an_interleave_the_count_does_not_allow_is_refused_with_the_count():
    stage = _stage([2.5, 2.5, 0.0], [0.5, 0.5, 1.0], orders=['forward', 'interleave:2', 'forward'])
    with pytest.raises(ScanDesignRefusedError, match='this axis has 5'):
        _built(stage)


def test_the_outermost_axis_cannot_serpentine():
    stage = _stage([2.5, 2.5, 0.0], [0.5, 0.5, 1.0], orders=['forward', 'serpentine', 'forward'])
    with pytest.raises(ScanDesignRefusedError, match='outermost axis'):
        _built(stage)


def test_a_dwell_off_the_sample_clock_is_refused_not_rounded():
    stage = _stage([2.5, 2.5, 0.0], [0.5, 0.5, 1.0], orders=['serpentine', 'forward', 'forward'],
                   dwell=DWELL_S + 0.5 / RATE)
    with pytest.raises(ScanDesignRefusedError, match='strict-v1'):
        _built(stage)


# ---------------------------------------------------------------------------
# Intra-pixel moves (Guillaume's line program), per line step
# ---------------------------------------------------------------------------

def _with_intra_pixel(stage, device, step, window, size):
    stage = copy.deepcopy(stage)
    phases = int(stage['n_linesteps'])
    starts = [[] for _ in range(phases)]
    ends = [[] for _ in range(phases)]
    sizes = [[] for _ in range(phases)]
    starts[step], ends[step], sizes[step] = [window[0]], [window[1]], [size]
    stage.update({
        'intra_pixel_positioner_movement': True,
        'positioner_target_device': [device],
        'positioner_linestep_enable': {device: [s == step for s in range(phases)]},
        'positioner_movement_starts_s': {device: starts},
        'positioner_movement_ends_s': {device: ends},
        'positioner_step_size_um': {device: sizes},
    })
    return stage


def test_an_intra_pixel_move_appears_in_the_ao_trace_in_its_line_step_only():
    # The move to the offset and back takes the drive's move and settle on
    # each side; a long dwell leaves room for both around a 2 ms window.
    dwell = 2 * DWELL_S
    lead = DEFAULT_MOVE_TIME_S + DEFAULT_SETTLE_TIME_S
    window = (dwell / 2 - 1e-3, dwell / 2 + 1e-3)
    stage = _with_intra_pixel(_stage([1.0, 1.0, 1.0], [0.5, 0.5, 1.0], dwell=dwell, linesteps=2),
                              'StageZ', step=1, window=window, size=0.4)
    assert uses_program_features(stage, stage)
    designed, (signals, info) = _built(stage)
    z = signals['StageZ'] * CONVERSION['StageZ']
    base = designed['StageZ'] * CONVERSION['StageZ']
    per_pixel = int(info['samples_per_pixel'])
    per_line = int(info['scan_samples_d2_period'])
    inside = int(dwell / 2 * RATE)
    before = int((window[0] - lead) / 2 * RATE)   # before the move to the offset starts
    for line in range(4):   # (y, phase) = (0,0), (0,1), (1,0), (1,1)
        phase = line % 2
        for pixel in range(2):
            start = line * per_line + pixel * per_pixel
            expected = 0.4 if phase == 1 else 0.0
            assert z[start + inside] - base[start + inside] == pytest.approx(expected)
            assert z[start + before] == pytest.approx(base[start + before])


def test_an_intra_pixel_window_without_room_for_its_moves_is_refused():
    lead = DEFAULT_MOVE_TIME_S + DEFAULT_SETTLE_TIME_S
    stage = _with_intra_pixel(_stage([1.0, 1.0, 1.0], [0.5, 0.5, 1.0]),
                              'StageZ', step=0, window=(lead / 2, lead), size=0.4)
    with pytest.raises(ScanDesignRefusedError, match='StageZ offset: with its moves and settles'):
        _built(stage)


def test_an_intra_pixel_move_during_the_stage_move_is_refused():
    moving = (DWELL_S - DEFAULT_SETTLE_TIME_S - DEFAULT_MOVE_TIME_S / 2, DWELL_S)
    stage = _with_intra_pixel(_stage([1.0, 1.0, 1.0], [0.5, 0.5, 1.0]),
                              'StageZ', step=0, window=moving, size=0.4)
    with pytest.raises(ScanDesignRefusedError, match='stationary|moving|move'):
        _built(stage)


# ---------------------------------------------------------------------------
# Frame streams: the layout says where every frame was taken
# ---------------------------------------------------------------------------

def _camera_layout(info, *, linesteps=1):
    return build_advanced_scan_layouts(
        info, ('Camera',), scan_source='test',
        devices={'scan_x': 'StageX', 'scan_y': 'StageY', 'scan_z': 'StageZ'},
        # as the controller passes them: a stage stepping the other way
        directions={'scan_x': 1, 'scan_y': -1, 'scan_z': 1},
        detector_masks={'Camera': [True] * linesteps},
        pulse_counts_by_condition={'Camera': [1] * linesteps},
    )['Camera']


def test_an_interleaved_camera_stream_is_cycles_by_planes_at_their_positions():
    stage = _stage([0.0, 3.0, 0.0], [0.5, 0.5, 1.0], orders=['forward', 'interleave:2', 'forward'])
    _designed, (signals, info) = _built(stage)
    layout = decode_acquisition_layout(encode_acquisition_layout(_camera_layout(info)))
    assert layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM
    assert [(loop.kind, loop.count) for loop in layout.event_loops] == [('cycle', 2), ('plane', 3)]
    assert [loop.step for loop in layout.event_loops] == [0.5, 1.0]
    assert {loop.device for loop in layout.event_loops} == {'StageY'}

    # Frame f was taken at the position the waveform held during step f.
    y = signals['StageY'] * CONVERSION['StageY']
    frames = [line[0] for line in _dwell_midpoints(info, signals)]
    first = -(6 - 1) * 0.5 / 2.0
    for frame in range(recorded_frame_count(layout)):
        c = recorded_frame_coordinates(layout, frame)
        index = c['cycle'] + 2 * c['plane']
        assert y[frames[frame]] == pytest.approx(first + index * 0.5)

    # SNOUTY reads its geometry from the layout, and its restack puts the
    # frames in position order.
    resolved = SimpleNamespace(layout=layout, confidence='high', is_authoritative=True)
    cycles, planes, _time = recorded_snouty_geometry(SimpleNamespace(acquisition_layout=resolved))
    assert (cycles, planes) == (2, 3)
    stack = np.zeros((6, 1, 1))
    for frame in range(6):
        c = recorded_frame_coordinates(layout, frame)
        stack[frame] = c['cycle'] + 2 * c['plane']
    assert restack_interleaved(stack, cycles, planes)[:, 0, 0].tolist() == [0, 1, 2, 3, 4, 5]


def test_a_serpentine_camera_stream_declares_its_traversal():
    stage = _stage([1.5, 1.0, 0.0], [0.5, 0.5, 1.0],
                   orders=['serpentine', 'forward', 'forward'], linesteps=2)
    _designed, (signals, info) = _built(stage)
    layout = _camera_layout(info, linesteps=2)
    rules = {rule.loop_id: rule for rule in layout.traversal}
    assert rules['scan_x'].order == 'serpentine'
    assert rules['scan_x'].parity_loops == ('scan_y', 'condition')
    x = signals['StageX'] * CONVERSION['StageX']
    samples = [s for line in _dwell_midpoints(info, signals, 2) for s in line]
    first = -(3 - 1) * 0.5 / 2.0
    for frame in range(recorded_frame_count(layout)):
        c = recorded_frame_coordinates(layout, frame)
        assert x[samples[frame]] == pytest.approx(first + c['scan_x'] * 0.5)


def test_a_forward_scan_keeps_its_layout():
    stage = _stage([1.5, 1.0, 0.0], [0.5, 0.5, 1.0])
    _designed, _positions, info = BetaScanDesigner().make_signal(copy.deepcopy(stage), SETUP)
    layout = _camera_layout(info)
    assert [loop.kind for loop in layout.event_loops] == ['scan_y', 'scan_x']
    assert all(rule.order == 'forward' for rule in layout.traversal)


def test_two_interleaved_axes_cannot_be_described_as_a_frame_stream():
    stage = _stage([2.0, 2.0, 0.0], [0.5, 0.5, 1.0],
                   orders=['interleave:2', 'interleave:2', 'forward'])
    _designed, (_signals, info) = _built(stage)
    with pytest.raises(ValueError, match='one interleaved axis'):
        _camera_layout(info)


# ---------------------------------------------------------------------------
# Detectors that cannot place a visit say so
# ---------------------------------------------------------------------------

def test_the_visit_order_refuses_an_order_it_does_not_know():
    with pytest.raises(ValueError, match='Unknown visit order'):
        VisitOrder([{'serpentine': 'slice'}], [3])
    assert VisitOrder.from_scan_info({'img_dims': [3, 2]}) is None
    assert VisitOrder.from_scan_info({'img_dims': [3, 2], 'axis_visit_order': [None, None]}) is None


def test_the_visit_orders_follow_the_image_axes():
    stage = _stage([1.5, 2.0, 1.0], [0.5, 0.5, 1.0], orders=['serpentine', 'interleave:2', 'forward'])
    assert axis_visit_orders(stage, [3, 4, 1]) == [
        {'serpentine': 'line'}, {'interleave': [0, 2, 1, 3]}, None]


def test_a_detector_that_reads_in_time_order_refuses_a_scan_it_cannot_place():
    # The Swabian TimeTagger builds its image from line pulses in time order.
    info = {'img_dims': [4, 2], 'axis_visit_order': [None, {'interleave': [0, 1]}]}
    with pytest.raises(ValueError, match='TimeTagger cannot place .* out of order'):
        require_forward('TimeTagger', info)
    require_forward('TimeTagger', {'img_dims': [4, 2]})


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
