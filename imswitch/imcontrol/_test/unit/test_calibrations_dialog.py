"""The Calibrations window (Hardware → Calibrations…) driven through its
controller, offscreen. Screenshots go to ``IMSWITCH_CALIBRATION_SCREENSHOTS``
when that variable names a folder."""
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.CalibrationsController import (
    CalibrationsController,
)
from imswitch.imcontrol.model.calibrations import (
    CalibrationStore,
    DeviceIdentity,
    parse_legacy_csv,
)
from imswitch.imcontrol.view.widgets.CalibrationsWidget import CalibrationsWidget

from . import _calibration_rig as rig

AOTF_CURVE = """# laser: 561AOTF
# setting unit: arb
# laser wavelength nm: 561
# columns: setting power_mW
0 0.01
1000 5
"""


def _add(store, text=rig.CURVE_W, comment='', **device):
    record = parse_legacy_csv(text)
    if comment:
        record = record.withAnnotations(comment=comment)
    if device:
        record = replace(record, device=DeviceIdentity(**{**record.device.__dict__, **device}))
    return store.add(record).record


@pytest.fixture
def dialog(tmp_path, qtbot):
    store = CalibrationStore(tmp_path / 'store')
    legacy = rig.write_csv(tmp_path, name='legacy488.csv')
    m488, daq = rig.nidaq('488', calibCsvPath=legacy)
    m561, _ = rig.nidaq('561', calibCsvPath=str(tmp_path / 'gone.csv'))
    cobolt = rig.RawOnlyLaserManager(rig.laser_info(), 'cobolt')
    managers = {'488': m488, '561': m561, 'cobolt': cobolt}
    setupPath = rig.write_setup(tmp_path, managers)
    laserController, laserWidget = rig.laser_controller(managers, qtbot)
    widget = CalibrationsWidget(None)
    qtbot.addWidget(widget)
    asked = SimpleNamespace(confirm=[], laser=[], answer=True)

    def confirm(title, text):
        asked.confirm.append(text)
        return asked.answer
    widget.confirm = confirm
    widget.askLaser = lambda question, names, default=None: (
        asked.laser.append((question, list(names))) or '488')
    controller = CalibrationsController(
        laserController._setupInfo, laserController._commChannel, laserController._master,
        widget=widget, factory=None, moduleCommChannel=None, laserController=laserController,
        store=store, setupFilePath=lambda: setupPath)
    records = {
        'match': _add(store),
        'renamed': _add(store, comment='other rig', name='Ex488'),
        'other': _add(store, comment='the 775 line', wavelengthNm=775.0, name='775'),
        'aotf': _add(store, AOTF_CURVE),
    }
    controller.refresh()
    return SimpleNamespace(controller=controller, widget=widget, store=store, daq=daq,
                           managers=managers, laserController=laserController,
                           laserWidget=laserWidget, setupPath=setupPath, asked=asked,
                           records=records, tmp=tmp_path)


def _laser(model, name):
    return next(laser for laser in model['lasers'] if laser['name'] == name)


def test_lists_matching_calibrations_per_laser_and_the_others_apart(dialog):
    model = dialog.controller.model()
    l488 = _laser(model, '488')
    assert [e['id'] for e in l488['entries']] == [dialog.records['match'].id]
    assert l488['state'] == 'calibrated' and l488['csvInForce']
    others = {e['id'] for e in model['others']}
    assert others == {dialog.records[k].id for k in ('renamed', 'other', 'aotf')}
    l561 = _laser(model, '561')
    assert l561['state'] == 'failed' and 'not found' in l561['error']
    assert 'cobolt' in [laser['name'] for laser in model['lasers']]
    assert 'raw units only' in _laser(model, 'cobolt')['blocked']


def test_activate_a_matching_calibration_then_save_it_to_the_setup(dialog):
    record = dialog.records['match']
    dialog.controller.select(('calibration', record.id, '488'))
    dialog.controller.activateSelected()
    assert dialog.asked.confirm == []                          # a match needs no question
    assert dialog.managers['488'].powerCalibration.id == record.id
    assert dialog.daq.analog[-1] == 0.0                        # reset to 0 %
    assert dialog.widget.setupBanner.isVisibleTo(dialog.widget)
    assert '488' in dialog.widget.setupBanner.text()
    before = json.loads(open(dialog.setupPath).read())
    assert dialog.controller.saveSelectionToSetup()
    assert 'Only managerProperties.calibrationId' in dialog.asked.confirm[-1]
    after = json.loads(open(dialog.setupPath).read())
    props = after['lasers']['488']['managerProperties']
    assert props['calibrationId'] == record.id and props['calibCsvPath'] == \
        before['lasers']['488']['managerProperties']['calibCsvPath']
    after['lasers']['488']['managerProperties'].pop('calibrationId')
    assert after == before                                     # nothing else changed
    assert dialog.laserController._setupInfo.lasers['488'].managerProperties[
        'calibrationId'] == record.id                          # in-memory kept in step
    assert not dialog.widget.setupBanner.isVisibleTo(dialog.widget)


