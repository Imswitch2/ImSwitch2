"""Regression coverage for event-triggered stale and terminal callbacks."""

from collections import deque
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.controller.basecontrollers import (
    ImConWidgetController,
)
from imswitch.imcontrol.model.EventTriggeredSession import (
    EventScanInitiationMode as ScanInitiationMode,
    EventTriggeredSessionState,
)
from imswitch.imcontrol.model.managers import LeasePurpose


pytestmark = pytest.mark.nohardware


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Signal:
    def __init__(self, *, error=None):
        self.slots = []
        self.emitted = []
        self.error = error

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        self.emitted.append(args)
        if self.error is not None:
            raise self.error
        for slot in tuple(self.slots):
            slot(*args)


def _bare_controller(*, mode=ScanInitiationMode.ScanWidget):
    controller = EventTriggeredControllerBase.__new__(
        EventTriggeredControllerBase
    )
    controller._logger = _Logger()
    controller._state = EventTriggeredSessionState()
    controller._state.detectorFast = 'CAM'
    controller._state.scanInitiationMode = mode
    controller._experimentActive = True
    controller._stopRequested = False
    controller._closed = False
    controller._triggeredRecordingInFlight = False
    return controller


def _qt_bare_controller(*, mode=ScanInitiationMode.ScanWidget):
    controller = _bare_controller(mode=mode)
    ImConWidgetController.__init__(
        controller,
        setupInfo=SimpleNamespace(),
        commChannel=SimpleNamespace(),
        master=SimpleNamespace(),
        widget=SimpleNamespace(),
        factory=None,
        moduleCommChannel=None,
    )
    controller._logger = _Logger()
    return controller


@pytest.mark.parametrize(
    ('closed', 'stopRequested', 'experimentActive', 'running'),
    [
        (False, False, True, False),   # slow scan has paused the fast mode
        (False, True, False, False),   # Stop was requested
        (True, True, False, False),    # controller has closed
    ],
)
def test_queued_pipeline_frame_is_rejected_outside_active_fast_session(
        closed, stopRequested, experimentActive, running):
    controller = _bare_controller()
    controller._closed = closed
    controller._stopRequested = stopRequested
    controller._experimentActive = experimentActive
    controller._state.running = running
    controller._state.busy = False
    controller._pipelineRunner = SimpleNamespace(
        execute=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('stale frame reached the analysis pipeline')
        )
    )

    controller.runPipeline('CAM', np.zeros((2, 2)), False, [], True)

    assert controller._state.busy is False


def test_stale_direct_event_callback_cannot_prepare_or_move_scan():
    controller = _bare_controller()
    controller._state.running = False
    controller._state.busy = True
    preparations = []
    controller.initiateSlowScan = lambda **_kwargs: preparations.append(True)

    controller._handle_event_frame(
        np.asarray([[1.0, 2.0]]), np.zeros((2, 2))
    )

    assert preparations == []
    assert controller._state.busy is False


def test_event_run_reserves_before_scan_preparation():
    controller = _bare_controller()
    controller._state.running = True
    controller._state.busy = True
    controller._prevFrames = deque(maxlen=2)
    calls = []
    controller.setDetLogLine = lambda *_args: None
    controller._set_status = lambda *_args: None
    controller.pauseFastModality = lambda: (
        calls.append('pause'),
        setattr(controller._state, 'running', False),
    )
    controller._beginTriggeredScanRun = lambda: (
        calls.append('reserve') or True
    )
    controller._transformService = SimpleNamespace(
        apply=lambda coords, _extra: (
            calls.append('transform') or coords
        )
    )
    controller._transform_apply_extra_arg = lambda: None
    controller._log_all_detected_coords = lambda *_args, **_kwargs: None
    controller.initiateSlowScan = lambda **_kwargs: (
        calls.append('prepare') or True
    )
    controller.runSlowScan = lambda: calls.append('arm') or True
    controller.updateScatter = lambda *_args: None
    controller.saveValidationImages = lambda **_kwargs: None

    controller._handle_event_frame(
        np.asarray([[1.0, 2.0]]), np.zeros((2, 2))
    )

    assert calls.index('reserve') < calls.index('transform')
    assert calls.index('reserve') < calls.index('prepare')
    assert calls.index('prepare') < calls.index('arm')
    assert controller._state.busy is False


