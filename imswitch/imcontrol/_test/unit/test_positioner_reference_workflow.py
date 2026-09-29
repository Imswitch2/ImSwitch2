"""No-hardware tests for explicit Positioner referencing."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy import QtWidgets

from imswitch.imcontrol.controller.controllers.PositionerController import PositionerController
from imswitch.imcontrol.model.managers.positioners.PositionerManager import PositionerManager
from imswitch.imcontrol.view.widgets import PositionerWidget

pytestmark = pytest.mark.nohardware


class _Managers:
    def __init__(self, entries):
        self._entries = list(entries)
        self._by_name = dict(entries)

    def __iter__(self):
        return iter(self._entries)

    def __getitem__(self, name):
        return self._by_name[name]

    def getAllDeviceNames(self):
        return list(self._by_name)


class _Positioner:
    def __init__(
        self,
        axes=('X',),
        position=None,
        referenced=None,
        *,
        requiresReference=True,
        failReference=False,
        referenceWaitAfterS=0.3,
        defaultReferenceVoltage=5.0,
        conversionFactor=10.0,
    ):
        self.axes = list(axes)
        self.position = dict(position or {axis: 0.0 for axis in self.axes})
        self._referenced = dict(referenced or {axis: False for axis in self.axes})
        self.requiresReference = requiresReference
        self.failReference = failReference
        self.referenceWaitAfterS = referenceWaitAfterS
        self.defaultReferenceVoltage = defaultReferenceVoltage
        self._conversionFactor = conversionFactor
        self.referenceCalls = []
        self.forPositioning = True
        self.hide = False
        self.positionUnit = 'um'

    @property
    def defaultReferencePosition(self):
        if self.defaultReferenceVoltage is None:
            return None
        return self.defaultReferenceVoltage * self._conversionFactor

    def positionToVoltage(self, position):
        return position / self._conversionFactor

    def isAxisReferenced(self, axis):
        return self._referenced[axis]

    def reference(self, axis=None, position=None):
        if axis is None:
            axis = self.axes[0]
        self.referenceCalls.append((axis, position))
        if self.failReference:
            raise RuntimeError('reference failed')
        if position is None:
            if self.defaultReferencePosition is None:
                raise RuntimeError('no default reference')
            position = self.defaultReferencePosition
        self._referenced[axis] = True
        self.position[axis] = position


def _controller(entries, widget=None):
    ctrl = PositionerController.__new__(PositionerController)
    ctrl._master = SimpleNamespace(positionersManager=_Managers(entries))
    ctrl._widget = widget or MagicMock()
    ctrl._commChannel = SimpleNamespace(sharedAttrs={})
    ctrl.settingAttr = False
    ctrl._referenceBatchPlan = []
    ctrl._referenceBatchIndex = 0
    ctrl._referenceBatchRunning = False
    ctrl._referenceBatchAbortRequested = False
    ctrl._PositionerController__logger = MagicMock()
    return ctrl


def test_positioner_manager_default_reference_wait_after_is_significant():
    assert PositionerManager.referenceWaitAfterS == 0.3


def test_reference_button_hidden_when_no_reference_capable_positioners(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    ctrl = _controller([('Stage', _Positioner(requiresReference=False))], widget)

    ctrl._refreshReferenceStatus()

    assert widget.pars['ReferenceButton'].isHidden()
    assert not widget.pars['ReferenceButton'].isEnabled()


def test_reference_button_counts_unreferenced_axes(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)

    widget.setReferenceAxesStatus([
        {'positionerName': 'Stage', 'axis': 'X', 'referenced': False},
        {'positionerName': 'Stage', 'axis': 'Y', 'referenced': True},
        {'positionerName': 'Piezo', 'axis': 'Z', 'referenced': False},
    ])

    assert not widget.pars['ReferenceButton'].isHidden()
    assert widget.pars['ReferenceButton'].isEnabled()
    assert widget.pars['ReferenceButton'].text() == '⚠ 2 unreferenced'


def test_reference_button_reads_reference_when_all_axes_are_referenced(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)

    widget.setReferenceAxesStatus([
        {'positionerName': 'Stage', 'axis': 'X', 'referenced': True},
        {'positionerName': 'Piezo', 'axis': 'Z', 'referenced': True},
    ])

    assert widget.pars['ReferenceButton'].text() == 'Reference…'
    assert widget.pars['ReferenceButton'].isEnabled()


def test_reference_status_labels_include_default_and_displayed_values():
    manager = _Positioner(position={'X': 12.5}, defaultReferenceVoltage=5.0)
    ctrl = _controller([('Stage', manager)])

    status = ctrl._getReferenceAxesStatus()

    assert status == [{
        'positionerName': 'Stage',
        'axis': 'X',
        'referenced': False,
        'defaultTargetLabel': 'Default (50um, 5V)',
        'displayedTargetLabel': 'Displayed position (12.5um, 1.25V)',
    }]


def test_reference_status_omits_default_label_when_no_default_value():
    manager = _Positioner(
        position={'X': 12.5},
        defaultReferenceVoltage=None,
    )
    ctrl = _controller([('Stage', manager)])

    status = ctrl._getReferenceAxesStatus()

    assert status[0]['defaultTargetLabel'] is None
    assert status[0]['displayedTargetLabel'] == 'Displayed position (12.5um, 1.25V)'


def test_reference_target_combo_hides_missing_default(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    combo = QtWidgets.QComboBox()
    qtbot.addWidget(combo)

    widget._populateReferenceTargetCombo(combo, {
        'displayedTargetLabel': 'Displayed position (12.5um, 1.25V)',
    })

    assert combo.count() == 1
    assert combo.itemData(0) == 'displayed'
    assert combo.itemText(0) == 'Displayed position (12.5um, 1.25V)'


def test_default_action_passes_position_none():
    manager = _Positioner()
    widget = MagicMock()
    widget.confirmReferencePositioner.return_value = True
    ctrl = _controller([('Stage', manager)], widget)
    ctrl.referencePositioner = MagicMock(return_value=True)

    assert ctrl._referencePositionerFromWidget('Stage', 'X', 'default') is True

    widget.confirmReferencePositioner.assert_called_once_with(
        'Stage', 'X', 'Default (50um, 5V)'
    )
    ctrl.referencePositioner.assert_called_once_with('Stage', 'X', position=None)


def test_displayed_position_action_passes_tracked_axis_position():
    manager = _Positioner(position={'X': 12.5})
    widget = MagicMock()
    widget.confirmReferencePositioner.return_value = True
    ctrl = _controller([('Stage', manager)], widget)
    ctrl.referencePositioner = MagicMock(return_value=True)

    assert ctrl._referencePositionerFromWidget('Stage', 'X', 'displayed') is True

    widget.confirmReferencePositioner.assert_called_once_with(
        'Stage', 'X', 'Displayed position (12.5um, 1.25V)'
    )
    ctrl.referencePositioner.assert_called_once_with('Stage', 'X', position=12.5)


def test_successful_reference_refreshes_position_and_status():
    manager = _Positioner(position={'X': 1.0}, referenced={'X': False})
    widget = MagicMock()
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.referencePositioner('Stage', 'X') is True

    assert manager.referenceCalls == [('X', None)]
    widget.updatePosition.assert_called_once_with('Stage', 'X', 50.0)
    widget.setReferenceAxesStatus.assert_called_once_with([
        {
            'positionerName': 'Stage',
            'axis': 'X',
            'referenced': True,
            'defaultTargetLabel': 'Default (50um, 5V)',
            'displayedTargetLabel': 'Displayed position (50um, 5V)',
        }
    ])


def test_failed_reference_leaves_state_unchanged_and_reports_failure():
    manager = _Positioner(referenced={'X': False}, failReference=True)
    widget = MagicMock()
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.referencePositioner('Stage', 'X') is False

    assert manager.isAxisReferenced('X') is False
    widget.showReferenceError.assert_called_once()
    widget.updatePosition.assert_not_called()
    widget.setReferenceAxesStatus.assert_not_called()


def test_only_selected_axis_is_referenced():
    manager = _Positioner(
        axes=('X', 'Y'),
        position={'X': 1.0, 'Y': 2.0},
        referenced={'X': False, 'Y': False},
    )
    widget = MagicMock()
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.referencePositioner('Stage', 'Y', position=2.0) is True

    assert manager.referenceCalls == [('Y', 2.0)]
    assert manager.isAxisReferenced('X') is False
    assert manager.isAxisReferenced('Y') is True


def test_reference_all_plan_freezes_displayed_positions():
    manager = _Positioner(
        axes=('X', 'Y'),
        position={'X': 12.5, 'Y': 25.0},
        referenceWaitAfterS=0.45,
    )
    widget = MagicMock()
    widget.getReferenceTargetModes.return_value = {
        ('Stage', 'X'): 'displayed',
        ('Stage', 'Y'): 'default',
    }
    ctrl = _controller([('Stage', manager)], widget)

    plan = ctrl._buildReferenceBatchPlan()

    assert plan[0]['positionerName'] == 'Stage'
    assert plan[0]['axis'] == 'X'
    assert plan[0]['position'] == 12.5
    assert plan[0]['targetDescription'] == 'Displayed position (12.5um, 1.25V)'
    assert plan[0]['waitAfterS'] == 0.45
    assert plan[1]['axis'] == 'Y'
    assert plan[1]['position'] is None
    assert plan[1]['targetDescription'] == 'Default (50um, 5V)'


def test_reference_all_confirmation_happens_before_first_reference():
    manager = _Positioner()
    widget = MagicMock()
    widget.getReferenceTargetModes.return_value = {('Stage', 'X'): 'default'}
    widget.confirmReferenceBatch.return_value = False
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.referenceAllPositioners() is False

    widget.confirmReferenceBatch.assert_called_once()
    assert manager.referenceCalls == []


def test_reference_all_runs_one_axis_at_a_time_with_waits():
    manager = _Positioner(
        axes=('X', 'Y'),
        position={'X': 1.0, 'Y': 2.0},
        referenceWaitAfterS=0.3,
    )
    widget = MagicMock()
    widget.getReferenceTargetModes.return_value = {
        ('Stage', 'X'): 'default',
        ('Stage', 'Y'): 'displayed',
    }
    widget.confirmReferenceBatch.return_value = True
    ctrl = _controller([('Stage', manager)], widget)

    scheduled = []
    ctrl._scheduleReferenceBatchContinue = lambda wait: scheduled.append(wait)

    assert ctrl.referenceAllPositioners() is True

    assert manager.referenceCalls == [('X', None)]
    assert scheduled == [0.3]
    assert ctrl._referenceBatchRunning is True

    ctrl._continueReferenceBatch()

    assert manager.referenceCalls == [('X', None), ('Y', 2.0)]
    assert scheduled == [0.3, 0.3]

    ctrl._continueReferenceBatch()

    assert ctrl._referenceBatchRunning is False
    widget.setReferenceBatchRunning.assert_any_call(True)
    widget.setReferenceBatchRunning.assert_any_call(False)


def test_reference_all_abort_between_axes_stops_remaining_axes():
    manager = _Positioner(axes=('X', 'Y'), position={'X': 1.0, 'Y': 2.0})
    widget = MagicMock()
    widget.getReferenceTargetModes.return_value = {
        ('Stage', 'X'): 'default',
        ('Stage', 'Y'): 'default',
    }
    widget.confirmReferenceBatch.return_value = True
    ctrl = _controller([('Stage', manager)], widget)
    ctrl._scheduleReferenceBatchContinue = MagicMock()

    assert ctrl.referenceAllPositioners() is True
    assert ctrl.abortReferenceBatch() is True

    ctrl._continueReferenceBatch()

    assert manager.referenceCalls == [('X', None)]
    assert manager.isAxisReferenced('Y') is False
    assert ctrl._referenceBatchRunning is False


def test_reference_all_stops_on_reference_failure():
    bad = _Positioner(failReference=True)
    good = _Positioner()
    widget = MagicMock()
    widget.getReferenceTargetModes.return_value = {
        ('Bad', 'X'): 'default',
        ('Good', 'X'): 'default',
    }
    widget.confirmReferenceBatch.return_value = True
    ctrl = _controller([('Bad', bad), ('Good', good)], widget)
    ctrl._scheduleReferenceBatchContinue = MagicMock()

    assert ctrl.referenceAllPositioners() is True

    assert bad.referenceCalls == [('X', None)]
    assert good.referenceCalls == []
    assert ctrl._referenceBatchRunning is False
    ctrl._scheduleReferenceBatchContinue.assert_not_called()
