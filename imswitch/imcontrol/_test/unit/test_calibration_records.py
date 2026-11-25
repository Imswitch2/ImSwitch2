"""Calibration records, the legacy CSV format, the store, matching and the
percent applicator -- no Qt, no hardware."""
import json
import math
from dataclasses import replace

import numpy as np
import pytest

from imswitch.imcontrol.model.calibrations import (
    DIFFERENT,
    INCOMPATIBLE,
    MATCH,
    NAME_DIFFERS,
    OTHER_DEVICE,
    RAW_UNITS_ID,
    SAME,
    UNKNOWN,
    Calibration,
    CalibrationFormatError,
    CalibrationInvalid,
    CalibrationRangeError,
    CalibrationStore,
    LaserDescriptor,
    LaserPowerTable,
    compare_calibration_identity,
    export_legacy_csv,
    format_power,
    match_to_laser,
    parse_legacy_csv,
    resolve_laser_calibration,
    validate_laser_power,
)
from imswitch.imcontrol.model.calibrations.display import laser_calibration_tooltip
from imswitch.imcontrol.model.calibrations.setup_refs import (
    read_setup_selection,
    write_setup_selection,
)

# The measureLaserPowerLut writer (imcommon/algorithms/power_lut.py): W.
MEASURED_W = """# format: imswitch calibCsvPath LUT (raw drive -> optical power)
# laser: 775
# setting unit: V
# laser wavelength nm: 775.0
# meter: PM100D (simulated) P0011748
# meter wavelength nm: 775.0
# plane: BFP
# zeroed in the dark: True
# samples per point: 3
# source run: run-20261125-072141-7df8fb (run-20261125-072141-7df8fb.run.h5)
# created: 2026-11-25T07:21:41.344001+00:00
# imswitch version: 0.4.0
# notes:
# columns: setting power_W  (calibCsvPath LUT; fitted, strictly increasing)
0 1.61741784e-05
0.5 0.000211601106
1 0.000779765189
2.5 0.00401603818
5 0.00801597146
"""

# The AA AOTF utility (utility_scripts/aa_aotf_calibration.py): mW, footer notes.
AA_MW = """# Raw AOTF power, measured optical power [mW] | laser=561AOTF, wavelength=561 nm, frequency=96.5 MHz
0.000000000000000000e+00 1.000000000000000021e-02
5.000000000000000000e+02 2.500000000000000000e+00
1.000000000000000000e+03 5.000000000000000000e+00
# aligned after fibre change | ND removed
"""

PLAIN = """0 0
1 2
3 7
5 10
"""


def test_measure_writer_header_is_read():
    record = parse_legacy_csv(MEASURED_W)
    assert record.input.unit == 'V' and record.output.unit == 'W'
    assert record.device.name == '775' and record.device.wavelengthNm == 775.0
    assert record.measuredAt == 'BFP'
    assert record.provenance.tool == 'measureLaserPowerLut'
    assert record.provenance.referenceInstrument == 'PM100D (simulated) P0011748'
    assert record.provenance.sourceRun.startswith('run-20261125')
    assert ('zeroed in the dark', 'True') in record.provenance.header
    assert record.validRange == (0.0, 5.0)


def test_aa_utility_header_and_footer_are_read():
    record = parse_legacy_csv(AA_MW)
    assert record.input.unit == 'arb' and record.output.unit == 'mW'
    assert record.device.name == '561AOTF' and record.device.wavelengthNm == 561.0
    assert record.comment == 'aligned after fibre change | ND removed'
    assert ('frequency', '96.5 MHz') in record.provenance.header
    assert record.provenance.tool == 'aa_aotf_calibration utility'


def test_a_file_that_says_nothing_gets_no_power_unit_and_the_assumed_drive_unit():
    record = parse_legacy_csv(PLAIN, drive_unit='V')
    assert record.output.unit == ''              # never guessed
    assert record.input.unit == 'V'
    assert ('drive unit assumed', 'V (the file does not say)') in record.provenance.header
    table = LaserPowerTable(record, drive_unit='V')
    assert not table.hasAbsolutePower and table.power_for_percent(50) is None


