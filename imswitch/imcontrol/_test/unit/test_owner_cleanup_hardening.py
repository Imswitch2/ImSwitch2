"""Regression tests for controller-owned detector and thread cleanup."""

from collections import deque
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.controller.controllers.AutofocusController import (
    AutofocusController,
)
from imswitch.imcontrol.controller.controllers.BeadRecController import (
    BeadRecController,
)
from imswitch.imcontrol.controller.controllers.EtSnoutyController import (
    EtSnoutyController,
)
from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusCalibThread, FocusLockController,
)
from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.managers.RecordingManager import (
    RecordingManager,
    RecordingWorker,
    SaveMode,
    WriterThread,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


def test_widget_touching_owner_apis_are_marshaled_to_ui_thread():
    assert TilingController.startTiling._APIRunOnUIThread is True
    assert TilingController.setTileLabel._APIRunOnUIThread is True
    assert AutofocusController.autoFocus._APIRunOnUIThread is True


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _CaptureSignal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _LeaseManager:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, detectorNames, purpose):
        handle = f'lease-{len(self.acquired) + 1}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


def test_etsnouty_temporary_read_always_owns_a_reference():
    manager = _LeaseManager()
    # The old implementation consulted this global membership and borrowed
    # another owner's lease instead of taking its own reference.
    manager.isDetectorLeased = lambda _name: (_ for _ in ()).throw(
        AssertionError('global membership must not be consulted')
    )
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._EtSnoutyController__logger = _Logger()

    with EtSnoutyController._temporaryDetectorRead(ctrl, 'CAM'):
        pass

    assert manager.acquired == [(('CAM',), LeasePurpose.SNAP, 'lease-1')]
    assert manager.released == ['lease-1']


def test_etsnouty_clock_failure_turns_laser_off_and_clears_busy(monkeypatch):
    class _Laser:
        def __init__(self):
            self.states = []

        def setEnabled(self, enabled):
            self.states.append(enabled)

    class _Lasers:
        def __init__(self, laser):
            self.laser = laser

        def execOn(self, _name, operation):
            return operation(self.laser)

    class _Detector:
        def wait_and_get_NewFrame(self, properFrame):
            raise RuntimeError('camera read failed')

    laser = _Laser()
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._master = SimpleNamespace(lasersManager=_Lasers(laser))
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._EtSnoutyController__clock_busy = False
    ctrl.laserFast = 'Laser'
    ctrl.detectorFast_controller = _Detector()
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.EtSnoutyController.time.sleep',
        lambda _seconds: None,
    )

    try:
        EtSnoutyController.clockWidefield_fct(ctrl)
    except RuntimeError:
        pass
    else:
        raise AssertionError('camera exception should remain visible to the caller')

    assert laser.states == [True, False]
    assert ctrl._EtSnoutyController__clock_busy is False


def test_etsnouty_terminal_reset_stops_timer_and_releases_lease():
    class _Emitter:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    manager = _LeaseManager()
    handle = manager.acquire(['CAM'], LeasePurpose.EVENT_DIRECT)
    emitter = _Emitter()
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._detectorFastHandle = handle
    ctrl.emitter = emitter
    ctrl._EtSnoutyController__prevFrames = deque([1])
    ctrl._EtSnoutyController__prevAnaFrames = deque([2])

    EtSnoutyController.resetRunParams(ctrl)

    assert emitter.stopped
    assert ctrl.emitter is None
    assert manager.released == [handle]


def test_etsnouty_shutdown_retries_timed_out_stream_lease_release():
    class _RetryingLeaseManager(_LeaseManager):
        def __init__(self):
            super().__init__()
            self.failuresRemaining = 1

        def release(self, handle):
            if self.failuresRemaining:
                self.failuresRemaining -= 1
                raise TimeoutError('poller is still stopping')
            super().release(handle)

    manager = _RetryingLeaseManager()
    handle = manager.acquire(['CAM'], LeasePurpose.EVENT_STREAM)
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._detectorFastHandle = handle
    ctrl._closed = True

    assert ctrl.shutdownComplete() is False
    assert ctrl._detectorFastHandle is handle

    assert ctrl.shutdownComplete() is True
    assert ctrl._detectorFastHandle is None
    assert manager.released == [handle]


def test_etsnouty_restart_never_reuses_failed_release_handle():
    class _RetryingLeaseManager(_LeaseManager):
        def __init__(self):
            super().__init__()
            self.failuresRemaining = 1
            self.releaseCalls = []

        def release(self, handle):
            self.releaseCalls.append(handle)
            if self.failuresRemaining:
                self.failuresRemaining -= 1
                raise TimeoutError('poller is still stopping')
            super().release(handle)

    manager = _RetryingLeaseManager()
    oldHandle = object()
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._detectorFastHandle = oldHandle
    ctrl._detectorFastReleasePending = True
    ctrl.detectorFast = 'CAM'
    ctrl.ClockWidefield = False

    with pytest.raises(
        RuntimeError, match='previous EtSnouty detector lease'
    ):
        ctrl._acquireDetectorFastLease()

    assert ctrl._detectorFastHandle is oldHandle
    assert manager.acquired == []

    ctrl._acquireDetectorFastLease()

    assert ctrl._detectorFastHandle == 'lease-1'
    assert ctrl._detectorFastReleasePending is False
    assert manager.releaseCalls == [oldHandle, oldHandle]
    assert manager.acquired == [
        (('CAM',), LeasePurpose.EVENT_STREAM, 'lease-1')
    ]


