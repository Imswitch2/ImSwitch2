"""Realizing a program on a timing grid (docs/design/plans/
scan-engines-and-expert-scan.md §2.2, §2.4, §2.6).

Under ``strict-v1`` every time must already be on the grid; what is not is
refused with its representable neighbours. Order and overlap never change
silently. Motion is per device: an offset is a motion with its own moves and
settles, and a ``stationary`` event may not overlap any of them. Deadline
scheduling does not accumulate, and an iteration that would overrun its
deadline is refused. A wait splits the schedule into segments; after an
external barrier, a preparation stage readies only the devices that need it.
"""

import json

import numpy as np
import pytest

from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, DeviceFacts, InterpretError, Loop, Motion, RunContext, ScanProgram,
    ScheduleRefused, Separation, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program import policy as policies
from imswitch.imcontrol.model.scan_program.densify import DensifyError, densify
from imswitch.imcontrol.model.scan_program.schedule import approach_span

RATE = 100_000          # a 10 µs grid
STAGE = Motion(transition='smooth', move_s=1e-3, settle_s=1e-3, origin='absolute',
               restore='first')


def _realize(program, policy=None, context=None):
    return realize(expand(program, context), RATE, policy)


def _pulses(*windows, step_s=100e-6, count=2):
    events = tuple(TimelineEvent('L488', 'ttl', start, end, id=f'p{i}', during='any')
                   for i, (start, end) in enumerate(windows))
    return ScanProgram(loops=(Loop('x', 'scan_x', count),), drives=(), step_s=step_s,
                       timeline=events)


def _tolerant(**changes):
    """A bundled preset policy that rounds to the nearest step."""
    document = json.loads((policies.POLICY_DIR / 'strict-v1.json').read_text())
    document.update(id='test-nearest', title='Nearest step', rationale='For these tests.')
    document['rounding'] = dict.fromkeys(policies.QUANTITIES, 'nearest-even')
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = dict(document[key], **value)
        else:
            document[key] = value
    return document


# ---------------------------------------------------------------------------
# strict-v1: exact or refused
# ---------------------------------------------------------------------------

def test_an_exact_program_realizes_with_no_change():
    schedule = _realize(_pulses((10e-6, 30e-6)))
    assert schedule.changes == [] and schedule.error.end_deviation_s == 0
    assert [(e.start_tick, e.end_tick) for e in schedule.events] == [(1, 3), (11, 13)]


def test_a_pulse_off_the_grid_is_refused_with_its_neighbours():
    """The review's case: 6-24 µs on a 10 µs grid would play 10-20 µs, so
    the illumination would fall from 18 to 10 µs. Strict refuses it."""
    with pytest.raises(ScheduleRefused) as refused:
        _realize(_pulses((6e-6, 24e-6)))
    text = str(refused.value)
    assert '6 µs is not on the 10 µs grid (policy strict-v1): 0 s or 10 µs' in text
    assert '24 µs is not on the 10 µs grid (policy strict-v1): 20 µs or 30 µs' in text


def test_a_step_period_off_the_grid_is_refused_with_its_neighbours():
    with pytest.raises(ScheduleRefused, match=r'105 µs is not on the 10 µs grid.*100 µs or 110 µs'):
        _realize(_pulses(step_s=105e-6))


def test_decimal_times_that_are_on_the_grid_are_exact():
    """0.3 ms * 100 kHz is 29.999999999999996 in binary; it IS 30 steps."""
    schedule = _realize(_pulses((0.0, 0.3e-3), step_s=0.3e-3))
    assert schedule.changes == [] and schedule.step_spans()[0].ticks == 30


# ---------------------------------------------------------------------------
# Order and overlap; vanishing pulses
# ---------------------------------------------------------------------------