def test_another_laser_name_needs_confirmation(dialog):
    record = dialog.records['renamed']
    dialog.asked.answer = False
    assert not dialog.controller.activate('488', record.id)
    assert "Made for 'Ex488', applying to '488'" in dialog.asked.confirm[-1]
    assert dialog.managers['488'].powerCalibration.id != record.id
    dialog.asked.answer = True
    assert dialog.controller.activate('488', record.id)
    assert dialog.managers['488'].powerCalibration.id == record.id


def test_another_wavelength_warns_strongly_and_another_drive_unit_is_blocked(dialog):
    dialog.asked.answer = False
    assert not dialog.controller.activate('488', dialog.records['other'].id)
    assert 'measured at 775 nm' in dialog.asked.confirm[-1]
    asked = len(dialog.asked.confirm)
    assert not dialog.controller.activate('488', dialog.records['aotf'].id)
    assert len(dialog.asked.confirm) == asked                  # no "use anyway" offered
    assert 'driven in' in dialog.widget.statusLabel.text()
    assert not dialog.controller.activateLaserCalibration('488', dialog.records['other'].id)


def test_activation_is_refused_while_the_laser_is_on(dialog):
    dialog.laserWidget.setLaserActive('488', True)
    before = dialog.managers['488'].powerCalibration
    assert not dialog.controller.activate('488', dialog.records['match'].id)
    assert 'Switch it off first' in dialog.widget.statusLabel.text()
    assert dialog.managers['488'].powerCalibration is before


def test_run_on_raw_units(dialog):
    assert dialog.controller.runLaserOnRawUnits('488')
    module = dialog.laserWidget.laserModules['488']
    assert module.setPointLabel.text() == 'Setpoint [V]'
    assert dialog.controller._sessionSelection['488'] == 'raw'
    dialog.controller.saveSelectionToSetup(confirmed=True)
    props = json.loads(open(dialog.setupPath).read())['lasers']['488']['managerProperties']
    assert props['calibrationId'] == 'raw' and 'calibCsvPath' in props


def test_a_laser_that_is_not_connected_offers_no_laser_actions(dialog):
    manager = dialog.managers['488']
    manager._setConnectionError('unplugged', summary='NI-DAQ not answering')
    dialog.controller.select(('calibration', dialog.records['match'].id, '488'))
    assert not dialog.widget.activateButton.isEnabled()
    assert not dialog.widget.rawButton.isEnabled()
    assert 'not connected' in dialog.widget.blockedLabel.text()
    assert dialog.widget.exportButton.isEnabled()              # files still work


def test_import_asks_for_the_laser_when_the_file_does_not_name_it(dialog):
    path = dialog.tmp / 'anonymous.csv'
    path.write_text('0 0\n1 2\n5 9\n')
    record_id = dialog.controller.importCsv(str(path))
    assert dialog.asked.laser and '488' in dialog.asked.laser[0][1]
    record = dialog.store.get(record_id).record
    assert record.device.name == '488' and record.device.wavelengthNm == 488
    assert record.device.managerClass == 'NidaqLaserManager' and record.input.unit == 'V'
    assert dialog.managers['488'].powerCalibration.id != record_id     # not activated


def test_import_the_referenced_file_leaves_file_and_setup_alone(dialog):
    legacy = dialog.tmp / 'legacy488.csv'
    text, setup = legacy.read_text(), open(dialog.setupPath).read()
    dialog.controller.select(('laser', '488'))
    assert dialog.widget.importReferencedButton.isEnabled()
    record_id = dialog.controller.importReferencedCsv()
    assert dialog.store.get(record_id).record.contentHash() == \
        dialog.managers['488'].powerCalibration.contentHash()
    assert legacy.read_text() == text and open(dialog.setupPath).read() == setup


def test_measured_lut_registers_through_the_script_api(dialog):
    lut = dialog.tmp / '488_run-1.calib.csv'
    lut.write_text(rig.CURVE_W)
    record_id = dialog.controller.importLaserCalibration(str(lut))
    assert record_id in [c['id'] for c in dialog.controller.getStoredCalibrations()]


