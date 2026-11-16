"""The sparse preview draws what densify draws, from the schedule's writes
and edges instead of one sample per tick: the same values at every
breakpoint, and a size that does not grow with the run's length."""

import time

import numpy as np

from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, Prologue, RunContext, ScanProgram, TimelineEvent, Wait, expand,
    realize,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.preview import sparse_traces

JUMP = Motion(transition='jump', move_s=0.0, settle_s=0.001, placement='in-step',
              return_s=0.001, origin='absolute', restore='first')


def _program(cycles=2, planes=3, timepoints=1):
    loops = ([Loop('time', 'time', timepoints)] if timepoints > 1 else []) + [
        Loop('cycle', 'cycle', cycles), Loop('plane', 'plane', planes)]
    return ScanProgram(
        loops=tuple(loops),
        drives=(DeviceDrive('G', offset_um=-21.0, weights_um={'cycle': 0.21, 'plane': 2.1},
                            motion=JUMP),
                DeviceDrive('X', offset_um=-280.0, motion=Motion(
                    transition='jump', move_s=0.0, settle_s=0.0, placement='in-step',
                    return_s=0.001, origin='absolute', restore='first'))),
        step_s=0.0076,
        timeline=(TimelineEvent('RO', 'ttl', 0.001, 0.002, id='ro'),
                  TimelineEvent('L2', 'ttl', 0.0024, 0.0044, id='l2'),
                  TimelineEvent('CAM', 'detector', 0.001, 0.0058, during='any', id='exposure'),
                  TimelineEvent('X', 'offset', 0.002, 0.0044, value=280.0, during='any',
                                id='second'),
                  TimelineEvent('X', 'offset', 0.0044, 0.0076, value=560.0, during='any',
                                id='third'),
                  TimelineEvent('ON', 'ttl', 0.003, 0.021, anchor='prologue:plane',
                                id='activation')),
        prologues=(Prologue('plane', 0.0615, offsets_um={'G': 21.0}),),
        waits=(Wait(at='time', trigger='time', interval_s=0.1),) if timepoints > 1 else (),
        timing_policy='strict-v1',
    )


def test_the_sparse_preview_agrees_with_densify_at_every_breakpoint():
    schedule = realize(expand(_program(), RunContext(captured_um={'G': 0.0, 'X': -280.0})),
                       1e6)
    dense = densify(schedule)
    time_s, sparse = sparse_traces(schedule)
    ticks = np.round(time_s * 1e6).astype(int)
    total = schedule.segments[0].ticks
    # Just before a breakpoint the dense sample is the one at tick - 1, just
    # after it the one at the tick itself.
    before = np.clip(ticks[0::2] - 1, 0, total - 1)
    after = np.clip(ticks[1::2], 0, total - 1)
    for group in ('analog', 'digital'):
        assert set(sparse[group]) == set(dense[group])
        for name, values in sparse[group].items():
            reference = np.asarray(dense[group][name], dtype=float)
            np.testing.assert_allclose(values[0::2], reference[before], atol=1e-9,
                                       err_msg=f'{name} before its breakpoints')
            np.testing.assert_allclose(values[1::2], reference[after], atol=1e-9,
                                       err_msg=f'{name} after its breakpoints')
    assert time_s[0] == 0.0 and time_s[-1] == total / 1e6


def test_the_sparse_preview_stays_small_and_fast_for_a_long_run():
    # 3 time points x 10 cycles x 20 planes with 0.1 s waits: 5.9 s on a
    # 1 µs grid is 5.9 million samples per trace densified.
    program = _program(cycles=10, planes=20, timepoints=3)
    schedule = realize(expand(program, RunContext(captured_um={'G': 0.0, 'X': -280.0})), 1e6)
    started = time.perf_counter()
    time_s, sparse = sparse_traces(schedule)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0
    assert time_s.size < 200_000
    assert len(sparse['digital']['CAM']) == time_s.size
    # The galvo visits the resting position before every cycle.
    galvo = sparse['analog']['G']
    assert np.isclose(galvo, 0.0, atol=1e-9).any()
