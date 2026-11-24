"""The replay dialog: what the user sees and the choices it hands back.

Drives :class:`ReplaySettingsDialog` offscreen through its row editors (the
same combo boxes and fields a user clicks) and checks the plan it leaves.
"""
import pytest
from qtpy import QtWidgets

from imswitch.imcontrol.model import replay
from imswitch.imcontrol.view.ReplaySettingsDialog import (
    COL_ACTION, COL_APPLIED, COL_STATUS, COL_STORED, COL_TARGET, _OUT_OF_RANGE_COLOR,
    ReplaySettingsDialog,
)


def _snapshot():
    inv = replay.emptyInventory()
    inv['lasers'] = {
        '488': {'wavelength': 488, 'valueUnits': 'mW', 'valueRangeMin': 0,
                'valueRangeMax': 200, 'isBinary': False},
        '561': {'wavelength': 561, 'valueUnits': 'mW', 'valueRangeMin': 0,
                'valueRangeMax': 100, 'isBinary': False},
    }
    inv['positioners'] = {'Stage': {'axes': ['X', 'Y'], 'axisRanges': {'X': [0, 100]}}}
    return replay.SetupSnapshot(setupName='now.json', setupHash='sha256:now', inventory=inv,
                                current={('Laser', '488', 'Value'): 3.0})


def _plan(attrs):
    parsed = replay.parseReplayAttributes(attrs)
    parsed.sourceFile, parsed.sourceDataset = 'run.h5', 'Cam'
    return replay.resolveReplay(parsed, _snapshot())


ATTRS = {
    'Laser:488:Value': 1e6, 'Laser:488:Enabled': True,
    'Laser:Coherent:Value': 5.0,
    'Positioner:Stage:X:Position': 50.0,
    'setup:hash': 'sha256:then', 'setup:name': 'rigA.json',
    'acquisition:software_version': '2.0',
}


@pytest.fixture
def dialog(qtbot):
    widget = ReplaySettingsDialog()
    qtbot.addWidget(widget)
    return widget


def _flush(qtbot):
    qtbot.wait(10)


def test_dialog_lists_every_reference_and_highlights_out_of_range(dialog):
    plan = _plan(ATTRS)
    dialog.setPlan(plan, canRemember=True)
    row = dialog.rowFor('laser:488', 'Value')
    assert dialog.cellText(row, COL_ACTION) == 'Skip'
    assert dialog.cellColor(row, COL_STORED).name() == _OUT_OF_RANGE_COLOR.name()
    assert dialog.cellText(dialog.rowFor('laser:488', 'Enabled'), COL_ACTION) == 'Skip'
    foreign = dialog.rowFor('laser:Coherent')
    assert dialog.cellText(foreign, COL_TARGET) == 'Skip'
    assert dialog.cellText(foreign, COL_STATUS) == 'not in this setup'
    assert 'different setup configuration' in dialog.sourceLabel.text()
    assert not dialog.movesCheck.isChecked()
    assert not dialog.applyButton.isEnabled()  # nothing to apply yet
    assert dialog.rememberCheck.isVisibleTo(dialog)


def test_picking_a_replacement_device_in_the_combo_retargets_the_plan(dialog, qtbot):
    plan = _plan(ATTRS)
    dialog.setPlan(plan)
    combo = dialog.table.cellWidget(dialog.rowFor('laser:Coherent'), COL_TARGET)
    assert [combo.itemText(i) for i in range(combo.count())] == ['Skip', '488', '561']
    combo.setCurrentIndex(2)
    _flush(qtbot)
    assert plan.item('laser:Coherent').target == '561'
    assert dialog.applyButton.isEnabled()
    row = dialog.rowFor('laser:Coherent', 'Value')
    assert dialog.cellText(row, COL_APPLIED) == '5'


def test_clamp_and_edit_from_the_action_combo(dialog, qtbot):
    plan = _plan(ATTRS)
    dialog.setPlan(plan)
    combo = dialog.table.cellWidget(dialog.rowFor('laser:488', 'Value'), COL_ACTION)
    combo.setCurrentIndex(combo.findData(replay.CLAMP))
    _flush(qtbot)
    assert {(a.itemId, a.value) for a in replay.buildActions(plan).actions} == {('laser:488', 200.0)}

    combo = dialog.table.cellWidget(dialog.rowFor('laser:488', 'Value'), COL_ACTION)
    combo.setCurrentIndex(combo.findData(replay.EDIT))
    _flush(qtbot)
    edit = dialog.table.cellWidget(dialog.rowFor('laser:488', 'Value'), COL_APPLIED)
    assert isinstance(edit, QtWidgets.QLineEdit)
    edit.setText('120')
    edit.editingFinished.emit()
    _flush(qtbot)
    assert {(a.itemId, a.value) for a in replay.buildActions(plan).actions} == {('laser:488', 120.0)}


def test_stage_moves_need_the_checkbox(dialog, qtbot):
    plan = _plan({'Positioner:Stage:X:Position': 50.0})
    dialog.setPlan(plan)
    assert replay.buildActions(plan).actions == []
    assert not dialog.applyButton.isEnabled()
    dialog.movesCheck.setChecked(True)
    assert plan.allowMoves is True
    assert [a.value for a in replay.buildActions(plan).actions] == [50.0]
    assert dialog.applyButton.isEnabled()


def test_set_plan_resets_the_moves_opt_in(dialog):
    plan = _plan({'Positioner:Stage:X:Position': 50.0})
    plan.allowMoves = True
    dialog.setPlan(plan)
    assert plan.allowMoves is False and not dialog.movesCheck.isChecked()


def test_ask_to_apply_returns_the_users_answer(dialog, qtbot, monkeypatch):
    plan = _plan({'Laser:488:Value': 5.0})
    monkeypatch.setattr(dialog, 'exec_', lambda: QtWidgets.QDialog.Rejected)
    assert dialog.askToApply(plan) is False
    monkeypatch.setattr(dialog, 'exec_', lambda: QtWidgets.QDialog.Accepted)
    assert dialog.askToApply(plan) is True


def test_result_summary_lists_failures(dialog, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, 'exec_', lambda box: shown.append(box) or 0)
    result = replay.ReplayResult(applied=[('laser:488', 'Value', 5.0, '')],
                                 skipped=[('laser:561', 'Value', 'Outside 0 to 100 mW.')],
                                 failed=[('detector:Cam', 'Binning', 'camera busy')])
    dialog.showResult(result)
    box = shown[0]
    assert 'failed: 1' in box.text()
    assert 'camera busy' in box.detailedText() and 'Outside' in box.detailedText()