def test_a_gap_that_would_close_is_refused_when_relations_are_preserved():
    program = _pulses((0.0, 16e-6), (24e-6, 40e-6))
    with pytest.raises(ScheduleRefused, match='requested to gap, but would touch'):
        _realize(program, _tolerant())
    reported = _realize(program, _tolerant(events={'relations': 'as-designers'}))
    assert any(c.quantity == 'relation' for c in reported.changes)


def test_a_pulse_that_rounds_to_nothing_is_refused_or_dropped_as_the_policy_says():
    program = _pulses((1e-6, 4e-6))
    with pytest.raises(ScheduleRefused, match='rounds to nothing'):
        _realize(program, _tolerant())
    dropped = _realize(program, _tolerant(events={'vanished': 'drop'}))
    assert dropped.events == [] and any(c.quantity == 'event' for c in dropped.changes)


def test_a_critical_separation_beyond_its_tolerance_is_refused():
    program = ScanProgram(
        loops=(Loop('x', 'scan_x', 1),), drives=(), step_s=100e-6,
        timeline=(TimelineEvent('Off', 'ttl', 0.0, 14e-6, id='off', during='any'),
                  TimelineEvent('Readout', 'ttl', 26e-6, 40e-6, id='ro', during='any')),
        separations=(Separation(('off', 'end'), ('ro', 'start')),),
    )
    with pytest.raises(ScheduleRefused, match=r'off.end -> ro.start is 20 µs, requested 12 µs'):
        _realize(program, _tolerant())


# ---------------------------------------------------------------------------
# Motion is per device; offsets are motions
# ---------------------------------------------------------------------------

def _beta_like(*timeline):
    """A stepped X with 1 ms move and 1 ms settle at the end of a 10 ms
    dwell, and a Z held still that can take intra-pixel offsets."""
    return ScanProgram(
        loops=(Loop('x', 'scan_x', 2),),
        drives=(DeviceDrive('X', weights_um={'x': 10.0}, motion=STAGE),
                DeviceDrive('Z', offset_um=5.0, motion=STAGE)),
        step_s=10e-3, timeline=tuple(timeline), timing_policy='strict-v1',
        approach=False,     # as Beta: the first sample is the first position
    )


def test_an_offset_is_a_motion_with_its_moves_and_settles():
    schedule = _realize(_beta_like(TimelineEvent('Z', 'offset', 3e-3, 5e-3, value=0.2)))
    z = densify(schedule)['analog']['Z']
    at = lambda ms: z[int(round(ms * 1e-3 * RATE))]
    assert at(0.5) == 5.0 and at(6.5) == 5.0            # still before and after
    assert at(2.5) == pytest.approx(5.2)                 # settled before the hold
    assert at(4.0) == pytest.approx(5.2)                 # held 3-5 ms
    assert 5.0 < at(1.5) < 5.2                           # moving up 1-2 ms


def test_an_offset_that_does_not_fit_where_the_device_is_still_is_refused():
    with pytest.raises(ScheduleRefused, match='Z is stationary from 0 to 10 ms'):
        _realize(_beta_like(TimelineEvent('Z', 'offset', 0.5e-3, 5e-3, value=0.2)))
    with pytest.raises(ScheduleRefused, match='X is stationary from 0 to 8 ms'):
        _realize(_beta_like(TimelineEvent('X', 'offset', 3e-3, 6.5e-3, value=0.2)))


def test_a_stationary_window_may_not_overlap_any_device_moving():
    camera = dict(device='Camera', kind='detector', start_s=1.5e-3, end_s=2.5e-3)
    offset = TimelineEvent('Z', 'offset', 3e-3, 5e-3, value=0.2)
    with pytest.raises(ScheduleRefused, match=r'overlaps Z moving from 1 ms to 3 ms'):
        _realize(_beta_like(offset, TimelineEvent(**camera)))
    _realize(_beta_like(offset, TimelineEvent(**camera, during='any')))
    late = dict(camera, start_s=8.5e-3, end_s=9.5e-3)
    with pytest.raises(ScheduleRefused, match=r'overlaps X moving from 8 ms to 10 ms'):
        _realize(_beta_like(TimelineEvent(**late)))
    _realize(_beta_like(TimelineEvent(**late, still=('Z',))))


