"""Run-level scan ownership across controllers and MoNaLISA follow-ups."""

import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import (
    ImConWidgetController,
    SuperScanController,
)
from imswitch.imcontrol.controller.WorkflowServices import (
    ScanRequestCompletion,
)
from imswitch.imcontrol.controller.controllers.ScanControllerBase import (
    ScanControllerBase,
)
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.controller.controllers.ScanControllerMoNaLISA import (
    ScanControllerMoNaLISA,
)
from imswitch.imcontrol.controller.controllers.ScanControllerPointScan import (
    ScanControllerPointScan,
)
from imswitch.imcontrol.model.managers._scan_execution import (
    FINISH_ABORT,
    ScanBusyError,
    ScanExecutionCoordinator,
)


class _OwnedCoordinator:
    def __init__(self, owner):
        self.owner = owner
        self.token = object()
        self.resolved = []

    def tokenForOwner(self, owner):
        return self.token if owner is self.owner else None

    def resolve(self, token, mode, onComplete=None):
        self.resolved.append((token, mode, onComplete))


class _Logger:
    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _QtBarrierScanController(SuperScanController):
    """Concrete shell used to exercise QObject-affine terminal handoffs."""

    def setParameters(self):
        pass

    def getParameters(self):
        pass

    def updatePixels(self):
        pass

    def emitScanSignal(self, signal, *args):
        self.emissions.append((signal, threading.current_thread()))

    def runScanAdvanced(self, **_kwargs):
        pass

    def scanDone(self):
        self.doneThreads.append(threading.current_thread())


def _makeQtBarrierController():
    ctrl = _QtBarrierScanController.__new__(_QtBarrierScanController)
    ImConWidgetController.__init__(
        ctrl,
        setupInfo=SimpleNamespace(),
        commChannel=SimpleNamespace(),
        master=SimpleNamespace(),
        widget=SimpleNamespace(),
        factory=None,
        moduleCommChannel=None,
    )
    ctrl.doneThreads = []
    ctrl.failedThreads = []
    ctrl.emissions = []
    ctrl.scanFailed = (
        lambda **_kwargs:
        ctrl.failedThreads.append(threading.current_thread())
    )
    return ctrl


def test_broadcast_nidaq_done_is_ignored_by_non_owner_scan_controller():
    owner = SimpleNamespace()
    coordinator = _OwnedCoordinator(owner)
    nonOwner = SimpleNamespace(
        _scanCoordinator=coordinator,
        scanDone=lambda: (_ for _ in ()).throw(
            AssertionError('non-owner published scan completion')
        ),
    )

    SuperScanController._SuperScanController__onNidaqScanDone(nonOwner)

    assert coordinator.resolved == []


def test_same_owner_duplicate_start_does_not_rearm_or_end_active_run():
    runToken = object()
    iterationToken = object()

    class _Coordinator:
        reserveCalls = 0

        def runForOwner(self, _owner):
            return runToken

        def tokenForOwner(self, _owner):
            return iterationToken

        def reserveRun(self, _owner):
            self.reserveCalls += 1
            return runToken

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=_Coordinator(),
        _logger=_Logger(),
    )

    result = SuperScanController._beginScanRun(
        ctrl, sigScanStartingEmitted=False
    )

    assert result is None
    assert ctrl._scanCoordinator.reserveCalls == 0
    assert ctrl._scanRunToken is runToken


def test_internal_continuation_reuses_owner_run_in_iteration_gap():
    runToken = object()

    class _Coordinator:
        reserveCalls = 0

        def runForOwner(self, _owner):
            return runToken

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            self.reserveCalls += 1
            return runToken

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=_Coordinator(),
        _logger=_Logger(),
    )

    result = SuperScanController._beginScanRun(
        ctrl, sigScanStartingEmitted=True
    )

    assert result is runToken
    assert ctrl._scanCoordinator.reserveCalls == 1


def test_duplicate_user_start_in_iteration_gap_restores_not_running_state():
    runToken = object()

    class _Coordinator:
        def runForOwner(self, _owner):
            return runToken

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            raise AssertionError('duplicate must not reserve again')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=_Coordinator(),
        _logger=_Logger(),
        # runScanAdvanced sets this before calling _beginScanRun.
        isRunning=True,
    )

    result = SuperScanController._beginScanRun(
        ctrl, sigScanStartingEmitted=False
    )

    assert result is None
    assert ctrl.isRunning is False


def test_losing_controller_cannot_replace_the_real_active_scan_source():
    owner = object()

    class Channel:
        activeSource = owner

        def setActiveScanSource(self, source):
            self.activeSource = source

        def clearActiveScanSource(self, source):
            if self.activeSource is source:
                self.activeSource = None

    class Coordinator:
        def runForOwner(self, _owner):
            return None

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            raise RuntimeError('another controller owns the run')

    channel = Channel()
    widgetCalls = []
    ctrl = ScanControllerBase.__new__(ScanControllerBase)
    ctrl._commChannel = channel
    ctrl._scanCoordinator = Coordinator()
    ctrl._scanRunToken = None
    ctrl._scanRunStartingPublished = False
    ctrl._logger = _Logger()
    ctrl._widget = SimpleNamespace(
        setScanButtonChecked=lambda checked: widgetCalls.append(checked)
    )
    ctrl.scanFailed = lambda: setattr(ctrl, 'isRunning', False)

    ScanControllerBase.runScanAdvanced(
        ctrl, sigScanStartingEmitted=False
    )

    assert channel.activeSource is owner
    assert widgetCalls == []