@pytest.mark.parametrize('failurePoint', ['lifecycle', 'snapshot', 'log'])
def test_scan_terminal_bookkeeping_failure_still_recovers_fast_modality(
        failurePoint):
    controller = _bare_controller()
    controller._state.busy = True
    controller._state.frame = 9
    controller.setDetLogLine = lambda *_args: None
    controller._set_status = lambda *_args: None
    controller.scanInfoDict = {
        'scan_samples_total': 10,
        'sample_rate': 10,
    }
    controller._commChannel = SimpleNamespace(
        sigSnapImg=_Signal(
            error=(
                RuntimeError('snapshot failed')
                if failurePoint == 'snapshot' else None
            )
        )
    )
    if failurePoint == 'lifecycle':
        controller._finishTriggeredScanRun = lambda: (
            (_ for _ in ()).throw(RuntimeError('lifecycle failed'))
        )
    else:
        controller._finishTriggeredScanRun = lambda: True
    if failurePoint == 'log':
        controller.endRecording = lambda: (
            (_ for _ in ()).throw(RuntimeError('log failed'))
        )
    else:
        controller.endRecording = lambda: None
    recovered = []
    controller.continueFastModality = lambda: recovered.append(True)

    controller.scanEnded()

    assert recovered == [True]
    assert controller._state.busy is False
    assert controller._state.frame == 0


def test_triggered_end_notification_waits_for_run_release_callback():
    controller = _bare_controller()
    token = object()
    controller._triggeredScanRunToken = token
    controller._triggeredScanStartingPublished = True
    callbacks = []
    controller._scanCoordinator = SimpleNamespace(
        releaseRun=lambda releasedToken, *, onReleased: (
            callbacks.append(onReleased) or releasedToken is token
        )
    )
    ended = []
    disconnected = []
    controller._commChannel = SimpleNamespace(
        scanWorkflow=SimpleNamespace(
            notify_scan_ended=lambda: ended.append(True)
        )
    )
    controller._disconnectNidaqCompletionSignalsIfIdle = (
        lambda: disconnected.append(True)
    )

    assert controller._finishTriggeredScanRun() is True
    assert ended == []
    assert disconnected == []

    callbacks[0]()

    assert ended == [True]
    assert disconnected == [True]


@pytest.mark.parametrize('buildFailed', (False, True))
def test_triggered_worker_barrier_handoff_runs_on_ui_thread(
    qtbot, buildFailed,
):
    controller = _qt_bare_controller()
    runToken = object()
    iterationToken = object()
    controller._triggeredScanRunToken = runToken

    class Coordinator:
        def tokenForOwner(self, _owner):
            return iterationToken

        def runForOwner(self, _owner):
            return runToken

        def resolve(self, token, _mode, onComplete=None):
            assert token is iterationToken
            worker = threading.Thread(target=onComplete)
            worker.start()
            worker.join(timeout=1)
            return True

    controller._scanCoordinator = Coordinator()
    observed = []
    controller.scanEnded = (
        lambda: observed.append(threading.current_thread())
    )
    controller._afterTriggeredScanBuildFailed = (
        lambda: observed.append(threading.current_thread())
    )

    if buildFailed:
        controller._onTriggeredNidaqScanBuildFailed()
    else:
        controller._onTriggeredNidaqScanDone()

    qtbot.waitUntil(lambda: len(observed) == 1, timeout=1000)
    assert observed == [threading.main_thread()]


