"""Focused no-hardware tests for scan/reference integration."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.ScanControllerMoNaLISA import (
    ScanControllerMoNaLISA,
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


def test_external_preflight_without_coordinator_leaves_reference_to_dialog():
    ctrl = _bare_controller()
    ctrl._scanCompletionPublishing = False
    ctrl._isRunningFlag = False
    ctrl._scanCoordinator = None

    assert ctrl._externalScanStartRefusal() == ''


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
