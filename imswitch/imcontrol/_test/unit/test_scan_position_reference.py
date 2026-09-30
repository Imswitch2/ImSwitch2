"""Focused no-hardware tests for scan/reference integration."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.controller.controllers.ScanControllerMoNaLISA import (
    ScanControllerMoNaLISA,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_controller_point_scan_layouts,
)

pytestmark = pytest.mark.nohardware


class _ConcreteScanController(SuperScanController):
    def setParameters(self):
        pass

    def getParameters(self):
        pass

    def updatePixels(self):
        pass

    def emitScanSignal(self, signal, *args):
        pass

    def runScanAdvanced(self, *, recalculateSignals=True,
                        isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted=False):
        pass

    def scanDone(self):
        pass


def _bare_controller():
    ctrl = _ConcreteScanController.__new__(_ConcreteScanController)
    ctrl._analogParameterDict = {'target_device': ['Stage']}
    ctrl._digitalParameterDict = {}
    ctrl._positionersScan = []
    ctrl._logger = MagicMock()
    return ctrl


class _ScanPositioner:
    def __init__(self, axes=('X',), *, referenceActionable=True, referenced=None):
        self.axes = list(axes)
        self.isReferenceActionable = referenceActionable
        self._referenced = dict(referenced or {axis: False for axis in self.axes})

    def isAxisReferenced(self, axis):
        return self._referenced[axis]


def test_common_build_forgets_runtime_position_snapshot_even_on_success():
    ctrl = _bare_controller()
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[3.0]]
    )
    ctrl._master = SimpleNamespace(
        scanManager=SimpleNamespace(makeFullScan=lambda *_args, **_kwargs: ({}, {}))
    )

    result = SuperScanController._buildScanSignals(ctrl)

    assert result == ({}, {})
    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_common_build_forgets_runtime_position_snapshot_when_design_raises():
    ctrl = _bare_controller()
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[3.0]]
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError('design failed')

    ctrl._master = SimpleNamespace(scanManager=SimpleNamespace(makeFullScan=fail))

    with pytest.raises(RuntimeError, match='design failed'):
        SuperScanController._buildScanSignals(ctrl)

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_monalisa_build_forgets_runtime_position_snapshot():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    ctrl._analogParameterDict = {'target_device': ['Stage']}
    ctrl._digitalParameterDict = {}
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[3.0]]
    )
    ctrl._widget = SimpleNamespace(isContLaserMode=lambda: False)
    ctrl._master = SimpleNamespace(
        scanManager=SimpleNamespace(makeFullScan=lambda *_args, **_kwargs: ({}, {}))
    )

    ScanControllerMoNaLISA._buildScanSignals(ctrl)

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_position_snapshot_context_restores_outer_snapshot_when_nested():
    ctrl = _bare_controller()
    captures = iter(([[1.0]], [[2.0]]))
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', next(captures)
    )

    with ctrl._positionSnapshotForScanDesign():
        assert ctrl._analogParameterDict['axis_position_before_scan'] == [[1.0]]

        with ctrl._positionSnapshotForScanDesign():
            assert ctrl._analogParameterDict['axis_position_before_scan'] == [[2.0]]

        assert ctrl._analogParameterDict['axis_position_before_scan'] == [[1.0]]

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_position_snapshot_context_restores_existing_value_if_capture_raises():
    ctrl = _bare_controller()
    ctrl._analogParameterDict['axis_position_before_scan'] = [[11.0]]

    def fail_capture():
        ctrl._analogParameterDict['axis_position_before_scan'] = [[99.0]]
        raise RuntimeError('capture failed')

    ctrl._capturePositionersBeforeScan = fail_capture

    with pytest.raises(RuntimeError, match='capture failed'):
        with ctrl._positionSnapshotForScanDesign():
            pass

    assert ctrl._analogParameterDict['axis_position_before_scan'] == [[11.0]]


def test_advanced_acquisition_layout_design_receives_position_snapshot():
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    ctrl._analogParameterDict = {'target_device': ['Stage']}
    ctrl._digitalParameterDict = {}
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[7.0]]
    )

    def make_full_scan(analog, _digital):
        assert analog['axis_position_before_scan'] == [[7.0]]
        return None, None

    ctrl._make_full_scan = make_full_scan

    with pytest.raises(
        RuntimeError,
        match='did not produce layout metadata',
    ):
        ctrl.getAcquisitionLayouts(('Camera',))

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_advanced_scan_cache_tracks_position_snapshot():
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    ctrl._analogParameterDict = {'target_device': ['Stage']}
    ctrl._digitalParameterDict = {}
    ctrl.getParameters = MagicMock()
    current_position = [3.0]
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[current_position[0]]]
    )
    ctrl.signalDict = None
    ctrl.scanInfoDict = None
    ctrl._lastBuiltParams = None
    builds = []

    def make_full_scan(_analog, _digital):
        builds.append(current_position[0])
        return {'scanSignalsDict': {}}, {'position': current_position[0]}

    ctrl._make_full_scan = make_full_scan

    first = ctrl._buildScanSignals()
    ctrl.signalDict, ctrl.scanInfoDict = first
    second = ctrl._buildScanSignals()

    current_position[0] = 4.0
    third = ctrl._buildScanSignals()

    assert second == first
    assert third[1]['position'] == 4.0
    assert builds == [3.0, 4.0]
    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_point_scan_layout_design_receives_position_snapshot():
    ctrl = _bare_controller()
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[4.0]]
    )

    def make_full_scan(analog, _digital):
        assert analog['axis_position_before_scan'] == [[4.0]]
        return None, None

    ctrl._master = SimpleNamespace(
        scanManager=SimpleNamespace(makeFullScan=make_full_scan)
    )

    with pytest.raises(
        RuntimeError,
        match='did not produce ScanInfoContract metadata',
    ):
        build_controller_point_scan_layouts(ctrl, ('Camera',))

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict


def test_advanced_plot_design_receives_position_snapshot():
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    ctrl._analogParameterDict = {
        'target_device': ['Stage'],
        'scan_dim_target_device': ['Stage'],
    }
    ctrl._digitalParameterDict = {}
    ctrl.settingParameters = False
    ctrl.getParameters = MagicMock()
    ctrl._capturePositionersBeforeScan = lambda: ctrl._analogParameterDict.__setitem__(
        'axis_position_before_scan', [[9.0]]
    )
    ctrl._widget = SimpleNamespace(isPlotTTLIncluded=lambda: True)
    ctrl._logger = MagicMock()

    def make_full_scan(analog, _digital):
        assert analog['axis_position_before_scan'] == [[9.0]]
        return {'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}}, {}

    ctrl._make_full_scan = make_full_scan

    ctrl.plotScanCurves()

    assert 'axis_position_before_scan' not in ctrl._analogParameterDict
    ctrl._logger.warning.assert_called_with('No scan curves to plot')


def test_beta_does_not_preposition_non_scanned_axes_to_raw_center():
    ctrl = _bare_controller()
    stage = MagicMock()
    ctrl._master = SimpleNamespace(positionersManager={'Stage': stage})
    ctrl._setupInfo = SimpleNamespace(
        scan=SimpleNamespace(scanDesigner='BetaScanDesigner')
    )
    ctrl._analogParameterDict = {
        'target_device': ['Stage'],
        'axis_centerpos': [5.0],
    }

    ctrl._setNonScanPositionersToCenter()

    stage.setPosition.assert_not_called()


def test_non_beta_scan_keeps_legacy_non_scanned_center_positioning():
    ctrl = _bare_controller()
    stage = MagicMock()
    ctrl._master = SimpleNamespace(positionersManager={'Stage': stage})
    ctrl._setupInfo = SimpleNamespace(
        scan=SimpleNamespace(scanDesigner='GalvoScanDesigner')
    )
    ctrl._analogParameterDict = {
        'target_device': ['Stage'],
        'axis_centerpos': [5.0],
    }

    ctrl._setNonScanPositionersToCenter()

    stage.setPosition.assert_called_once_with(5.0, 0)


def test_external_preflight_refuses_unreferenced_axes_without_dialog():
    ctrl = _bare_controller()
    stage = _ScanPositioner(axes=('X',), referenced={'X': False})
    ctrl._scanCompletionPublishing = False
    ctrl.isRunning = False
    ctrl._scanCoordinator = None
    ctrl._suppressUnreferencedScanWarning = False
    ctrl.positioners = {'Stage': stage}
    ctrl._master = SimpleNamespace(positionersManager={'Stage': stage})
    ctrl._setupInfo = SimpleNamespace(
        positioners={'Stage': SimpleNamespace(axes=['X'])}
    )
    ctrl._widget = SimpleNamespace(
        confirmUnreferencedScan=MagicMock(
            side_effect=AssertionError('external preflight must not open UI')
        )
    )

    refusal = ctrl._externalScanStartRefusal()

    assert 'unreferenced open-loop positioners' in refusal
    assert 'Stage (X)' in refusal
    ctrl._widget.confirmUnreferencedScan.assert_not_called()


def test_external_preflight_respects_session_acceptance_of_unreferenced_axes():
    ctrl = _bare_controller()
    ctrl._scanCompletionPublishing = False
    ctrl.isRunning = False
    ctrl._scanCoordinator = None
    ctrl._suppressUnreferencedScanWarning = True

    assert ctrl._externalScanStartRefusal() == ''


def test_scan_warning_uses_actionable_reference_predicate():
    ctrl = _bare_controller()
    scanOnly = _ScanPositioner(
        axes=('X',), referenceActionable=False, referenced={'X': False}
    )
    manual = _ScanPositioner(
        axes=('Y',), referenceActionable=True, referenced={'Y': False}
    )
    alreadyReferenced = _ScanPositioner(
        axes=('Z',), referenceActionable=True, referenced={'Z': True}
    )
    ctrl.positioners = {
        'ScanOnly': scanOnly,
        'Manual': manual,
        'AlreadyReferenced': alreadyReferenced,
    }
    ctrl._master = SimpleNamespace(
        positionersManager={
            'ScanOnly': scanOnly,
            'Manual': manual,
            'AlreadyReferenced': alreadyReferenced,
        }
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={
            'ScanOnly': SimpleNamespace(axes=['X']),
            'Manual': SimpleNamespace(axes=['Y']),
            'AlreadyReferenced': SimpleNamespace(axes=['Z']),
        }
    )

    assert ctrl._getUnreferencedScanAxes() == [('Manual', 'Y')]


def test_unreferenced_scan_confirmation_suppresses_on_continue():
    ctrl = _bare_controller()
    unreferenced = [('Stage', 'X')]
    ctrl._suppressUnreferencedScanWarning = False
    ctrl._getUnreferencedScanAxes = MagicMock(return_value=unreferenced)
    ctrl._widget = SimpleNamespace(
        confirmUnreferencedScan=MagicMock(return_value=(True, True))
    )

    assert ctrl._confirmUnreferencedScanIfNeeded() is True

    ctrl._widget.confirmUnreferencedScan.assert_called_once_with(unreferenced)
    assert ctrl._suppressUnreferencedScanWarning is True


def test_unreferenced_scan_confirmation_cancel_leaves_warning_enabled():
    ctrl = _bare_controller()
    unreferenced = [('Stage', 'X')]
    ctrl._suppressUnreferencedScanWarning = False
    ctrl._getUnreferencedScanAxes = MagicMock(return_value=unreferenced)
    ctrl._widget = SimpleNamespace(
        confirmUnreferencedScan=MagicMock(return_value=(False, True))
    )

    assert ctrl._confirmUnreferencedScanIfNeeded() is False

    ctrl._widget.confirmUnreferencedScan.assert_called_once_with(unreferenced)
    assert ctrl._suppressUnreferencedScanWarning is False


def test_run_scan_uses_shared_reference_preflight():
    ctrl = _bare_controller()
    ctrl._confirmUnreferencedScanIfNeeded = MagicMock(return_value=False)
    ctrl.runScanAdvanced = MagicMock()

    ctrl.runScan()

    ctrl._confirmUnreferencedScanIfNeeded.assert_called_once_with()
    ctrl.runScanAdvanced.assert_not_called()


def test_external_scan_uses_shared_reference_preflight_before_widget_changes():
    ctrl = _bare_controller()
    workflowResults = []
    ctrl._commChannel = SimpleNamespace(
        scanWorkflow=SimpleNamespace(
            report_scan_request_result=lambda *args: workflowResults.append(args)
        )
    )
    ctrl._widget = SimpleNamespace(
        setScanMode=MagicMock(),
        setRepeatEnabled=MagicMock(),
    )
    ctrl._scanCompletionPublishing = False
    ctrl._isRunningFlag = False
    ctrl._scanCoordinator = None
    ctrl._confirmUnreferencedScanIfNeeded = MagicMock(return_value=False)
    ctrl.runScanAdvanced = MagicMock()

    ctrl.runScanExternal(True, False)

    ctrl._confirmUnreferencedScanIfNeeded.assert_called_once_with()
    ctrl._widget.setScanMode.assert_not_called()
    ctrl._widget.setRepeatEnabled.assert_not_called()
    ctrl.runScanAdvanced.assert_not_called()
    assert workflowResults[0][1] is False
    assert workflowResults[0][2] == (
        'Scan cancelled because unreferenced positioners were not accepted.'
    )