def test_close_waits_for_owned_scan_before_disconnecting_completion_signals():
    controller = _bare_controller()
    active = SimpleNamespace(
        iteration=object(),
        run=object(),
        tokenForOwner=lambda _owner: active.iteration,
        runForOwner=lambda _owner: active.run,
    )
    controller._scanCoordinator = active
    controller._detectorFastHandle = None
    controller._binaryMaskHandle = None
    controller._nidaqCompletionSignalsConnected = True
    doneSignal = _Signal()
    failedSignal = _Signal()
    doneSignal.connect(controller._onTriggeredNidaqScanDone)
    failedSignal.connect(controller._onTriggeredNidaqScanBuildFailed)
    controller._master = SimpleNamespace(
        nidaqManager=SimpleNamespace(
            sigScanDone=doneSignal,
            sigScanBuildFailed=failedSignal,
        )
    )
    controller._commChannel = SimpleNamespace(
        sigSendScanParameters=_Signal(),
        sigSendScanFreq=_Signal(),
    )
    controller._commChannel.sigSendScanParameters.connect(
        controller.assignScanParameters
    )
    controller._commChannel.sigSendScanFreq.connect(controller.logScanFreq)
    controller.stopExperiment = lambda resetParams=False: None

    assert controller.closeEvent() is False
    assert controller._nidaqCompletionSignalsConnected is True
    assert doneSignal.slots == [controller._onTriggeredNidaqScanDone]
    assert failedSignal.slots == [
        controller._onTriggeredNidaqScanBuildFailed
    ]

    active.iteration = None
    assert controller.shutdownComplete() is False
    assert controller._nidaqCompletionSignalsConnected is True

    active.run = None
    assert controller.shutdownComplete() is True
    assert controller._nidaqCompletionSignalsConnected is False
    assert doneSignal.slots == []
    assert failedSignal.slots == []


def test_close_retries_detector_stream_handle_after_release_timeout():
    class _ReleaseManager:
        def __init__(self):
            self.failuresRemaining = 1
            self.calls = []

        def release(self, handle):
            self.calls.append(handle)
            if self.failuresRemaining:
                self.failuresRemaining -= 1
                raise TimeoutError('poller is still stopping')

    controller = _bare_controller()
    manager = _ReleaseManager()
    handle = object()
    controller._closed = True
    controller._scanCoordinator = None
    controller._nidaqCompletionSignalsConnected = False
    controller._detectorFastHandle = handle
    controller._binaryMaskHandle = None
    controller._master = SimpleNamespace(detectorsManager=manager)

    assert controller.shutdownComplete() is False
    assert controller._detectorFastHandle is handle

    assert controller.shutdownComplete() is True
    assert controller._detectorFastHandle is None
    assert manager.calls == [handle, handle]


def test_binary_mask_cleanup_retains_timed_out_handle_for_retry():
    class _ReleaseManager:
        def __init__(self):
            self.failuresRemaining = 1
            self.calls = []

        def release(self, handle):
            self.calls.append(handle)
            if self.failuresRemaining:
                self.failuresRemaining -= 1
                raise TimeoutError('poller is still stopping')

    controller = _bare_controller()
    manager = _ReleaseManager()
    handle = object()
    controller._master = SimpleNamespace(detectorsManager=manager)
    controller._binaryMaskHandle = handle
    controller._binaryMaskGeneration = 0
    controller._binaryMaskFrameSlot = None
    controller._state.binaryMaskSignalConnected = False
    controller._binary_stack_list = []
    controller._setFastLaserEnabled = lambda *_args, **_kwargs: None
    controller._widget = SimpleNamespace()

    controller._cleanupBinaryMaskRecording()
    assert controller._binaryMaskHandle is handle

    controller._cleanupBinaryMaskRecording()
    assert controller._binaryMaskHandle is None
    assert manager.calls == [handle, handle]