def test_etsnouty_foreign_scan_end_cannot_resume_fast_modality():
    source = SimpleNamespace(isRunning=True)
    activeSource = [source]
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._triggeredScanInFlight = True
    ctrl._triggeredScanSource = source
    ctrl._triggeredScanSourceObservedRunning = True
    ctrl._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: activeSource[0],
    )
    ctrl.continueFastModality = lambda: (_ for _ in ()).throw(
        AssertionError('foreign end resumed EtSnouty')
    )

    ctrl.scanEnded()

    assert ctrl._triggeredScanInFlight is True
    assert ctrl._triggeredScanSource is source


def test_etsnouty_exact_source_end_is_accepted_once():
    source = SimpleNamespace(isRunning=True)
    activeSource = [source]
    snapshots = _CaptureSignal()
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._EtSnoutyController__detLog = {}
    ctrl._EtSnoutyController__frame = 7
    ctrl._triggeredScanInFlight = True
    ctrl._triggeredScanSource = None
    ctrl._triggeredScanSourceObservedRunning = False
    ctrl._closed = False
    ctrl._stopRequested = False
    ctrl._experimentActive = True
    ctrl._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: activeSource[0],
        sigSnapImg=snapshots,
    )
    ctrl._onTriggeredScanStarting()
    assert ctrl._triggeredScanSource is source

    source.isRunning = False
    activeSource[0] = None
    events = []
    ctrl.endRecording = lambda: events.append('logged')
    ctrl.continueFastModality = lambda: events.append('resumed')
    ctrl.scanInfoDict = {}

    ctrl.scanEnded()
    ctrl.scanEnded()

    assert ctrl._triggeredScanInFlight is False
    assert snapshots.calls == [()]
    assert events == ['logged', 'resumed']
    assert ctrl._EtSnoutyController__frame == 0


def test_etsnouty_terminal_bookkeeping_failure_still_resumes():
    source = SimpleNamespace(isRunning=False)
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._EtSnoutyController__detLog = {}
    ctrl._EtSnoutyController__frame = 5
    ctrl._triggeredScanInFlight = True
    ctrl._triggeredScanSource = source
    ctrl._triggeredScanSourceObservedRunning = True
    ctrl._closed = False
    ctrl._stopRequested = False
    ctrl._experimentActive = True
    ctrl._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: None,
        sigSnapImg=SimpleNamespace(
            emit=lambda: (_ for _ in ()).throw(
                RuntimeError('snapshot failed')
            )
        ),
    )
    resumed = []
    ctrl.continueFastModality = lambda: resumed.append(True)

    ctrl.scanEnded()

    assert resumed == [True]
    assert ctrl._EtSnoutyController__frame == 0
    assert ctrl._triggeredScanInFlight is False


def test_etsnouty_queued_exact_end_after_stop_never_resumes():
    source = SimpleNamespace(isRunning=False)
    ctrl = EtSnoutyController.__new__(EtSnoutyController)
    ctrl._EtSnoutyController__logger = _Logger()
    ctrl._EtSnoutyController__detLog = {}
    ctrl._EtSnoutyController__frame = 4
    ctrl._triggeredScanInFlight = True
    ctrl._triggeredScanSource = source
    ctrl._triggeredScanSourceObservedRunning = True
    ctrl._closed = False
    ctrl._stopRequested = True
    ctrl._experimentActive = False
    ctrl._commChannel = SimpleNamespace(
        getActiveScanSource=lambda: None,
        sigSnapImg=SimpleNamespace(
            emit=lambda: (_ for _ in ()).throw(
                AssertionError('post-stop end took a snapshot')
            )
        ),
    )
    ctrl.continueFastModality = lambda: (_ for _ in ()).throw(
        AssertionError('post-stop end resumed EtSnouty')
    )

    ctrl.scanEnded()

    assert ctrl._triggeredScanInFlight is False
    assert ctrl._EtSnoutyController__frame == 0


class _OrderedThread:
    def __init__(self, events, aliveAfterJoin=False):
        self.events = events
        self.aliveAfterJoin = aliveAfterJoin

    def quit(self):
        self.events.append('quit')

    def wait(self):
        self.events.append('wait')

    def join(self, timeout):
        self.events.append(('join', timeout))

    def is_alive(self):
        return self.aliveAfterJoin


class _OrderedManager(_LeaseManager):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def release(self, handle):
        self.events.append('release')
        super().release(handle)


