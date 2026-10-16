"""The pause before every slice of a 3-D scan is declared, not typed per scan
(scan timing plan, S5; Lenny: "remove D3 field"): a slice positioner's
``managerProperties.settleTimeS``. GalvoScanDesigner pauses every slice for
the longest declared value -- that, a user setup's legacy
``scanDesignerParams.d3step_delay``, or a per-scan ``d3step_delay`` the old
PointScan panel or a script still passes -- records the pause and its
source, and the Advanced panel's timing note says it."""
import copy
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.configeditor import resources, roles as roles_module
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    SETTLE_TIME_KEY, GalvoScanDesigner, slice_pause_note, slice_pause_us,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like


def _setup(settle_s=None, legacy_us=None):
    setup = _setup_sted_like()
    if settle_s is not None:
        setup.positioners['PiezoZ'].managerProperties[SETTLE_TIME_KEY] = settle_s
    if legacy_us is not None:
        setup.scan.scanDesignerParams = {'d3step_delay': legacy_us}
    return setup


def _xyz(**extra):
    params = _params(['GalvoX', 'GalvoY', 'PiezoZ'], [5.0, 5.0, 2.0], [1.0, 1.0, 1.0])
    params.update(extra)
    return params


def test_the_declared_settle_is_the_slice_pause_and_is_recorded_with_its_source():
    _sig, _pos, info = GalvoScanDesigner().make_signal(_xyz(), _setup(settle_s=2e-3))
    assert info['slice_pause_us'] == 2000
    assert info['slice_pause_source'] == 'PiezoZ declares settleTimeS 0.002 s'
    _sig, _pos, plain = GalvoScanDesigner().make_signal(_xyz(), _setup())
    assert plain['slice_pause_us'] == 0 and plain['slice_pause_source'] == ''
    # the pause is in the waveform: the settling before each slice's first
    # line grows by the 2 ms (200 samples at 100 kHz)
    assert info['scan_throw_settling'] - plain['scan_throw_settling'] == 200


def test_the_longest_declaration_wins_whatever_declares_it():
    setup = _setup(settle_s=2e-3, legacy_us=500)
    assert slice_pause_us({}, setup, ['PiezoZ'])[0] == 2000
    assert slice_pause_us({'d3step_delay': 5000}, setup, ['PiezoZ']) == (
        5000, "this scan's d3step_delay is 5000 µs")
    legacy = slice_pause_us({}, _setup(legacy_us=500), ['PiezoZ'])
    assert legacy[0] == 500 and 'declare settleTimeS on the slice positioner instead' in legacy[1]
    assert slice_pause_us({}, _setup(settle_s=1.5e-6), ['PiezoZ']) == (
        2, 'PiezoZ declares settleTimeS 1.5e-06 s')                         # rounded up


def test_only_the_slice_axes_count():
    setup = _setup()
    setup.positioners['GalvoX'].managerProperties[SETTLE_TIME_KEY] = 1.0   # the fast axis
    assert slice_pause_us({}, setup, ['PiezoZ']) == (0, '')
    # a 2-D scan has no slice axis; a D3 value still counts (before its first line)
    assert slice_pause_us({'d3step_delay': 300}, setup, []) == (
        300, "this scan's d3step_delay is 300 µs")
    _sig, _pos, info = GalvoScanDesigner().make_signal(_xyz(), setup)
    assert info['slice_pause_us'] == 0


def test_the_timing_note_says_the_pause_or_that_there_is_none():
    assert slice_pause_note(_xyz(), _setup(settle_s=2e-3)) == (
        'Every slice starts after a 2000 µs pause: PiezoZ declares settleTimeS 0.002 s.')
    assert slice_pause_note(_xyz(), _setup()) == (
        'Slices start without a settle pause: no slice positioner declares settleTimeS.')
    two_d = _params(['GalvoX', 'GalvoY'], [5.0, 5.0], [1.0, 1.0])
    assert slice_pause_note(two_d, _setup(settle_s=2e-3)) == ''          # no slice axis
    assert slice_pause_note(dict(two_d, d3step_delay=500), _setup()) == (
        "The scan starts after a 500 µs pause: this scan's d3step_delay is 500 µs.")


def test_a_d3_value_still_settles_a_2d_scan_before_its_first_line():
    """Before the D3 field went, its value was part of the settling before
    a 2-D scan's first line (scan_throw_settling); a setup or a script that
    still passes one keeps it (review 2026-10-16)."""
    two_d = _params(['GalvoX', 'GalvoY'], [5.0, 5.0], [1.0, 1.0])
    _sig, _pos, plain = GalvoScanDesigner().make_signal(two_d, _setup())
    _sig, _pos, settled = GalvoScanDesigner().make_signal(dict(two_d, d3step_delay=500), _setup())
    assert settled['scan_throw_settling'] - plain['scan_throw_settling'] == 50   # 500 µs at 100 kHz
    _sig, _pos, legacy = GalvoScanDesigner().make_signal(two_d, _setup(legacy_us=500))
    assert legacy['scan_throw_settling'] == settled['scan_throw_settling']


def test_a_stepped_fast_axis_plays_no_pause_and_says_so():
    setup = _setup(settle_s=2e-3)
    setup.positioners['GalvoX'].managerProperties['smoothScan'] = False
    _sig, _pos, info = GalvoScanDesigner().make_signal(_xyz(), setup)
    assert info['slice_pause_us'] == 0
    assert info['slice_pause_source'].endswith('not played: the fast axis is stepped')
    assert slice_pause_note(_xyz(), setup) == (
        'A 2000 µs pause is declared (PiezoZ declares settleTimeS 0.002 s) but not played: '
        'the fast axis GalvoX is stepped.')


def test_the_advanced_panel_has_no_d3_field_and_writes_no_d3_key(qtbot):
    from imswitch.imcontrol._test import optionsBasic
    from imswitch.imcontrol.model.scan_parameters import AdvancedScanParameterSerializer
    from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
    widget = ScanWidgetAdvanced(optionsBasic)
    qtbot.addWidget(widget)
    assert not hasattr(widget, 'd3StepDelayPar') and 'frameDelay' not in widget.scanPar
    assert not hasattr(widget, 'setd3StepDelayPar')
    reader = SimpleNamespace(getSeqTimePar=lambda: 0.02, getPhaseDelayPar=lambda: 30.0,
                             getScanDim=lambda i: 'None')
    analog, _ = AdvancedScanParameterSerializer().build_analog(reader, positioners=[])
    assert 'd3step_delay' not in analog and analog['phase_delay'] == 30.0


def test_the_role_rule_offers_the_key_the_designer_reads():
    [role] = [r for r in resources.roles() if r['role'] == 'galvo_slice_axis']
    assert role['consumer'].endswith(':slice_pause_us')
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
    [finding] = roles_module.evaluate(role, setup)
    assert finding.missing_required == ()
    assert [(r.key, r.fallback) for r in finding.missing_reads] == [(SETTLE_TIME_KEY, None)]