def test_external_scan_request_reports_acceptance_after_run_reservation():
    runToken = object()
    reports = []

    class _Coordinator:
        def runForOwner(self, _owner):
            return None

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            return runToken

    ctrl = SimpleNamespace(
        _scanRunToken=None,
        _scanRunStartingPublished=False,
        _scanStopRequested=False,
        _scanCoordinator=_Coordinator(),
        _externalScanRequestInProgress=False,
        _externalScanRequestAccepted=False,
        _externalScanRequestFailed=False,
        _externalScanRequestFailureMessage='',
        _widget=SimpleNamespace(
            setScanMode=lambda: None,
            setRepeatEnabled=lambda _value: None,
        ),
        _commChannel=SimpleNamespace(
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            )
        ),
        _logger=_Logger(),
    )

    def runAdvanced(**_kwargs):
        SuperScanController._beginScanRun(
            ctrl, sigScanStartingEmitted=True
        )

    ctrl.runScanAdvanced = runAdvanced

    SuperScanController.runScanExternal(ctrl, True, False)

    assert len(reports) == 1
    assert reports[0][:4] == (ctrl, True, '', runToken)
    completion = reports[0][4]
    assert completion.owner is ctrl
    assert completion.runToken is runToken
    assert completion.wait(timeout=0) is False
    completions = (
        SuperScanController._detachExternalScanRequestCompletions(
            ctrl, runToken
        )
    )
    assert SuperScanController._completeExternalScanRequest(
        ctrl,
        runToken,
        successful=True,
        completions=completions,
    )
    assert completion.wait(timeout=0) is True
    assert completion.successful is True


def test_external_scan_request_reports_rejection_when_no_run_is_reserved():
    reports = []
    ctrl = SimpleNamespace(
        _externalScanRequestInProgress=False,
        _externalScanRequestAccepted=False,
        _externalScanRequestFailed=False,
        _externalScanRequestFailureMessage='',
        _widget=SimpleNamespace(
            setScanMode=lambda: None,
            setRepeatEnabled=lambda _value: None,
        ),
        _commChannel=SimpleNamespace(
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            )
        ),
        runScanAdvanced=lambda **_kwargs: None,
    )

    SuperScanController.runScanExternal(ctrl, True, False)

    assert reports[0][:2] == (ctrl, False)
    assert 'refused' in reports[0][2]
    assert reports[0][3] is None
    assert reports[0][4] is None


def test_external_request_refuses_active_iteration_before_widget_mutation():
    runToken = object()
    calls = []
    reports = []
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[],
        _scanCoordinator=SimpleNamespace(
            activeToken=object(),
            activeRunToken=runToken,
        ),
        _externalScanRequestInProgress=False,
        _externalScanRequestAccepted=False,
        _externalScanRequestFailed=False,
        _externalScanRequestFailureMessage='',
        isRunning=True,
        _widget=SimpleNamespace(
            setScanMode=lambda: calls.append('mode'),
            setRepeatEnabled=lambda value: calls.append(
                ('repeat', value)
            ),
        ),
        _commChannel=SimpleNamespace(
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            )
        ),
        runScanAdvanced=lambda **_kwargs: calls.append('run'),
    )

    SuperScanController.runScanExternal(ctrl, True, False)

    assert calls == []
    assert reports[0][1] is False
    assert 'active iteration' in reports[0][2]


def test_external_request_refuses_unresolved_same_run_request_in_idle_gap():
    runToken = object()
    firstCompletion = ScanRequestCompletion(object())
    firstCompletion.bind(runToken)
    calls = []
    reports = []
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _repeatPending=False,
        _pendingExternalScanRequestCompletions=[firstCompletion],
        _scanCoordinator=SimpleNamespace(
            activeToken=None,
            activeRunToken=runToken,
        ),
        _externalScanRequestInProgress=False,
        _externalScanRequestAccepted=False,
        _externalScanRequestFailed=False,
        _externalScanRequestFailureMessage='',
        isRunning=False,
        _widget=SimpleNamespace(
            setScanMode=lambda: calls.append('mode'),
            setRepeatEnabled=lambda value: calls.append(
                ('repeat', value)
            ),
        ),
        _commChannel=SimpleNamespace(
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            )
        ),
        runScanAdvanced=lambda **_kwargs: calls.append('run'),
    )

    SuperScanController.runScanExternal(ctrl, True, False)

    assert calls == []
    assert reports[0][1] is False
    assert 'unresolved external request' in reports[0][2]
    assert ctrl._pendingExternalScanRequestCompletions == [firstCompletion]
    assert firstCompletion.wait(timeout=0) is False


def test_continuation_setup_failure_terminalizes_idle_retained_run():
    runToken = object()
    reports = []
    terminalized = []

    class Coordinator:
        def runForOwner(self, _owner):
            return runToken

        def tokenForOwner(self, _owner):
            return None

    def failSetMode():
        raise RuntimeError('cannot configure continuation')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanCoordinator=Coordinator(),
        _externalScanRequestInProgress=False,
        _externalScanRequestAccepted=False,
        _externalScanRequestFailed=False,
        _externalScanRequestFailureMessage='',
        _widget=SimpleNamespace(
            setScanMode=failSetMode,
            setRepeatEnabled=lambda _value: None,
        ),
        _commChannel=SimpleNamespace(
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            )
        ),
        _logger=_Logger(),
        scanFailed=lambda: terminalized.append(runToken),
    )

    with pytest.raises(RuntimeError, match='cannot configure continuation'):
        SuperScanController.runScanExternal(ctrl, True, False)

    assert terminalized == [runToken]
    assert reports[0][1] is False