def test_focus_lock_waits_for_workers_before_releasing_camera():
    events = []
    manager = _OrderedManager(events)
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._focusAcqHandle = manager.acquire(['CAM'], LeasePurpose.FOCUS)
    ctrl._FocusLockController__processDataThread = _OrderedThread(events)
    ctrl._FocusLockController__focusCalibThread = _OrderedThread(events)
    ctrl._logger = _Logger()

    FocusLockController.closeEvent(ctrl)

    assert events[-1] == 'release'
    assert events.count('wait') == 2


def test_autofocus_defers_release_while_worker_is_still_alive():
    events = []
    manager = _OrderedManager(events)
    ctrl = AutofocusController.__new__(AutofocusController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._focusAcqHandle = manager.acquire(['CAM'], LeasePurpose.FOCUS)
    ctrl._focusThread = _OrderedThread(events, aliveAfterJoin=True)
    ctrl._logger = _Logger()

    AutofocusController.closeEvent(ctrl)

    assert manager.released == []
    AutofocusController._releaseFocusLease(ctrl)
    assert manager.released == ['lease-1']


class _SnapDetector:
    def getLatestFrameShared(self, is_save=False):
        return np.ones((2, 2), dtype=np.uint16)


class _SnapManager(_LeaseManager):
    def __init__(self):
        super().__init__()
        self.detectors = {'A': _SnapDetector(), 'B': _SnapDetector()}

    def __getitem__(self, name):
        return self.detectors[name]


def test_numpy_snap_materializes_generator_selection_once():
    detectors = _SnapManager()
    recording = RecordingManager.__new__(RecordingManager)
    recording._RecordingManager__detectorsManager = detectors
    # This shell intentionally bypasses the manager constructor; keep its
    # destructor from exercising unrelated recording-thread state.
    recording.endRecording = lambda *args, **kwargs: None

    images = RecordingManager.snap(
        recording,
        (name for name in ('A', 'B')),
        'unused',
        SaveMode.Numpy,
        None,
        {},
    )

    assert tuple(images) == ('A', 'B')
    assert detectors.acquired == [
        (('A', 'B'), LeasePurpose.SNAP, 'lease-1')
    ]
    assert detectors.released == ['lease-1']


def test_recording_acquire_failure_unblocks_readiness_and_resets_state():
    class _FailingDetectors:
        def acquire(self, detectorNames, purpose):
            raise RuntimeError('cannot arm camera')

    class _Recording:
        def __init__(self):
            self.detectorsManager = _FailingDetectors()
            self.record = True
            self.failed = False
            self.ended = []

        def waitForAcquisitionStarted(self, timeout=None):
            return False

        def _signalAcquisitionFailed(self):
            self.failed = True

        def endRecording(self, emitSignal=True, wait=True):
            self.record = False
            self.ended.append((emitSignal, wait))

    recording = _Recording()
    worker = RecordingWorker(recording)
    worker.detectorNames = ['CAM']

    worker.run()

    assert recording.failed
    assert recording.record is False
    assert recording.ended == [(False, False)]


class _FlushDetector:
    forAcquisition = True

    def __init__(self, name, events, error=None):
        self.name = name
        self.events = events
        self.error = error

    def flushBuffers(self):
        self.events.append(('flush', self.name))
        if self.error is not None:
            raise self.error

    def releaseChunkConsumer(self, consumerKey):
        self.events.append(('release-consumer', self.name, consumerKey))


class _SelectionManager:
    def __init__(self, detectors):
        self.detectors = detectors

    def __getitem__(self, name):
        return self.detectors[name]


class _RecordingThread:
    def __init__(self, events):
        self.events = events

    def start(self):
        self.events.append('start')

    def quit(self):
        self.events.append('quit')

    def wait(self):
        self.events.append('wait')


def _recording_shell(detectors, events):
    manager = RecordingManager.__new__(RecordingManager)
    manager._RecordingManager__logger = _Logger()
    manager._RecordingManager__detectorsManager = _SelectionManager(detectors)
    manager._RecordingManager__record = False
    manager._RecordingManager__abort = False
    manager._RecordingManager__activeDetectorNames = ()
    manager._RecordingManager__acqStartedEvent = threading.Event()
    manager._RecordingManager__acqStartFailed = False
    manager._RecordingManager__recordingSignalLock = threading.Lock()
    manager._RecordingManager__recordingGeneration = 0
    manager._RecordingManager__endSignalEmitted = False
    manager._RecordingManager__failureSignalEmitted = False
    manager._RecordingManager__lastRecordingError = None
    manager._RecordingManager__recordingWorker = None
    manager._RecordingManager__thread = None

    def prepare():
        manager._RecordingManager__recordingWorker = SimpleNamespace()
        manager._RecordingManager__thread = _RecordingThread(events)

    manager._RecordingManager__prepareRecordingThread = prepare
    return manager


def test_recording_normalizes_selection_without_global_buffer_flush():
    events = []
    detectors = {
        name: _FlushDetector(name, events)
        for name in ('A', 'B', 'UNRELATED')
    }
    manager = _recording_shell(detectors, events)

    manager.startRecording(
        detectorNames=(name for name in ('A', 'A', 'B')),
        recMode=object(),
        savename='unused',
        saveMode=SaveMode.Numpy,
        attrs={},
    )

    assert manager.record is True
    assert manager._RecordingManager__recordingWorker.detectorNames == ('A', 'B')
    assert events == ['start']
    manager.endRecording = lambda *args, **kwargs: None


def test_recording_rejects_invalid_selection_before_state_changes():
    events = []
    manager = _recording_shell(
        {'A': _FlushDetector('A', events)},
        events,
    )

    with pytest.raises(KeyError):
        manager.startRecording(
            detectorNames=['A', 'MISSING'],
            recMode=object(),
            savename='unused',
            saveMode=SaveMode.Numpy,
            attrs={},
        )

    assert manager.record is False
    assert events == []
    manager.endRecording = lambda *args, **kwargs: None


def test_end_recording_stops_thread_without_racing_detector_flush():
    events = []
    manager = _recording_shell(
        {'A': _FlushDetector('A', events, RuntimeError('flush failed'))},
        events,
    )
    manager._RecordingManager__record = True
    manager._RecordingManager__activeDetectorNames = ('A',)
    manager._RecordingManager__thread = _RecordingThread(events)

    manager.endRecording(emitSignal=False, wait=True)

    assert manager.record is False
    assert manager._RecordingManager__activeDetectorNames == ()
    assert events == ['quit', 'wait']
    manager.endRecording = lambda *args, **kwargs: None


def test_end_recording_does_not_publish_success_when_worker_wait_fails():
    manager = _recording_shell({}, [])
    manager.sigRecordingEnded = _CaptureSignal()

    class _FailingWaitThread(_RecordingThread):
        def wait(self):
            super().wait()
            raise TimeoutError('worker stuck')

    manager._RecordingManager__record = True
    manager._RecordingManager__thread = _FailingWaitThread([])

    with pytest.raises(TimeoutError, match='worker stuck'):
        manager.endRecording(emitSignal=True, wait=True)

    assert manager.sigRecordingEnded.calls == []
    manager.endRecording = lambda *args, **kwargs: None


def test_abort_recording_cleans_up_when_writer_abort_fails():
    events = []
    manager = _recording_shell(
        {'A': _FlushDetector('A', events, RuntimeError('flush failed'))},
        events,
    )

    class _Worker:
        def requestWriterAbort(self):
            events.append('writer-abort')
            raise OSError('writer failed')

    manager._RecordingManager__record = True
    manager._RecordingManager__activeDetectorNames = ('A',)
    manager._RecordingManager__recordingWorker = _Worker()
    manager._RecordingManager__thread = _RecordingThread(events)

    with pytest.raises(OSError, match='writer failed'):
        manager.abortRecording(emitSignal=False, wait=True)

    assert manager.record is False
    assert manager.aborting is True
    assert manager._RecordingManager__activeDetectorNames == ()
    assert events == ['writer-abort', 'quit', 'wait']
    manager.endRecording = lambda *args, **kwargs: None


def test_recording_failure_is_distinct_and_terminal_exactly_once():
    manager = _recording_shell({}, [])
    manager.sigRecordingFailed = _CaptureSignal()
    manager.sigRecordingFailedDetailed = _CaptureSignal()
    manager.sigRecordingEnded = _CaptureSignal()

    assert manager._signalRecordingFailed(RuntimeError('disk full')) is True
    assert manager._signalRecordingFailed(RuntimeError('second')) is False
    manager.endRecording(emitSignal=True, wait=False)

    assert manager.sigRecordingFailed.calls == [('disk full',)]
    assert manager.sigRecordingFailedDetailed.calls == [('disk full', 0)]
    assert manager.sigRecordingEnded.calls == []
    assert str(manager.lastRecordingError) == 'disk full'
    manager.endRecording = lambda *args, **kwargs: None


def test_recording_detailed_terminal_survives_legacy_signal_suppression():
    manager = _recording_shell({}, [])
    manager._RecordingManager__recordingGeneration = 3
    manager.sigRecordingEnded = _CaptureSignal()
    manager.sigRecordingEndedDetailed = _CaptureSignal()

    assert manager._signalRecordingEnded(3, emitLegacy=False) is True
    assert manager._signalRecordingEnded(3, emitLegacy=False) is False

    assert manager.sigRecordingEndedDetailed.calls == [(3,)]
    assert manager.sigRecordingEnded.calls == []
    manager.endRecording = lambda *args, **kwargs: None


def test_stale_or_post_success_failure_cannot_poison_current_generation():
    manager = _recording_shell({}, [])
    manager._RecordingManager__recordingGeneration = 4
    manager.sigRecordingEnded = _CaptureSignal()
    manager.sigRecordingEndedDetailed = _CaptureSignal()
    manager.sigRecordingFailed = _CaptureSignal()
    manager.sigRecordingFailedDetailed = _CaptureSignal()

    assert manager._signalRecordingFailed('old', generation=3) is False
    assert manager._RecordingManager__failureSignalEmitted is False
    assert manager._RecordingManager__endSignalEmitted is False

    assert manager._signalRecordingEnded(4, emitLegacy=False) is True
    assert manager._signalRecordingFailed('late', generation=4) is False

    assert manager.sigRecordingEndedDetailed.calls == [(4,)]
    assert manager.sigRecordingFailedDetailed.calls == []
    assert manager.sigRecordingFailed.calls == []
    manager.endRecording = lambda *args, **kwargs: None


class _WriterStorer:
    def __init__(self, writeError=None):
        self.writeError = writeError
        self.aborted = False
        self.finalized = False

    def openStream(self, **kwargs):
        pass

    def writeFrames(self, detectorName, frames):
        if self.writeError is not None:
            raise self.writeError

    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        self.finalized = True

    def abortStream(self, filePaths, fileDests, saveMode):
        self.aborted = True


def _writer(storer):
    return WriterThread(
        storer=storer,
        fileDests={'CAM': 'CAM.h5'},
        detectorNames=['CAM'],
        shapes={'CAM': (2, 2)},
        attrs={'CAM': {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={'CAM': 'CAM.h5'},
        recordingManager=None,
    )


def test_writer_finalization_surfaces_write_failure_and_aborts_partial_output():
    storer = _WriterStorer(writeError=OSError('disk full'))
    writer = _writer(storer)
    writer.start()
    writer.wait_for_open()
    writer.enqueue_frames('CAM', np.ones((1, 2, 2), dtype=np.uint16))

    with pytest.raises(OSError, match='disk full'):
        writer.finish()

    assert not writer.is_alive()
    assert storer.aborted is True
    assert storer.finalized is False


def test_writer_rejects_enqueue_after_it_has_stopped():
    storer = _WriterStorer()
    writer = _writer(storer)
    writer.start()
    writer.wait_for_open()
    writer.finish()

    with pytest.raises(RuntimeError, match='not running'):
        writer.enqueue_frames('CAM', np.ones((1, 2, 2), dtype=np.uint16))


def test_autofocus_restores_starting_z_after_non_cancel_failure(monkeypatch):
    class _Positioner:
        def __init__(self):
            self.position = {'Z': 100.0}
            self.moves = []

        def setPosition(self, value, axis):
            self.position[axis] = value
            self.moves.append((value, axis))

    class _Detector:
        def getLatestFrame(self):
            raise RuntimeError('camera failed')

    positioner = _Positioner()
    ctrl = AutofocusController.__new__(AutofocusController)
    QtCore.QObject.__init__(ctrl)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': positioner},
        detectorsManager={'CAM': _Detector()},
    )
    ctrl.positioner = 'STAGE'
    ctrl.camera = 'CAM'
    ctrl._focusCancel = threading.Event()
    ctrl._closed = False
    ctrl._focusing = True
    ctrl._logger = _Logger()
    button = SimpleNamespace(
        setText=lambda text: setattr(button, 'text', text),
        setEnabled=lambda enabled: setattr(button, 'enabled', enabled),
        setChecked=lambda checked: setattr(button, 'checked', checked),
    )
    ctrl._widget = SimpleNamespace(focusButton=button)
    ctrl.sigFocusStopped.connect(ctrl._onFocusStopped)
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.AutofocusController._SETTLE_S',
        0,
    )

    AutofocusController._runFocus(ctrl, rangez=20.0, resolutionz=10.0)

    assert positioner.moves[0] == (90.0, 'Z')
    assert positioner.moves[-1] == (100.0, 'Z')
    assert ctrl._focusing is False
    assert button.text == 'Autofocus'
    assert button.enabled is True
    assert button.checked is False


@pytest.mark.parametrize(
    ('rangez', 'resolutionz'),
    [
        (0, 1),
        (-1, 1),
        (1, 0),
        (1, -1),
        (np.nan, 1),
        (1, np.inf),
    ],
)
def test_autofocus_rejects_non_finite_or_non_positive_inputs(
    rangez, resolutionz
):
    ctrl = AutofocusController.__new__(AutofocusController)
    ctrl._logger = _Logger()
    ctrl._closed = False
    ctrl._focusing = False
    ctrl._focusThread = None
    button = SimpleNamespace(
        setText=lambda text: setattr(button, 'text', text),
        setEnabled=lambda enabled: setattr(button, 'enabled', enabled),
        setChecked=lambda checked: setattr(button, 'checked', checked),
    )
    ctrl._widget = SimpleNamespace(focusButton=button)

    AutofocusController.autoFocus(ctrl, rangez, resolutionz)

    assert ctrl._focusThread is None
    assert ctrl._focusing is False
    assert button.text == 'Autofocus'
    assert button.enabled is True
    assert button.checked is False


def test_tiling_restores_origin_after_camera_failure(monkeypatch):
    class _Positioner:
        def __init__(self):
            self.position = {'X': 10.0, 'Y': 20.0}
            self.moves = []

        def move(self, value, axis):
            self.position[axis] += value
            self.moves.append(('move', value, axis))

        def setPosition(self, value, axis):
            self.position[axis] = value
            self.moves.append(('set', value, axis))

    class _Detector:
        pixelSizeUm = [1.0, 1.0]

        def __init__(self):
            self.calls = 0

        def getLatestFrameShared(self):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('camera failed')
            return np.ones((2, 2), dtype=np.uint16)

    class _Stitcher:
        def __init__(self, **kwargs):
            pass

        def add_tile(self, frame, gx, gy):
            pass

        def get_overview(self):
            return np.ones((2, 2), dtype=np.uint16)

    positioner = _Positioner()
    detector = _Detector()
    leases = _LeaseManager()
    leases.__getitem__ = lambda self, name: detector

    class _Detectors(_LeaseManager):
        def __getitem__(self, name):
            return detector

    detectors = _Detectors()
    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': positioner},
        detectorsManager=detectors,
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
    )
    ctrl._stopRequested = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._scanAcqHandle = None
    ctrl._scanning = True
    ctrl._scanThread = object()
    # Suppress GUI signal emission from this bare QObject shell.
    ctrl._closed = True
    ctrl._logger = _Logger()
    tilingInfo = SimpleNamespace(xyPositioner='STAGE', camera='CAM')
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.StitchedImage',
        _Stitcher,
    )

    TilingController._runScan(
        ctrl, tilingInfo, n_tiles=2, step_um=5.0,
        blend_overlaps=False, intensity_correction=False,
    )

    assert positioner.position == {'X': 10.0, 'Y': 20.0}
    assert ('move', 5.0, 'X') in positioner.moves
    # The origin is restored by unwinding the commanded displacement, never by
    # an absolute move to the tracked coordinate: the tracked frame and the
    # controller's absolute frame need not share an origin, and assuming they
    # did sent the stage flying across its travel range at the end of a run.
    assert positioner.moves[-1] == ('move', -5.0, 'X')
    assert not any(entry[0] == 'set' for entry in positioner.moves)
    assert detectors.released == ['lease-1']