def test_recording_failure_signal_is_paired_with_ended_signal_connection():
    controller = _bare_controller(mode=ScanInitiationMode.RecordingWidget)
    controller._state.imageSignalConnected = True
    controller._state.scanEndSignalConnected = False
    controller._detectorFastHandle = None
    controller._commChannel = SimpleNamespace(
        sigRecordingEnded=_Signal(),
        sigRecordingFailed=_Signal(),
        sigUpdateImage=_Signal(),
    )

    controller._connectRunSignals()

    assert controller._commChannel.sigRecordingEnded.slots == [
        controller.scanEnded
    ]
    assert controller._commChannel.sigRecordingFailed.slots == [
        controller._onTriggeredRecordingFailed
    ]

    controller._state.imageSignalConnected = False
    controller._disconnectRunSignals()

    assert controller._commChannel.sigRecordingEnded.slots == []
    assert controller._commChannel.sigRecordingFailed.slots == []


def test_unrelated_or_post_stop_recording_failure_cannot_resume():
    controller = _bare_controller(mode=ScanInitiationMode.RecordingWidget)
    controller._triggeredRecordingInFlight = False
    controller._stopRequested = True
    controller._experimentActive = False
    controller.continueFastModality = lambda: (_ for _ in ()).throw(
        AssertionError('post-stop recording failure resumed the modality')
    )

    controller._onTriggeredRecordingFailed('disk full')

    assert controller._state.running is False


def test_active_triggered_recording_failure_recovers_once():
    controller = _bare_controller(mode=ScanInitiationMode.RecordingWidget)
    controller._triggeredRecordingInFlight = True
    controller._state.running = False
    controller._state.busy = True
    controller._set_status = lambda *_args: None
    recovered = []
    controller.continueFastModality = lambda: recovered.append(True)

    controller._onTriggeredRecordingFailed('disk full')
    controller._onTriggeredRecordingFailed('late duplicate')

    assert recovered == [True]
    assert controller._triggeredRecordingInFlight is False
    assert controller._state.busy is False


def test_triggered_recording_pins_new_manager_generation():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._master = SimpleNamespace(
        recordingManager=SimpleNamespace(
            recordingGeneration=4,
            record=True,
        )
    )
    controller._recordingGenerationBeforeTrigger = 3
    controller._triggeredRecordingGeneration = None

    assert controller._captureTriggeredRecordingOperation() is True
    assert controller._triggeredRecordingGeneration == 4


def test_triggered_recording_rejects_missing_new_generation():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._master = SimpleNamespace(
        recordingManager=SimpleNamespace(
            recordingGeneration=3,
            record=True,
        )
    )
    controller._recordingGenerationBeforeTrigger = 3

    assert controller._captureTriggeredRecordingOperation() is False


@pytest.mark.parametrize('busyKind', ('active-source', 'held-run'))
def test_triggered_recording_refuses_before_opening_busy_identity_window(
    busyKind,
):
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    source = SimpleNamespace()
    activeSource = source if busyKind == 'active-source' else None
    activeRun = object() if busyKind == 'held-run' else None
    controller._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: activeSource,
        getRecordingScanSource=lambda: source,
    )
    controller._master = SimpleNamespace(
        recordingManager=SimpleNamespace(recordingGeneration=4),
        scanExecutionCoordinator=SimpleNamespace(
            activeRunToken=activeRun
        ),
    )

    assert controller._beginTriggeredScanRun() is False
    assert controller._triggeredRecordingInFlight is False
    assert controller.__dict__.get(
        '_recordingGenerationBeforeTrigger'
    ) is None


def test_stale_triggered_recording_terminal_signals_are_ignored():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingGeneration = 8
    controller._triggeredRecordingCaptured = True
    controller._triggeredRecordingLifecycleEnded = True
    controller._triggeredRecordingManagerTerminal = False
    controller._triggeredRecordingFailureMessage = None
    events = []
    controller.scanEnded = lambda: events.append('ended')
    controller._onTriggeredRecordingFailed = (
        lambda message: events.append(('failed', message))
    )

    controller._onTriggeredRecordingEndedDetailed(7)
    controller._onTriggeredRecordingFailedDetailed('old', 7)
    controller._onTriggeredRecordingEndedDetailed(8)

    assert events == ['ended']


