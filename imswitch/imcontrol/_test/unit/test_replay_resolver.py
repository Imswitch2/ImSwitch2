"""Replay resolver: a recording's settings checked against the running setup.

Pure tests of :mod:`imswitch.imcontrol.model.replay` -- no Qt, no hardware.
The matrix covers the same setup, foreign device names of every kind,
different axis names, out-of-range values, old files without an inventory,
new files with one, a different scan type, a device that failed at startup
and numpy-typed (HDF5) against plain (Zarr) values.  The safety rules are
asserted on :func:`replay.buildActions`, the single gate every applied value
passes through.
"""
import json

import numpy as np
import pytest

from imswitch.imcontrol.model import replay


def _inventory(**sections):
    inv = replay.emptyInventory()
    inv.update(sections)
    return inv


LASERS = {
    '488': {'managerClass': 'MockLaser', 'wavelength': 488, 'valueUnits': 'mW',
            'valueRangeMin': 0, 'valueRangeMax': 200, 'isBinary': False},
    '561': {'managerClass': 'MockLaser', 'wavelength': 561, 'valueUnits': 'mW',
            'valueRangeMin': 0, 'valueRangeMax': 100, 'isBinary': False},
    'LED': {'managerClass': 'MockLED', 'wavelength': 0, 'valueUnits': '',
            'valueRangeMin': None, 'valueRangeMax': None, 'isBinary': True},
}
POSITIONERS = {
    'Stage': {'managerClass': 'MockStage', 'axes': ['X', 'Y'], 'units': 'µm',
              'axisRanges': {'X': [0, 1000], 'Y': [0, 1000]}},
    'Focus': {'managerClass': 'MockPiezo', 'axes': ['Z'], 'units': 'µm'},
}
DETECTORS = {
    'Cam': {'managerClass': 'MockCam', 'model': 'mock', 'fullShape': [512, 512],
            'supportedBinnings': [1, 2, 4],
            'parameters': {
                'exposure': {'kind': 'number', 'units': 'ms', 'editable': True},
                'mode': {'kind': 'list', 'options': ['fast', 'slow'], 'editable': True},
                'temperature': {'kind': 'number', 'units': 'C', 'editable': False},
                'Camera pixel size': {'kind': 'number', 'units': 'µm', 'editable': True,
                                      'configOwned': True},
            }},
}
SCAN = {'controller': 'ScanControllerAdvanced', 'scanWidgetType': 'Advanced',
        'positioners': ['Stage', 'Focus'], 'ttlDevices': ['488', '561'],
        'analogKeys': ['target_device', 'axis_length', 'scan_dim_target_device'],
        'digitalKeys': ['target_device', 'pulse_starts_s', 'sequence_time']}


def _snapshot(unavailable=(), lasers=LASERS, positioners=POSITIONERS, detectors=DETECTORS,
              scan=SCAN, rotators=None, laserEnabled=(), setupHash='sha256:current'):
    return replay.SetupSnapshot(
        setupName='current.json', setupHash=setupHash,
        inventory=_inventory(lasers=dict(lasers), positioners=dict(positioners),
                             detectors=dict(detectors), scan=scan,
                             rotators=dict(rotators or {})),
        unavailable=set(unavailable),
        current={('Laser', '488', 'Value'): 10.0, ('Positioner', 'Stage', 'X', 'Position'): 5.0},
        laserEnabled=set(laserEnabled))


def _plan(attrs, snapshot=None, **kw):
    return replay.resolveReplay(replay.parseReplayAttributes(attrs), snapshot or _snapshot(), **kw)


def _value(plan, itemId, key):
    return next(v for v in plan.item(itemId).values if v.key == key)


def _applied(plan):
    return {(a.itemId, a.key): a.value for a in replay.buildActions(plan).actions}


# ------------------------------------------------------------ same setup

def test_same_setup_everything_matches_and_in_range_values_are_proposed():
    plan = _plan({
        'Laser:488:Value': 50.0, 'Laser:488:Enabled': False,
        'Detector:Cam:Binning': 2, 'Detector:Cam:ROI': [0, 0, 256, 256],
        'Detector:Cam:Param:exposure': 20.0, 'Detector:Cam:Param:mode': 'slow',
        'Positioner:Stage:X:Position': 100.0,
        'setup:hash': 'sha256:current',
    })
    assert plan.sameSetup is True
    assert all(item.status == replay.EXACT for item in plan.items)
    applied = _applied(plan)
    assert applied[('laser:488', 'Value')] == 50.0
    assert applied[('detector:Cam', 'Binning')] == 2
    assert applied[('detector:Cam', 'ROI')] == [0, 0, 256, 256]
    assert applied[('detector:Cam', 'Param:mode')] == 'slow'
    # Positions need the opt-in.
    assert ('positioner:Stage:X', 'Position') not in applied
    plan.allowMoves = True
    assert _applied(plan)[('positioner:Stage:X', 'Position')] == 100.0


