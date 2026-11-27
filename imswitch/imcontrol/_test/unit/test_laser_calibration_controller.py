"""The Laser panel under calibrations: percent iff one is in force, visible
failures, tooltips, provenance, swap rules and saved values under another
calibration. Real managers on fake transports, a real LaserController and a
real LaserWidget (offscreen)."""
import ast
from pathlib import Path

import numpy as np
import pytest

from imswitch.imcommon.model import isCriticalRestoreWarning
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers._laser_calibration import CalibrationSwapRefused
from imswitch.imcontrol.model.calibrations import LaserPowerTable, parse_legacy_csv
from imswitch.imcontrol.model.managers.lasers.LaserManager import RawDriveError

from . import _calibration_rig as rig

OTHER_CURVE = """# laser: 488
# setting unit: V
# laser wavelength nm: 488
# plane: sample plane
# comment: with ND 1.0
# columns: setting power_W
0 0.0
2 0.001
3 0.002
"""


def _table(text=OTHER_CURVE, record_id='cal-other'):
    record = parse_legacy_csv(text)
    from dataclasses import replace
    return LaserPowerTable(replace(record, id=record_id), drive_unit='V')


@pytest.fixture
def rig488(tmp_path, qtbot):
    manager, daq = rig.nidaq('488', calibCsvPath=rig.write_csv(tmp_path))
    controller, widget = rig.laser_controller({'488': manager}, qtbot)
    return controller, widget, manager, daq


# -- percent iff a calibration is in force -------------------------------------------------


def test_a_loaded_calibration_shows_percent(rig488):
    controller, widget, manager, _ = rig488
    module = widget.laserModules['488']
    assert manager.usesCalibrationLookup() and manager.valueUnits == '%'
    assert module.setPointLabel.text() == 'Setpoint [%]' and module.maxpower.text() == '100'
    assert not module.calibrationIndicator.isVisibleTo(module)


@pytest.mark.parametrize('content', [
    None,                                  # missing file
    'not numbers\n',                       # malformed
    '0 1\n1 1\n2 1\n',                     # constant power
    '0 1\n',                               # single row
    '0 0\n1 5\n2 3\n3 9\n',                # not monotonic
])
def test_a_calibration_that_does_not_load_gives_raw_units_and_a_visible_report(
        tmp_path, qtbot, content):
    path = tmp_path / 'bad.csv'
    if content is not None:
        path.write_text(content)
    manager, daq = rig.nidaq('488', calibCsvPath=str(path))
    controller, widget = rig.laser_controller({'488': manager}, qtbot)
    module = widget.laserModules['488']
    assert not manager.usesCalibrationLookup() and manager.valueUnits == 'V'
    assert manager.powerCalibrationResolution().failed
    assert module.setPointLabel.text() == 'Setpoint [V]' and module.maxpower.text() == '5.0'
    assert module.calibrationIndicator.isVisibleTo(module)
    assert module.setPointEdit.toolTip().startswith('Raw units — the calibration did not load')
    controller.setLaserValue('488', 2.5)           # a volt value goes out as volts
    assert daq.analog[-1] == 2.5


def test_managers_that_ignore_the_key_no_longer_flip_the_ui(tmp_path, qtbot):
    path = rig.write_csv(tmp_path)
    cobolt = rig.RawOnlyLaserManager(rig.laser_info(calibCsvPath=path), 'cobolt')
    plain = rig.RawOnlyLaserManager(rig.laser_info(calibCsvPath=''), 'plain')
    controller, widget = rig.laser_controller({'cobolt': cobolt, 'plain': plain}, qtbot)
    module = widget.laserModules['cobolt']
    assert module.setPointLabel.text() == 'Setpoint [mW]' and module.maxpower.text() == '5.0'
    assert module.calibrationIndicator.isVisibleTo(module)          # named but not applied
    assert 'does not apply power calibrations' in module.setPointEdit.toolTip()
    assert not widget.laserModules['plain'].calibrationIndicator.isVisibleTo(
        widget.laserModules['plain'])
    controller.setLaserValue('cobolt', 3.0)
    assert cobolt.values[-1] == 3.0
    assert controller.getComponentState()['lasers']['cobolt']['valueUnits'] == 'mW'