def test_triggered_recording_waits_for_writer_and_scan_lifecycle_barriers():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingGeneration = 12
    controller._triggeredRecordingCaptured = True
    controller._triggeredRecordingManagerTerminal = False
    controller._triggeredRecordingLifecycleEnded = False
    controller._triggeredRecordingFailureMessage = None
    events = []
    controller.scanEnded = lambda: events.append('ended')

    controller._onTriggeredRecordingEndedDetailed(12)
    assert events == []

    controller._onTriggeredRecordingLifecycleEnded()
    assert events == ['ended']


def test_triggered_recording_failure_waits_for_scan_lifecycle_end():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingGeneration = 13
    controller._triggeredRecordingCaptured = True
    controller._triggeredRecordingManagerTerminal = False
    controller._triggeredRecordingLifecycleEnded = False
    controller._triggeredRecordingFailureMessage = None
    events = []
    controller._onTriggeredRecordingFailed = (
        lambda message: events.append(('failed', message))
    )

    controller._onTriggeredRecordingFailedDetailed('disk full', 13)
    assert events == []

    controller._onTriggeredRecordingLifecycleEnded()
    assert events == [('failed', 'disk full')]


def test_stale_scan_end_cannot_complete_active_triggered_scan_source():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    active_source = object()
    controller._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: active_source
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingGeneration = 14
    controller._triggeredRecordingCaptured = True
    controller._triggeredRecordingManagerTerminal = True
    controller._triggeredRecordingLifecycleEnded = False
    controller._triggeredRecordingFailureMessage = None
    events = []
    controller.scanEnded = lambda: events.append('ended')

    controller._onTriggeredRecordingLifecycleEnded()

    assert controller._triggeredRecordingLifecycleEnded is False
    assert events == []


def test_held_barrier_cleared_run_accepts_triggered_recording_lifecycle_end():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    source = SimpleNamespace(isRunning=False)
    token = SimpleNamespace(
        owner=source,
        releaseRequested=True,
        releaseBarrierCleared=True,
        holdReleaseUntilFinalized=True,
    )
    controller._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: None
    )
    controller._master = SimpleNamespace(
        scanExecutionCoordinator=SimpleNamespace(activeRunToken=token)
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingScanSource = source
    controller._triggeredRecordingRunToken = None
    controller._triggeredRecordingSourceObservedRunning = False
    controller._triggeredRecordingLifecycleEnded = False
    readyChecks = []
    controller._finishTriggeredRecordingIfReady = (
        lambda: readyChecks.append(True)
    )

    controller._onTriggeredRecordingLifecycleEnded()

    assert controller._triggeredRecordingLifecycleEnded is True
    assert controller._triggeredRecordingRunToken is token
    assert readyChecks == [True]


def test_foreign_held_run_cannot_end_triggered_recording():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    expectedSource = SimpleNamespace(isRunning=False)
    foreignSource = object()
    token = SimpleNamespace(
        owner=foreignSource,
        releaseRequested=True,
        releaseBarrierCleared=True,
        holdReleaseUntilFinalized=True,
    )
    controller._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: None
    )
    controller._master = SimpleNamespace(
        scanExecutionCoordinator=SimpleNamespace(activeRunToken=token)
    )
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingCaptured = True
    controller._triggeredRecordingScanSource = expectedSource
    controller._triggeredRecordingRunToken = None
    controller._triggeredRecordingSourceObservedRunning = True
    controller._triggeredRecordingLifecycleEnded = False
    readyChecks = []
    controller._finishTriggeredRecordingIfReady = (
        lambda: readyChecks.append(True)
    )

    controller._onTriggeredRecordingLifecycleEnded()

    assert controller._triggeredRecordingLifecycleEnded is False
    assert controller._triggeredRecordingRunToken is None
    assert readyChecks == []


