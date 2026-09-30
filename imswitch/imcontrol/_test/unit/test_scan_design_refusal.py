"""A scan design the scan manager refuses ends the request with its reason.

``makeFullScan`` used to log why it refused a design ("Scan would take 20.8
min, above the 1 min cap (scan.maxScanTimeMin).") and return None. The Base,
PointScan and MoNaLISA controllers unpacked that None: the refusal surfaced as
``TypeError: cannot unpack non-iterable NoneType object`` with a traceback,
after the run had been reserved and published, and a script calling
``runScanAndWait`` saw only "Scan controller failed before the requested run
armed." The Advanced controller did not crash but lost the reason the same
way. On a fresh install this is the first thing a script meets:
hamamatsu_mock_scan_setup.json caps scans at 1 min and the Base widget's
defaults (5 x 5 x 5 um at 0.1 um, 10 ms per position) take 20.8.

Now the manager raises ScanDesignRefusedError with the reason. A new run is
designed before anything about it is published, so the refusal is a refused
start: sigScanRequestRejected carries the reason and an API caller gets
ScanRequestRejectedError with it. A continuation of a run already underway
fails that run, with the reason as its requests' failure message.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcommon.framework import SignalInterface
from imswitch.imcontrol._test import setupInfoBasic
from imswitch.imcontrol.controller.WorkflowServices import (
    ScanRequestCompletion,
    ScanWorkflowService,
)
from imswitch.imcontrol.controller.basecontrollers import (
    ImConWidgetController,
    SuperScanController,
)
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.controller.controllers.ScanControllerBase import (
    ScanControllerBase,
)
from imswitch.imcontrol.controller.controllers.ScanControllerMoNaLISA import (
    ScanControllerMoNaLISA,
)
from imswitch.imcontrol.controller.controllers.ScanControllerPointScan import (
    ScanControllerPointScan,
)
from imswitch.imcontrol.controller.controllers.WorkflowFacadeController import (
    WorkflowFacadeController,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_controller_point_scan_layouts,
)
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.managers.ScanManagerBase import ScanManagerBase
from imswitch.imcontrol.model.managers.ScanManagerMoNaLISA import (
    ScanManagerMoNaLISA,
)
from imswitch.imcontrol.model.managers.ScanManagerPointScan import (
    ScanManagerPointScan,
)
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)
from imswitch.imcontrol.model.scan_request import ScanRequestRejectedError
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.basesignaldesigners import (
    ScanDesigner,
)

REASON = 'Scan would take 20.8 min, above the 1 min cap (scan.maxScanTimeMin).'


def _cappedSetup(minutes=1):
    scan = type(setupInfoBasic.scan)(
        **{**setupInfoBasic.scan.__dict__, 'maxScanTimeMin': minutes}
    )
    return type(setupInfoBasic)(**{**setupInfoBasic.__dict__, 'scan': scan})


def _defaultWidgetScan():
    """What the Base scan widget shows on a fresh install."""
    analog = {
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [5.0, 5.0, 5.0],
        'axis_step_size': [0.1, 0.1, 0.1],
        'axis_centerpos': [0.0, 0.0, 0.0],
        'axis_startpos': [[0], [0], [0]],
        'sequence_time': 0.01,
    }
    digital = {
        'target_device': [], 'TTL_start': [], 'TTL_end': [],
        'sequence_time': 0.01,
    }
    return analog, digital


# --------------------------------------------------------------------------
# Designers and managers say why
# --------------------------------------------------------------------------

def test_the_beta_designer_says_why_a_scan_is_too_long():
    analog, _ = _defaultWidgetScan()
    designer = BetaScanDesigner()

    assert designer.signalLengthRefusal(analog, _cappedSetup()) == REASON
    assert designer.checkSignalLength(analog, _cappedSetup()) is False
    assert designer.signalLengthRefusal(analog, _cappedSetup(30)) == ''
    assert designer.checkSignalLength(analog, _cappedSetup(30)) is True


def test_the_galvo_designer_says_why_a_scan_is_too_long():
    analog, _ = _defaultWidgetScan()

    refusal = GalvoScanDesigner().signalLengthRefusal(analog, _cappedSetup())

    assert 'above the 1 min cap (scan.maxScanTimeMin)' in refusal
    assert GalvoScanDesigner().checkSignalLength(analog, _cappedSetup()) is False


def test_a_designer_that_only_answers_yes_or_no_still_gives_a_reason():
    class YesNoDesigner(ScanDesigner):
        def __init__(self):
            super().__init__()
            self._expectedParameters = []

        def checkSignalLength(self, scanParameters, setupInfo):
            return False

        def checkSignalComp(self, scanParameters, setupInfo, scanInfo):
            return True

        def make_signal(self, parameterDict, setupInfo):
            raise AssertionError('a refused scan is never generated')

    assert 'Signal too long' in YesNoDesigner().signalLengthRefusal({}, None)


@pytest.mark.parametrize(
    'managerClass',
    [ScanManagerBase, ScanManagerMoNaLISA, ScanManagerPointScan],
)
def test_every_scan_manager_raises_the_reason_instead_of_returning_none(
    managerClass,
):
    analog, digital = _defaultWidgetScan()
    manager = managerClass(_cappedSetup())

    with pytest.raises(ScanDesignRefusedError) as refused:
        manager.makeFullScan(analog, digital)

    assert str(refused.value) == REASON


def test_recording_layouts_carry_the_reason_too():
    """Scan-mode recording asks for acquisition layouts before the scan. It
    reported "Scan signal generation did not produce ScanInfoContract
    metadata"; the reason now reaches its failure message instead."""
    analog, digital = _defaultWidgetScan()
    controller = SimpleNamespace(
        getParameters=lambda: None,
        _analogParameterDict=analog,
        _digitalParameterDict=digital,
        _master=SimpleNamespace(scanManager=ScanManagerBase(_cappedSetup())),
    )

    with pytest.raises(ScanDesignRefusedError, match='above the 1 min cap'):
        build_controller_point_scan_layouts(controller, ['Camera'])