def test_beadrec_ignores_prearm_callback_after_shutdown():
    manager = _LeaseManager()
    manager.getCurrentDetectorName = lambda: 'CAM'
    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._widget = SimpleNamespace(
        runButton=SimpleNamespace(isChecked=lambda: True)
    )
    ctrl._scanDetectorHandle = None
    ctrl._scanDetectorName = None
    ctrl._shutdownComplete = True
    ctrl._logger = _Logger()

    BeadRecController.onScanStarting(ctrl)

    assert manager.acquired == []


def test_beadrec_run_off_releases_prearmed_detector():
    events = []

    class _Detector:
        def releaseChunkConsumer(self, consumerKey):
            events.append(('release-consumer', consumerKey))

    class _Detectors(_LeaseManager):
        def __init__(self):
            super().__init__()
            self.detector = _Detector()

        def execOnAll(self, operation):
            operation(self.detector)

        def execOn(self, _name, operation):
            operation(self.detector)

    class _Stoppable:
        def stop(self):
            events.append('stop')

    class _Thread:
        def quit(self):
            events.append('quit')

        def wait(self):
            events.append('wait')

    detectors = _Detectors()
    handle = detectors.acquire(['CAM'], LeasePurpose.WORKFLOW)
    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._master = SimpleNamespace(detectorsManager=detectors)
    ctrl._widget = SimpleNamespace(
        runButton=SimpleNamespace(isChecked=lambda: False),
        setStatusText=lambda _text: None,
    )
    ctrl._scanEndDrainTimer = _Stoppable()
    ctrl.beadWorker = _Stoppable()
    ctrl.thread = _Thread()
    ctrl.running = True
    ctrl._scanArmed = True
    ctrl._drainingEndedScan = True
    ctrl._scanEndDrainDeadline = 1.0
    ctrl._scanDetectorHandle = handle
    ctrl._scanDetectorName = 'CAM'
    ctrl._scanDetectorGeneration = 1
    ctrl._scanGeneration = 1
    ctrl._chunkConsumerDetectorName = 'CAM'
    ctrl._chunkConsumerGeneration = 1
    ctrl._shutdownComplete = False
    ctrl._logger = _Logger()

    BeadRecController.run(ctrl)

    assert detectors.released == [handle]
    assert ctrl._scanDetectorHandle is None
    assert ('release-consumer', 'BeadRec') in events