@pytest.mark.parametrize('text', [MEASURED_W, AA_MW, PLAIN])
def test_csv_round_trip_is_lossless(text):
    record = parse_legacy_csv(text, drive_unit='V', source_file='/data/orig.csv')
    record = replace(record, id='cal-1')
    again = parse_legacy_csv(export_legacy_csv(record))
    assert again == record                       # every field, every number
    assert again.contentHash() == record.contentHash()
    # and the exported rows are still what np.loadtxt (the old loader) reads
    import io
    np.testing.assert_array_equal(np.loadtxt(io.StringIO(export_legacy_csv(record))),
                                  np.asarray(record.table))


@pytest.mark.parametrize('text', [MEASURED_W, AA_MW])
def test_json_round_trip(text):
    record = replace(parse_legacy_csv(text), id='cal-2')
    assert Calibration.from_json(record.to_json()) == record
    body = json.loads(record.to_json())
    assert body['schema'] == 'imswitch.calibration' and body['contentHash'] == record.contentHash()


def test_hash_ignores_annotations_but_not_numbers_or_units():
    record = replace(parse_legacy_csv(MEASURED_W), id='cal-3')
    base = record.contentHash()
    assert record.withAnnotations(comment='new comment', measuredAt='sample plane') \
        .contentHash() == base
    assert replace(record, id='other', device=replace(record.device, name='x')).contentHash() == base
    assert replace(record, data=dict(record.data, columns=['a', 'b'])).contentHash() == base
    table = record.table
    table[1][1] *= 1.01
    assert replace(record, data=dict(record.data, table=table)).contentHash() != base
    assert replace(record, output=replace(record.output, unit='mW')).contentHash() != base


def test_a_hand_edited_json_is_refused_with_the_reason(tmp_path):
    record = replace(parse_legacy_csv(MEASURED_W), id='cal-4')
    body = json.loads(record.to_json())
    body['data']['table'][0][1] = 1.0
    with pytest.raises(CalibrationFormatError, match='content hash'):
        Calibration.from_dict(body)


@pytest.mark.parametrize('text, reason', [
    ('0 1\n1 1\n2 1\n', 'constant'),
    ('0 0\n', 'at least two'),
    ('0 0\n1 5\n2 3\n3 9\n', 'not monotonic'),
    ('0 0\n1 1\n1 2\n', 'more than once'),
    ('0 0\n1 nan\n', 'non-finite'),
])
def test_invalid_curves_are_refused(text, reason):
    record = parse_legacy_csv(text)
    assert any(reason in p for p in validate_laser_power(record))
    with pytest.raises(CalibrationInvalid):
        LaserPowerTable(record)


@pytest.mark.parametrize('text', ['', 'a b\n1 2\n', '1\n2\n3\n'])
def test_unreadable_files_are_format_errors(text):
    with pytest.raises(CalibrationFormatError):
        parse_legacy_csv(text)


def test_drive_unit_mismatch_is_refused():
    record = parse_legacy_csv(AA_MW)
    assert 'driven in' in ' '.join(validate_laser_power(record, drive_unit='V'))


def test_requests_outside_the_range_are_refused_never_nan_or_clamped():
    table = LaserPowerTable(parse_legacy_csv(PLAIN), drive_unit='V')
    for bad in (-0.1, 100.1, math.nan, math.inf, 'x', None):
        with pytest.raises(CalibrationRangeError):
            table.drive_for_percent(bad)
    with pytest.raises(CalibrationRangeError):
        table.drive_for_percent_array([10, 50, 101])
    np.testing.assert_allclose(table.drive_for_percent_array([0, 20, 100]), [0, 1, 5])
    assert table.percent_for_drive(5.5) is None and table.percent_for_drive(-1) is None
    assert table.percent_for_drive(3) == pytest.approx(70.0)


def test_absolute_power_follows_the_measured_curve():
    table = LaserPowerTable(parse_legacy_csv(AA_MW), drive_unit='arb')
    assert table.power_for_percent(0) == pytest.approx(0.01)
    assert table.power_for_percent(100) == pytest.approx(5.0)
    assert table.power_for_percent(50) == pytest.approx(2.505)
    assert format_power(table.power_for_percent(50), 'mW') == '2.5 mW'
    assert format_power(0.0124, 'W') == '12.4 mW'
    assert format_power(1.6e-5, 'W') == '16 µW'