# --------------------------------------------------------------------------
# Controllers: a refused design is a refused start, or a failed continuation
# --------------------------------------------------------------------------

class _Signal:
    def __init__(self, log, name):
        self.log = log
        self.name = name
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        self.log.append((self.name,) + args)
        for slot in list(self.slots):
            slot(*args)


class _Channel:
    """The CommunicationChannel surface the scan request path touches."""

    def __init__(self):
        self.lifecycle = []
        for name in ('sigScanStarting', 'sigScanStarted', 'sigScanDone',
                     'sigScanEnded', 'sigRunScan', 'sigScanRequestRejected'):
            setattr(self, name, _Signal(self.lifecycle, name))
        self.source = None
        self.activeSource = None
        self.scanWorkflow = ScanWorkflowService(self)

    def setActiveScanSource(self, source):
        self.activeSource = source

    def clearActiveScanSource(self, source):
        if self.activeSource is source:
            self.activeSource = None

    def getActiveScanSource(self):
        return self.activeSource

    def getRecordingScanSource(self, preferredKey=None):
        return self.source

    def getRecordingScanSourceNames(self):
        return ['Scan']

    def controllerRegistry(self):
        return {'Scan': self.source}


class _Widget:
    def __init__(self):
        self.buttonStates = []

    def setScanButtonChecked(self, checked):
        self.buttonStates.append(checked)

    def setScanMode(self):
        pass

    def setRepeatEnabled(self, _enabled):
        pass

    def isContLaserMode(self):
        return False

    def repeatEnabled(self):
        return False


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args, exc_info=False, **_kwargs):
        self.errors.append((str(message) % args if args else str(message),
                            bool(exc_info)))

    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


