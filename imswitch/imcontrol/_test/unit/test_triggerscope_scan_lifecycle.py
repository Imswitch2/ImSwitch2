"""TriggerScope controllers share the coordinated scan-execution contract."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers._triggerscope_scan_lifecycle import (
    TriggerScopeScanLifecycleMixin,
)
from imswitch.imcontrol.controller.controllers import (
    _triggerscope_scan_lifecycle as lifecycleModule,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_DIR = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers'
TRIGGERSCOPE_CONTROLLERS = {
    'TriggerScopeRasterController.py': 'TriggerScopeRasterController',
    'TriggerScopeScanController.py': 'TriggerScopeScanController',
    'TriggerScopePLSRController.py': 'TriggerScopePLSRController',
    'TriggerScopePLSRMulticolorController.py':
        'TriggerScopePLSRMulticolorController',
    'TriggerScopeLSXYRController.py': 'TriggerScopeLSXYRController',
    'TriggerScopeGalvoDetectionController.py':
        'TriggerScopeGalvoDetectionController',
    'LightSheetMulticolorController.py': 'LightSheetMulticolorController',
}


class _Signal:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        self.events.append((self.name, args))
        for slot in tuple(self.slots):
            slot(*args)


class _Logger:
    def __init__(self):
        self.messages = []

    def debug(self, message, *args, **kwargs):
        self.messages.append(('debug', message))

    def warning(self, message, *args, **kwargs):
        self.messages.append(('warning', message))

    def error(self, message, *args, **kwargs):
        self.messages.append(('error', message))


class _Detector:
    name = 'PointDetector'
    isScanDriven = True

    def __init__(self, *, autoAcknowledge=True):
        self.autoAcknowledge = autoAcknowledge
        self.finishCalls = []
        self.pendingAcknowledge = None

    def finishScan(self, mode, acknowledge):
        self.finishCalls.append(mode)
        if self.autoAcknowledge:
            acknowledge()
        else:
            self.pendingAcknowledge = acknowledge


class _DetectorsManager:
    def __init__(self, events, detector):
        self.events = events
        self.detector = detector
        self.released = []

    def getAllDeviceNames(self, condition=None):
        if condition is None or condition(self.detector):
            return [self.detector.name]
        return []

    def isDetectorFaulted(self, name):
        return False

    def acquire(self, detectorNames, purpose):
        assert tuple(detectorNames) == (self.detector.name,)
        assert purpose is LeasePurpose.SCAN
        self.events.append(('detectors-acquired', tuple(detectorNames)))
        return 'scan-lease'

    def release(self, handle):
        self.released.append(handle)
        self.events.append(('detectors-released', handle))

    def execOn(self, name, callback):
        assert name == self.detector.name
        return callback(self.detector)


class _ScanManager:
    def __init__(self, events):
        self.events = events
        self.sigScanStarted = _Signal('board-started', events)
        self.sigScanDone = _Signal('board-done', events)
        self.error = None
        self.calls = []

    def runScan(self, parameters, scan_type):
        self.calls.append((parameters, scan_type))
        if self.error is not None:
            raise self.error
        # ScanManagerTriggerScope publishes this immediately before sending the
        # firmware command.
        self.sigScanStarted.emit()
        self.events.append(('firmware-command', scan_type))


class _Widget:
    autoStartRec = False
    autoStopRec = False

    def __init__(self):
        self.scanButtonChecked = False
        self.abortPending = False
        self.repeat = False

    def setScanButtonChecked(self, checked):
        self.scanButtonChecked = checked

    def setAbortPending(self, pending):
        self.abortPending = pending

    def repeatEnabled(self):
        return self.repeat


class _ScanWorkflow:
    """Stand-in for the recording-acceptance hook the real service provides."""

    def __init__(self, events):
        self.events = events
        self.accept = True
        self.sources = []

    def prepare_recording_for_scan(self, source):
        self.sources.append(source)
        self.events.append(('recording-prepare', source))
        return self.accept


class _CommChannel:
    def __init__(self, events):
        self.activeSource = None
        self.scanWorkflow = _ScanWorkflow(events)
        self.sigScanStarting = _Signal('scan-starting', events)
        self.sigScanDevicesResolved = _Signal('devices-resolved', events)
        self.sigScanBuilt = _Signal('scan-built', events)
        self.sigScanStarted = _Signal('scan-started', events)
        self.sigScanDone = _Signal('scan-done', events)
        self.sigScanEnded = _Signal('scan-ended', events)
        self.sigStartRecording = _Signal('recording-start', events)
        self.sigStopRecording = _Signal('recording-stop', events)

    def setActiveScanSource(self, controller):
        self.activeSource = controller

    def clearActiveScanSource(self, controller):
        if self.activeSource is controller:
            self.activeSource = None


class _Controller(TriggerScopeScanLifecycleMixin):
    def __init__(self, master, commChannel):
        self._master = master
        self._commChannel = commChannel
        self._widget = _Widget()
        self._logger = _Logger()
        self.doingNonFinalPartOfSequence = False
        self.isRunning = False
        self._initTriggerScopeScanLifecycle()

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    def _invokeOnControllerThreadIfNeeded(self, callback):
        callback()

    def runScanAdvanced(
        self, *, recalculateSignals=True,
        isNonFinalPartOfSequence=False, sigScanStartingEmitted,
    ):
        return self._startTriggerScopeScan(
            parameters={'value': 1},
            scanType='rasterScan',
            laserDevices=['Laser 561'],
            sigScanStartingEmitted=sigScanStartingEmitted,
            isNonFinalPartOfSequence=isNonFinalPartOfSequence,
        )


def _setup(*, autoAcknowledge=True):
    events = []
    detector = _Detector(autoAcknowledge=autoAcknowledge)
    detectorsManager = _DetectorsManager(events, detector)
    scanManager = _ScanManager(events)
    coordinator = ScanExecutionCoordinator(
        detectorsManager, SimpleNamespace()
    )
    master = SimpleNamespace(
        detectorsManager=detectorsManager,
        nidaqManager=SimpleNamespace(),
        scanManager=scanManager,
        scanExecutionCoordinator=coordinator,
    )
    commChannel = _CommChannel(events)
    return (
        _Controller(master, commChannel),
        master,
        commChannel,
        detector,
        events,
    )


def test_recording_acceptance_precedes_every_announcement_and_command():
    controller, master, commChannel, _, events = _setup()

    assert controller.runScanAdvanced(sigScanStartingEmitted=False) is True

    names = [event[0] for event in events]
    assert commChannel.scanWorkflow.sources == [controller]
    for later in (
        'scan-starting', 'devices-resolved', 'detectors-acquired',
        'firmware-command',
    ):
        assert names.index('recording-prepare') < names.index(later), later


def test_a_refused_recording_stops_the_scan_without_a_lifecycle():
    """The scan must not run when the recording armed for it cannot bind. No
    boundary was published for this attempt, so none may be published now —
    a sigScanEnded here would disarm lasers for a scan that never existed."""
    controller, master, commChannel, detector, events = _setup()
    commChannel.scanWorkflow.accept = False

    assert controller.runScanAdvanced(sigScanStartingEmitted=False) is False

    names = [event[0] for event in events]
    assert master.scanManager.calls == []
    assert 'scan-starting' not in names
    assert 'devices-resolved' not in names
    assert 'scan-ended' not in names
    assert 'detectors-acquired' not in names
    assert detector.finishCalls == []
    # The reservation is handed back, so the next scan can arm normally.
    assert controller._scanCoordinator.activeRunToken is None
    assert controller._triggerScopeRunToken is None
    assert controller.isRunning is False
    assert commChannel.activeSource is None
    assert controller._widget.scanButtonChecked is False

    commChannel.scanWorkflow.accept = True
    assert controller.runScanAdvanced(sigScanStartingEmitted=False) is True
    assert len(master.scanManager.calls) == 1


def test_a_refused_continuation_terminalizes_the_published_run():
    """A sequence part whose recording refuses cannot simply drop the run: its
    start boundary was already published and needs its pair."""
    controller, master, commChannel, _, events = _setup()
    controller.runScanAdvanced(
        sigScanStartingEmitted=False, isNonFinalPartOfSequence=True
    )
    master.scanManager.sigScanDone.emit()
    assert controller._triggerScopeRunToken is not None

    commChannel.scanWorkflow.accept = False
    assert controller.runScanAdvanced(
        sigScanStartingEmitted=True, isNonFinalPartOfSequence=False
    ) is False

    names = [event[0] for event in events]
    assert len(master.scanManager.calls) == 1
    assert names.count('scan-ended') == 1
    assert controller._scanCoordinator.activeRunToken is None
    assert controller.isRunning is False


def test_membership_and_detector_lease_precede_firmware_start():
    controller, master, commChannel, _, events = _setup()

    assert controller.runScanAdvanced(sigScanStartingEmitted=False) is True

    names = [event[0] for event in events]
    assert names.index('scan-starting') < names.index('devices-resolved')
    assert names.index('devices-resolved') < names.index('detectors-acquired')
    assert names.index('detectors-acquired') < names.index('board-started')
    assert names.index('scan-started') < names.index('firmware-command')
    assert master.scanManager.calls == [
        ({'value': 1}, 'rasterScan')
    ]
    assert commChannel.activeSource is controller
    assert controller._widget.scanButtonChecked is False


def test_board_done_waits_for_detector_finish_before_terminal_signals():
    controller, master, commChannel, detector, events = _setup(
        autoAcknowledge=False
    )
    controller.runScanAdvanced(sigScanStartingEmitted=False)

    master.scanManager.sigScanDone.emit()

    names = [event[0] for event in events]
    assert 'scan-done' not in names
    assert 'scan-ended' not in names
    assert master.detectorsManager.released == []
    assert detector.pendingAcknowledge is not None

    detector.pendingAcknowledge()

    names = [event[0] for event in events]
    assert names.index('detectors-released') < names.index('scan-done')
    assert names.index('scan-done') < names.index('scan-ended')
    assert master.detectorsManager.released == ['scan-lease']
    assert commChannel.activeSource is None
    assert controller._scanCoordinator.activeRunToken is None


def test_stop_during_nonfinal_completion_publication_releases_the_run():
    controller, master, commChannel, _, events = _setup()
    commChannel.sigScanDone.connect(controller._requestTriggerScopeStop)

    controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=True,
    )
    master.scanManager.sigScanDone.emit()

    names = [event[0] for event in events]
    assert names.count('scan-done') == 1
    assert names.count('scan-ended') == 1
    assert len(master.scanManager.calls) == 1
    assert controller._scanStopRequested is False
    assert controller._triggerScopeRunToken is None
    assert controller._scanCoordinator.activeRunToken is None


def test_stopped_nonfinal_run_refuses_a_sequence_continuation():
    controller, master, _, _, _ = _setup()
    controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=True,
    )
    master.scanManager.sigScanDone.emit()
    controller._scanStopRequested = True

    assert controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=False,
    ) is False
    assert len(master.scanManager.calls) == 1
    assert controller.isRunning is False

    controller._failTriggerScopeScan()


def test_releasing_nonfinal_run_refuses_a_sequence_continuation():
    controller, master, _, _, _ = _setup()
    controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=True,
    )
    master.scanManager.sigScanDone.emit()
    runToken = controller._triggerScopeRunToken
    assert controller._scanCoordinator.releaseRun(
        runToken,
        onReleased=lambda: None,
        holdUntilFinalized=True,
    )

    assert controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=False,
    ) is False
    assert len(master.scanManager.calls) == 1
    assert controller.isRunning is False

    assert controller._scanCoordinator.finalizeRunRelease(runToken)


def test_nonfinal_sequence_part_does_not_auto_stop_external_recording():
    controller, master, _, _, events = _setup()
    controller._widget.autoStopRec = True

    controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=True,
    )
    master.scanManager.sigScanDone.emit()

    assert 'recording-stop' not in [event[0] for event in events]
    assert controller._triggerScopeRunToken is not None

    controller.runScanAdvanced(
        sigScanStartingEmitted=True,
        isNonFinalPartOfSequence=False,
    )
    master.scanManager.sigScanDone.emit()

    assert [event[0] for event in events].count('recording-stop') == 1
    assert controller._scanCoordinator.activeRunToken is None


def test_firmware_done_finishes_detector_gracefully_when_stop_is_pending():
    controller, master, _, detector, _ = _setup()
    controller.runScanAdvanced(sigScanStartingEmitted=False)

    controller._requestTriggerScopeStop()
    master.scanManager.sigScanDone.emit()

    assert detector.finishCalls == ['graceful']
    assert controller._scanCoordinator.activeRunToken is None


def test_terminal_publication_is_idempotent_after_queued_handoff_raises():
    controller, master, _, _, events = _setup()
    queued = []

    def queueThenRaise(callback):
        queued.append(callback)
        raise RuntimeError('queued before transport failure')

    controller._invokeOnControllerThreadIfNeeded = queueThenRaise
    controller.runScanAdvanced(sigScanStartingEmitted=False)
    master.scanManager.sigScanDone.emit()

    assert len(queued) == 2
    for callback in queued:
        callback()

    names = [event[0] for event in events]
    assert names.count('scan-done') == 1
    assert names.count('scan-ended') == 1
    assert controller._scanCoordinator.activeRunToken is None


def test_firmware_start_failure_unwinds_run_detector_and_laser_lifecycle():
    controller, master, commChannel, detector, events = _setup()
    master.scanManager.error = RuntimeError('serial write failed')

    with pytest.raises(RuntimeError, match='serial write failed'):
        controller.runScanAdvanced(sigScanStartingEmitted=False)
    controller._failTriggerScopeScan()

    names = [event[0] for event in events]
    assert detector.finishCalls == ['abort']
    assert master.detectorsManager.released == ['scan-lease']
    assert names.count('scan-ended') == 1
    assert commChannel.activeSource is None
    assert controller._scanCoordinator.activeRunToken is None


def test_board_wide_signals_are_relayed_only_by_the_iteration_owner():
    controller, master, commChannel, _, events = _setup()
    bystander = _Controller(master, commChannel)

    controller.runScanAdvanced(sigScanStartingEmitted=False)
    master.scanManager.sigScanDone.emit()

    names = [event[0] for event in events]
    assert names.count('scan-started') == 1
    assert names.count('scan-done') == 1
    assert names.count('scan-ended') == 1
    assert bystander.isRunning is False


def test_broadcast_loser_treats_scan_busy_as_a_benign_refusal():
    controller, master, commChannel, _, events = _setup()
    bystander = _Controller(master, commChannel)

    assert controller.runScanAdvanced(sigScanStartingEmitted=False) is True
    assert bystander.runScanAdvanced(sigScanStartingEmitted=False) is False

    assert len(master.scanManager.calls) == 1
    assert not [
        message for level, message in bystander._logger.messages
        if level == 'error'
    ]
    assert any(
        level == 'debug' and 'another scan run owns' in message
        for level, message in bystander._logger.messages
    )
    assert [event[0] for event in events].count('scan-starting') == 1

    master.scanManager.sigScanDone.emit()


def test_repeat_retains_the_run_and_rechecks_the_widget_before_rearming(
    monkeypatch,
):
    controller, master, _, _, events = _setup()
    scheduled = []
    controller._widget.repeat = True
    monkeypatch.setattr(
        lifecycleModule.QtCore.QTimer,
        'singleShot',
        lambda delay, callback: scheduled.append(callback),
    )

    controller.runScanAdvanced(sigScanStartingEmitted=False)
    master.scanManager.sigScanDone.emit()

    names = [event[0] for event in events]
    assert 'scan-done' not in names
    assert 'scan-ended' not in names
    assert controller._scanCoordinator.runForOwner(controller) is not None
    # One callback is the finish-barrier timeout (already made harmless by the
    # synchronous acknowledgement); the other is the deferred repeat.
    assert len(scheduled) == 2

    controller._widget.repeat = False
    scheduled[-1]()

    names = [event[0] for event in events]
    assert names.count('scan-done') == 1
    assert names.count('scan-ended') == 1
    assert controller._scanCoordinator.activeRunToken is None


def test_every_triggerscope_controller_uses_the_shared_lifecycle():
    offenders = []
    for filename, className in TRIGGERSCOPE_CONTROLLERS.items():
        path = CONTROLLER_DIR / filename
        module = ast.parse(path.read_text(encoding='utf-8'))
        classNode = next(
            node for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == className
        )
        bases = {
            base.id for base in classNode.bases if isinstance(base, ast.Name)
        }
        runMethod = next(
            node for node in classNode.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'runScanAdvanced'
        )
        calls = {
            node.func.attr
            for node in ast.walk(runMethod)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        if (
            'TriggerScopeScanLifecycleMixin' not in bases
            or '_startTriggerScopeScan' not in calls
        ):
            offenders.append(className)

    assert offenders == [], (
        'TriggerScope scan controllers must inherit and enter through '
        'TriggerScopeScanLifecycleMixin: ' + ', '.join(offenders)
    )