def test_beadrec_boundary_and_consumer_cleanup_are_pinned_detector_scoped():
    events = []

    class _Detector:
        def __init__(self, name):
            self.name = name

        def startChunkConsumer(self, key):
            events.append(('start-consumer', self.name, key))

        def releaseChunkConsumer(self, key):
            events.append(('release-consumer', self.name, key))

    class _Detectors:
        def __init__(self):
            self.detectors = {
                'PINNED': _Detector('PINNED'),
                'UNRELATED': _Detector('UNRELATED'),
            }

        def execOn(self, name, operation):
            return operation(self.detectors[name])

    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._master = SimpleNamespace(detectorsManager=_Detectors())
    ctrl._scanDetectorHandle = 'lease'
    ctrl._scanDetectorName = 'PINNED'
    ctrl._scanDetectorGeneration = 7
    ctrl._scanGeneration = 7

    assert BeadRecController._discardBufferedFrames(ctrl) is True
    BeadRecController._releaseDetectorChunkConsumer(ctrl, 7)

    assert events == [
        ('start-consumer', 'PINNED', 'BeadRec'),
        ('release-consumer', 'PINNED', 'BeadRec'),
    ]


def test_beadrec_cleanup_cannot_release_consumer_during_chunk_read():
    readStarted = threading.Event()
    allowRead = threading.Event()
    consumerReleased = threading.Event()

    class _Detector:
        def readChunk(self, _key):
            readStarted.set()
            assert allowRead.wait(1)
            return [np.zeros((2, 2))]

        def releaseChunkConsumer(self, _key):
            consumerReleased.set()

    class _Detectors:
        detector = _Detector()

        def execOn(self, _name, operation):
            return operation(self.detector)

    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._master = SimpleNamespace(detectorsManager=_Detectors())
    ctrl._beadResourceLock = threading.Lock()
    ctrl._scanDetectorHandle = object()
    ctrl._scanDetectorName = 'CAM'
    ctrl._scanDetectorGeneration = 3
    ctrl._chunkConsumerDetectorName = 'CAM'
    ctrl._chunkConsumerGeneration = 3
    ctrl._toDisplayedFrames = lambda frames: frames

    reader = threading.Thread(
        target=BeadRecController._getCurrentDetectorChunk,
        args=(ctrl,),
    )
    cleanup = threading.Thread(
        target=BeadRecController._releaseDetectorChunkConsumer,
        args=(ctrl, 3),
    )
    reader.start()
    assert readStarted.wait(1)
    cleanup.start()

    assert consumerReleased.wait(0.05) is False
    allowRead.set()
    reader.join(1)
    cleanup.join(1)

    assert not reader.is_alive()
    assert not cleanup.is_alive()
    assert consumerReleased.is_set()