def test_export_writes_a_csv_that_imports_back_identically(dialog):
    record = dialog.records['match']
    dialog.controller.select(('calibration', record.id, '488'))
    target = dialog.tmp / 'out.csv'
    assert dialog.controller.exportSelected(str(target)) == str(target)
    again = parse_legacy_csv(target.read_text())
    assert again == record


def test_edit_location_and_comment(dialog):
    record = dialog.records['match']
    dialog.controller.activate('488', record.id)
    dialog.controller.select(('calibration', record.id, '488'))
    assert dialog.controller.saveAnnotations('sample plane', 'after realignment')
    stored = dialog.store.get(record.id).record
    assert (stored.measuredAt, stored.comment) == ('sample plane', 'after realignment')
    assert stored.contentHash() == record.contentHash()
    tooltip = dialog.laserWidget.laserModules['488'].setPointEdit.toolTip()
    assert 'at sample plane' in tooltip and 'after realignment' in tooltip


def test_details_and_screenshots(dialog, qtbot):
    record = dialog.records['match']
    dialog.controller.activate('488', record.id)
    dialog.laserController.setLaserValue('488', 60)
    dialog.widget.show()
    qtbot.waitExposed(dialog.widget)
    dialog.controller.refresh()
    from qtpy import QtCore, QtWidgets
    target = ('calibration', record.id, '488')
    iterator = QtWidgets.QTreeWidgetItemIterator(dialog.widget.tree)
    while iterator.value() is not None:
        item = iterator.value()
        data = item.data(0, QtCore.Qt.UserRole)
        if data and tuple(data) == target:
            dialog.widget.tree.setCurrentItem(item)        # through the real click path
        iterator += 1
    assert dialog.controller._selected == target
    info = dialog.controller.details()
    text = '\n'.join(info['lines'])
    assert 'Measured at: back focal plane' in text and '5 points' in text
    assert info['plot'] is not None and len(info['plot']['drive']) == 5
    folder = os.environ.get('IMSWITCH_CALIBRATION_SCREENSHOTS')
    if folder:
        os.makedirs(folder, exist_ok=True)
        dialog.widget.resize(1100, 680)
        dialog.widget.grab().save(os.path.join(folder, 'calibrations-dialog.png'))
        from qtpy import QtGui
        dialog.laserWidget.resize(760, 260)
        dialog.laserWidget.show()
        qtbot.waitExposed(dialog.laserWidget)
        rows = dialog.laserWidget.grab()
        tip = QtWidgets.QLabel(dialog.laserWidget.laserModules['488'].setPointEdit.toolTip())
        tip.setStyleSheet('background: #ffffe1; color: black; border: 1px solid #767676;'
                          ' padding: 4px;')
        tip.adjustSize()
        tipImage = tip.grab()
        canvas = QtGui.QPixmap(max(rows.width(), tipImage.width()),
                               rows.height() + tipImage.height() + 8)
        canvas.fill(QtGui.QColor('white'))
        painter = QtGui.QPainter(canvas)
        painter.drawPixmap(0, 0, rows)
        painter.drawPixmap(140, rows.height() + 4, tipImage)
        painter.end()
        canvas.save(os.path.join(folder, 'laser-tooltip.png'))


NOISY = """# laser: 488
# setting unit: V
# laser wavelength nm: 488
# columns: setting power_W
0 0.0
1 0.0021
2 0.0019
3 0.006
3 0.0062
5 0.0124
"""


def test_a_noisy_file_is_refused_with_the_way_out_or_fitted_when_asked(dialog):
    path = dialog.tmp / 'noisy.csv'
    path.write_text(NOISY)
    dialog.asked.answer = False
    assert dialog.controller.importCsv(str(path)) is None
    assert 'isotonic fit' in dialog.asked.confirm[-1]
    assert 'largest correction' in dialog.asked.confirm[-1]
    assert 'Fit a monotonic curve' in dialog.widget.statusLabel.text()
    dialog.asked.answer = True
    record_id = dialog.controller.importCsv(str(path))
    record = dialog.store.get(record_id).record
    power = [row[1] for row in record.table]
    assert power == sorted(power) and len(set(power)) == len(power)
    assert any(key == 'fitted on import' for key, _ in record.provenance.header)
    assert path.read_text() == NOISY                            # the file is left alone


def test_the_script_api_fits_only_when_asked(dialog):
    from imswitch.imcontrol.model.calibrations import CalibrationError

    path = dialog.tmp / 'noisy2.csv'
    path.write_text(NOISY)
    with pytest.raises(CalibrationError, match='not monotonic'):
        dialog.controller.importLaserCalibration(str(path))
    record_id = dialog.controller.importLaserCalibration(str(path), fitMonotone=True)
    assert dialog.store.get(record_id) is not None