def test_precapture_global_end_without_source_identity_is_not_cached():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: None
    )
    controller._master = SimpleNamespace()
    controller._triggeredRecordingInFlight = True
    controller._triggeredRecordingCaptured = False
    controller._triggeredRecordingScanSource = None
    controller._triggeredRecordingLifecycleEnded = False
    readyChecks = []
    controller._finishTriggeredRecordingIfReady = (
        lambda: readyChecks.append(True)
    )

    controller._onTriggeredRecordingLifecycleEnded()

    assert controller._triggeredRecordingLifecycleEnded is False
    assert readyChecks == []


def test_triggered_recording_rejects_skipped_manager_generation():
    controller = _bare_controller(
        mode=ScanInitiationMode.RecordingWidget
    )
    controller._master = SimpleNamespace(
        recordingManager=SimpleNamespace(
            recordingGeneration=9,
            record=True,
        )
    )
    controller._recordingGenerationBeforeTrigger = 7

    assert controller._captureTriggeredRecordingOperation() is False
    assert controller.__dict__.get(
        '_triggeredRecordingGeneration'
    ) is None


def test_event_stream_restart_does_not_reuse_failed_release_handle():
    class _RetryManager:
        def __init__(self):
            self.failuresRemaining = 1
            self.releaseCalls = []
            self.acquireCalls = []

        def release(self, handle):
            self.releaseCalls.append(handle)
            if self.failuresRemaining:
                self.failuresRemaining -= 1
                raise TimeoutError('poller is still stopping')

        def acquire(self, detectorNames, purpose):
            self.acquireCalls.append((tuple(detectorNames), purpose))
            return 'new-stream'

    controller = _bare_controller()
    manager = _RetryManager()
    oldHandle = object()
    controller._master = SimpleNamespace(detectorsManager=manager)
    controller._detectorFastHandle = oldHandle
    controller._detectorFastReleasePending = True

    with pytest.raises(
        RuntimeError, match='previous event-stream lease'
    ):
        controller._acquireDetectorFastStream()

    assert controller._detectorFastHandle is oldHandle
    assert manager.acquireCalls == []

    controller._acquireDetectorFastStream()

    assert controller._detectorFastHandle == 'new-stream'
    assert controller._detectorFastReleasePending is False
    assert manager.releaseCalls == [oldHandle, oldHandle]
    assert manager.acquireCalls == [
        (('CAM',), LeasePurpose.EVENT_STREAM)
    ]


def test_direct_triggered_run_stays_reserved_through_end_publication():
    controller = _bare_controller()
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

    def notifyEnded():
        assert coordinator.activeRunToken is runToken
        events.append('ended-while-held')

    controller._scanCoordinator = coordinator
    controller._triggeredScanRunToken = runToken
    controller._triggeredScanStartingPublished = True
    controller._triggeredScanCompletionPublishing = False
    controller._disconnectNidaqCompletionSignalsIfIdle = lambda: None
    controller._commChannel = SimpleNamespace(
        scanWorkflow=SimpleNamespace(notify_scan_ended=notifyEnded)
    )

    assert controller._finishTriggeredScanRun() is True
    assert events == [
        'release-requested',
        'ended-while-held',
        'release-finalized',
    ]
    assert coordinator.activeRunToken is None


def test_failed_triggered_terminal_handoff_cannot_strand_held_run():
    controller = _bare_controller()
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
    controller._scanCoordinator = coordinator
    controller._triggeredScanRunToken = runToken
    controller._triggeredScanStartingPublished = True
    controller._triggeredScanCompletionPublishing = False
    controller._disconnectNidaqCompletionSignalsIfIdle = lambda: None
    controller._commChannel = SimpleNamespace(
        scanWorkflow=SimpleNamespace(
            notify_scan_ended=lambda: events.append('ended')
        )
    )
    controller._invokeOnControllerThreadIfNeeded = lambda _callback: (
        (_ for _ in ()).throw(RuntimeError('handoff failed'))
    )

    assert controller._finishTriggeredScanRun() is True
    assert events == [
        'release-requested',
        'ended',
        'release-finalized',
    ]
    assert coordinator.activeRunToken is None
    assert controller._triggeredScanCompletionPublishing is False