def test_beadrec_scan_start_fails_closed_without_prearm_lease():
    statuses = []

    class _Timer:
        def stop(self):
            pass

    class _Worker:
        def configure(self, _config):
            raise AssertionError('an unarmed worker must not be configured')

    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._widget = SimpleNamespace(
        runButton=SimpleNamespace(isChecked=lambda: True),
        setStatusText=statuses.append,
    )
    ctrl._scanEndDrainTimer = _Timer()
    ctrl._scanDetectorHandle = None
    ctrl._scanDetectorName = None
    ctrl._scanDetectorGeneration = None
    ctrl._scanGeneration = 3
    ctrl._scanArmed = True
    ctrl._shutdownComplete = False
    ctrl._logger = _Logger()
    ctrl.autoAxial = False
    ctrl.beadWorker = _Worker()

    BeadRecController.onNewScan(ctrl)

    assert ctrl._scanArmed is False
    assert any('not pre-armed' in status for status in statuses)


def test_beadrec_rejects_restart_while_previous_worker_is_stopping():
    class _Button:
        def __init__(self):
            self.checked = True

        def isChecked(self):
            return self.checked

        def setChecked(self, checked):
            self.checked = checked

    button = _Button()
    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._widget = SimpleNamespace(
        runButton=button,
        setStatusText=lambda _text: None,
    )
    ctrl._beadWorkerStopping = True
    ctrl._shutdownComplete = False
    ctrl.thread = SimpleNamespace(is_alive=lambda: False)
    ctrl._logger = _Logger()
    ctrl.running = False
    ctrl._scanArmed = False

    BeadRecController.run(ctrl)

    assert button.checked is False
    assert ctrl.running is False


