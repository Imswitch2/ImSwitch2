"""Review round 2, the second recheck (plan 7.13): the reviewer's independent
boundary checks of 2026-10-14, kept as written -- actual sample and log
times and positions against the metadata, not the helpers.
"""
from dataclasses import replace
import numpy as np
import pytest
from imswitch.imcontrol.model.scan_program import DeviceDrive, Loop, Motion, ScanProgram
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol._test.unit.test_scan_program_expert_model import NIDAQ, CHANNELS


@pytest.mark.parametrize('lines', [1, 2, 3])
@pytest.mark.parametrize('slices', [1, 2])
@pytest.mark.parametrize('return_s', [0, 0.0001])
@pytest.mark.parametrize('restore', ['first', 'hold'])
def test_raster_periods_match_actual_line_and_slice_starts(lines, slices, return_s, restore):
    p = ScanProgram((Loop('z', 'scan_z', slices), Loop('y', 'scan_y', lines),
                     Loop('x', 'scan_x', 3)),
                    (DeviceDrive('X', weights_um={'x': 1}, motion=Motion(
                        transition='jump', move_s=0.0002, settle_s=0.0001,
                        origin='absolute', restore=restore, return_s=return_s)),), 0.001)
    c = NIDAQ.compile(p, CHANNELS, captured_um={'X': 0})
    info = c.scanInfoDict
    assert info['program_raster']
    steps = c.schedule.step_spans()
    line_starts = [s.start_tick for s in steps[::3]]
    if len(line_starts) > 1:
        np.testing.assert_array_equal(np.diff(line_starts), info['scan_samples_d2_period'])
    if slices > 1:
        actual_frame_stride = steps[lines * 3].start_tick - steps[0].start_tick
        assert info['scan_samples'][2] == actual_frame_stride
    emitted = c.signalDict['scanSignalsDict']['X'] * 2
    assert len(emitted) == info['scan_samples_total']
    assert c.schedule.final_um()['X'] == emitted[-1]


@pytest.mark.parametrize('kind', ['simulated', 'wire'])
@pytest.mark.parametrize('lines', [1, 2])
@pytest.mark.parametrize('mode', ['explicit', 'hold', 'unrepresentable'])
def test_board_end_time_and_position_or_explicit_refusal(kind, lines, mode):
    from imswitch.imcontrol._test.unit.test_scan_engine_conformance import BOARDS
    from imswitch.imcontrol._test.unit.test_scan_engine_conformance import CHANNELS as board_channels
    engine = BOARDS[kind]()
    motion = Motion(transition='jump', move_s=0.0002, settle_s=0.0001,
                    origin='absolute', restore='hold' if mode == 'hold' else 'first',
                    return_s=0.0003 if mode == 'explicit' else 0)
    p = ScanProgram((Loop('y', 'scan_y', lines), Loop('x', 'scan_x', 3)),
                    (DeviceDrive('X', weights_um={'x': 1}, motion=motion),), 0.001)
    try:
        if mode == 'unrepresentable':
            with pytest.raises(ScanEngineRefused, match='return_s'):
                engine.compile(p, board_channels, captured_um={'X': 0})
            return
        c = engine.compile(p, board_channels, captured_um={'X': 0})
        engine.prepare(c)
        engine.run(c, blocking=True)
        assert engine.outcome[0] == 'done'
        assert engine.log.of('end')[-1][2] == sum(s.ticks for s in c.schedule.segments)
        assert engine.log.of('dac')[-1][-1] == c.schedule.final_um()['X']
    finally:
        finalizer = getattr(engine, 'finalize', None)
        if finalizer:
            finalizer()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), 'invalid'])
def test_trajectory_captured_origin_is_rejected_before_output(value):
    from imswitch.imcontrol._test.unit.test_scan_program_trajectory import NIDAQ as engine
    from imswitch.imcontrol._test.unit.test_scan_program_trajectory import SETUP, _spiral
    p = _spiral(turns=1, radius_um=1, duration_s=0.1, approach_s=0.005)
    with pytest.raises(ScanEngineRefused, match='captured position'):
        engine.compile(p, setup_channels(SETUP), captured_um={'X': value, 'Y': 0})


@pytest.mark.parametrize('a_anchor,b_anchor', [
    ('step', 'pass-end:repeat'), ('pass-start:repeat', 'pass-end:repeat'),
    ('pass-end:repeat', 'step'), ('pass-end:repeat', 'pass-start:repeat'),
])
def test_anchor_separations_share_the_same_requested_origin(a_anchor, b_anchor):
    from imswitch.imcontrol.model.scan_program import TimelineEvent
    from imswitch.imcontrol.model.scan_program.program import Separation
    from imswitch.imcontrol.model.scan_program.interpreter import expand
    from imswitch.imcontrol.model.scan_program.schedule import realize
    def event(event_id, anchor, start, end):
        shift = 0.004 if anchor.startswith('pass-end:') else 0
        return TimelineEvent('Camera', 'detector', start - shift, end - shift,
                             id=event_id, anchor=anchor)
    p = ScanProgram((Loop('repeat', 'repeat', 1),), (), 0.004,
                    timeline=(event('a', a_anchor, 0, 0.001),
                              event('b', b_anchor, 0.003, 0.004)),
                    separations=(Separation(('a', 'end'), ('b', 'start')),))
    s = realize(expand(p), 100000)
    assert s.events[1].start_tick - s.events[0].end_tick == 200