def test_tooltip_texts():
    record = replace(parse_legacy_csv(AA_MW), id='561-20261120-abc123', measuredAt='back focal plane')
    table = LaserPowerTable(record, drive_unit='arb')
    from imswitch.imcontrol.model.calibrations import CalibrationResolution
    text = laser_calibration_tooltip(CalibrationResolution('calibrated', table=table), 50,
                                     rawUnits='arb')
    assert text.splitlines()[0] == '2.5 mW at back focal plane'
    assert 'aligned after fibre change' in text and '561-20261120-abc123' in text
    assert 'Drive: 501 arb' in text      # 50 % of 0.01..5 mW
    raw = laser_calibration_tooltip(CalibrationResolution('raw'), 2.0, rawUnits='V',
                                    rawRange=(0, 5))
    assert raw.startswith('Raw units — no calibration') and '0-5 V' in raw
    failed = laser_calibration_tooltip(
        CalibrationResolution('failed', error='file not found: /x.csv'), 2.0, rawUnits='V')
    assert failed.startswith('Raw units — the calibration did not load: file not found')


# -- store -----------------------------------------------------------------------


def test_the_store_folder_is_global_and_created_on_first_write(tmp_path, monkeypatch):
    from imswitch.imcommon.model import dirtools
    from imswitch.imcontrol.model.calibrations.store import default_store_root

    monkeypatch.setattr(dirtools, '_baseUserFilesDir', str(tmp_path / 'ImSwitchConfig'))
    assert default_store_root() == tmp_path / 'ImSwitchConfig' / 'calibrations'
    store = CalibrationStore()
    assert store.list() == [] and not store.root.exists()        # reading writes nothing
    stored = store.add(parse_legacy_csv(MEASURED_W))
    assert stored.path.parent == tmp_path / 'ImSwitchConfig' / 'calibrations'
    assert 'calibrations' not in [d.split('/')[-1] for d in dirtools.UserFileDirs.list()]


def test_store_names_files_for_people_and_keys_records_by_id(tmp_path):
    store = CalibrationStore(tmp_path)
    stored = store.add(parse_legacy_csv(MEASURED_W))
    assert stored.path.name.startswith('775_775nm_bfp_20261125_')
    assert stored.path.name.endswith('.calib.json')
    renamed = stored.path.with_name('anything.calib.json')
    stored.path.rename(renamed)
    assert store.get(stored.record.id).path == renamed           # the id decides
    with pytest.raises(CalibrationFormatError, match='already holds'):
        store.add(stored.record)


def test_annotations_are_rewritten_in_place_without_changing_the_hash(tmp_path):
    store = CalibrationStore(tmp_path)
    stored = store.add(parse_legacy_csv(MEASURED_W))
    updated = store.update_annotations(stored.record.id, measuredAt='sample plane',
                                       comment='with ND 1.0')
    assert updated.path == stored.path
    assert store.get(stored.record.id).record.comment == 'with ND 1.0'
    assert updated.record.contentHash() == stored.record.contentHash()


def test_unreadable_files_are_reported_not_dropped(tmp_path):
    store = CalibrationStore(tmp_path)
    store.add(parse_legacy_csv(MEASURED_W))
    (tmp_path / 'broken.calib.json').write_text('{not json')
    records, unreadable = store.scan()
    assert len(records) == 1 and unreadable[0].path.name == 'broken.calib.json'


# -- resolution and setup references ------------------------------------------------


def test_resolution_precedence(tmp_path):
    store = CalibrationStore(tmp_path / 'store')
    stored = store.add(parse_legacy_csv(MEASURED_W))
    csv = tmp_path / 'legacy.csv'
    csv.write_text(PLAIN)
    common = dict(laser_name='775', drive_unit='V', store=store)
    both = resolve_laser_calibration(calibration_id=stored.record.id, calib_csv_path=str(csv),
                                     **common)
    assert both.state == 'calibrated' and both.table.id == stored.record.id
    raw = resolve_laser_calibration(calibration_id=RAW_UNITS_ID, calib_csv_path=str(csv), **common)
    assert raw.state == 'raw' and not raw.failed
    legacy = resolve_laser_calibration(calibration_id='', calib_csv_path=str(csv), **common)
    assert legacy.state == 'calibrated' and legacy.table.id == 'csv:legacy.csv'
    assert resolve_laser_calibration(calib_csv_path='', **common).state == 'raw'   # editor default
    missing = resolve_laser_calibration(calibration_id='nope', **common)
    assert missing.failed and 'no calibration with id' in missing.error