def test_scan_done_reentry_cannot_complete_the_next_request():
    runToken = object()
    first = ScanRequestCompletion(object())
    second = ScanRequestCompletion(object())
    first.bind(runToken)

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanStopRequested=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[first],
        _commChannel=SimpleNamespace(sigScanDone=object()),
        _logger=_Logger(),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )

    def emitAndStartNext(_signal):
        second.bind(runToken)
        ctrl._pendingExternalScanRequestCompletions.append(second)

    ctrl.emitScanSignal = emitAndStartNext

    SuperScanController._publishScanDone(
        ctrl, isFinalPart=False
    )

    assert first.wait(timeout=0) is True
    assert first.successful is True
    assert second.wait(timeout=0) is False
    assert ctrl._pendingExternalScanRequestCompletions == [second]


def test_aborted_scan_resolves_exact_request_as_failure():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)

    def finishRun(onReleased=None, **_kwargs):
        assert completion.wait(timeout=0) is False
        if onReleased is not None:
            onReleased()

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanStopRequested=True,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[completion],
        _commChannel=SimpleNamespace(sigScanDone=object()),
        _logger=_Logger(),
        emitScanSignal=lambda _signal: None,
        _finishScanRun=finishRun,
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )

    SuperScanController._publishScanDone(
        ctrl, isFinalPart=True
    )

    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert completion.message == 'Scan was aborted.'


def test_done_signal_reentrant_failure_cannot_resolve_detached_success():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    events = []

    class Coordinator:
        activeRunToken = runToken
        reserveCalls = 0

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.activeRunToken = None
            if onReleased is not None:
                onReleased()
            return True

        def runForOwner(self, _owner):
            return self.activeRunToken

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            self.reserveCalls += 1
            self.activeRunToken = object()
            return self.activeRunToken

    coordinator = Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _externalScanRequestInProgress=False,
        _externalScanRequestFailureMessage='',
        _pendingExternalScanRequestCompletions=[completion],
        _repeatPending=False,
        isRunning=True,
        doingNonFinalPartOfSequence=False,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _widget=SimpleNamespace(
            setScanButtonChecked=lambda _checked: None
        ),
        _logger=_Logger(),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )
    ctrl.scanFailed = (
        lambda **kwargs: SuperScanController.scanFailed(ctrl, **kwargs)
    )

    restartResults = []

    def emit(signal):
        events.append(signal)
        if signal == 'done':
            ctrl.scanFailed()
            restartResults.append(
                SuperScanController._beginScanRun(
                    ctrl, sigScanStartingEmitted=True
                )
            )

    ctrl.emitScanSignal = emit

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == ['done', 'ended']
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert ctrl._scanRunFailed is True
    assert ctrl._scanCompletionPublishing is False
    assert restartResults == [None]
    assert coordinator.reserveCalls == 0


def test_exact_completion_callback_observes_cleared_publication_gate():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    observed = []
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[completion],
        _commChannel=SimpleNamespace(sigScanDone='done'),
        _logger=_Logger(),
        emitScanSignal=lambda _signal: None,
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    completion.add_done_callback(
        lambda _resolved: observed.append(
            ctrl._scanCompletionPublishing
        )
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=False)

    assert completion.successful is True
    assert observed == [False]