def _scanController(controllerClass, channel):
    """A scan controller whose parameters are the refused default design.

    Base, PointScan and MoNaLISA design through the real scan manager of
    their family; Advanced through its own designer instances. Either way
    the real Beta designer refuses, capped at 1 min.
    """
    setup = _cappedSetup()
    managerClass = {
        ScanControllerBase: ScanManagerBase,
        ScanControllerPointScan: ScanManagerPointScan,
        ScanControllerMoNaLISA: ScanManagerMoNaLISA,
        ScanControllerAdvanced: ScanManagerBase,
    }[controllerClass]
    analog, digital = _defaultWidgetScan()
    ctrl = controllerClass.__new__(controllerClass)
    # The QObject only: attribute defaults (getattr) and thread() must work.
    SignalInterface.__init__(ctrl)
    ctrl.__dict__.update(
        _commChannel=channel,
        _setupInfo=setup,
        _master=SimpleNamespace(scanManager=managerClass(setup)),
        _widget=_Widget(),
        _logger=_Logger(),
        _scanCoordinator=ScanExecutionCoordinator(None, None),
        _scanRunToken=None,
        _scanRunStartingPublished=False,
        _analogParameterDict=analog,
        _digitalParameterDict=digital,
        _lastBuiltParams=None,
        signalDict=None,
        scanInfoDict=None,
        TTLDevices={},
    )
    # The parameters above are what the widget would hand over.
    ctrl.getParameters = lambda: None
    channel.source = ctrl
    return ctrl


def _facade(channel):
    facade = WorkflowFacadeController.__new__(WorkflowFacadeController)
    ImConWidgetController.__init__(
        facade, setupInfo=SimpleNamespace(), commChannel=channel,
        master=SimpleNamespace(), widget=None, factory=None,
        moduleCommChannel=None,
    )
    return facade


def _names(lifecycle):
    return [event[0] for event in lifecycle]


CONTROLLERS = [
    ScanControllerBase,
    ScanControllerPointScan,
    ScanControllerMoNaLISA,
    ScanControllerAdvanced,
]


@pytest.mark.parametrize('controllerClass', CONTROLLERS)
def test_an_api_scan_with_a_refused_design_is_rejected_with_the_reason(
    controllerClass, qtbot,
):
    channel = _Channel()
    ctrl = _scanController(controllerClass, channel)

    with pytest.raises(ScanRequestRejectedError) as rejected:
        _facade(channel).runScan()

    # runScanAndWait re-raises this RuntimeError as it is.
    assert str(rejected.value) == REASON
    # The dispatcher's own start is paired; the controller published nothing
    # of a run, only the refusal, with its reason.
    assert channel.lifecycle == [
        ('sigScanStarting',),
        ('sigScanRequestRejected', REASON),
        ('sigScanEnded',),
    ]
    # Refused cleanly: no traceback, no failed run, the run given back.
    assert ctrl._logger.errors == [(f'Scan not started: {REASON}', False)]
    assert ctrl._scanCoordinator.activeRunToken is None
    assert ctrl.isRunning is False
    assert ctrl._widget.buttonStates == []


@pytest.mark.parametrize('controllerClass', CONTROLLERS)
def test_the_scan_button_with_a_refused_design_publishes_only_the_refusal(
    controllerClass,
):
    channel = _Channel()
    ctrl = _scanController(controllerClass, channel)

    SuperScanController.runScan(ctrl)

    assert channel.lifecycle == [('sigScanRequestRejected', REASON)]
    assert ctrl._logger.errors == [(f'Scan not started: {REASON}', False)]
    assert ctrl._scanCoordinator.activeRunToken is None
    assert ctrl._scanRunToken is None
    assert ctrl._widget.buttonStates == []


@pytest.mark.parametrize('controllerClass', CONTROLLERS)
def test_a_refused_repeat_frame_fails_the_run_with_the_reason(
    controllerClass,
):
    """A repeat frame re-designs the scan. Once the run is underway the
    refusal can no longer be a refused start: the run fails, publishes its
    end, and the request waiting on it learns why."""
    channel = _Channel()
    ctrl = _scanController(controllerClass, channel)
    coordinator = ctrl._scanCoordinator
    runToken = coordinator.reserveRun(ctrl)
    request = ScanRequestCompletion(ctrl)
    request.bind(runToken)
    ctrl.__dict__.update(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _pendingExternalScanRequestCompletions=[request],
    )

    controllerClass.runScanAdvanced(ctrl, sigScanStartingEmitted=True)

    assert request.wait(timeout=0) is True
    assert request.successful is False
    assert request.message == REASON
    assert _names(channel.lifecycle) == ['sigScanEnded']
    assert coordinator.activeRunToken is None
    assert ctrl._widget.buttonStates == [False]
    assert (f'Scan stopped: {REASON}', False) in ctrl._logger.errors
    assert not any('Traceback' in message for message, _ in ctrl._logger.errors)


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