def test_relative_calib_csv_path_resolves_against_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / 'rel.csv').write_text(PLAIN)
    monkeypatch.chdir(tmp_path)
    ok = resolve_laser_calibration(laser_name='x', drive_unit='V', calib_csv_path='rel.csv')
    assert ok.state == 'calibrated' and ok.resolvedPath == str(tmp_path / 'rel.csv')
    monkeypatch.chdir(tmp_path.parent)
    gone = resolve_laser_calibration(laser_name='x', drive_unit='V', calib_csv_path='rel.csv')
    assert gone.failed and 'working directory' in gone.error


def test_writing_the_selection_changes_only_calibration_ids(tmp_path):
    path = tmp_path / 'setup.json'
    original = {'// comment': 'kept', 'lasers': {
        '488': {'managerName': 'NidaqLaserManager',
                'managerProperties': {'calibCsvPath': 'a.csv', 'x': 1}},
        '561': {'managerName': 'AAAOTFLaserManager',
                'managerProperties': {'calibrationId': 'old', 'channel': 2}}},
        'laserPresets': {'p': {'488': {'value': 3}}}}
    path.write_text(json.dumps(original))
    write_setup_selection(path, {'488': 'cal-1', '561': ''})
    after = json.loads(path.read_text())
    assert after['lasers']['488']['managerProperties'] == {'calibCsvPath': 'a.csv', 'x': 1,
                                                           'calibrationId': 'cal-1'}
    assert after['lasers']['561']['managerProperties'] == {'channel': 2}
    assert after['// comment'] == 'kept' and after['laserPresets'] == original['laserPresets']
    assert read_setup_selection(path)['488'] == {'calibrationId': 'cal-1', 'calibCsvPath': 'a.csv'}
    with pytest.raises(KeyError):
        write_setup_selection(path, {'nope': 'x'})


# -- matching ------------------------------------------------------------------------


def _laser(**kw):
    base = dict(name='775', wavelengthNm=775, driveUnit='V', managerClass='NidaqLaserManager')
    base.update(kw)
    return LaserDescriptor(**base)


def test_match_levels():
    record = parse_legacy_csv(MEASURED_W)
    assert match_to_laser(record, _laser()).level == MATCH
    renamed = match_to_laser(record, _laser(name='Ex775'))
    assert renamed.level == NAME_DIFFERS and "Made for '775', applying to 'Ex775'" in renamed.text()
    other = match_to_laser(record, _laser(wavelengthNm=488))
    assert other.level == OTHER_DEVICE and other.needsConfirmation
    tagged = replace(record, device=replace(record.device, managerClass='AAAOTFLaserManager'))
    assert match_to_laser(tagged, _laser()).level == OTHER_DEVICE
    blocked = match_to_laser(record, _laser(driveUnit='arb'))
    assert blocked.level == INCOMPATIBLE and blocked.blocked
    assert match_to_laser(record, _laser(supportsCalibration=False)).blocked


def test_identity_comparison_helper():
    a = {'id': 'a', 'contentHash': 'sha256:1', 'driveValue': 1.0}
    assert compare_calibration_identity(a, dict(a, driveValue=2.0)).status == SAME
    assert compare_calibration_identity(a, {'id': 'b', 'contentHash': 'sha256:1'}).status == SAME
    changed = compare_calibration_identity(a, {'id': 'a', 'contentHash': 'sha256:2'})
    assert changed.status == DIFFERENT and 'changed' in changed.message
    assert compare_calibration_identity(a, None).status == DIFFERENT
    assert compare_calibration_identity(None, a).status == DIFFERENT
    assert compare_calibration_identity(None, None).status == SAME
    assert compare_calibration_identity(None, a, recorded_known=False).status == UNKNOWN
    assert compare_calibration_identity('garbage', a).status == UNKNOWN
