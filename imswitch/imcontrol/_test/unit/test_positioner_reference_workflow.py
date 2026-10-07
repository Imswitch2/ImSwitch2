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
        restoredAxes=(),
    ):
        self.axes = list(axes)
        self.position = dict(position or {axis: 0.0 for axis in self.axes})
        self._referenced = dict(referenced or {axis: False for axis in self.axes})
        self.requiresReference = requiresReference
        self.failReference = failReference
        self.referenceWaitAfterS = referenceWaitAfterS
        self.defaultReferenceVoltage = defaultReferenceVoltage
        self._conversionFactor = conversionFactor
        self._restoredAxes = set(restoredAxes)
        self.referenceCalls = []
        self.forPositioning = True
        self.hide = False
        self.positionUnit = 'um'

    @property
    def isReferenceActionable(self):
        return bool(
            self.requiresReference
            and self.forPositioning
            and not self.hide
        )

    @property
    def defaultReferencePosition(self):
        if self.defaultReferenceVoltage is None:
            return None
        return self.defaultReferenceVoltage * self._conversionFactor

    def positionToVoltage(self, position):
        return position / self._conversionFactor

    def isAxisReferenced(self, axis):
        return self._referenced[axis]

    def isPositionRestored(self, axis):
        return axis in self._restoredAxes

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
        self._restoredAxes.discard(axis)


class _ReferencePredicatePositioner(PositionerManager):
    def __init__(self, *, requiresReference=True, forPositioning=True, hide=False):
        self.requiresReference = requiresReference
        positionerInfo = SimpleNamespace(
            axes=['X'],
            forPositioning=forPositioning,
            forScanning=True,
            resetOnClose=False,
            joystick=False,
            liveUpdate=False,
            hide=hide,
            shortcutModifier=None,
        )
        super().__init__(positionerInfo, 'Stage', {'X': 0.0})

    def move(self, dist: float, axis: str):
        pass

    def setPosition(self, position: float, axis: str):
        pass


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


def test_positioner_manager_reference_actionable_predicate():
    assert _ReferencePredicatePositioner(
        requiresReference=True, forPositioning=True, hide=False
    ).isReferenceActionable is True
    assert _ReferencePredicatePositioner(
        requiresReference=False, forPositioning=True, hide=False
    ).isReferenceActionable is False
    assert _ReferencePredicatePositioner(
        requiresReference=True, forPositioning=False, hide=False
    ).isReferenceActionable is False
    assert _ReferencePredicatePositioner(
        requiresReference=True, forPositioning=True, hide=True
    ).isReferenceActionable is False