# ------------------------------------------------------------ decision 1

@pytest.mark.parametrize('enabled', [True, np.bool_(True), 'true', 1])
def test_enabled_state_is_shown_and_never_becomes_an_action(enabled):
    plan = _plan({'Laser:488:Enabled': enabled, 'Laser:488:Value': 5.0})
    check = _value(plan, 'laser:488', 'Enabled')
    assert check.status == replay.V_SHOWN and check.stored == 'on'
    with pytest.raises(ValueError):
        replay.setValueAction(check, replay.APPLY)
    plan.allowMoves = True
    keys = {a.key for a in replay.buildActions(plan).actions}
    assert 'Enabled' not in keys


# ------------------------------------------------------------ decision 3

def test_out_of_range_laser_value_is_highlighted_and_skipped_by_default():
    plan = _plan({'Laser:488:Value': np.float64(1e6)})
    check = _value(plan, 'laser:488', 'Value')
    assert check.status == replay.V_OUT_OF_RANGE
    assert check.action == replay.SKIP
    assert check.range == (0.0, 200.0)
    assert set(check.allowedActions) == {replay.SKIP, replay.CLAMP, replay.EDIT}
    assert _applied(plan) == {}


def test_out_of_range_value_can_only_be_applied_as_clamp_or_in_range_edit():
    plan = _plan({'Laser:488:Value': 1e6})
    check = _value(plan, 'laser:488', 'Value')
    with pytest.raises(ValueError):
        replay.setValueAction(check, replay.APPLY)

    replay.setValueAction(check, replay.CLAMP)
    assert _applied(plan) == {('laser:488', 'Value'): 200.0}

    replay.setValueAction(check, replay.EDIT, 500.0)  # still out of range
    actionSet = replay.buildActions(plan)
    assert actionSet.actions == []
    assert 'outside' in actionSet.skipped[('laser:488', 'Value')]

    replay.setValueAction(check, replay.EDIT, 150.0)
    assert _applied(plan) == {('laser:488', 'Value'): 150.0}


def test_out_of_range_position_is_skipped_by_default_even_with_moves_allowed():
    plan = _plan({'Positioner:Stage:X:Position': 5000.0})
    plan.allowMoves = True
    check = _value(plan, 'positioner:Stage:X', 'Position')
    assert check.status == replay.V_OUT_OF_RANGE and check.action == replay.SKIP
    assert _applied(plan) == {}
    replay.setValueAction(check, replay.CLAMP)
    assert _applied(plan) == {('positioner:Stage:X', 'Position'): 1000.0}


# ------------------------------------------------------------ decision 2

def test_positions_and_rotations_need_the_moves_opt_in():
    snapshot = _snapshot(rotators={'hwp': {'managerClass': 'MockRot'}})
    plan = _plan({'Positioner:Focus:Z:Position': 3.0, 'Rotator:hwp:Position': 45.0}, snapshot)
    assert plan.allowMoves is False
    actionSet = replay.buildActions(plan)
    assert actionSet.actions == []
    assert 'Moves not enabled' in actionSet.skipped[('positioner:Focus:Z', 'Position')]
    assert 'Moves not enabled' in actionSet.skipped[('rotator:hwp', 'Position')]
    plan.allowMoves = True
    assert _applied(plan) == {('positioner:Focus:Z', 'Position'): 3.0,
                              ('rotator:hwp', 'Position'): 45.0}


# ------------------------------------------------------------ decision 4

def test_missing_laser_preselects_only_a_wavelength_match_from_the_inventory():
    attrs = {'Laser:Coherent561:Value': 20.0, 'Laser:Old405:Value': 1.0,
             'setup:inventory': json.dumps({'lasers': {
                 'Coherent561': {'wavelength': 561, 'valueUnits': 'mW'},
                 'Old405': {'wavelength': 405, 'valueUnits': 'mW'}}})}
    plan = _plan(attrs)
    match = plan.item('laser:Coherent561')
    assert match.status == replay.MISSING
    assert match.target == '561'
    assert set(match.candidates) == {'488', '561', 'LED'}  # all lasers offered
    noMatch = plan.item('laser:Old405')
    assert noMatch.target is None
    assert _applied(plan) == {('laser:Coherent561', 'Value'): 20.0}