def test_beadrec_stale_generation_cannot_release_current_lease():
    manager = _LeaseManager()
    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._master = SimpleNamespace(detectorsManager=manager)
    ctrl._logger = _Logger()
    ctrl._scanDetectorHandle = 'current'
    ctrl._scanDetectorName = 'CAM'
    ctrl._scanDetectorGeneration = 2

    BeadRecController._releaseScanDetectorLease(ctrl, 1)
    assert manager.released == []
    assert ctrl._scanDetectorHandle == 'current'

    BeadRecController._releaseScanDetectorLease(ctrl, 2)
    assert manager.released == ['current']


def test_beadrec_stale_drain_timer_cannot_finish_a_new_scan():
    ctrl = BeadRecController.__new__(BeadRecController)
    currentTimer = object()
    ctrl._shutdownComplete = False
    ctrl._drainingEndedScan = True
    ctrl._scanEndDrainGeneration = 2
    ctrl._scanEndDrainTimer = currentTimer
    ctrl._scanEndDrainDeadline = 0
    ctrl._hasExpectedScanFrames = lambda: True
    completed = []
    ctrl._completeEndedScan = completed.append

    BeadRecController._finishEndedScanIfReady(ctrl, 1, object())
    assert completed == []

    BeadRecController._finishEndedScanIfReady(ctrl, 2, currentTimer)
    assert completed == [2]