def test_unproven_release_failure_keeps_exact_terminal_pending():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    events = []

    class Coordinator:
        activeRunToken = runToken
        releaseCalls = 0

        def runForOwner(self, _owner):
            return self.activeRunToken

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            raise AssertionError('failed retained run must not re-arm')

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.releaseCalls += 1
            raise RuntimeError('coordinator release failed')

    coordinator = Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _externalScanRequestInProgress=False,
        _externalScanRequestFailureMessage='',
        _pendingExternalScanRequestCompletions=[completion],
        _repeatPending=False,
        isRunning=True,
        doingNonFinalPartOfSequence=False,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _widget=SimpleNamespace(
            setScanButtonChecked=lambda _checked: None
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )
    ctrl.scanFailed = (
        lambda **kwargs: SuperScanController.scanFailed(ctrl, **kwargs)
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == ['done']
    assert coordinator.releaseCalls == 2
    assert completion.wait(timeout=0) is False
    assert ctrl._pendingExternalScanRequestCompletions == [completion]
    assert ctrl._scanRunToken is runToken
    assert ctrl._scanRunStartingPublished is True
    assert ctrl._scanRunFailed is True
    assert ctrl._scanCompletionPublishing is False
    assert SuperScanController._beginScanRun(
        ctrl, sigScanStartingEmitted=True
    ) is None


def test_scan_failure_widget_error_cannot_strand_exact_terminal():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    events = []

    class Coordinator:
        activeRunToken = runToken

        def tokenForOwner(self, _owner):
            return None

        def runForOwner(self, _owner):
            return self.activeRunToken

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.activeRunToken = None
            if onReleased is not None:
                onReleased()
            return True

    def failWidgetCleanup(_checked):
        raise RuntimeError('widget cleanup failed')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _externalScanRequestInProgress=False,
        _externalScanRequestFailureMessage='hardware arm failed',
        _pendingExternalScanRequestCompletions=[completion],
        _scanCompletionPublishing=False,
        _repeatPending=False,
        isRunning=True,
        doingNonFinalPartOfSequence=True,
        _scanCoordinator=Coordinator(),
        _commChannel=SimpleNamespace(sigScanEnded='ended'),
        _widget=SimpleNamespace(
            setScanButtonChecked=failWidgetCleanup
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    SuperScanController.scanFailed(ctrl)

    assert events == ['ended']
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert completion.message == 'hardware arm failed'
    assert ctrl._scanCompletionPublishing is False


def test_end_signal_failure_resolves_exact_request_as_failure():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    events = []

    class Coordinator:
        activeRunToken = runToken

        def tokenForOwner(self, _owner):
            return None

        def runForOwner(self, _owner):
            return self.activeRunToken

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.activeRunToken = None
            if onReleased is not None:
                onReleased()
            return True

    def emit(signal):
        events.append(signal)
        if signal == 'ended':
            raise RuntimeError('end listener failed')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[completion],
        _scanCoordinator=Coordinator(),
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _logger=_Logger(),
        emitScanSignal=emit,
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )
    ctrl.scanFailed = (
        lambda **kwargs: SuperScanController.scanFailed(ctrl, **kwargs)
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == ['done', 'ended']
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert completion.message == 'Failed to finalize the scan-run lifecycle.'
    assert ctrl._scanCompletionPublishing is False


def test_non_final_done_signal_failure_releases_failed_sequence_run():
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    events = []

    class Coordinator:
        activeRunToken = runToken

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.activeRunToken = None
            if onReleased is not None:
                onReleased()
            return True

    def emit(signal):
        events.append(signal)
        if signal == 'done':
            raise RuntimeError('done listener failed')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[completion],
        _scanCoordinator=Coordinator(),
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _logger=_Logger(),
        emitScanSignal=emit,
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=False)

    assert events == ['done', 'ended']
    assert ctrl._scanCoordinator.activeRunToken is None
    assert ctrl._scanRunToken is None
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert completion.message == 'A scan-completion listener failed.'


def test_done_signal_reentry_cannot_start_a_continuation():
    class Coordinator:
        def runForOwner(self, _owner):
            raise AssertionError('must reject before coordinator access')

    ctrl = SimpleNamespace(
        _scanCompletionPublishing=True,
        _logger=_Logger(),
        isRunning=True,
        _scanCoordinator=Coordinator(),
    )

    result = SuperScanController._beginScanRun(
        ctrl, sigScanStartingEmitted=True
    )

    assert result is None
    assert ctrl.isRunning is False


def test_non_final_decision_is_frozen_across_done_signal_reentry():
    class Stub:
        scanDone = ScanControllerBase.scanDone

    ctrl = Stub()
    ctrl.isRunning = True
    ctrl.doingNonFinalPartOfSequence = True
    ctrl._widget = SimpleNamespace(
        isContLaserMode=lambda: False,
        repeatEnabled=lambda: False,
        setScanButtonChecked=lambda _checked: None,
    )
    finished = []

    def publishDone(*, isFinalPart):
        assert isFinalPart is False
        # Simulate a synchronous completion listener starting/configuring the
        # next part before the outer scanDone returns.
        ctrl.doingNonFinalPartOfSequence = False

    ctrl._publishScanDone = publishDone
    ctrl._finishScanRun = lambda: finished.append(True)

    ScanControllerBase.scanDone(ctrl)

    assert finished == []


def test_point_scan_reset_failure_still_publishes_terminal():
    class Stub:
        scanDone = ScanControllerPointScan.scanDone

    published = []
    ctrl = Stub()
    ctrl.isRunning = True
    ctrl.doingNonFinalPartOfSequence = False
    ctrl._widget = SimpleNamespace(
        repeatEnabled=lambda: False,
        setScanButtonChecked=lambda _checked: None,
    )
    ctrl._logger = _Logger()

    def failReset():
        raise RuntimeError('stage reset failed')

    ctrl._resetReturnToCenterPositionersAfterScan = failReset
    # The real wrapper, so the swallow-and-warn under test is the shipped one.
    ctrl._restoreScanPositioners = (
        lambda: SuperScanController._restoreScanPositioners(ctrl)
    )
    ctrl._publishScanDone = (
        lambda *, isFinalPart: published.append(isFinalPart)
    )

    ctrl.scanDone()

    assert published == [True]


@pytest.mark.parametrize(
    'scanDone',
    (
        ScanControllerBase.scanDone,
        ScanControllerPointScan.scanDone,
        ScanControllerAdvanced.scanDone,
    ),
)
def test_scan_done_widget_failure_still_publishes_terminal(scanDone):
    published = []

    def failWidgetCleanup(_checked):
        raise RuntimeError('widget cleanup failed')

    ctrl = SimpleNamespace(
        isRunning=True,
        doingNonFinalPartOfSequence=False,
        _widget=SimpleNamespace(
            isContLaserMode=lambda: False,
            repeatEnabled=lambda: False,
            setScanButtonChecked=failWidgetCleanup,
        ),
        _logger=_Logger(),
        _resetReturnToCenterPositionersAfterScan=lambda: None,
        _restoreScanPositioners=lambda: None,
        _publishScanDone=lambda *, isFinalPart: published.append(
            isFinalPart
        ),
    )

    scanDone(ctrl)

    assert published == [True]


@pytest.mark.parametrize(
    'scanDone',
    (
        ScanControllerBase.scanDone,
        ScanControllerPointScan.scanDone,
        ScanControllerAdvanced.scanDone,
    ),
)
def test_scan_done_mode_query_failure_routes_to_scan_failure(scanDone):
    failures = []

    def failRepeatQuery():
        raise RuntimeError('widget was disposed')

    ctrl = SimpleNamespace(
        isRunning=True,
        doingNonFinalPartOfSequence=False,
        _widget=SimpleNamespace(
            isContLaserMode=lambda: False,
            repeatEnabled=failRepeatQuery,
        ),
        _logger=_Logger(),
        scanFailed=lambda: failures.append(True),
    )

    scanDone(ctrl)

    assert failures == [True]


def test_abort_requested_iteration_resolves_with_abort_mode():
    owner = SimpleNamespace()
    iterationToken = object()
    runToken = object()

    class _Coordinator:
        def __init__(self):
            self.resolved = []

        def tokenForOwner(self, candidate):
            return iterationToken if candidate is owner else None

        def runForOwner(self, candidate):
            return runToken if candidate is owner else None

        def resolve(self, token, mode, onComplete=None):
            self.resolved.append((token, mode, onComplete))

    coordinator = _Coordinator()
    owner._scanCoordinator = coordinator
    owner._scanStopRequested = True

    SuperScanController._SuperScanController__onNidaqScanDone(owner)

    assert coordinator.resolved[0][:2] == (iterationToken, FINISH_ABORT)


def test_scan_end_releases_run_before_notifying_reentrant_listeners():
    events = []
    runToken = object()
    endedSignal = object()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=SimpleNamespace(
            releaseRun=lambda token, onReleased=None: (
                events.append(('release', token)),
                onReleased() if onReleased is not None else None,
                True,
            )[-1]
        ),
        _commChannel=SimpleNamespace(sigScanEnded=endedSignal),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(('emit', signal)),
    )

    SuperScanController._finishScanRun(ctrl)

    assert events == [
        ('release', runToken),
        ('emit', endedSignal),
    ]