def test_missing_laser_from_an_old_file_starts_on_skip():
    plan = _plan({'Laser:Coherent561:Value': 20.0})  # no inventory: no wavelength
    item = plan.item('laser:Coherent561')
    assert item.status == replay.MISSING and item.target is None
    assert item.candidates
    assert _applied(plan) == {}
    plan.retarget('laser:Coherent561', '561')
    assert _applied(plan) == {('laser:Coherent561', 'Value'): 20.0}


def test_ambiguous_wavelength_match_is_not_preselected():
    lasers = dict(LASERS, **{'561b': dict(LASERS['561'])})
    attrs = {'Laser:X:Value': 1.0,
             'setup:inventory': {'lasers': {'X': {'wavelength': 561}}}}
    plan = _plan(attrs, _snapshot(lasers=lasers))
    assert plan.item('laser:X').target is None
    assert 'Several lasers' in plan.item('laser:X').note


def test_same_name_laser_with_another_wavelength_is_a_capability_mismatch():
    attrs = {'Laser:488:Value': 10.0,
             'setup:inventory': {'lasers': {'488': {'wavelength': 491, 'valueUnits': 'mW'}}}}
    item = _plan(attrs).item('laser:488')
    assert item.status == replay.CAPABILITY_MISMATCH and item.target is None


def test_value_in_other_units_is_never_applied_unconverted():
    attrs = {'Laser:488:Value': 1.5, 'Laser:488:Units': 'V'}
    plan = _plan(attrs)
    assert plan.item('laser:488').status == replay.CAPABILITY_MISMATCH
    plan.retarget('laser:488', '488')
    assert _value(plan, 'laser:488', 'Value').status == replay.V_INVALID
    assert _applied(plan) == {}


def test_binary_target_takes_no_power_value():
    plan = _plan({'Laser:Old:Value': 5.0})
    plan.retarget('laser:Old', 'LED')
    assert _value(plan, 'laser:Old', 'Value').status == replay.V_INVALID
    assert _applied(plan) == {}


def test_two_stored_lasers_cannot_drive_the_same_target():
    plan = _plan({'Laser:A:Value': 5.0, 'Laser:B:Value': 6.0})
    plan.retarget('laser:A', '561')
    plan.retarget('laser:B', '561')
    actionSet = replay.buildActions(plan)
    assert [(a.itemId, a.value) for a in actionSet.actions] == [('laser:A', 5.0)]
    assert 'already receives' in actionSet.skipped[('laser:B', 'Value')]


def test_retarget_refuses_a_device_that_is_not_a_candidate():
    plan = _plan({'Laser:Coherent561:Value': 20.0})
    with pytest.raises(ValueError):
        plan.retarget('laser:Coherent561', 'Stage')


# ------------------------------------------------------------ positioners

def test_foreign_positioner_and_axis_names_offer_every_axis():
    plan = _plan({'Positioner:XYStage:X:Position': 1.0, 'Positioner:Stage:Z:Position': 2.0})
    foreign = plan.item('positioner:XYStage:X')
    assert foreign.status == replay.MISSING and foreign.target is None
    assert set(foreign.candidates) == {'Stage:X', 'Stage:Y', 'Focus:Z'}
    otherAxis = plan.item('positioner:Stage:Z')
    assert otherAxis.status == replay.CAPABILITY_MISMATCH
    plan.allowMoves = True
    plan.retarget('positioner:Stage:Z', 'Focus:Z')
    assert _applied(plan) == {('positioner:Stage:Z', 'Position'): 2.0}


# ------------------------------------------------------------ detectors

def test_detector_values_are_checked_against_the_target_detector():
    plan = _plan({
        'Detector:Cam:Binning': 3,
        'Detector:Cam:ROI': [0, 0, 1024, 1024],
        'Detector:Cam:Param:mode': 'turbo',
        'Detector:Cam:Param:temperature': -20,
        'Detector:Cam:Param:Camera pixel size': 0.1,
        'Detector:Cam:Param:nonexistent': 1,
        'Detector:Cam:Param:exposure': np.float32(12.5),
    })
    statuses = {v.key: v.status for v in plan.item('detector:Cam').values}
    assert statuses['Binning'] == replay.V_INVALID
    assert statuses['ROI'] == replay.V_INVALID
    assert statuses['Param:mode'] == replay.V_INVALID
    assert statuses['Param:temperature'] == replay.V_SHOWN
    assert statuses['Param:Camera pixel size'] == replay.V_SHOWN
    assert statuses['Param:nonexistent'] == replay.V_INVALID
    assert _applied(plan) == {('detector:Cam', 'Param:exposure'): 12.5}