def test_only_the_nidaq_and_aa_managers_apply_calibrations():
    folder = Path(__file__).parents[2] / 'model' / 'managers' / 'lasers'
    supporting = set()
    for path in folder.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (isinstance(item, ast.Assign) and any(
                            getattr(t, 'id', None) == 'supportsPowerCalibration'
                            for t in item.targets)
                            and getattr(item.value, 'value', None) is True):
                        supporting.add(node.name)
    assert supporting == {'NidaqLaserManager', 'AAAOTFLaserManager'}


# -- refusal on every path ------------------------------------------------------------------


def test_out_of_range_requests_are_refused_on_every_set_path(rig488):
    controller, widget, manager, daq = rig488
    before = list(daq.analog)
    assert manager.setValue(150) is False                  # legacy path: was NaN to the DAQ
    assert manager.setValue(-1) is False
    with pytest.raises(RawDriveError):
        manager.applyValue(100.5)
    controller.setLaserValue('488', 120)                   # scripting / UI path
    assert daq.analog == before
    with pytest.raises(Exception):
        manager.powerCalibration.drive_for_percent_array([0, 101])


# -- tooltip and provenance -------------------------------------------------------------------


def test_tooltip_shows_power_location_and_comment(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 50)
    tooltip = widget.laserModules['488'].setPointEdit.toolTip()
    assert tooltip.splitlines()[0] == '6.2 mW at back focal plane'
    assert 'ND 1.0 removed' in tooltip and 'Drive: 2.1 V' in tooltip
    assert tooltip == controller.laserCalibrationTooltip('488')


def test_provenance_is_published_and_saved(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 40)
    identity = controller._commChannel.sharedAttrs[('Laser', '488', 'Calibration')]
    assert identity['id'] == manager.powerCalibration.id
    assert identity['contentHash'].startswith('sha256:')
    assert identity['driveValue'] == daq.analog[-1]
    assert controller._laserCalibrationIdentity('488') == identity
    state = controller.getComponentState()['lasers']['488']
    assert state['calibration'] == identity and state['valueUnits'] == '%'
    info = controller.getLaserCalibration('488')
    assert info['state'] == 'calibrated' and info['powerUnit'] == 'W'
    assert info['power'] == pytest.approx(0.00496)


# -- swap ---------------------------------------------------------------------------------------


def test_swap_is_refused_while_the_laser_is_on(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 30)
    widget.setLaserActive('488', True)
    before, writes = manager.powerCalibration, len(daq.analog)
    with pytest.raises(CalibrationSwapRefused, match='Switch it off first'):
        controller.setLaserCalibrationTable('488', _table())
    with pytest.raises(CalibrationSwapRefused):
        controller.setLaserCalibrationTable('488', None)
    assert manager.powerCalibration is before and len(daq.analog) == writes


def test_swap_is_refused_while_the_applied_state_is_on(rig488):
    controller, widget, manager, daq = rig488
    controller.toggleLaser('488', True)
    assert controller.getLaserActive('488')
    widget.setLaserActive('488', False, emitSignal=False)   # panel out of step
    with pytest.raises(CalibrationSwapRefused, match='Switch it off first'):
        controller.setLaserCalibrationTable('488', _table())


def test_swap_is_refused_during_a_scan(rig488):
    controller, widget, manager, daq = rig488
    controller.is_scanning = True
    with pytest.raises(CalibrationSwapRefused, match='scan'):
        controller.setLaserCalibrationTable('488', _table())


def test_swap_resets_the_setpoint_to_the_bottom_of_the_new_scale(rig488, qtbot):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 80)
    controller.setLaserPowerSetting('488', 70, 'scan')
    assert controller._scanPowerValues == {'488': 70}
    with qtbot.waitSignal(controller.sigLaserCalibrationChanged):
        controller.setLaserCalibrationTable('488', _table())
    assert manager.powerCalibration.id == 'cal-other'
    assert controller._livePowerValues['488'] == 0
    assert daq.analog[-1] == 0.0                           # drive at 0 % of the new curve
    assert controller._scanPowerValues == {}               # old-scale override dropped
    assert controller._commChannel.sharedAttrs[('Laser', '488', 'Calibration')]['id'] == 'cal-other'
    assert 'with ND 1.0' in widget.laserModules['488'].setPointEdit.toolTip()

    controller.setLaserCalibrationTable('488', None)       # raw units
    module = widget.laserModules['488']
    assert module.setPointLabel.text() == 'Setpoint [V]' and module.maxpower.text() == '5.0'
    assert manager.valueUnits == 'V' and daq.analog[-1] == 0.0
    assert controller._commChannel.sharedAttrs[('Laser', '488', 'Calibration')] is None
    assert not module.calibrationIndicator.isVisibleTo(module)