def test_scan_end_waits_for_deferred_run_release_callback():
    events = []
    runToken = object()

    class _Coordinator:
        callback = None

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.callback = onReleased
            events.append('release-requested')
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(sigScanEnded='ended'),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )

    SuperScanController._finishScanRun(ctrl)

    assert events == ['release-requested']
    coordinator.callback()
    assert events == ['release-requested', 'ended']


@pytest.mark.parametrize('failed', (False, True))
def test_worker_barrier_terminal_is_delivered_on_controller_thread(
    qtbot, failed,
):
    runToken = object()
    ctrl = _makeQtBarrierController()
    ctrl._scanRunToken = runToken
    ctrl._scanCoordinator = SimpleNamespace(
        runForOwner=lambda _owner: runToken
    )
    callback = (
        ctrl._SuperScanController__afterScanBuildFailureBarrier
        if failed else
        ctrl._SuperScanController__afterScanFinishBarrier
    )

    worker = threading.Thread(target=lambda: callback(runToken))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()

    observed = ctrl.failedThreads if failed else ctrl.doneThreads
    qtbot.waitUntil(lambda: len(observed) == 1, timeout=1000)
    assert observed == [threading.main_thread()]


def test_worker_release_callback_publishes_end_and_exact_terminal_on_ui(
    qtbot,
):
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    completionThreads = []
    completion.add_done_callback(
        lambda _resolved: completionThreads.append(
            threading.current_thread()
        )
    )
    ctrl = _makeQtBarrierController()
    ctrl._scanRunToken = runToken
    ctrl._scanRunStartingPublished = True
    ctrl._scanStopRequested = False
    ctrl._scanRunFailed = False
    ctrl._scanCompletionPublishing = False
    ctrl._pendingExternalScanRequestCompletions = [completion]
    ctrl._commChannel = SimpleNamespace(
        sigScanDone='done',
        sigScanEnded='ended',
    )

    class Coordinator:
        activeRunToken = runToken

        def tokenForOwner(self, _owner):
            return None

        def runForOwner(self, _owner):
            return self.activeRunToken

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.activeRunToken = None
            worker = threading.Thread(target=onReleased)
            worker.start()
            worker.join(timeout=1)
            return True

    ctrl._scanCoordinator = Coordinator()

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert SuperScanController.shutdownComplete(ctrl) is False
    qtbot.waitUntil(lambda: completion.wait(timeout=0), timeout=1000)
    endedThreads = [
        thread for signal, thread in ctrl.emissions if signal == 'ended'
    ]
    assert endedThreads == [threading.main_thread()]
    assert completionThreads == [threading.main_thread()]
    assert ctrl._scanCompletionPublishing is False
    assert SuperScanController.shutdownComplete(ctrl) is True


def test_shutdown_fails_closed_when_coordinator_ownership_query_raises():
    class _Coordinator:
        def tokenForOwner(self, _owner):
            raise RuntimeError('coordinator unavailable')

        def runForOwner(self, _owner):
            raise AssertionError('short-circuited after iteration query')

    ctrl = SimpleNamespace(
        _scanCoordinator=_Coordinator(),
        _pendingExternalScanRequestCompletions=[],
        _repeatPending=False,
        isRunning=False,
        _scanCompletionPublishing=False,
        _scanRunStartingPublished=False,
        _logger=_Logger(),
    )

    assert SuperScanController.shutdownComplete(ctrl) is False


def test_terminal_listener_cannot_start_next_owner_before_exact_wakeup():
    ctrl = _makeQtBarrierController()
    coordinator = ScanExecutionCoordinator(object(), object())
    runToken = coordinator.reserveRun(ctrl)
    completion = ScanRequestCompletion(ctrl)
    completion.bind(runToken)
    nextOwner = object()
    events = []

    ctrl._scanCoordinator = coordinator
    ctrl._scanRunToken = runToken
    ctrl._scanRunStartingPublished = True
    ctrl._scanStopRequested = False
    ctrl._scanRunFailed = False
    ctrl._scanCompletionPublishing = False
    ctrl._pendingExternalScanRequestCompletions = [completion]
    ctrl._commChannel = SimpleNamespace(
        sigScanDone='done',
        sigScanEnded='ended',
    )

    def emit(signal, *_args):
        events.append(signal)
        if signal == 'ended':
            with pytest.raises(ScanBusyError, match='still finishing'):
                coordinator.reserveRun(nextOwner)
            events.append('listener-refused')

    def exactTerminal(_resolved):
        events.append('completion')
        coordinator.reserveRun(nextOwner)
        events.append('next-owner-reserved')

    ctrl.emitScanSignal = emit
    completion.add_done_callback(exactTerminal)

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == [
        'done',
        'ended',
        'listener-refused',
        'completion',
        'next-owner-reserved',
    ]
    assert completion.successful is True
    assert coordinator.runForOwner(nextOwner) is not None