def test_foreign_detector_name_is_offered_the_current_detectors():
    plan = _plan({'Detector:Orca:Binning': 2})
    item = plan.item('detector:Orca')
    assert item.status == replay.MISSING and item.target is None and item.candidates == ['Cam']
    plan.retarget('detector:Orca', 'Cam')
    assert _applied(plan) == {('detector:Orca', 'Binning'): 2}


# ------------------------------------------------------------ scan / TTL

_SCAN_ATTRS = {
    'ScanStage:target_device': ['Stage', 'Focus'],
    'ScanStage:axis_length': [10.0, 5.0],
    'ScanStage:positive_direction': [True, True],  # derived: never applied
    'ScanTTL:target_device': ['488'],
    'ScanTTL:pulse_starts_s': {'488': [[0.0]]},
    'ScanTTL:sequence_time': 1e-4,
}


def test_matching_scan_is_one_atomic_action():
    plan = _plan(dict(_SCAN_ATTRS))
    scanActions = [a for a in replay.buildActions(plan).actions if a.kind == replay.SCAN]
    assert len(scanActions) == 1
    params = scanActions[0].value
    assert 'positive_direction' not in params['analog']
    assert params['analog']['target_device'] == ['Stage', 'Focus']
    assert params['digital']['pulse_starts_s'] == {'488': [[0.0]]}


def test_foreign_ttl_line_can_be_remapped_and_the_scan_follows():
    attrs = dict(_SCAN_ATTRS, **{'ScanTTL:target_device': ['OldLaser'],
                                 'ScanTTL:pulse_starts_s': {'OldLaser': [[0.0]]}})
    plan = _plan(attrs)
    assert plan.item('ttl:OldLaser').status == replay.MISSING
    # An unresolved TTL line blocks the whole scan (all-or-nothing).
    assert not [a for a in replay.buildActions(plan).actions if a.kind == replay.SCAN]
    plan.retarget('ttl:OldLaser', '561')
    scan = next(a for a in replay.buildActions(plan).actions if a.kind == replay.SCAN)
    assert scan.value['digital']['target_device'] == ['561']
    assert scan.value['digital']['pulse_starts_s'] == {'561': [[0.0]]}


def test_different_scan_type_is_skipped_by_default():
    attrs = dict(_SCAN_ATTRS, **{'setup:inventory': {'scan': {'scanWidgetType': 'MoNaLISA'}}})
    plan = _plan(attrs)
    item = plan.item('scan:parameters')
    assert item.status == replay.CAPABILITY_MISMATCH and item.target is None
    assert not [a for a in replay.buildActions(plan).actions if a.kind == replay.SCAN]


def test_old_file_with_unknown_scan_keys_is_skipped_by_default():
    attrs = dict(_SCAN_ATTRS, **{'ScanStage:monalisa_only_key': 1})
    plan = _plan(attrs)
    assert plan.item('scan:parameters').target is None
    plan.retarget('scan:parameters', 'Scan')
    scan = next(a for a in replay.buildActions(plan).actions if a.kind == replay.SCAN)
    assert 'monalisa_only_key' not in scan.value['analog']


def test_setup_without_scan_or_lasers_reports_no_such_kind():
    plan = _plan(dict(_SCAN_ATTRS, **{'Laser:488:Value': 1.0}),
                 _snapshot(lasers={}, scan=None))
    assert plan.item('laser:488').status == replay.NO_SUCH_KIND
    assert plan.item('scan:parameters').status == replay.NO_SUCH_KIND
    assert replay.buildActions(plan).actions == []


# ------------------------------------------------------------ startup failures

def test_device_that_failed_at_startup_is_not_a_target():
    # Present but not connected (main), or absent from the manager (#96).
    plan = _plan({'Laser:488:Value': 5.0, 'Positioner:Stage:X:Position': 1.0},
                 _snapshot(unavailable={('laser', '488'), ('positioner', 'Stage')}))
    assert plan.item('laser:488').target is None
    assert '488' not in plan.item('laser:488').candidates
    assert all(not c.startswith('Stage:') for c in plan.item('positioner:Stage:X').candidates)
    absent = _plan({'Laser:488:Value': 5.0},
                   _snapshot(lasers={k: v for k, v in LASERS.items() if k != '488'}))
    assert absent.item('laser:488').status == replay.MISSING
    assert _applied(absent) == {}


# ------------------------------------------------------------ value types

