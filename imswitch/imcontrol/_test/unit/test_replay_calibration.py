"""Replay x laser power calibrations: a recorded laser value is compared with
the calibration in force on the target laser, by the same rule presets and
setup modes use (``calibrations.decide_saved_value``).

Real NI-DAQ laser managers on a recording fake DAQ, a real LaserController
(and LaserWidget, offscreen) and the real ReplayController. A "recording"
is what the Laser panel publishes plus the device inventory, as the
recording manager writes it.

The matrix (target laser x calibration x range):

=========================  ==================  ==============================
                           same laser          remapped laser
=========================  ==================  ==============================
same calibration           value as stored     value as stored
different, drive in range  recorded drive,     recorded drive offered,
                           applied (also       default Skip (a different
                           unattended)         laser); never unattended
different, drive outside   Skip (invalid)      Skip (invalid)
unknown (older file)       as stored, note     as stored, note
                           when calibrated     when calibrated
drive units differ         --                  refused, never converted
=========================  ==================  ==============================
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model import SharedAttributes
from imswitch.imcontrol.controller.controllers.ReplayController import ReplayController
from imswitch.imcontrol.model import replay, replay_inventory
from imswitch.imcontrol.model.calibrations import LaserPowerTable, parse_legacy_csv

from . import _calibration_rig as rig

OTHER = """# laser: 488
# setting unit: V
# laser wavelength nm: 488
# plane: sample plane
# columns: setting power_W
0 0.0
2 0.001
3 0.002
"""


def _table(text=OTHER, record_id='cal-other'):
    return LaserPowerTable(replace(parse_legacy_csv(text), id=record_id), drive_unit='V')


class _Dialog:
    def __init__(self):
        self.plans, self.results, self.decide = [], [], None

    def askToApply(self, plan, *, canRemember=False):
        self.plans.append(plan)
        if self.decide:
            self.decide(plan)
        return True

    def rememberRequested(self):
        return False

    def showResult(self, result):
        self.results.append(result)


@pytest.fixture
def bench(tmp_path, qtbot):
    m488, daq488 = rig.nidaq('488', calibCsvPath=rig.write_csv(tmp_path))
    m561, daq561 = rig.nidaq('561')
    m561._laserInfo.wavelength = 561
    managers = {'488': m488, '561': m561}
    laser, widget = rig.laser_controller(managers, qtbot)
    master = laser._master
    master.positionersManager = rig.Group({})
    master.detectorsManager = rig.Group({})
    master.rotatorsManager = rig.Group({})
    master.flipMirrorsManager = rig.Group({})
    master.pulseGeneratorManager = None
    dialog = _Dialog()
    setupInfo = SimpleNamespace(to_dict=lambda: {'lasers': {'488': {}, '561': {}}}, scan=None,
                                flipMirrors=None)
    controller = ReplayController(setupInfo, laser._commChannel, master, widget=dialog,
                                  factory=None, moduleCommChannel=None)
    controller.setControllers({'Laser': laser})
    controller.setSetupName('rig.json')
    return SimpleNamespace(laser=laser, widget=widget, master=master, replay=controller,
                           dialog=dialog, m488=m488, m561=m561, daq488=daq488, daq561=daq561)


def _record(bench, *, drop=()):
    """What a recording made now would carry for the lasers."""
    attrs = {k: v for k, v in bench.laser._commChannel.sharedAttrs
             if k[0] == 'Laser' and k[2] not in drop}
    attrs[replay.SETUP_INVENTORY_KEY] = json.dumps(
        replay_inventory.buildInventory(bench.master, None))
    attrs[replay.METADATA_SCHEMA_KEY] = 1
    parsed = replay.parseReplayAttributes(attrs)
    parsed.sourceFile, parsed.sourceDataset, parsed.sourceFormat = 'run.h5', 'Cam', 'HDF5'
    return parsed


def _value(plan, laser='488'):
    item = plan.item(f'laser:{laser}')
    return item, next(v for v in item.values if v.key == 'Value')


def _unattended(bench, parsed):
    plan = replay.applyUnattendedPolicy(bench.replay.plan(parsed, useMemory=False))
    return plan, bench.replay.apply(plan, mode='unattended')


# -- same laser ------------------------------------------------------------------------------


def test_same_calibration_applies_the_value_as_stored(bench):
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench)
    bench.laser.setLaserValue('488', 10)
    plan, result = _unattended(bench, parsed)
    item, value = _value(plan)
    assert item.status == replay.EXACT and value.converted is None
    assert [(i, k, v) for i, k, v, _ in result.applied
            if i == 'laser:488'] == [('laser:488', 'Value', 40)]
    assert bench.laser._livePowerValues['488'] == 40


def test_different_calibration_applies_the_recorded_drive_also_unattended(bench):
    bench.laser.setLaserValue('488', 40)
    drive = bench.daq488.analog[-1]
    parsed = _record(bench)
    bench.laser.setLaserCalibrationTable('488', _table())     # laser off: allowed
    plan, result = _unattended(bench, parsed)
    item, value = _value(plan)
    assert value.status == replay.V_OK and value.action == replay.APPLY
    assert value.converted == pytest.approx(bench.m488.powerCalibration.percent_for_drive(drive))
    assert 'Applied the saved drive' in value.reason
    (itemId, key, applied, note), = [a for a in result.applied if a[0] == 'laser:488']
    assert 'by recorded drive' in note and applied == value.converted
    assert bench.daq488.analog[-1] == pytest.approx(drive)   # same output as recorded
    # provenance of the next recording says so
    mapping = json.loads(bench.laser._commChannel.sharedAttrs[(replay.REPLAY_CATEGORY, 'mapping')])
    entry = next(e for e in mapping if e['id'] == 'laser:488')
    assert 'by recorded drive' in entry['applied']['Value']['note']
    assert entry['calibration']['id'] != entry['calibrationAtTarget']['id']


def test_different_calibration_with_the_drive_outside_it_is_skipped(bench):
    bench.laser.setLaserValue('488', 90)                      # ~4.6 V
    parsed = _record(bench)
    bench.laser.setLaserCalibrationTable('488', _table())     # covers 0-3 V
    writes = len(bench.daq488.analog)
    plan, result = _unattended(bench, parsed)
    _, value = _value(plan)
    assert value.status == replay.V_INVALID and value.allowedActions == (replay.SKIP,)
    assert [a for a in result.applied if a[0] == 'laser:488'] == []
    assert any('outside the current calibration' in r for _, _, r in result.skipped)
    assert len(bench.daq488.analog) == writes


def test_recorded_under_a_calibration_now_raw_applies_volts(bench):
    bench.laser.setLaserValue('488', 40)
    drive = bench.daq488.analog[-1]
    parsed = _record(bench)
    bench.laser.setLaserCalibrationTable('488', None)
    plan, result = _unattended(bench, parsed)
    _, value = _value(plan)
    assert value.converted == pytest.approx(drive)
    assert bench.daq488.analog[-1] == pytest.approx(drive)
    assert bench.m488.valueUnits == 'V'


def test_recorded_on_raw_units_now_calibrated_applies_the_same_drive(bench):
    bench.laser.setLaserCalibrationTable('488', None)
    bench.laser.setLaserValue('488', 2.5)
    parsed = _record(bench)
    assert replay.LASER_CALIBRATION_ATTR in parsed.lasers['488']     # cleared to None
    bench.laser.setLaserCalibrationTable('488', _table(rig.CURVE_W, 'cal-back'))
    plan, result = _unattended(bench, parsed)
    _, value = _value(plan)
    assert value.converted == pytest.approx(bench.m488.powerCalibration.percent_for_drive(2.5))
    assert bench.daq488.analog[-1] == pytest.approx(2.5)


def test_an_older_file_applies_as_stored_with_a_note(bench):
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench, drop=(replay.LASER_CALIBRATION_ATTR,))   # pre-calibration file
    bench.laser.setLaserValue('488', 10)
    plan, result = _unattended(bench, parsed)
    _, value = _value(plan)
    assert value.status == replay.V_OK and value.converted is None
    assert 'cannot be verified' in value.reason
    assert bench.laser._livePowerValues['488'] == 40


# -- remapped laser ---------------------------------------------------------------------------


def _remapped(bench, parsed):
    """488 recorded, 488 renamed away: the file's laser maps onto 561."""
    parsed.lasers = {'Ex488': parsed.lasers['488']}
    parsed.inventory['lasers'] = {'Ex488': parsed.inventory['lasers']['488']}
    parsed.inventory['lasers']['Ex488']['wavelength'] = 561      # pre-selects 561
    plan = bench.replay.plan(parsed, useMemory=False)
    item = plan.item('laser:Ex488')
    plan.retarget(item.id, '561')
    return plan, item