def test_final_exact_success_waits_for_release_and_end_signal():
    events = []
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    completion.add_done_callback(
        lambda resolved: events.append(
            ('completion', resolved.successful)
        )
    )

    class _Coordinator:
        callback = None

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.callback = onReleased
            events.append('release-requested')
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[completion],
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == ['done', 'release-requested']
    assert completion.wait(timeout=0) is False
    assert ctrl._scanCompletionPublishing is True

    coordinator.callback()

    assert events == [
        'done',
        'release-requested',
        'ended',
        ('completion', True),
    ]
    assert completion.wait(timeout=0) is True
    assert ctrl._scanCompletionPublishing is False


def test_run_reservation_stays_held_through_end_signal_publication():
    events = []
    runToken = SimpleNamespace(
        released=False,
        releaseRequested=False,
        releaseBarrierCleared=False,
        holdReleaseUntilFinalized=False,
    )

    class _Coordinator:
        activeRunToken = runToken

        def releaseRun(
            self, token, *, onReleased=None, holdUntilFinalized=False
        ):
            assert token is runToken
            assert holdUntilFinalized is True
            token.releaseRequested = True
            token.releaseBarrierCleared = True
            token.holdReleaseUntilFinalized = True
            events.append('release-requested')
            onReleased()
            return True

        def finalizeRunRelease(self, token):
            assert token is runToken
            assert self.activeRunToken is token
            events.append('release-finalized')
            token.released = True
            self.activeRunToken = None
            return True

    coordinator = _Coordinator()

    def emit(signal):
        assert signal == 'ended'
        assert coordinator.activeRunToken is runToken
        events.append('ended-while-held')

    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(sigScanEnded='ended'),
        _logger=_Logger(),
        emitScanSignal=emit,
    )

    assert SuperScanController._finishScanRun(ctrl) is True
    assert events == [
        'release-requested',
        'ended-while-held',
        'release-finalized',
    ]
    assert coordinator.activeRunToken is None


def test_concurrent_terminal_paths_cannot_claim_the_same_local_run_twice():
    coordinator = ScanExecutionCoordinator(object(), object())
    ctrl = SimpleNamespace(
        _scanCoordinator=coordinator,
        _scanRunStartingPublished=True,
    )
    runToken = coordinator.reserveRun(ctrl)
    ctrl._scanRunToken = runToken
    start = threading.Barrier(3)
    results = []

    def claim():
        start.wait()
        try:
            results.append(
                SuperScanController._claimScanRunTerminal(ctrl, runToken)
            )
        except RuntimeError:
            results.append('already-claimed')

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result == 'already-claimed' for result in results) == 1
    claimed = [result for result in results if result != 'already-claimed']
    assert claimed == [(runToken, True, True)]
    assert ctrl._scanRunToken is None
    assert ctrl._scanRunStartingPublished is False


def test_failed_controller_thread_handoff_cannot_strand_held_run():
    events = []
    runToken = SimpleNamespace(
        released=False,
        releaseRequested=False,
        releaseBarrierCleared=False,
        holdReleaseUntilFinalized=False,
    )

    class _Coordinator:
        activeRunToken = runToken

        def releaseRun(
            self, token, *, onReleased=None, holdUntilFinalized=False
        ):
            assert token is runToken
            assert holdUntilFinalized is True
            token.releaseRequested = True
            token.releaseBarrierCleared = True
            token.holdReleaseUntilFinalized = True
            events.append('release-requested')
            onReleased()
            return True

        def finalizeRunRelease(self, token):
            assert token is runToken
            assert self.activeRunToken is token
            events.append('release-finalized')
            token.released = True
            self.activeRunToken = None
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(sigScanEnded='ended'),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
        _invokeOnControllerThreadIfNeeded=lambda _callback: (
            (_ for _ in ()).throw(RuntimeError('handoff failed'))
        ),
    )

    assert SuperScanController._finishScanRun(ctrl) is True
    assert events == [
        'release-requested',
        'ended',
        'release-finalized',
    ]
    assert coordinator.activeRunToken is None


