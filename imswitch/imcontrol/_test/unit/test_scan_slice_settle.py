"""A slice axis's settle time is declared on its positioner (scan timing
plan, S5): ``managerProperties.settleTimeS``. GalvoScanDesigner never pauses
a slice (D3) for less, records the pause as played beside the request, and
the Advanced panel's timing note says when it raised D3. The D3 field stays
as an optional longer pause."""
import copy

import pytest

from imswitch.imcontrol.model.configeditor import resources, roles as roles_module
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    SETTLE_TIME_KEY, GalvoScanDesigner, slice_settle_floor_us, slice_settle_note,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like


def _setup(settle_s=None):
    setup = _setup_sted_like()
    if settle_s is not None:
        setup.positioners['PiezoZ'].managerProperties[SETTLE_TIME_KEY] = settle_s
    return setup


def _xyz(d3_us=0.0):
    params = _params(['GalvoX', 'GalvoY', 'PiezoZ'], [5.0, 5.0, 2.0], [1.0, 1.0, 1.0])
    params['d3step_delay'] = d3_us
    return params


def test_a_declared_settle_is_the_slice_pause_floor_and_is_recorded():
    _sig, _pos, info = GalvoScanDesigner().make_signal(_xyz(0), _setup(settle_s=2e-3))
    assert info['d3step_delay_us'] == 2000 and info['d3step_delay_requested_us'] == 0
    _sig, _pos, longer = GalvoScanDesigner().make_signal(_xyz(5000), _setup(settle_s=2e-3))
    assert longer['d3step_delay_us'] == 5000 and longer['d3step_delay_requested_us'] is None
    _sig, _pos, plain = GalvoScanDesigner().make_signal(_xyz(0), _setup())
    assert plain['d3step_delay_us'] == 0 and plain['d3step_delay_requested_us'] is None
    # the pause is in the waveform: the settling before each slice's first
    # line grows by the 2 ms (200 samples at 100 kHz)
    assert info['scan_throw_settling'] - plain['scan_throw_settling'] == 200


def test_only_the_slice_axes_count():
    setup = _setup()
    setup.positioners['GalvoX'].managerProperties[SETTLE_TIME_KEY] = 1.0   # the fast axis
    assert slice_settle_floor_us(setup, ['PiezoZ']) == (0, None)
    _sig, _pos, info = GalvoScanDesigner().make_signal(_xyz(0), setup)
    assert info['d3step_delay_us'] == 0
    assert slice_settle_floor_us(_setup(settle_s=1.5e-6), ['PiezoZ']) == (2, 'PiezoZ')  # up


def test_the_timing_note_says_when_d3_was_raised():
    note = slice_settle_note(_xyz(0), _setup(settle_s=2e-3))
    assert note == ('D3 0 µs runs as 2000 µs: PiezoZ declares settleTimeS 0.002 s, and a '
                    'slice never starts before it has settled.')
    assert slice_settle_note(_xyz(5000), _setup(settle_s=2e-3)) == ''
    two_d = _params(['GalvoX', 'GalvoY'], [5.0, 5.0], [1.0, 1.0])
    two_d['d3step_delay'] = 0
    assert slice_settle_note(two_d, _setup(settle_s=2e-3)) == ''      # no slice axis


def test_the_role_rule_offers_the_key_the_designer_reads():
    [role] = [r for r in resources.roles() if r['role'] == 'galvo_slice_axis']
    assert [read['key'] for read in role['reads']] == [SETTLE_TIME_KEY]
    assert SETTLE_TIME_KEY in role['properties']
    assert role['requires'] == []                  # offered, never required
    setup = {'scan': {'scanDesigner': 'GalvoScanDesigner'},
             'positioners': {'Z': {'forScanning': True, 'managerProperties': {}},
                             'Park': {'forScanning': False, 'managerProperties': {}}}}

    def offered(data, name):
        return roles_module.holds(role['when'], device=data['positioners'][name], name=name,
                                  setup=data)

    assert offered(setup, 'Z') and not offered(setup, 'Park')
    beta = copy.deepcopy(setup)
    beta['scan']['scanDesigner'] = 'BetaScanDesigner'
    assert not offered(beta, 'Z')
    # an optional read without a fallback: the validator reports nothing
    # (it notes only reads with a declared fallback)
    [finding] = roles_module.evaluate(role, setup)
    assert finding.missing_required == ()
    assert [(r.key, r.fallback) for r in finding.missing_reads] == [(SETTLE_TIME_KEY, None)]