def test_a_swept_loop_is_not_densified_here():
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2, mode='swept'),),
                          drives=(DeviceDrive('X', weights_um={'x': 1.0}, motion=STAGE),),
                          step_s=10e-3)
    with pytest.raises(DensifyError, match='GalvoScanDesigner'):
        densify(_realize(program))


# ---------------------------------------------------------------------------
# Deadlines and the overrun rule
# ---------------------------------------------------------------------------

def test_deadlines_do_not_accumulate():
    """14 µs steps on a 10 µs grid realize as 1 step each; scheduled against
    deadlines they still start within half a step of the request."""
    schedule = _realize(_pulses(step_s=14e-6, count=4), _tolerant(scheduling='deadlines'))
    assert [s.start_tick for s in schedule.step_spans()] == [0, 1, 3, 4]


def test_an_iteration_that_would_overrun_its_deadline_is_refused():
    with pytest.raises(ScheduleRefused, match='overruns its deadline'):
        _realize(_pulses(step_s=15e-6, count=4), _tolerant(scheduling='deadlines'))


# ---------------------------------------------------------------------------
# Waits, segments and preparation (§2.4)
# ---------------------------------------------------------------------------

def _waiting(trigger='cameraReady', **wait):
    return ScanProgram(
        loops=(Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 2)),
        drives=(DeviceDrive('X', weights_um={'x': 10.0}, motion=STAGE),),
        step_s=10e-3,
        timeline=(TimelineEvent('Shutter', 'ttl', 0.0, 10e-3, during='any'),),
        waits=(Wait('y', trigger, **wait),),
    )


def test_an_external_wait_begins_a_new_segment():
    schedule = _realize(_waiting())
    assert len(schedule.segments) == 3        # before the first barrier, then one per y
    # before it: only the approach, X's 1 ms move and 1 ms settle (§7.9)
    assert schedule.segments[0].ticks == 200
    assert approach_span(schedule).ticks == 200
    assert {e.segment for e in schedule.events} == {1, 2}
    with pytest.raises(DensifyError):
        densify(schedule, segment=3)


def test_a_time_wait_is_an_ordinary_span():
    schedule = _realize(_waiting('time', interval_s=1e-3))
    assert len(schedule.segments) == 1
    assert [s.ticks for s in schedule.spans if s.span.kind == 'wait'] == [100, 100]


def test_preparation_readies_only_what_needs_it_and_moves_no_timed_event():
    context = RunContext(devices={'Shutter': DeviceFacts(4e-3, 'Shutter.preparation.durationS')})
    schedule = _realize(_waiting(), context=context)
    approach = approach_span(schedule)
    stages = [s for s in schedule.spans if s.span.kind == 'preparation' and s is not approach]
    assert [s.ticks for s in stages] == [400, 400]
    assert all(not s.span.events for s in stages)
    steps = schedule.step_spans()
    shutter = [e for e in schedule.events if e.device == 'Shutter']
    assert [e.start_tick for e in shutter] == [s.start_tick for s in steps]


def test_a_device_whose_preparation_is_unknown_refuses_the_program():
    context = RunContext(devices={'Shutter': DeviceFacts('unknown', 'Shutter: no preparation key')})
    with pytest.raises(InterpretError, match='preparation time is unknown'):
        expand(_waiting(), context)


def test_the_grid_records_where_it_came_from():
    summary = realize(expand(_pulses()), RATE, rate_source='engine capability: test').summary()
    assert {'quantity': 'timing grid', 'value': 1e-5, 'unit': 's',
            'source': 'engine capability: test'} in summary['sources']
    assert np.isclose(summary['grid_s'], 1e-5)


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