def test_tiling_and_cell_targeting_are_mutually_exclusive():
    ctrl = TilingController.__new__(TilingController)
    ctrl._setupInfo = SimpleNamespace(tiling=object())
    ctrl._scanning = False
    ctrl._cellTargetingRunning = True
    ctrl._closed = False
    ctrl._logger = _Logger()

    TilingController.startTiling(ctrl)

    assert ctrl._scanning is False

    ctrl._scanning = True
    ctrl._cellTargetingRunning = False
    ctrl._detectCellTargets = lambda: (_ for _ in ()).throw(
        AssertionError('segmentation must not race an active tiling run')
    )
    TilingController.runCellTargeting(ctrl, move_only=True)


def test_cell_target_callback_is_cancelled_after_stage_settle():
    ctrl = TilingController.__new__(TilingController)
    QtCore.QObject.__init__(ctrl)
    ctrl._closed = False
    ctrl._cellTargetingRunning = True
    ctrl._cellTargetThread = object()
    ctrl._logger = _Logger()
    cancel = threading.Event()
    ctrl._cellTargetCancel = cancel

    def move_and_cancel(_row, _col):
        cancel.set()
        return (1.0, 2.0)

    ctrl._moveStageToPixel = move_and_cancel
    seen = []

    TilingController._iterateCells(
        ctrl,
        np.array([[1, 2]]),
        {'area': np.array([3])},
        lambda *args: seen.append(args),
    )

    assert seen == []


def test_focus_calibration_is_cancellable_without_worker_widget_access():
    class _Controller:
        def __init__(self):
            self.position = 0.0
            self.positions = []
            self._logger = _Logger()
            self.thread = None

        @property
        def setPointSignal(self):
            return self.position * 2 + 1

        def movePositioner(self, value):
            self.position += value
            self.positions.append(self.position)
            # Cancel from inside the first hardware step. Event.wait() must
            # notice this immediately instead of sleeping for 0.5 seconds.
            self.thread.stop()

        def getPositionerAbs(self):
            return self.position

    controller = _Controller()
    thread = FocusCalibThread(controller)
    controller.thread = thread
    thread.configure(-1.0, 1.0)

    thread.run()

    assert controller.position == 0.0
    assert thread.getData()['signalData'] == []


def test_focus_calibration_completes_without_reading_widget_from_worker():
    class _FastEvent:
        def __init__(self):
            self.flag = False

        def clear(self):
            self.flag = False

        def set(self):
            self.flag = True

        def is_set(self):
            return self.flag

        def wait(self, timeout):
            return self.flag

    class _Controller:
        def __init__(self):
            self.position = 0.0
            self.positions = []
            self._logger = _Logger()

        @property
        def setPointSignal(self):
            return 3 * self.position + 2

        def movePositioner(self, value):
            self.position += value
            self.positions.append(self.position)

        def getPositionerAbs(self):
            return self.position

    controller = _Controller()  # deliberately has no _widget attribute
    thread = FocusCalibThread(controller)
    thread._stopRequested = _FastEvent()
    thread.configure(-1.0, 1.0)

    thread.run()

    data = thread.getData()
    assert len(data['signalData']) == 20
    assert len(data['positionData']) == 20
    assert data['poly'][0] == pytest.approx(3.0)
    assert data['positionData'][0] == pytest.approx(-1.0)
    assert data['positionData'][-1] == pytest.approx(1.0)
    assert controller.position == pytest.approx(0.0)
    assert controller.positions[-1] == pytest.approx(0.0)


def test_focus_lock_pauses_pi_motion_while_calibrating():
    class _Process:
        def takeResult(self):
            return (np.ones((2, 2)), 1.0, 0.0)

    class _Value:
        def setValue(self, _value):
            pass

    class _Image:
        def setImage(self, _image):
            pass

    class _Curve:
        def setData(self, *_args):
            pass

    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._shutdownComplete = False
    ctrl._focusCalibrationActive = True
    ctrl._FocusLockController__processDataThread = _Process()
    ctrl.twoFociVar = False
    ctrl.locked = True
    ctrl.aboutToLock = False
    ctrl.noStepVar = True
    ctrl.currPoint = 0
    ctrl.buffer = 2
    ctrl.setPointData = np.zeros(2)
    ctrl.timeData = np.zeros(2)
    ctrl.startTime = 0
    ctrl._widget = SimpleNamespace(
        center=_Value(),
        camImg=_Image(),
        focusPlotCurve=_Curve(),
    )
    ctrl.updatePI = lambda *_args: (_ for _ in ()).throw(
        AssertionError('PI must be paused during calibration')
    )

    FocusLockController.update(ctrl)
