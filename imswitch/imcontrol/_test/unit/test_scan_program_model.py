"""The scan-program model: what a program says and how it is visited
(docs/design/plans/scan-engines-and-expert-scan.md §1, §2.1).

Loops are chronological counters, separate from the devices they drive; a
device position is an affine map of the counters (or a table). So an
interleave (MS-RESOLFT's cycles x planes), a composite axis and a serpentine
are ordinary programs, not special features.
"""

import json

import pytest

from imswitch.imcontrol.model.scan_program import (
    DetectorBinding, DeviceDrive, InterpretError, Loop, Motion, ProgramError, RunContext,
    ScanProgram, TimelineEvent, Wait, expand, validate, visits,
)
from imswitch.imcontrol.model.scan_program.interpreter import position_um
from imswitch.imcontrol.model.scan_program.preparation import (
    LEGACY_RULE, PreparationError, resolve_preparation,
)

ABSOLUTE = Motion(origin='absolute', restore='first')


def _program(loops, drives, **extra):
    return ScanProgram(loops=tuple(loops), drives=tuple(drives),
                       step_s=extra.pop('step_s', 1e-3), **extra)


# ---------------------------------------------------------------------------
# Visiting order
# ---------------------------------------------------------------------------

def test_an_interleave_is_two_loops_driving_one_device():
    """2 cycles x 3 planes visit 0, 2, 4, then 1, 3, 5 (Lenny's 1,3,5,2,4,6
    counted from 1): position index = cycles * plane + cycle."""
    dy = 0.25
    program = _program(
        [Loop('cycle', 'cycle', 2), Loop('plane', 'plane', 3)],
        [DeviceDrive('RO', weights_um={'cycle': dy, 'plane': 2 * dy}, motion=ABSOLUTE)],
    )
    order = [position_um(program.drives[0], v, RunContext()) / dy for v in visits(program)]
    assert order == [0, 2, 4, 1, 3, 5]


def test_a_composite_axis_drives_several_devices_from_one_loop():
    program = _program(
        [Loop('s', 'scan_y', 3)],
        [DeviceDrive('GalvoY', weights_um={'s': 2.0}, motion=ABSOLUTE),
         DeviceDrive('PiezoZ', offset_um=10.0, weights_um={'s': -0.5}, motion=ABSOLUTE)],
    )
    positions = [{d.device: position_um(d, v, RunContext()) for d in program.drives}
                 for v in visits(program)]
    assert positions == [{'GalvoY': 0.0, 'PiezoZ': 10.0}, {'GalvoY': 2.0, 'PiezoZ': 9.5},
                         {'GalvoY': 4.0, 'PiezoZ': 9.0}]


def test_traversals():
    loops = [Loop('y', 'scan_y', 2), Loop('x', 'scan_x', 3, traversal='serpentine',
                                          parity_loops=('y',))]
    assert [(v['y'], v['x']) for v in visits(_program(loops, []))] == \
        [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]
    reverse = [Loop('x', 'scan_x', 3, traversal='reverse')]
    assert [v['x'] for v in visits(_program(reverse, []))] == [2, 1, 0]


def test_an_explicit_order_is_a_table():
    program = _program(
        [Loop('x', 'scan_x', 4)],
        [DeviceDrive('X', table_loop='x', table_um=(0.0, 2.0, 1.0, 3.0), motion=ABSOLUTE)],
    )
    assert [position_um(program.drives[0], v, RunContext()) for v in visits(program)] == \
        [0.0, 2.0, 1.0, 3.0]


def test_a_position_relative_to_the_capture_needs_the_capture():
    program = _program([Loop('x', 'scan_x', 2)],
                       [DeviceDrive('X', weights_um={'x': 1.0}, motion=Motion())])
    with pytest.raises(InterpretError, match='not captured'):
        expand(program, RunContext())
    expansion = expand(program, RunContext(captured_um={'X': 50.0}))
    assert expansion.first_um == {'X': 50.0} and expansion.restore_um == {'X': 50.0}


# ---------------------------------------------------------------------------
# Validation lists every problem
# ---------------------------------------------------------------------------

def test_a_malformed_program_is_refused_with_every_problem():
    program = ScanProgram(
        loops=(Loop('x', 'scan_x', 0), Loop('x', 'bogus', 2, traversal='serpentine')),
        drives=(DeviceDrive('X', weights_um={'nope': 1.0}),),
        step_s=1e-3,
        timeline=(TimelineEvent('L488', 'ttl', 0.5e-3, 2e-3),
                  TimelineEvent('Z', 'offset', 0.1e-3, 0.2e-3)),
        detectors=(DetectorBinding('APD', events='missing'),),
        waits=(Wait('step', 'time'),),
    )
    with pytest.raises(ProgramError) as refused:
        validate(program)
    text = str(refused.value)
    for problem in ('Loop ids must be unique', "'bogus' is not one of", 'count must be at least 1',
                    'serpentine traversal names its parity loops', "unknown loop 'nope'",
                    'outside its step', 'needs a value', "needs a drive for 'Z'",
                    "unknown events 'missing'", 'needs a positive interval_s'):
        assert problem in text, problem


def test_an_event_names_a_known_anchor():
    program = _program([Loop('x', 'scan_x', 2)], [],
                       timeline=(TimelineEvent('clock', 'marker', 0.0, 1e-5,
                                               anchor='pass-start:nope'),))
    with pytest.raises(ProgramError, match="unknown anchor 'pass-start:nope'"):
        validate(program)


# ---------------------------------------------------------------------------
# Preparation keys (§2.4)
# ---------------------------------------------------------------------------

def test_preparation_has_three_states_and_absence_is_not_zero():
    new = dict(setup_has_scan_engines=True)
    assert resolve_preparation('Shutter', 'shutter', {}, **new).preparation == 'unknown'
    assert resolve_preparation('Z', 'positioner', {}, **new).preparation == 'notRequired'
    known = resolve_preparation('Shutter', 'shutter', {'preparation': {'durationS': 0.004}}, **new)
    assert known.preparation == 0.004 and known.source == 'Shutter.preparation.durationS'
    assert resolve_preparation('L', 'laser', {'preparation': 'unknown'}, **new).preparation \
        == 'unknown'
    with pytest.raises(PreparationError):
        resolve_preparation('L', 'laser', {'preparation': 0.004}, **new)


def test_a_legacy_setup_keeps_todays_behaviour_by_a_named_rule():
    facts = resolve_preparation('Shutter', 'shutter', {}, setup_has_scan_engines=False)
    assert facts.preparation == 'notRequired' and LEGACY_RULE in facts.source


def test_the_program_round_trips_through_json_shapes():
    """Programs are plain data: a saved program is its dataclass fields."""
    program = _program([Loop('x', 'scan_x', 2)],
                       [DeviceDrive('X', weights_um={'x': 1.0}, motion=ABSOLUTE)])
    text = json.dumps({'loops': [vars(loop) for loop in program.loops]})
    assert json.loads(text)['loops'][0]['count'] == 2


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