def test_numpy_values_from_hdf5_and_plain_values_from_zarr_resolve_alike():
    hdf5 = {'Laser:488:Value': np.float64(50), 'Detector:Cam:ROI': np.array([0, 0, 64, 64]),
            'Detector:Cam:Binning': np.int64(2), 'Laser:488:Enabled': np.bool_(False)}
    zarr = {'Laser:488:Value': 50.0, 'Detector:Cam:ROI': [0, 0, 64, 64],
            'Detector:Cam:Binning': 2, 'Laser:488:Enabled': False}
    a, b = _applied(_plan(hdf5)), _applied(_plan(zarr))
    assert a == b
    assert all(type(v) in (int, float, list, str) for v in a.values())


def test_old_file_without_metadata_is_matched_by_name():
    plan = _plan({'Laser:488:Value': 5.0, 'acquisition:software_version': '1.0'})
    assert plan.parsed.hasInventory is False and plan.sameSetup is None
    assert plan.item('laser:488').status == replay.EXACT


# ------------------------------------------------------------ unattended

def test_unattended_policy_applies_only_exact_in_range_values():
    attrs = {'Laser:488:Value': 50.0, 'Laser:561:Value': 1e6, 'Laser:Foreign:Value': 1.0,
             'Positioner:Stage:X:Position': 1.0,
             'setup:inventory': {'lasers': {'Foreign': {'wavelength': 488}}}}
    plan = replay.applyUnattendedPolicy(_plan(attrs))
    assert _applied(plan) == {('laser:488', 'Value'): 50.0}
    plan = replay.applyUnattendedPolicy(_plan(attrs), allowMoves=True)
    assert _applied(plan) == {('laser:488', 'Value'): 50.0,
                              ('positioner:Stage:X', 'Position'): 1.0}


# ------------------------------------------------------------ memory / provenance

def test_remembered_targets_prefill_but_never_override_exact_matches():
    plan = _plan({'Laser:Old:Value': 1.0, 'Laser:488:Value': 2.0},
                 rememberedTargets={'laser:Old': '561', 'laser:488': '561'})
    assert plan.item('laser:Old').target == '561'
    assert plan.item('laser:488').target == '488'


def test_provenance_records_substitutions_and_skips_without_paths():
    parsed = replay.parseReplayAttributes({'Laser:Old:Value': 1.0, 'Laser:561:Value': 1e6,
                                           'setup:hash': 'sha256:src', 'setup:name': 'rigA.json',
                                           'acquisition:software_version': '2.1'})
    parsed.sourceFile, parsed.sourceDataset, parsed.sourceFormat = 'scan.h5', 'Cam', 'HDF5'
    plan = replay.resolveReplay(parsed, _snapshot())
    plan.retarget('laser:Old', '488')
    result = replay.ReplayResult(
        applied=[('laser:Old', 'Value', 1.0, '')],
        skipped=[('laser:561', 'Value', 'Outside 0 to 100 mW.')])
    prov = replay.buildProvenance(plan, result, appliedAt='t', mode='interactive')
    assert prov[('replay', 'source_file')] == 'scan.h5'
    assert prov[('replay', 'source_setup_hash')] == 'sha256:src'
    assert prov[('replay', 'source_software_version')] == '2.1'
    mapping = {m['id']: m for m in json.loads(prov[('replay', 'mapping')])}
    assert mapping['laser:Old']['target'] == '488'
    assert mapping['laser:Old']['applied'] == {'Value': 1.0}
    assert mapping['laser:561']['skipped'] == {'Value': 'Outside 0 to 100 mW.'}


# ------------------------------------------------------------ setup identity

def test_setup_hash_ignores_shortcuts_and_is_stable():
    a = replay.computeSetupHash({'lasers': {'488': {'port': 'COM3'}}, 'shortcuts': {'x': 'A'}})
    b = replay.computeSetupHash({'shortcuts': {'x': 'B'}, 'lasers': {'488': {'port': 'COM3'}}})
    c = replay.computeSetupHash({'lasers': {'488': {'port': 'COM4'}}})
    assert a == b != c
    assert a.startswith('sha256:') and 'COM3' not in a


@pytest.mark.parametrize('name,sensitive', [
    ('serialNumber', True), ('serial_number', True), ('port', True), ('comPort', True),
    ('calibCsvPath', True), ('ipAddress', True), ('host', True), ('correctionPatternsDir', True),
    ('supportsAnalog', False), ('flipMirrors', False), ('description', False),
    ('isPositiveDirection', False), ('pinMap', False), ('wavelength', False),
])
def test_sensitive_field_detection(name, sensitive):
    assert replay.isSensitiveField(name) is sensitive
