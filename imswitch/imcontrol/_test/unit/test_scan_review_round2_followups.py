"""Review round 2, the recheck (plan 7.13): the reviewer's follow-up probes of
2026-10-14, kept as written."""
from dataclasses import replace

import numpy as np
import pytest

from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent,
)
from imswitch.imcontrol.model.scan_program.program import Separation
from imswitch.imcontrol.model.scan_program.interpreter import expand
from imswitch.imcontrol.model.scan_program.schedule import realize
from imswitch.imcontrol._test.unit.test_scan_program_expert_model import NIDAQ, CHANNELS


def raster(return_s=0):
    return ScanProgram((Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 3)),
        (DeviceDrive('X', weights_um={'x': 1.},
            motion=Motion(transition='jump', origin='absolute', restore='first',
                          return_s=return_s)),), 0.001)


@pytest.mark.parametrize('return_s', [0, 0.0001])
def test_raster_line_period_agrees_with_emitted_step_starts(return_s):
    compiled = NIDAQ.compile(raster(return_s), CHANNELS, captured_um={'X': 0})
    info = compiled.scanInfoDict
    if not info['program_raster']:
        return  # a documented refusal is also safe
    steps = compiled.schedule.step_spans()
    actual = steps[3].start_tick - steps[0].start_tick
    assert info['scan_samples_d2_period'] == actual


@pytest.mark.parametrize('engine_kind', ['simulated', 'wire'])
def test_step_board_plays_derived_final_return_time(engine_kind):
    from imswitch.imcontrol._test.unit.test_scan_engine_conformance import BOARDS
    from imswitch.imcontrol._test.unit.test_scan_engine_conformance import CHANNELS as board_channels
    engine = BOARDS[engine_kind]()
    p = ScanProgram((Loop('x', 'scan_x', 3),),
        (DeviceDrive('X', weights_um={'x': 1.},
            motion=Motion(transition='jump', origin='absolute', restore='first',
                          move_s=0.0002, settle_s=0.0001, return_s=0)),), 0.001)
    try:
        # A board plays one return time after every pass, the last included;
        # the schedule's longer final return (move + settle to the restore
        # position) cannot be told to it, so it is refused with the way out
        # rather than reported done before the stage has settled.
        with pytest.raises(ScanEngineRefused, match='one return time.*return_s.*hold'):
            engine.compile(p, board_channels, captured_um={'X': 0})
        timed = replace(p, drives=(replace(p.drives[0], motion=replace(
            p.drives[0].motion, return_s=0.0003)),))
        compiled = engine.compile(timed, board_channels, captured_um={'X': 0})
        engine.prepare(compiled)
        engine.run(compiled, blocking=True)
        assert engine.outcome[0] == 'done'
        actual = engine.log.of('end')[-1][2]
        expected = sum(s.ticks for s in compiled.schedule.segments)
        assert actual == expected                           # done when the stage has settled
    finally:
        finalizer = getattr(engine, 'finalize', None)
        if finalizer:
            finalizer()


def test_trajectory_rejects_nan_captured_origin():
    from imswitch.imcontrol._test.unit.test_scan_program_trajectory import NIDAQ as trajectory_engine
    from imswitch.imcontrol._test.unit.test_scan_program_trajectory import SETUP, _spiral
    from imswitch.imcontrol.model.scan_program.engine import setup_channels
    p = _spiral(turns=1, radius_um=1, duration_s=0.1, approach_s=0.005)
    with pytest.raises(ValueError):
        trajectory_engine.compile(p, setup_channels(SETUP), captured_um={'X': float('nan'), 'Y': 0.})


def test_mixed_anchor_separation_compares_normalized_requested_edges():
    p = ScanProgram((Loop('repeat', 'repeat', 1),), (), 0.004, timeline=(
        TimelineEvent('Camera', 'detector', 0, 0.001, id='first'),
        TimelineEvent('Camera', 'detector', -0.001, 0, id='last', anchor='pass-end:repeat'),
    ), separations=(Separation(('first', 'end'), ('last', 'start')),))
    schedule = realize(expand(p), 100_000)
    assert schedule.events[1].start_tick - schedule.events[0].end_tick == 200


def test_real_routing_probe_now_selects_pulse_engine():
    from imswitch.imcontrol._test.unit.test_pulse_generator_scan_engine import _engine
    from imswitch.imcontrol.model.managers.scan_engines.loader import ScanEngines
    from imswitch.imcontrol.model.scan_program.engine import DeviceChannels, assign_owners
    pulse = _engine()
    channels = {'L405': DeviceChannels('L405', analog='Dev1/ao0', digital='0', kind='laser')}
    engines = ScanEngines({'nidaq': NIDAQ, 'Teensy': pulse}, 'nidaq',
                         assign_owners([NIDAQ, pulse], channels))
    p = ScanProgram((Loop('repeat', 'repeat', 2),), (), 0.003,
                   timeline=(TimelineEvent('L405', 'ttl', 0, 0.001),))
    assert pulse.compile(p, channels).report.runnable
    assert engines.for_program(p, channels).engine is pulse