def test_a_swap_to_another_drive_unit_is_refused(rig488):
    controller, widget, manager, daq = rig488
    aa = parse_legacy_csv('# setting unit: arb\n0 0\n1000 5\n')
    with pytest.raises(CalibrationSwapRefused, match='arb'):
        controller.setLaserCalibrationTable('488', LaserPowerTable(aa))


# -- saved values under another calibration -----------------------------------------------------


def test_state_saved_under_another_calibration_applies_the_recorded_drive(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 40)
    saved = controller.getComponentState()
    drive = saved['lasers']['488']['calibration']['driveValue']
    controller.setLaserCalibrationTable('488', _table())
    warnings = controller.applyComponentState(
        saved, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    calibrationWarnings = [w for w in warnings if 'drive' in w]
    assert len(calibrationWarnings) == 1 and isCriticalRestoreWarning(calibrationWarnings[0])
    assert 'Applied the saved drive' in calibrationWarnings[0]
    assert daq.analog[-1] == pytest.approx(drive)          # same output as when saved
    assert controller._livePowerValues['488'] == pytest.approx(
        manager.powerCalibration.percent_for_drive(drive))


def test_saved_drive_outside_the_new_calibration_is_skipped(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 90)                    # drive ~4.6 V
    saved = controller.getComponentState()
    controller.setLaserCalibrationTable('488', _table())   # covers 0-3 V only
    writes = len(daq.analog)
    warnings = controller.applyComponentState(
        saved, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert any('outside the current calibration; not applied' in w for w in warnings)
    assert len(daq.analog) == writes


def test_state_saved_under_the_same_calibration_applies_unchanged(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 40)
    saved = controller.getComponentState()
    controller.setLaserValue('488', 10)
    warnings = controller.applyComponentState(
        saved, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert not [w for w in warnings if 'calibration' in w.lower()]
    assert controller._livePowerValues['488'] == 40


def test_old_percent_state_is_not_applied_to_a_laser_now_on_raw_units(tmp_path, qtbot):
    manager, daq = rig.nidaq('488', calibCsvPath=str(tmp_path / 'gone.csv'))
    controller, widget = rig.laser_controller({'488': manager}, qtbot)
    old = {'lasers': {'488': {'enabled': False, 'value': 60.0, 'isBinary': False,
                              'valueUnits': '%'}}}
    writes = len(daq.analog)
    warnings = controller.applyComponentState(
        old, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert any('saved as 60.0 %' in w and 'not applied' in w for w in warnings)
    assert len(daq.analog) == writes


def test_old_state_under_a_loading_calibration_means_what_it_meant(rig488):
    controller, widget, manager, daq = rig488
    old = {'lasers': {'488': {'enabled': False, 'value': 40.0, 'isBinary': False,
                              'valueUnits': '%'}}}
    controller.applyComponentState(old, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert daq.analog[-1] == manager.powerCalibration.drive_for_percent(40.0)


def test_presets_record_and_check_their_calibration(rig488):
    controller, widget, manager, daq = rig488
    controller.setLaserValue('488', 40)
    preset = controller.makePreset()
    serialized = controller._serializePresetPowers(preset)
    assert serialized['488']['calibration']['id'] == manager.powerCalibration.id
    drive = serialized['488']['calibration']['driveValue']
    controller.setLaserCalibrationTable('488', _table())
    warnings = controller.applyPreset(controller._normalizePresetPowers(serialized))
    assert len(warnings) == 1 and 'Laser preset' in warnings[0]
    assert daq.analog[-1] == pytest.approx(drive)
    # historical presets (no calibration recorded) apply as before
    assert controller.applyPreset(controller._normalizePresetPowers({'488': {'value': 0.0}})) == []


def test_annotation_edit_does_not_count_as_another_calibration():
    table = _table()
    edited = table.record.withAnnotations(comment='edited')
    from imswitch.imcontrol.model.calibrations import compare_calibration_identity
    assert compare_calibration_identity(
        table.identity(1.0), LaserPowerTable(edited, drive_unit='V').identity(None)).status == 'same'
    assert np.isfinite(table.drive_for_percent(50))
