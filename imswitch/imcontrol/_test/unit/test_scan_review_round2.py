"""Review round 2 (docs/design/plans/scan-engines-and-expert-scan.md §7.13): the
reviewer's probes of 2026-10-14, kept as they were written where the finding
was fixed as proposed, and adapted where it was fixed another way -- a final
return of no duration is played in a derived time and reported, not refused.
"""
from dataclasses import replace


import numpy as np
import pytest

from imswitch.imcontrol.model.scan_program import (
    DetectorBinding, DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait,
)
from imswitch.imcontrol.model.scan_program.bindings import binding_problems, wire_frames
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.interpreter import expand
from imswitch.imcontrol.model.scan_program.schedule import realize
from imswitch.imcontrol.model.scan_program.scan_info import scan_info_for
from imswitch.imcontrol.model.scan_program.serialize import program_from_dict, program_to_dict
from imswitch.imcontrol.model.managers.detectors._visit_order import VisitOrder
from .test_scan_program_expert_model import NIDAQ, CHANNELS


def pulse_program(end=0.001):
    return ScanProgram((Loop('repeat', 'repeat', 3),), (), 0.001,
                       timeline=(TimelineEvent('Camera', 'detector', 0, end, id='expose'),),
                       detectors=(DetectorBinding('Camera', 'expose'),))


@pytest.mark.parametrize('end', [0.0009, 0.001])
def test_camera_frame_count_matches_actual_wire_edges_across_steps(end):
    p = pulse_program(end)
    schedule = realize(expand(p), 100_000)
    wire = densify(schedule)['digital']['Camera']
    edges = np.count_nonzero(np.diff(np.r_[False, wire].astype(int)) == 1)
    assert sum(wire_frames(schedule, 'Camera')) == edges


def test_binding_rejects_three_frames_when_continuous_high_only_triggers_once():
    p = pulse_program()
    assert binding_problems(p, realize(expand(p), 100_000))


def test_mixed_anchors_preserve_nonoverlapping_pulses():
    p = ScanProgram((Loop('repeat', 'repeat', 1),), (), 0.004, timeline=(
        TimelineEvent('Camera', 'detector', 0, 0.001, id='first'),
        TimelineEvent('Camera', 'detector', -0.001, 0, id='last',
                      anchor='pass-end:repeat'),
    ))
    schedule = realize(expand(p), 100_000)
    assert [(e.start_tick, e.end_tick) for e in schedule.events] == [(0, 100), (300, 400)]


@pytest.mark.parametrize('traversal', ['forward', 'reverse', 'serpentine'])
def test_table_positions_and_traversal_reconstruct_in_physical_order(traversal):
    p = ScanProgram((Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 3,
        traversal=traversal, parity_loops=('y',) if traversal == 'serpentine' else ())),
        (DeviceDrive('X', table_loop='x', table_um=(0., 2., 1.),
                     motion=Motion(transition='jump', origin='absolute', restore='hold')),),
        0.001)
    compiled = NIDAQ.compile(p, CHANNELS)
    info = compiled.scanInfoDict
    # Refusing an unsupported combination also protects recording integrity.
    if not info['program_raster']:
        return
    order = VisitOrder.from_scan_info(info)
    acquired_positions = [s.span.pieces['X'].start_um for s in compiled.schedule.step_spans()]
    for line in range(2):
        actual, _, _ = order.place(acquired_positions[line * 3:(line + 1) * 3], line)
        np.testing.assert_array_equal(actual, [0., 1., 2.])


def test_phase_only_program_can_compile_for_a_camera():
    p = replace(pulse_program(0.0009), loops=(Loop('phase', 'condition', 3),))
    schedule = realize(expand(p), 100_000)
    info = scan_info_for(p, schedule, 100_000)
    assert info['scan_samples_total'] == 300


def test_nonfinite_position_is_refused_before_hardware_compilation():
    p = ScanProgram((Loop('x', 'scan_x', 2),),
        (DeviceDrive('X', offset_um=float('nan'),
                     motion=Motion(origin='absolute', restore='hold')),), 0.001)
    with pytest.raises(ValueError):
        NIDAQ.compile(p, CHANNELS)


def test_fractional_loop_count_is_not_silently_truncated_on_load():
    data = program_to_dict(pulse_program(0.0009))
    data['loops'][0]['count'] = 3.9
    with pytest.raises(ValueError):
        program_from_dict(data)


def _restore_first(shape, return_s):
    return ScanProgram((Loop('x', 'scan_x', 3),),
        (DeviceDrive('X', weights_um={'x': 1.},
                     motion=Motion(transition=shape, origin='absolute', restore='first',
                                   return_s=return_s)),), 0.001)


@pytest.mark.parametrize('shape,return_s', [('linear', 0.00001), ('jump', 0.00001),
                                         ('linear', 0.00002), ('smooth', 0.00001)])
def test_tracked_final_position_is_the_final_emitted_sample(shape, return_s):
    from imswitch.imcontrol.controller.controllers.ScanControllerExpert import _end_positions
    compiled = NIDAQ.compile(_restore_first(shape, return_s), CHANNELS)
    final_um = compiled.signalDict['scanSignalsDict']['X'][-1] * 2.0
    assert final_um == 0.0                                 # the return reaches its endpoint
    assert _end_positions(compiled.schedule)['X'] == final_um