def test_reference_button_hidden_when_no_reference_capable_positioners(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    ctrl = _controller([('Stage', _Positioner(requiresReference=False))], widget)

    ctrl._refreshReferenceStatus()

    assert widget.pars['ReferenceButton'].isHidden()
    assert not widget.pars['ReferenceButton'].isEnabled()


def test_reference_status_uses_actionable_reference_predicate():
    scanOnly = _Positioner(requiresReference=True)
    scanOnly.forPositioning = False
    hidden = _Positioner(requiresReference=True)
    hidden.hide = True
    manual = _Positioner(requiresReference=True)
    ctrl = _controller([
        ('ScanOnly', scanOnly),
        ('Hidden', hidden),
        ('Manual', manual),
    ])

    status = ctrl._getReferenceAxesStatus()

    assert [axisInfo['positionerName'] for axisInfo in status] == ['Manual']


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


def test_startup_reference_dialog_reports_unreferenced_and_restored_axes_without_moving():
    restored = _Positioner(
        position={'X': 20.0},
        referenced={'X': False},
        defaultReferenceVoltage=0.0,
        restoredAxes=('X',),
    )
    plain = _Positioner(
        position={'Y': 0.0},
        axes=('Y',),
        referenced={'Y': False},
        defaultReferenceVoltage=0.0,
    )
    widget = MagicMock()
    ctrl = _controller([('StageX', restored), ('StageY', plain)], widget)

    assert ctrl.openStartupReferenceDialogIfNeeded() is True

    widget.showReferenceDialog.assert_called_once()
    header = widget.showReferenceDialog.call_args.kwargs['startupHeader']
    assert '2 open-loop axes are unreferenced.' in header
    assert '1 displayed position was restored from persisted last commands.' in header
    assert 'have not been verified against hardware' in header
    assert restored.referenceCalls == []
    assert plain.referenceCalls == []


def test_startup_reference_dialog_does_not_call_out_persisted_value_matching_default():
    manager = _Positioner(
        position={'X': 0.0},
        referenced={'X': False},
        defaultReferenceVoltage=0.0,
        restoredAxes=('X',),
    )
    widget = MagicMock()
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.openStartupReferenceDialogIfNeeded() is True

    header = widget.showReferenceDialog.call_args.kwargs['startupHeader']
    assert '1 open-loop axis is unreferenced.' in header
    assert 'restored from persisted last commands' not in header
    assert 'Do you want to reference it now?' in header


def test_startup_reference_dialog_is_skipped_when_all_axes_are_referenced():
    manager = _Positioner(referenced={'X': True})
    widget = MagicMock()
    ctrl = _controller([('Stage', manager)], widget)

    assert ctrl.openStartupReferenceDialogIfNeeded() is False

    widget.showReferenceDialog.assert_not_called()


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
        'displayedPositionRestored': False,
        'preferredTargetMode': 'default',
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
    assert status[0]['preferredTargetMode'] == 'displayed'


def test_restored_position_is_labeled_and_preferred_when_it_differs_from_default():
    manager = _Positioner(
        position={'X': 12.5},
        defaultReferenceVoltage=5.0,
        restoredAxes=('X',),
    )
    ctrl = _controller([('Stage', manager)])

    status = ctrl._getReferenceAxesStatus()[0]

    assert status['displayedPositionRestored'] is True
    assert status['displayedTargetLabel'] == 'Persisted last command (12.5um, 1.25V)'
    assert status['preferredTargetMode'] == 'displayed'


def test_restored_position_equal_to_default_collapses_duplicate_target():
    manager = _Positioner(
        position={'X': 50.0},
        defaultReferenceVoltage=5.0,
        restoredAxes=('X',),
    )
    ctrl = _controller([('Stage', manager)])

    status = ctrl._getReferenceAxesStatus()[0]

    assert status['defaultTargetLabel'] == (
        'Default (50um, 5V, matches last command)'
    )
    assert status['displayedTargetLabel'] is None
    assert status['displayedPositionRestored'] is True
    assert status['preferredTargetMode'] == 'default'


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


def test_reference_target_combo_hides_duplicate_persisted_target(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    combo = QtWidgets.QComboBox()
    qtbot.addWidget(combo)

    widget._populateReferenceTargetCombo(combo, {
        'defaultTargetLabel': 'Default (0um, 0V, matches last command)',
        'displayedTargetLabel': None,
        'preferredTargetMode': 'default',
    })

    assert combo.count() == 1
    assert combo.itemData(0) == 'default'
    assert combo.itemText(0) == 'Default (0um, 0V, matches last command)'


def test_reference_target_combo_uses_preferred_mode_only_on_initial_population(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    combo = QtWidgets.QComboBox()
    qtbot.addWidget(combo)
    axisInfo = {
        'defaultTargetLabel': 'Default (0um, 0V)',
        'displayedTargetLabel': 'Persisted last command (2um, 0.2V)',
        'preferredTargetMode': 'displayed',
    }

    widget._populateReferenceTargetCombo(combo, axisInfo)
    assert combo.currentData() == 'displayed'

    combo.setCurrentIndex(combo.findData('default'))
    widget._populateReferenceTargetCombo(combo, axisInfo)
    assert combo.currentData() == 'default'


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
            'displayedPositionRestored': False,
            'preferredTargetMode': 'default',
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