def test_finalize_failure_keeps_exact_terminal_pending_until_retry_releases():
    events = []
    runToken = SimpleNamespace(
        released=False,
        releaseRequested=False,
        releaseBarrierCleared=False,
        holdReleaseUntilFinalized=False,
    )
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)

    class _Coordinator:
        activeRunToken = runToken
        finalizeCalls = 0

        def tokenForOwner(self, _owner):
            return None

        def runForOwner(self, _owner):
            return self.activeRunToken

        def releaseRun(
            self, token, *, onReleased=None, holdUntilFinalized=False
        ):
            assert token is runToken
            assert holdUntilFinalized is True
            token.releaseRequested = True
            token.releaseBarrierCleared = True
            token.holdReleaseUntilFinalized = True
            onReleased()
            return True

        def finalizeRunRelease(self, token):
            assert token is runToken
            self.finalizeCalls += 1
            if self.finalizeCalls == 1:
                raise RuntimeError('transient finalize failure')
            token.released = True
            self.activeRunToken = None
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _scanStopRequested=False,
        _scanRunFailed=False,
        _scanCompletionPublishing=False,
        _externalScanRequestInProgress=False,
        _externalScanRequestFailureMessage='',
        _pendingExternalScanRequestCompletions=[completion],
        _repeatPending=False,
        isRunning=False,
        doingNonFinalPartOfSequence=False,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(
            sigScanDone='done',
            sigScanEnded='ended',
        ),
        _widget=SimpleNamespace(
            setScanButtonChecked=lambda _checked: None
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._requeueExternalScanRequestCompletions = (
        lambda token, completions: SuperScanController
        ._requeueExternalScanRequestCompletions(
            ctrl, token, completions
        )
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    SuperScanController._publishScanDone(ctrl, isFinalPart=True)

    assert events == ['done', 'ended']
    assert completion.wait(timeout=0) is False
    assert ctrl._pendingExternalScanRequestCompletions == [completion]
    assert ctrl._scanRunToken is runToken
    assert ctrl._scanRunStartingPublished is False
    assert ctrl._scanCompletionPublishing is False
    assert coordinator.activeRunToken is runToken

    SuperScanController.scanFailed(ctrl)

    assert events == ['done', 'ended']
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert coordinator.activeRunToken is None
    assert ctrl._scanRunToken is None
    assert ctrl._scanCompletionPublishing is False


def test_exact_failure_waits_for_release_and_end_signal():
    events = []
    runToken = object()
    completion = ScanRequestCompletion(object())
    completion.bind(runToken)
    completion.add_done_callback(
        lambda resolved: events.append(
            ('completion', resolved.successful)
        )
    )

    class _Coordinator:
        callback = None

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.callback = onReleased
            events.append('release-requested')
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=runToken,
        _scanRunStartingPublished=True,
        _externalScanRequestInProgress=False,
        _externalScanRequestFailureMessage='hardware arm failed',
        _pendingExternalScanRequestCompletions=[completion],
        _scanCompletionPublishing=False,
        _repeatPending=False,
        isRunning=True,
        doingNonFinalPartOfSequence=True,
        _scanCoordinator=coordinator,
        _commChannel=SimpleNamespace(sigScanEnded='ended'),
        _widget=SimpleNamespace(
            setScanButtonChecked=lambda _checked: None
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    SuperScanController.scanFailed(ctrl)

    assert events == ['release-requested']
    assert completion.wait(timeout=0) is False
    assert ctrl._scanCompletionPublishing is True

    coordinator.callback()

    assert events == [
        'release-requested',
        'ended',
        ('completion', False),
    ]
    assert completion.message == 'hardware arm failed'
    assert ctrl._scanCompletionPublishing is False


def test_failed_accepted_external_request_reports_exact_terminal_until_release():
    events = []
    reports = []
    runToken = object()

    class _Coordinator:
        callback = None

        def runForOwner(self, _owner):
            return None

        def tokenForOwner(self, _owner):
            return None

        def reserveRun(self, _owner):
            return runToken

        def releaseRun(self, token, *, onReleased=None):
            assert token is runToken
            self.callback = onReleased
            events.append('release-requested')
            return True

    coordinator = _Coordinator()
    ctrl = SimpleNamespace(
        _scanRunToken=None,
        _scanRunStartingPublished=False,
        _scanStopRequested=False,
        _scanCompletionPublishing=False,
        _pendingExternalScanRequestCompletions=[],
        _scanCoordinator=coordinator,
        _widget=SimpleNamespace(
            setScanMode=lambda: None,
            setRepeatEnabled=lambda _value: None,
            setScanButtonChecked=lambda _checked: None,
        ),
        _commChannel=SimpleNamespace(
            sigScanEnded='ended',
            scanWorkflow=SimpleNamespace(
                report_scan_request_result=lambda *args: reports.append(args)
            ),
        ),
        _logger=_Logger(),
        emitScanSignal=lambda signal: events.append(signal),
    )
    ctrl._detachExternalScanRequestCompletions = (
        lambda token: SuperScanController
        ._detachExternalScanRequestCompletions(ctrl, token)
    )
    ctrl._completeExternalScanRequest = (
        lambda token, **kwargs: SuperScanController
        ._completeExternalScanRequest(ctrl, token, **kwargs)
    )
    ctrl._finishScanRun = (
        lambda onReleased=None, **kwargs: SuperScanController
        ._finishScanRun(ctrl, onReleased=onReleased, **kwargs)
    )

    def armThenFail(**_kwargs):
        SuperScanController._beginScanRun(
            ctrl, sigScanStartingEmitted=True
        )
        ctrl._externalScanRequestFailureMessage = 'arm failed'
        SuperScanController.scanFailed(ctrl)

    ctrl.runScanAdvanced = armThenFail

    SuperScanController.runScanExternal(ctrl, True, False)

    assert reports[0][:4] == (ctrl, True, 'arm failed', runToken)
    completion = reports[0][4]
    assert completion.runToken is runToken
    assert completion.wait(timeout=0) is False
    assert events == ['release-requested']

    coordinator.callback()

    assert events == ['release-requested', 'ended']
    assert completion.wait(timeout=0) is True
    assert completion.successful is False
    assert completion.message == 'arm failed'


def test_stale_queued_completion_cannot_finish_new_run():
    oldRun = object()
    newRun = object()
    completions = []
    ctrl = SimpleNamespace(
        _scanRunToken=newRun,
        _scanCoordinator=SimpleNamespace(
            runForOwner=lambda _owner: newRun
        ),
        scanDone=lambda: completions.append(True),
    )

    SuperScanController._SuperScanController__deliverScanDone(ctrl, oldRun)

    assert completions == []


def test_monalisa_axial_followup_does_not_end_the_run():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    endedSignal = object()
    emitted = []
    followUps = []
    ctrl._commChannel = SimpleNamespace(
        sigScanEnded=endedSignal,
        setActiveScanSource=lambda source: None,
        clearActiveScanSource=lambda source: None,
    )
    ctrl.isRunning = True
    ctrl.autoAxial = True
    ctrl.axialListBuffer = [3.0]
    ctrl.centerCoord = (4.0, 5.0)
    ctrl.resetPositioners = lambda: None
    ctrl.emitScanSignal = lambda signal, *args: emitted.append(signal)
    ctrl.runNextAxialScan = lambda: followUps.append(True)

    ScanControllerMoNaLISA.scanDone(ctrl)

    assert ctrl.nextAxial == 3.0
    assert followUps == [True]
    assert endedSignal not in emitted


def test_monalisa_followup_reuses_the_run_level_start_signal():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    calls = []
    ctrl.doingNonFinalPartOfSequence = False
    ctrl.updateScanParamForAxial = lambda: None
    ctrl.runScanAdvanced = lambda **kwargs: calls.append(kwargs)

    ScanControllerMoNaLISA.runNextAxialScan(ctrl)

    assert calls == [{
        'isNonFinalPartOfSequence': False,
        'sigScanStartingEmitted': True,
        'axialFollowUp': True,
    }]


def test_monalisa_followup_preserves_non_final_sequence_part():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    calls = []
    ctrl.doingNonFinalPartOfSequence = True
    ctrl.updateScanParamForAxial = lambda: None
    ctrl.runScanAdvanced = lambda **kwargs: calls.append(kwargs)

    ScanControllerMoNaLISA.runNextAxialScan(ctrl)

    assert calls == [{
        'isNonFinalPartOfSequence': True,
        'sigScanStartingEmitted': True,
        'axialFollowUp': True,
    }]


def test_monalisa_duplicate_start_does_not_mutate_axial_state():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    calls = []
    ctrl._widget = SimpleNamespace(
        setScanButtonChecked=lambda checked: calls.append(
            ('button', checked)
        )
    )
    ctrl._commChannel = SimpleNamespace(
        setActiveScanSource=lambda _source: None,
        clearActiveScanSource=lambda _source: None,
    )
    ctrl._beginScanRun = lambda **_kwargs: None
    ctrl.checkAxialAutoScan = lambda: calls.append(('check-axial',))
    ctrl.setupAxial = lambda: calls.append(('setup-axial',))
    ctrl.scanFailed = lambda: calls.append(('failed',))
    ctrl._logger = _Logger()

    ScanControllerMoNaLISA.runScanAdvanced(
        ctrl,
        sigScanStartingEmitted=False,
    )

    assert ('check-axial',) not in calls
    assert ('setup-axial',) not in calls


def test_monalisa_stale_pipeline_callback_cannot_restart_released_run():
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    oldToken = object()
    newToken = object()
    followUps = []
    ctrl.awaitingPipeline = True
    ctrl._pipelineRunToken = oldToken
    ctrl._scanRunToken = newToken
    ctrl._scanCoordinator = SimpleNamespace(
        runForOwner=lambda _owner: newToken
    )
    ctrl.pipelineTimeoutTimer = SimpleNamespace(stop=lambda: None)
    ctrl.runNextAxialScan = lambda: followUps.append(True)

    ScanControllerMoNaLISA.centerCoordPipelineFinished(ctrl, (1, 2))

    assert followUps == []
    assert ctrl.awaitingPipeline is True


def test_monalisa_pipeline_callback_failure_terminalizes_retained_run():
    runToken = object()
    failures = []
    ctrl = ScanControllerMoNaLISA.__new__(ScanControllerMoNaLISA)
    ctrl.awaitingPipeline = True
    ctrl._pipelineRunToken = runToken
    ctrl._scanRunToken = runToken
    ctrl._scanCoordinator = SimpleNamespace(
        runForOwner=lambda _owner: runToken
    )
    ctrl.pipelineTimeoutTimer = SimpleNamespace(stop=lambda: None)
    ctrl._widget = SimpleNamespace(
        yCenterEdit=SimpleNamespace(
            setText=lambda _text: (_ for _ in ()).throw(
                RuntimeError('widget was disposed')
            )
        ),
        xCenterEdit=SimpleNamespace(setText=lambda _text: None),
    )
    ctrl._logger = _Logger()
    ctrl.scanFailed = lambda: failures.append(True)

    ScanControllerMoNaLISA.centerCoordPipelineFinished(ctrl, (1, 2))

    assert failures == [True]
    assert ctrl.awaitingPipeline is False
    assert ctrl._pipelineRunToken is None


def test_monalisa_widget_cleanup_failure_preserves_success_terminal():
    published = []
    failures = []

    def failWidgetCleanup(_checked):
        raise RuntimeError('widget was disposed')

    ctrl = SimpleNamespace(
        isRunning=True,
        autoAxial=False,
        axialListBuffer=[],
        doingNonFinalPartOfSequence=False,
        resetPositioners=lambda: None,
        _widget=SimpleNamespace(
            isContLaserMode=lambda: False,
            repeatEnabled=lambda: False,
            setScanButtonChecked=failWidgetCleanup,
        ),
        _logger=_Logger(),
        _publishScanDone=lambda *, isFinalPart: published.append(
            isFinalPart
        ),
        scanFailed=lambda: failures.append(True),
    )

    ScanControllerMoNaLISA.scanDone(ctrl)

    assert published == [True]
    assert failures == []