def test_remapped_different_calibration_offers_the_drive_but_defaults_to_skip(bench):
    bench.laser.setLaserValue('488', 40)
    drive = bench.daq488.analog[-1]
    parsed = _record(bench)
    plan, item = _remapped(bench, parsed)
    value = next(v for v in item.values if v.key == 'Value')
    assert value.action == replay.SKIP and value.converted == pytest.approx(drive)
    assert 'not the laser it was recorded on' in value.reason
    assert replay.buildActions(plan).actions == []
    replay.setValueAction(value, replay.APPLY)                 # the user opts in
    result = bench.replay.apply(plan, mode='interactive')
    assert bench.daq561.analog[-1] == pytest.approx(drive)
    assert 'by recorded drive' in result.applied[0][3]


def test_remapped_never_applies_unattended(bench):
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench)
    parsed.lasers = {'Ex488': parsed.lasers['488']}
    parsed.inventory['lasers'] = {'Ex488': parsed.inventory['lasers']['488']}
    writes = len(bench.daq561.analog)
    plan, result = _unattended(bench, parsed)
    assert result.applied == [] and len(bench.daq561.analog) == writes


def test_remapped_onto_another_drive_unit_is_refused(bench):
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench)
    parsed.inventory['lasers']['488']['rawUnits'] = 'arb'      # an AOTF channel's file
    plan, item = _remapped(bench, parsed)
    value = next(v for v in item.values if v.key == 'Value')
    assert value.status == replay.V_INVALID and 'driven in arb' in value.reason