def test_a_return_of_no_duration_to_the_restore_position_is_played_and_reported():
    # a return of no duration cannot take X back: it is given the drive's
    # declared move and settle (one tick when it declares none), the change
    # is recorded, and the run ends where the program says
    compiled = NIDAQ.compile(_restore_first('jump', 0.0), CHANNELS)
    assert compiled.signalDict['scanSignalsDict']['X'][-1] * 2.0 == 0.0
    assert compiled.schedule.final_um() == {'X': 0.0}
    changes = [c for c in compiled.schedule.changes if c.quantity == 'return']
    assert len(changes) == 1 and 'no duration' in changes[0].where
    assert changes[0].realized_s == pytest.approx(1e-5)            # one tick: nothing declared
    timed = replace(_restore_first('jump', 0.0), drives=(DeviceDrive(
        'X', weights_um={'x': 1.}, motion=Motion(transition='jump', origin='absolute',
                                                 restore='first', move_s=2e-4,
                                                 settle_s=1e-4)),))
    compiled = NIDAQ.compile(timed, CHANNELS)
    assert compiled.schedule.changes[-1].realized_s == pytest.approx(3e-4)   # move + settle
    held = replace(_restore_first('jump', 0.0), drives=(DeviceDrive(
        'X', weights_um={'x': 1.}, motion=Motion(transition='jump', origin='absolute',
                                                 restore='hold')),))
    compiled = NIDAQ.compile(held, CHANNELS)
    assert compiled.schedule.final_um() == {'X': 2.0} and not compiled.schedule.changes


def test_a_line_held_through_a_wait_is_one_pulse_and_an_idle_wait_breaks_it():
    for hold, frames in (('hold', [1, 0, 0]), ('idle', [1, 1, 1])):
        p = replace(pulse_program(), waits=(Wait('step', 'time', interval_s=0.0005, hold=hold),))
        schedule = realize(expand(p), 100_000)
        assert wire_frames(schedule, 'Camera') == frames, hold
        wire = densify(schedule)['digital']['Camera']
        edges = np.count_nonzero(np.diff(np.r_[False, wire].astype(int)) == 1)
        assert edges == sum(frames), hold                  # the NI-DAQ plays the same


def test_the_engine_is_chosen_by_the_channel_the_program_switches():
    from imswitch.imcontrol.model.managers.scan_engines.loader import program_channels
    p = ScanProgram((Loop('repeat', 'repeat', 2),), (), 0.001, timeline=(
        TimelineEvent('L405', 'ttl', 0, 0.0005), TimelineEvent('L488', 'level', 0, 0.0005,
                                                                value=0.5)))
    assert program_channels(p) == {'L405': {'digital'}, 'L488': {'analog'}}


@pytest.mark.parametrize('outer', ['forward', 'reverse'])
@pytest.mark.parametrize('inner', ['forward', 'reverse', 'serpentine'])
@pytest.mark.parametrize('table', [False, True])
@pytest.mark.parametrize('placement', ['in-step', 'between-steps'])
@pytest.mark.parametrize('pause', [False, True])
def test_composed_program_samples_against_independent_positions(outer, inner, table,
                                                              placement, pause):
    """48 combinations: independent counter enumeration, sampled outputs, JSON roundtrip."""
    p = ScanProgram(
        (Loop('y', 'scan_y', 2, traversal=outer), Loop('x', 'scan_x', 3,
            traversal=inner, parity_loops=('y',) if inner == 'serpentine' else ())),
        (DeviceDrive('X', offset_um=0.5, weights_um={'y': 0.25, 'x': 0.5},
             table_loop='x' if table else None, table_um=(0., 1., -0.5) if table else None,
             motion=Motion(transition='jump', move_s=0.0001, settle_s=0.0001,
                           placement=placement, return_s=0.0002,
                           origin='absolute', restore='hold')),), 0.001,
        timeline=(TimelineEvent('Camera', 'detector', 0.0001, 0.0003),),
        waits=(Wait('step', 'time', interval_s=0.0001),) if pause else ())
    p = program_from_dict(program_to_dict(p))
    compiled = NIDAQ.compile(p, CHANNELS)
    ys = [0, 1] if outer == 'forward' else [1, 0]
    expected = []
    for ordinal, y in enumerate(ys):
        xs = [2, 1, 0] if inner == 'reverse' or (inner == 'serpentine' and ordinal % 2) \
            else [0, 1, 2]
        for x in xs:
            expected.append(0.5 + y * 0.25 + x * 0.5 + ((0., 1., -0.5)[x] if table else 0))
    waveform = compiled.signalDict['scanSignalsDict']['X'] * 2.0
    actual = [waveform[s.start_tick + 10] for s in compiled.schedule.step_spans()]
    np.testing.assert_array_equal(actual, expected)
    assert sum(wire_frames(compiled.schedule, 'Camera')) == 6
