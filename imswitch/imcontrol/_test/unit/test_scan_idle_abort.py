"""An abort that finds no scan to stop must not report a scan failure.

``SuperScanController.closeEvent`` aborts unconditionally and ``sigAbortScan``
is broadcast to every scan controller, while ``abortScan`` used to call
``scanFailed()`` whenever no iteration was running. Every ImControl shutdown
on a setup with a scan widget therefore ended in
``ERROR [ScanControllerBase] Scan failed`` for a scan that never ran.

The idle branch still has a job: a run retained between iterations (repeat
gap, non-final sequence part, MoNaLISA axial follow-up) has no NI-DAQ
completion left to end it, so an abort there must still fail and release it.

The controller is built through the real ``SuperScanController.__init__`` so
"idle" is the state the constructor actually leaves, with a real shared
``ScanExecutionCoordinator``.
"""

from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller import basecontrollers
from imswitch.imcontrol.controller.basecontrollers import SuperScanController


class _RecordingLogger:
    def __init__(self):
        self.errors = []

    def error(self, message, *_args, **_kwargs):
        self.errors.append(message)

    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class _ScanShell(SuperScanController):
    """The NI-DAQ scan-controller base with its abstract hooks stubbed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.emitted = []
        self._logger = _RecordingLogger()

    def setParameters(self):
        pass

    def getParameters(self):
        pass

    def updatePixels(self):
        pass

    def emitScanSignal(self, signal, *args):
        self.emitted.append(signal)

    def runScanAdvanced(self, **_kwargs):
        pass

    def scanDone(self):
        pass


@pytest.fixture
def makeController(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(
        basecontrollers.dirtools.UserFileDirs, 'Root', str(tmp_path)
    )

    def make(master=None):
        return _ScanShell(
            setupInfo=MagicMock(),
            commChannel=MagicMock(),
            master=master if master is not None else MagicMock(),
            widget=MagicMock(),
            factory=None,
            moduleCommChannel=None,
        )

    return make


def _startRun(ctrl):
    """Reserve a run and enter its first iteration, as runScanAdvanced does."""
    token = ctrl._beginScanRun(sigScanStartingEmitted=False)
    assert token is not None
    assert ctrl.isRunning is True
    ctrl.emitted.clear()
    return token


def test_closing_an_idle_scan_controller_reports_no_failure(makeController):
    ctrl = makeController()

    assert ctrl.closeEvent() is True

    assert ctrl._logger.errors == []
    assert ctrl.emitted == []
    assert ctrl._scanRunFailed is False
    assert ctrl._scanStopRequested is False
    assert ctrl._scanCoordinator.activeRunToken is None


def test_broadcast_abort_does_not_fail_or_disturb_another_owners_run(
    makeController,
):
    master = MagicMock()
    owner = makeController(master)
    bystander = makeController(master)
    assert bystander._scanCoordinator is owner._scanCoordinator
    token = _startRun(owner)

    bystander.abortScan()

    assert bystander._logger.errors == []
    assert bystander.emitted == []
    assert owner._scanCoordinator.runForOwner(owner) is token
    assert owner.isRunning is True
    assert owner._scanStopRequested is False


def test_abort_during_a_running_iteration_waits_for_its_completion(
    makeController,
):
    ctrl = makeController()
    token = _startRun(ctrl)

    ctrl.abortScan()

    # Unchanged: NI-DAQ completion ends the run, not the abort click.
    assert ctrl._logger.errors == []
    assert ctrl.emitted == []
    assert ctrl._scanStopRequested is True
    assert ctrl.isRunning is True
    assert ctrl._scanCoordinator.runForOwner(ctrl) is token


@pytest.mark.parametrize('gap', ('repeat', 'non-final sequence part'))
def test_abort_between_iterations_still_fails_the_retained_run(
    qtbot, makeController, gap,
):
    ctrl = makeController()
    _startRun(ctrl)
    # The iteration finished; the run reservation is deliberately kept.
    ctrl.isRunning = False
    if gap == 'repeat':
        ctrl._repeatPending = True
    else:
        ctrl.doingNonFinalPartOfSequence = True

    ctrl.abortScan()

    assert ctrl._logger.errors == ['Scan failed']
    assert ctrl._repeatPending is False
    qtbot.waitUntil(
        lambda: ctrl._commChannel.sigScanEnded in ctrl.emitted,
        timeout=1000,
    )
    assert ctrl._scanCoordinator.activeRunToken is None
    assert ctrl.shutdownComplete() is True


def test_abort_of_a_request_still_arming_fails_the_request(makeController):
    ctrl = makeController()
    ctrl._externalScanRequestInProgress = True

    ctrl.abortScan()

    assert ctrl._logger.errors == ['Scan failed']
    assert ctrl._externalScanRequestFailed is True


def test_abort_fails_closed_when_ownership_cannot_be_verified(makeController):
    ctrl = makeController()

    class _UnreadableCoordinator:
        def tokenForOwner(self, _owner):
            raise RuntimeError('coordinator unavailable')

        def runForOwner(self, _owner):
            raise RuntimeError('coordinator unavailable')

    ctrl._scanCoordinator = _UnreadableCoordinator()

    ctrl.abortScan()

    assert 'Scan failed' in ctrl._logger.errors