def test_remapped_both_raw_behaves_as_before(bench):
    bench.laser.setLaserCalibrationTable('488', None)
    bench.laser.setLaserValue('488', 2.0)
    parsed = _record(bench)
    plan, item = _remapped(bench, parsed)
    value = next(v for v in item.values if v.key == 'Value')
    assert value.status == replay.V_OK and value.action == replay.APPLY
    assert value.converted is None and value.range == (0.0, 5.0)


# -- inventory, apply-layer range, dialog ----------------------------------------------------


def test_inventory_records_scale_and_calibration_identity_only(bench):
    inventory = replay_inventory.buildInventory(bench.master, None)
    laser = inventory['lasers']['488']
    assert (laser['valueUnits'], laser['valueRangeMin'], laser['valueRangeMax']) == ('%', 0, 100)
    assert (laser['rawUnits'], laser['rawRangeMin'], laser['rawRangeMax']) == ('V', 0.0, 5.0)
    assert laser['valueIs'] == 'percent'
    assert set(laser['calibration']) == {'id', 'contentHash'}
    assert inventory['lasers']['561']['valueIs'] == 'raw'
    assert inventory['lasers']['561']['calibration'] is None
    text = json.dumps(inventory)
    assert '/' not in text.replace('sha256:', '') and len(text) < 4096
    assert replay.sanitizeInventory(inventory) == inventory


def test_the_apply_layer_checks_the_percent_range_of_a_calibrated_laser(bench):
    bench.laser.setLaserValue('488', 10)
    parsed = _record(bench)
    parsed.lasers['488']['Value'] = 40.0      # 40 % is outside the raw 0-5 V range
    plan, result = _unattended(bench, parsed)
    assert result.failed == []
    assert [a[2] for a in result.applied if a[0] == 'laser:488'] == [40.0]


def test_dialog_row_says_which_calibration_and_what_it_will_apply(bench, qtbot):
    from imswitch.imcontrol.view.ReplaySettingsDialog import (
        COL_APPLIED, COL_NOTE, ReplaySettingsDialog,
    )
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench)
    bench.laser.setLaserCalibrationTable('488', _table())
    plan = bench.replay.plan(parsed, useMemory=False)
    dialog = ReplaySettingsDialog()
    qtbot.addWidget(dialog)
    dialog.setPlan(plan)
    row = dialog.rowFor('laser:488', 'Value')
    assert 'cal-other is in force now' in dialog.cellText(row, COL_NOTE)
    converted = _value(plan)[1].converted
    assert dialog.cellText(row, COL_APPLIED).startswith(f'{converted:.3g}'[:3])
    calibrationRow = dialog.rowFor('laser:488', replay.LASER_CALIBRATION_ATTR)
    assert 'runs under cal-other' in dialog.cellText(calibrationRow, COL_NOTE)


def test_nothing_is_applied_before_apply_and_emission_stays_off(bench):
    bench.laser.setLaserValue('488', 40)
    parsed = _record(bench)
    bench.laser.setLaserCalibrationTable('488', _table())
    writes, digital = len(bench.daq488.analog), list(bench.daq488.digital)
    bench.replay.plan(parsed, useMemory=False)
    assert len(bench.daq488.analog) == writes                 # planning sends nothing
    bench.replay.apply(replay.applyUnattendedPolicy(bench.replay.plan(parsed, useMemory=False)),
                       mode='unattended')
    assert True not in bench.daq488.digital[len(digital):]     # never switched on
