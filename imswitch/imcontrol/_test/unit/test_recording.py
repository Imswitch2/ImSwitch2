import os
import time
import pytest

import h5py
import numpy as np
import zarr

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, SaveFormat, DetectorInfo
from imswitch.imcontrol.model.managers.RecordingManager import (
    FailureKind, HDF5Storer, HDF5_STREAM_LIBVER, ZarrStorer,
)
from imswitch.imcontrol.model.managers.recording_metadata import MODE_SCAN, MODE_TIMELAPSE
from . import detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare


class _Param:
    def __init__(self, value, valueUnits):
        self.value = value
        self.valueUnits = valueUnits


class _OmeMetaDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [9.9, 0.2, 0.1]
    parameters = {
        'Internal frame interval': _Param(25, 'ms'),
    }


class _OmeMetaDetectors:
    def __getitem__(self, name):
        return _OmeMetaDetector()

    def execOnAll(self, func, *, condition=None):
        return {}


class _OrphanWriter:
    def __init__(self):
        self.alive = True
        self.abortCalls = 0

    def abort(self):
        self.abortCalls += 1
        raise TimeoutError("writer did not stop")

    def is_alive(self):
        return self.alive


def test_build_ome_meta_uses_scan_step_and_parameter_units():
    manager = RecordingManager(_OmeMetaDetectors())

    scan_meta = manager.buildOmeMeta(
        'Cam', MODE_SCAN, 5, scanDims=(32, 32, 5), scanStepSizes=(0.1, 0.2, 0.75))
    assert scan_meta.axes_string == 'ZYX'
    assert scan_meta.scale == [0.75, 0.2, 0.1]

    time_meta = manager.buildOmeMeta('Cam', MODE_TIMELAPSE, 4)
    assert time_meta.axes_string == 'TYX'
    assert time_meta.scale[0] == 0.025


def test_recording_manager_retains_timed_out_writer_until_it_exits():
    manager = RecordingManager(_OmeMetaDetectors())
    worker = manager._RecordingManager__recordingWorker
    writer = _OrphanWriter()
    worker._writerThread = writer
    manager._RecordingManager__record = True

    with pytest.raises(TimeoutError, match="writer did not stop"):
        manager.abortRecording(emitSignal=False, wait=True)

    assert writer.abortCalls == 1
    assert worker._writerThread is writer
    assert manager.shutdownComplete() is False
    with pytest.raises(RuntimeError, match="still shutting down"):
        manager.startRecording(
            detectorNames=["CAM"],
            recMode=RecMode.SpecFrames,
            savename="blocked",
            saveMode=SaveMode.RAM,
            attrs={"CAM": {}},
            recFrames=1,
        )

    writer.alive = False
    assert manager.shutdownComplete() is True
    assert worker._writerThread is None


def record(qtbot, detectorInfos, *args, **kwargs):
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    filePerDetector, savedToDiskPerDetector = {}, {}

    def memoryRecordingAvailable(_, file, __, savedToDisk, detectorName):
        nonlocal filePerDetector, savedToDiskPerDetector
        filePerDetector[detectorName], savedToDiskPerDetector[detectorName] = file, savedToDisk
        return True

    recordingManager.startRecording(*args, **kwargs)
    with qtbot.waitSignals(
            [recordingManager.sigMemoryRecordingAvailable for _ in detectorInfos],
            check_params_cbs=[(lambda *args, detectorName=detectorName, **kwargs:
                               memoryRecordingAvailable(*args, detectorName=detectorName, **kwargs))
                              for detectorName in detectorInfos],
            timeout=30000
    ):
        pass

    return filePerDetector, savedToDiskPerDetector


@pytest.mark.parametrize('detectorInfos,numFrames',
                         [(detectorInfosBasic, 10), (detectorInfosNonSquare, 53)])
def test_recording_spec_frames(qtbot, detectorInfos, numFrames):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecFrames,
        savename='test_spec_frames',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recFrames=numFrames
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, f"Detector group '{detectorName}' not found"
        dataset = detector_group.get('data')
        assert dataset is not None, f"Data dataset not found in group '{detectorName}'"
        assert dataset.shape[0] == numFrames
        assert dataset.attrs['recording:detector_name'] == detectorName
        assert dataset.attrs['recording:source_format'] == 'HDF5'
        assert dataset.attrs['recording:expected_frames'] == numFrames
        assert dataset.attrs['recording:frames_per_stack'] == numFrames
        assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
        # Non-lapse recordings should have default lapse metadata
        assert dataset.attrs['recording:num_timepoints'] == 1
        assert dataset.attrs['recording:lapse_index'] == 0
        assert dataset.attrs['recording:single_lapse_file'] == False
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


def test_camera_lapse_point_records_one_fresh_frame_with_cadence_metadata(
    qtbot,
):
    detectorInfos = detectorInfosBasic
    files, _ = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.CameraLapse,
        savename='test_camera_lapse_point',
        saveMode=SaveMode.RAM,
        attrs={name: {} for name in detectorInfos},
        recFrames=1,
        recLapseTotal=10,
        recLapseIndex=3,
        recLapseIntervalS=3600.0,
        recLapseScheduledTime='2026-07-27T12:00:00+00:00',
    )

    for detectorName, file in files.items():
        with h5py.File(file) as h5file:
            dataset = h5file[f'{detectorName}/data']
            assert dataset.shape[0] == 1
            assert dataset.attrs['recording:num_timepoints'] == 10
            assert dataset.attrs['recording:lapse_index'] == 3
            assert dataset.attrs['recording:lapse_interval_s'] == 3600.0
            assert (
                dataset.attrs['recording:planned_start_time']
                == '2026-07-27T12:00:00+00:00'
            )
        file.close()


@pytest.mark.parametrize(
    'saveFormat,extension',
    [
        (SaveFormat.HDF5, 'hdf5'),
        (SaveFormat.ZARR, 'zarr'),
    ],
)
def test_camera_lapse_single_file_appends_and_releases_between_points(
    qtbot,
    tmp_path,
    saveFormat,
    extension,
):
    detectorInfos = detectorInfosBasic
    detectorName = next(iter(detectorInfos))
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    savename = str(tmp_path / 'camera_lapse')

    for index in range(2):
        with qtbot.waitSignal(
            recordingManager.sigRecordingEndedDetailed,
            timeout=30000,
        ):
            recordingManager.startRecording(
                detectorNames=[detectorName],
                recMode=RecMode.CameraLapse,
                savename=savename,
                saveMode=SaveMode.Disk,
                saveFormat=saveFormat,
                attrs={detectorName: {}},
                singleLapseFile=True,
                recFrames=1,
                recLapseTotal=2,
                recLapseIndex=index,
                recLapseIntervalS=3600,
            )

        # The detailed terminal is the writer barrier. The worker's outer
        # finally releases the short RECORDING lease immediately afterwards;
        # prove the cadence gap has no recording ownership.
        qtbot.waitUntil(
            lambda: (
                recordingManager.shutdownComplete()
                and not any(
                    handle.purpose.name == 'RECORDING'
                    for handle in detectorsManager.activeAcquisitionLeases()
                )
            ),
            timeout=5000,
        )

    path = tmp_path / f'camera_lapse_{detectorName}.{extension}'
    if saveFormat == SaveFormat.HDF5:
        container = h5py.File(path, 'r')
    else:
        container = zarr.open_group(str(path), mode='r')
    try:
        assert container[f'scan0/{detectorName}/data'].shape[0] == 1
        assert container[f'scan1/{detectorName}/data'].shape[0] == 1
        assert (
            container[
                f'scan1/{detectorName}/data'
            ].attrs['recording:lapse_index']
            == 1
        )
    finally:
        close = getattr(container, 'close', None)
        if callable(close):
            close()


@pytest.mark.parametrize('detectorInfos',
                         [detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare])
def test_recording_spec_time(qtbot, detectorInfos):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecTime,
        savename='test_spec_time',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recTime=5
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, f"Detector group '{detectorName}' not found"
        dataset = detector_group.get('data')
        assert dataset is not None, f"Data dataset not found in group '{detectorName}'"
        assert dataset.shape[0] > 0
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


@pytest.mark.parametrize('recMode,kwargs',
                         [(RecMode.SpecFrames, {'recFrames': 10}),
                          (RecMode.SpecTime, {'recTime': 2})])
def test_recording_emits_recording_ended(qtbot, recMode, kwargs):
    """Non-scan recordings must emit sigRecordingEnded when they self-terminate.

    Regression: SpecFrames suppressed sigRecordingEnded (it was lumped in with
    the scan-driven modes, which finish via sigScanDone instead). With no scan
    to drive RecordingController.recordingCycleEnded(), the REC button stayed
    stuck checked after a frame-count recording. SpecTime always emitted it;
    both non-scan modes must now behave the same.
    """
    detectorInfos = detectorInfosBasic
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    with qtbot.waitSignal(recordingManager.sigRecordingEnded, timeout=30000):
        recordingManager.startRecording(
            detectorNames=list(detectorInfos.keys()),
            recMode=recMode,
            savename='test_recording_ended',
            saveMode=SaveMode.RAM,
            attrs={detectorName: {} for detectorName in detectorInfos.keys()},
            **kwargs,
        )

    qtbot.wait(200)  # let the worker thread fully wind down
    assert not recordingManager.record, "Recording should have stopped on its own"


def test_queued_snap_is_inert_after_recording_controller_close():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    calls = []
    ctrl = types.SimpleNamespace(
        _shutdownRequested=True,
        updateRecAttrs=lambda **_kwargs: calls.append("attrs"),
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(
                snap=lambda *_args, **_kwargs: calls.append("snap")
            )
        ),
    )

    assert RecordingController.snap(ctrl) is False
    assert calls == []


def test_next_lapse_defers_start_while_previous_recording_active(monkeypatch):
    """A ScanLapse timepoint must not startRecording while the previous one is
    still finalizing.

    Regression: ScanLapse drives its cycle off sigScanDone and suppresses
    sigRecordingEnded, so sigScanDone can advance the lapse before the recording
    worker clears the record flag. With Freq=0 the lapse timer fires
    immediately, startRecording hits its "a recording is already active" guard,
    and the raised error escaped the timer callback and wedged the widget.
    nextLapse must instead re-arm a short retry until the recording drains.
    """
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
        _LAPSE_RECORDING_DRAIN_RETRY_MS,
    )

    class _FakeSignal:
        def __init__(self):
            self.cb = None

        def connect(self, cb):
            self.cb = cb

    class _FakeTimer:
        created = []

        def __init__(self, singleShot=False):
            self.singleShot = singleShot
            self.timeout = _FakeSignal()
            self.started_ms = None
            _FakeTimer.created.append(self)

        def start(self, ms):
            self.started_ms = ms

        def stop(self):
            pass

    monkeypatch.setattr(rc_mod, "Timer", _FakeTimer)

    class _FakeRecMgr:
        def __init__(self):
            self._record = True
            self.start_calls = 0

        @property
        def record(self):
            return self._record

        def startRecording(self, **kwargs):
            self.start_calls += 1

    # RecordingController is a QObject subclass, so it can't be built with
    # object.__new__; nextLapse's deferral path only reads a few attributes, so
    # drive it via a duck-typed self with the method bound onto it (it re-arms
    # the retry timer onto self.nextLapse).
    recMgr = _FakeRecMgr()
    ctrl = types.SimpleNamespace(
        stopRequested=False,
        timer=None,
        _widget=type("W", (), {"isRecButtonChecked": lambda self: True})(),
        _master=type("M", (), {"recordingManager": recMgr})(),
    )
    ctrl.nextLapse = types.MethodType(RecordingController.nextLapse, ctrl)

    ctrl.nextLapse()

    # Deferred: no recording started, a retry timer re-armed onto nextLapse.
    assert recMgr.start_calls == 0
    assert len(_FakeTimer.created) == 1
    timer = _FakeTimer.created[0]
    assert timer.started_ms == _LAPSE_RECORDING_DRAIN_RETRY_MS
    assert timer.timeout.cb == ctrl.nextLapse


def test_camera_lapse_uses_deadlines_and_starts_fresh_one_frame_session(
    monkeypatch,
):
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class _Signal:
        def __init__(self):
            self.cb = None

        def connect(self, cb):
            self.cb = cb

    class _Timer:
        created = []

        def __init__(self, singleShot=False):
            self.singleShot = singleShot
            self.timeout = _Signal()
            self.started_ms = None
            self.stopped = False
            _Timer.created.append(self)

        def start(self, interval):
            self.started_ms = interval

        def stop(self):
            self.stopped = True

        def isActive(self):
            return not self.stopped

    clock = {'now': 5000.0}
    monkeypatch.setattr(rc_mod, 'Timer', _Timer)
    monkeypatch.setattr(
        rc_mod.time, 'monotonic', lambda: clock['now']
    )

    class _Manager:
        def __init__(self):
            self.record = False
            self.recordingGeneration = 7
            self.calls = []

        def startRecording(self, **kwargs):
            assert self.record is False
            self.recordingGeneration += 1
            self.record = True
            self.calls.append(dict(kwargs))
            return self.recordingGeneration

    manager = _Manager()
    widget = types.SimpleNamespace(
        isRecButtonChecked=lambda: True,
        updateCameraLapseNum=lambda _value: None,
    )
    ctrl = types.SimpleNamespace(
        recMode=RecMode.CameraLapse,
        recording=True,
        doneScan=True,
        endedRecording=True,
        stopRequested=False,
        lapseCurrent=0,
        lapseTotal=3,
        timer=None,
        _shutdownRequested=False,
        _recordingCycleTerminalHandled=False,
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _recordingOperationActive=True,
        _recordingManagerGeneration=7,
        _recordingGenerationBeforeOperation=6,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _scanLifecycleEndedObserved=False,
        _cameraLapseIntervalS=3600.0,
        # The original target was missed by more than one interval. The next
        # capture must be one interval from now, not an immediate catch-up.
        _cameraLapseNextDeadline=100.0,
        _cameraLapsePlannedStart=None,
        savename='/tmp/camera_lapse',
        recordingArgs={
            'detectorNames': ['cam'],
            'recMode': RecMode.CameraLapse,
            'savename': '/tmp/camera_lapse',
            'saveMode': SaveMode.RAM,
            'saveFormat': SaveFormat.HDF5,
            'attrs': {'cam': {}},
            'singleMultiDetectorFile': False,
            'singleLapseFile': False,
            'recFrames': 1,
        },
        _widget=widget,
        _commChannel=types.SimpleNamespace(
            getActiveScanSource=lambda: None,
            sharedAttrs=types.SimpleNamespace(
                getHDF5Attributes=lambda: {'fresh': True}
            ),
        ),
        _master=types.SimpleNamespace(
            recordingManager=manager,
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=None
            ),
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
    )
    for name in (
        'recordingCycleEnded',
        '_scheduleCameraLapseTimer',
        '_cameraLapseTimerFired',
        'nextCameraLapse',
        '_assertCameraLapsePointIsIdle',
        '_startManagerRecording',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    ctrl.recordingCycleEnded()

    assert manager.calls == []
    assert ctrl.lapseCurrent == 1
    assert ctrl._cameraLapseNextDeadline == 8600.0
    timer = _Timer.created[-1]
    assert timer.started_ms == 3_600_000

    clock['now'] = 8600.0
    timer.timeout.cb()

    assert len(manager.calls) == 1
    point = manager.calls[0]
    assert point['recMode'] is RecMode.CameraLapse
    assert point['recFrames'] == 1
    assert point['recLapseTotal'] == 3
    assert point['recLapseIndex'] == 1
    assert point['recLapseIntervalS'] == 3600.0
    assert point['savename'] == '/tmp/camera_lapse_time1'
    assert point['attrs'] == {'cam': {'fresh': True}}
    assert ctrl._recordingManagerGeneration == 8


def test_camera_lapse_stop_in_idle_gap_cancels_without_touching_manager():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class _Timer:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    timer = _Timer()
    events = []
    manager = types.SimpleNamespace(
        record=False,
        abortRecording=lambda **_kwargs: events.append('abort'),
    )
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        _finalizingRecCycle=False,
        recMode=RecMode.CameraLapse,
        recording=True,
        stopRequested=False,
        lapseCurrent=2,
        timer=timer,
        endedRecording=False,
        _recordingCycleTerminalHandled=True,
        _master=types.SimpleNamespace(recordingManager=manager),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recordingCycleEnded=lambda: events.append('cycle-ended'),
    )

    RecordingController.toggleREC(ctrl, False)

    assert timer.stopped is True
    assert ctrl.timer is None
    assert ctrl.stopRequested is True
    assert events == ['cycle-ended']


def test_camera_lapse_waits_for_previous_worker_release(monkeypatch):
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
        _LAPSE_RECORDING_DRAIN_RETRY_MS,
    )

    class _Signal:
        def __init__(self):
            self.cb = None

        def connect(self, cb):
            self.cb = cb

    class _Timer:
        created = []

        def __init__(self, singleShot=False):
            self.timeout = _Signal()
            self.started_ms = None
            _Timer.created.append(self)

        def start(self, interval):
            self.started_ms = interval

        def stop(self):
            pass

    monkeypatch.setattr(rc_mod, 'Timer', _Timer)
    manager = types.SimpleNamespace(
        record=False,
        shutdownComplete=lambda: False,
        startRecording=lambda **_kwargs: pytest.fail(
            'new point started before previous worker released its lease'
        ),
    )
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        stopRequested=False,
        timer=None,
        _widget=types.SimpleNamespace(isRecButtonChecked=lambda: True),
        _master=types.SimpleNamespace(recordingManager=manager),
    )
    ctrl.nextCameraLapse = types.MethodType(
        RecordingController.nextCameraLapse, ctrl
    )

    assert ctrl.nextCameraLapse() is True
    assert len(_Timer.created) == 1
    assert (
        _Timer.created[0].started_ms
        == _LAPSE_RECORDING_DRAIN_RETRY_MS
    )
    assert _Timer.created[0].timeout.cb == ctrl.nextCameraLapse


@pytest.mark.parametrize(
    'detector,single_file,save_format,message',
    [
        (
            type('ScanDetector', (), {
                'isScanDriven': True,
                'parameters': {},
            })(),
            False,
            SaveFormat.HDF5,
            'scan-driven',
        ),
        (
            type('ExternalCamera', (), {
                'isScanDriven': False,
                'parameters': {
                    'Trigger source': type(
                        'P', (), {'value': 'External frame-trigger'}
                    )()
                },
            })(),
            False,
            SaveFormat.HDF5,
            'internal/free-running',
        ),
        (
            type('Camera', (), {
                'isScanDriven': False,
                'parameters': {},
            })(),
            True,
            SaveFormat.TIFF,
            'HDF5 and ZARR',
        ),
    ],
)
def test_camera_lapse_rejects_unsupported_configuration(
    detector, single_file, save_format, message,
):
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    ctrl = types.SimpleNamespace(
        _master=types.SimpleNamespace(
            detectorsManager={'det': detector}
        )
    )
    with pytest.raises(ValueError, match=message):
        RecordingController._validateCameraLapse(
            ctrl,
            ['det'],
            10,
            3600,
            save_format,
            single_file,
        )


def test_scanlapse_stop_in_cadence_gap_accepts_synchronous_run_terminal():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class _Timer:
        def __init__(self):
            self.stopped = False

        def isActive(self):
            return not self.stopped

        def stop(self):
            self.stopped = True

    timer = _Timer()
    events = []
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        _finalizingRecCycle=False,
        recMode=RecMode.ScanLapse,
        lapseCurrent=1,
        recording=True,
        stopRequested=False,
        timer=timer,
        _recordingCycleTerminalHandled=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=None,
        _acceptedScanCompletion=None,
        _recordingScanSource=None,
        _scanStartPublished=True,
        _usesDetailedRecordingSignals=True,
        endedRecording=True,
        doneScan=False,
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(record=False),
            scanExecutionCoordinator=None,
        ),
        _scanRunIsActive=lambda: True,
    )

    def cycleEnded():
        events.append("cycle")
        ctrl.recording = False
        ctrl.lapseCurrent = -1

    def abortOwned():
        events.append("abort")
        # The real owner may synchronously publish its run terminal from the
        # targeted abort call.
        RecordingController._scanLifecycleEnded(ctrl)

    ctrl.recordingCycleEnded = cycleEnded
    ctrl._abortOwnedScanSequence = abortOwned

    RecordingController.toggleREC(ctrl, False)

    assert timer.stopped is True
    assert ctrl.timer is None
    assert events == ["abort", "cycle"]
    assert ctrl.recording is False
    assert ctrl.lapseCurrent == -1
    assert ctrl._scanRequestAccepted is False


def test_new_recording_resets_previous_scan_and_writer_terminals_first():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    observed = []
    failures = []
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        _finalizingRecCycle=False,
        recording=False,
        doneScan=True,
        endedRecording=True,
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(
                recordingGeneration=2,
                record=False,
            )
        ),
        _handleRecordingFailure=(
            lambda message, **kwargs: failures.append((message, kwargs))
        ),
    )

    def stopAfterInitialState(**_kwargs):
        observed.append((ctrl.doneScan, ctrl.endedRecording))
        raise RuntimeError('stop after observing fresh terminal window')

    ctrl.updateRecAttrs = stopAfterInitialState

    RecordingController.toggleREC(ctrl, True)

    assert observed == [(False, False)]
    assert ctrl.doneScan is False
    assert ctrl.endedRecording is False
    assert failures == [
        (
            'stop after observing fresh terminal window',
            {'abortManager': False},
        )
    ]


def test_scanlapse_two_cycle_drain_and_progression(monkeypatch):
    """Two full ScanLapse timepoints through the controller's cycling state
    machine. A timepoint advances only after both scan completion and the
    identity-carrying writer-finalization terminal.

    This is deliberately a controller-level regression: it drives the real
    lapse-cycling contract (lapseCurrent progression, per-cycle savename/
    recLapseIndex, drain-retry, final-cycle teardown) against a fake
    RecordingManager, with no real QThread/WriterThread involved. Prior
    attempts at a full RecordingManager+WriterThread two-cycle regression
    reproduced standalone but reliably aborted under pytest during writer
    thread teardown (see docs/mock_scanning_mocker_improvement_plan.md) - a
    Qt/pytest-harness lifecycle issue, not a cycling-logic bug. That deeper
    issue remains open and undiagnosed; this test locks down the logic that
    previously only had single-cycle coverage.
    """
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
        _LAPSE_RECORDING_DRAIN_RETRY_MS,
    )
    from imswitch.imcontrol.model import RecMode

    class _FakeSignal:
        def __init__(self):
            self.cb = None
            self.emit_calls = 0

        def connect(self, cb):
            self.cb = cb

        def emit(self, *args, **kwargs):
            self.emit_calls += 1

    class _FakeTimer:
        created = []

        def __init__(self, singleShot=False):
            self.singleShot = singleShot
            self.timeout = _FakeSignal()
            self.started_ms = None
            self.stopped = False
            _FakeTimer.created.append(self)

        def start(self, ms):
            self.started_ms = ms

        def stop(self):
            self.stopped = True

        def isActive(self):
            return self.started_ms is not None and not self.stopped

    monkeypatch.setattr(rc_mod, "Timer", _FakeTimer)

    class _FakeRecMgr:
        def __init__(self):
            self._record = False
            self.start_calls = 0
            self.calls = []

        @property
        def record(self):
            return self._record

        def startRecording(self, **kwargs):
            self.start_calls += 1
            self.calls.append(dict(kwargs))
            self._record = True

        def waitForAcquisitionStarted(self, timeout=None):
            return True

    class _FakeScanWorkflow:
        def __init__(self):
            self.run_scan_calls = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, isFirst, keepGoing):
            self.run_scan_calls.append((isFirst, keepGoing))

    class _FakeSharedAttrs:
        def getHDF5Attributes(self):
            return {}

    class _FakeCommChannel:
        def __init__(self):
            self.scanWorkflow = _FakeScanWorkflow()
            self.sharedAttrs = _FakeSharedAttrs()
            self.sigRecordingEnded = _FakeSignal()

        def getNumScanPositions(self):
            return 10

        def getNumCamTTL(self):
            return {'det': 1}

        def getDimsScan(self):
            return (10,)

        def getScanStepSizes(self):
            return (1.0,)

    class _FakeWidget:
        def __init__(self):
            self._checked = True
            self.lapseNumUpdates = []
            self.recButtonSets = []
            self.fieldsEnabledCalls = []

        def isRecButtonChecked(self):
            return self._checked

        def setRecButtonChecked(self, value):
            self._checked = value
            self.recButtonSets.append(value)

        def updateRecLapseNum(self, n):
            self.lapseNumUpdates.append(n)

        def updateRecFrameNum(self, n):
            pass

        def updateRecTime(self, n):
            pass

        def setFieldsEnabled(self, enabled):
            self.fieldsEnabledCalls.append(enabled)

        def getTimelapseFreq(self):
            return 0  # Freq=0: the documented common case, not an edge one.

    recMgr = _FakeRecMgr()
    widget = _FakeWidget()
    commChannel = _FakeCommChannel()

    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanLapse,
        recording=True,
        doneScan=False,
        endedRecording=False,
        stopRequested=False,
        lapseCurrent=0,
        lapseTotal=2,
        timer=None,
        _finalizingRecCycle=False,
        _scanStartPublished=False,
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _recordingFailureHandled=False,
        _recordingFailedCurrent=False,
        _recordingCycleTerminalHandled=False,
        _recordingOperationActive=True,
        _recordingManagerGeneration=None,
        _recordingGenerationBeforeOperation=None,
        _usesDetailedRecordingSignals=True,
        savename='/tmp/rec/test_rec',
        recordingArgs={
            'detectorNames': ['det'],
            'recMode': RecMode.ScanLapse,
            'savename': '/tmp/rec/test_rec',
            'saveMode': None,
            'saveFormat': None,
            'attrs': {'det': {}},
            'singleMultiDetectorFile': False,
            'singleLapseFile': False,
        },
        _widget=widget,
        _commChannel=commChannel,
        _master=type("M", (), {"recordingManager": recMgr})(),
        _RecordingController__logger=types.SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    # Never reached on this happy path; a stub keeps the shell small.
    ctrl._handleRecordingFailure = lambda message, **kwargs: (_ for _ in ()).throw(
        AssertionError(f'unexpected recording failure: {message}')
    )
    for name in (
        'nextLapse', 'recordingCycleEnded', 'scanDone', 'recordingEnded',
        '_scanAccessor', '_applyScanGeometryToRecordingArgs',
        '_scanDimsForRecording', '_scanStepSizesForRecording',
        '_startManagerRecording', '_waitForManagerArm',
        '_notifyScanStarting', '_preflightNewScanRequest',
        '_requestScanStart',
    ):
        setattr(ctrl, name, types.MethodType(getattr(RecordingController, name), ctrl))

    # --- Cycle 0 (first lapse timepoint) ---
    ctrl.nextLapse()
    assert recMgr.start_calls == 1
    assert recMgr.calls[0]['recLapseIndex'] == 0
    assert recMgr.calls[0]['recLapseTotal'] == 2
    assert recMgr.calls[0]['savename'] == '/tmp/rec/test_rec_scan0'
    assert commChannel.scanWorkflow.run_scan_calls[-1] == (True, True)

    # Scan hardware reports done while the worker is still draining/writing.
    # The controller must not advance or claim success yet.
    ctrl.scanDone()
    assert ctrl.lapseCurrent == 0
    assert _FakeTimer.created == []

    # Writer finalization is the second terminal barrier.
    recMgr._record = False
    ctrl.recordingEnded()
    assert ctrl.lapseCurrent == 1
    assert widget.lapseNumUpdates == [1]
    # One lapse point is not the end of the recording: nothing public yet.
    assert commChannel.sigRecordingEnded.emit_calls == 0
    assert len(_FakeTimer.created) == 1
    cadenceTimer = _FakeTimer.created[-1]
    assert cadenceTimer.started_ms == 0  # getTimelapseFreq() * 1000

    # Timer fires only after the worker-drained terminal, so cycle 1 may start.
    cadenceTimer.timeout.cb()
    assert recMgr.start_calls == 2
    assert recMgr.calls[1]['recLapseIndex'] == 1
    assert recMgr.calls[1]['recLapseTotal'] == 2
    assert recMgr.calls[1]['savename'] == '/tmp/rec/test_rec_scan1'
    assert commChannel.scanWorkflow.run_scan_calls[-1] == (False, False)

    # --- Cycle 1 (final lapse timepoint) ---
    ctrl.scanDone()
    assert ctrl.recording is True
    recMgr._record = False
    ctrl.recordingEnded()

    # Final cycle: no further lapse scheduled, controller resets to idle.
    assert ctrl.recording is False
    assert ctrl.lapseCurrent == -1
    assert ctrl.timer is None
    assert widget.recButtonSets[-1] is False
    assert widget.fieldsEnabledCalls[-1] is True
    assert recMgr.start_calls == 2  # no third cycle
    # Natural completion publishes recordingEnded exactly once. The worker
    # holds the legacy signal back in the scan modes, so this is the only
    # place a script (or the joystick re-enable) can learn the recording --
    # every lapse point, every file -- is finished. It used to be soft-stop
    # only, and a script waiting for it after a completed lapse hung.
    assert commChannel.sigRecordingEnded.emit_calls == 1


def test_recording_arm_failure_aborts_without_starting_scan_and_pairs_lifecycle():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    events = []

    class _Logger:
        def error(self, *_args, **_kwargs):
            pass

    workflow = types.SimpleNamespace(
        abort_scan=lambda: events.append('abort-scan'),
        notify_scan_ended=lambda: events.append('scan-ended'),
    )
    manager = types.SimpleNamespace(
        abortRecording=lambda **kwargs: events.append(
            ('abort-recording', kwargs)
        )
    )
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanOnce,
        _recordingFailureHandled=False,
        _recordingFailedCurrent=False,
        _scanStartPublished=True,
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        stopRequested=False,
        recording=True,
        endedRecording=False,
        doneScan=False,
        _commChannel=types.SimpleNamespace(scanWorkflow=workflow),
        _master=types.SimpleNamespace(recordingManager=manager),
        _RecordingController__logger=_Logger(),
        recordingCycleEnded=lambda: events.append('cycle-ended'),
    )
    ctrl._abortOwnedScanSequence = types.MethodType(
        RecordingController._abortOwnedScanSequence, ctrl
    )
    ctrl._notifyScanEndedIfPending = types.MethodType(
        RecordingController._notifyScanEndedIfPending, ctrl
    )
    ctrl._scanRunIsActive = types.MethodType(
        RecordingController._scanRunIsActive, ctrl
    )

    RecordingController._handleRecordingFailure(
        ctrl, 'camera did not arm', abortManager=True
    )

    assert events == [
        'scan-ended',
        (
            'abort-recording',
            {'emitSignal': False, 'wait': True},
        ),
        'cycle-ended',
    ]
    assert ctrl.recording is False
    assert ctrl._scanStartPublished is False


def test_recording_failure_waits_for_active_scan_owner_to_publish_end():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    events = []
    runToken = object()
    coordinator = types.SimpleNamespace(activeRunToken=runToken)
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanOnce,
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=None,
        _recordingScanSource=None,
        _scanLifecycleEndedObserved=False,
        _usesDetailedRecordingSignals=True,
        stopRequested=False,
        recording=True,
        endedRecording=False,
        doneScan=False,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan=lambda: events.append('abort-scan'),
                notify_scan_ended=lambda: events.append('scan-ended'),
            )
        ),
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(),
            scanExecutionCoordinator=coordinator,
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recordingCycleEnded=lambda: events.append('cycle-ended'),
    )
    for name in (
        '_abortOwnedScanSequence',
        '_notifyScanEndedIfPending',
        '_scanRunIsActive',
        '_scanLifecycleEnded',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    RecordingController._handleRecordingFailure(
        ctrl, 'writer failed during scan', abortManager=False
    )

    assert events == ['abort-scan']
    assert ctrl._scanStartPublished is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert ctrl._recordingFailureAwaitingScanEnd is True

    coordinator.activeRunToken = None
    ctrl._scanLifecycleEnded()

    assert events == ['abort-scan', 'cycle-ended']
    assert ctrl._scanStartPublished is False
    assert ctrl._scanRequestAccepted is False
    assert ctrl._acceptedScanRunToken is None


def test_late_recording_failure_does_not_abort_unowned_scan():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    events = []
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanOnce,
        _recordingFailureHandled=False,
        _recordingFailedCurrent=False,
        _scanStartPublished=False,
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        stopRequested=False,
        recording=False,
        endedRecording=True,
        doneScan=True,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan=lambda: events.append('abort-scan')
            )
        ),
        _master=types.SimpleNamespace(recordingManager=types.SimpleNamespace()),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recordingCycleEnded=lambda: events.append('cycle-ended'),
    )

    RecordingController._handleRecordingFailure(
        ctrl, 'late writer failure', abortManager=False
    )

    assert events == ['cycle-ended']


def test_rejected_scan_request_aborts_recorder_without_broadcasting_scan_abort():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    failures = []
    result = types.SimpleNamespace(
        handled=True,
        accepted=False,
        rejectionMessage='scan owner busy',
    )
    ctrl = types.SimpleNamespace(
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: result
            )
        ),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    accepted = RecordingController._requestScanStart(
        ctrl, True, False
    )

    assert accepted is False
    assert failures == [
        ('scan owner busy', {'abortManager': True})
    ]


def test_accepted_scan_request_pins_exact_run_identity():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    runToken = object()
    owner = object()
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, runToken),),
        reports=((owner, True, ''),),
    )
    ctrl = types.SimpleNamespace(
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            warning=lambda *_args, **_kwargs: None
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
    )

    accepted = RecordingController._requestScanStart(
        ctrl, True, False
    )

    assert accepted is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken


def _exact_recording_request_controller():
    import types

    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model import RecMode

    class Owner:
        supportsExactScanRequestCompletion = True

    owner = Owner()
    runToken = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(runToken)
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, runToken),),
        acceptedCompletions=((owner, runToken, completion),),
        reports=((owner, True, ''),),
    )
    queued = []
    cycles = []
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=None,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            warning=lambda *_args, **_kwargs: None
        ),
        _sigExactScanRequestCompleted=types.SimpleNamespace(
            emit=lambda resolved: queued.append(resolved)
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _usesDetailedRecordingSignals=True,
        _shutdownRequested=False,
        stopRequested=False,
        recMode=RecMode.ScanOnce,
        doneScan=False,
        endedRecording=False,
        recordingCycleEnded=lambda: cycles.append(True),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )
    return ctrl, owner, runToken, completion, queued, cycles, failures


@pytest.mark.parametrize("writer_first", [False, True])
def test_exact_scan_terminal_and_writer_terminal_advance_once(
        writer_first):
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    (
        ctrl,
        _owner,
        runToken,
        completion,
        queued,
        cycles,
        failures,
    ) = _exact_recording_request_controller()

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is True
    assert ctrl._acceptedScanCompletion is completion

    # A stale/global compatibility signal cannot complete an exact request.
    RecordingController.scanDone(ctrl)
    assert ctrl.doneScan is False
    assert cycles == []

    if writer_first:
        RecordingController.recordingEnded(ctrl)
        assert cycles == []

    assert completion.resolve(runToken, True) is True
    assert queued == [completion]
    RecordingController._exactScanRequestCompleted(ctrl, queued.pop())

    if not writer_first:
        assert cycles == []
        RecordingController.recordingEnded(ctrl)

    assert cycles == [True]
    assert failures == []
    RecordingController._exactScanRequestCompleted(ctrl, completion)
    assert cycles == [True]


def test_exact_scan_failure_uses_failure_terminal_not_global_success():
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    (
        ctrl,
        _owner,
        runToken,
        completion,
        queued,
        cycles,
        failures,
    ) = _exact_recording_request_controller()

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is True
    RecordingController.scanDone(ctrl)
    assert completion.resolve(
        runToken, False, "detector final-frame barrier failed"
    ) is True
    RecordingController._exactScanRequestCompleted(ctrl, queued.pop())

    assert ctrl.doneScan is False
    assert cycles == []
    assert failures == [
        (
            "detector final-frame barrier failed",
            {"abortManager": True},
        )
    ]


def test_failure_completion_cannot_advance_before_exact_run_end():
    import types

    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    owner = object()
    runToken = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(runToken)
    completion.resolve(runToken, False, 'scan failed')
    coordinator = types.SimpleNamespace(activeRunToken=runToken)
    cycles = []
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=completion,
        _acceptedScanRunToken=runToken,
        _scanRequestAccepted=True,
        _scanStartPublished=True,
        _recordingScanSource=None,
        _exactScanCompletionHandled=False,
        _recordingFailureAwaitingScanEnd=True,
        _scanLifecycleEndedObserved=False,
        _shutdownRequested=False,
        _usesDetailedRecordingSignals=True,
        stopRequested=True,
        endedRecording=True,
        doneScan=False,
        recMode=RecMode.ScanOnce,
        timer=None,
        _recordingFailureHandled=True,
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=coordinator
        ),
        recordingCycleEnded=lambda: cycles.append(True),
    )

    RecordingController._exactScanRequestCompleted(ctrl, completion)

    assert ctrl.doneScan is True
    assert cycles == []
    assert ctrl._acceptedScanRunToken is runToken

    coordinator.activeRunToken = None
    RecordingController._scanLifecycleEnded(ctrl)

    assert cycles == [True]
    assert ctrl._scanLifecycleEndedObserved is True


def test_exact_capable_scan_owner_must_report_one_matching_completion():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Owner:
        supportsExactScanRequestCompletion = True

    owner = Owner()
    runToken = object()
    failures = []
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, runToken),),
        acceptedCompletions=(),
        reports=((owner, True, ''),),
    )
    ctrl = types.SimpleNamespace(
        _recordingScanSource=None,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            )
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    # The hardware may already be armed, so failure handling retains exact
    # abort authority even though the extension contract was broken.
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert failures == [
        (
            'Scan controller acknowledged the recording request without one '
            'matching exact completion terminal.',
            {'abortManager': True},
        )
    ]


def test_malformed_reported_token_retains_real_targeted_abort_authority():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Owner:
        supportsExactScanRequestCompletion = True
        isRunning = True

    owner = Owner()
    realToken = object()
    reportedToken = object()
    failures = []
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, reportedToken),),
        acceptedCompletions=(),
        reports=((owner, True, ''),),
    )
    coordinator = types.SimpleNamespace(
        runForOwner=lambda candidate: (
            realToken if candidate is owner else None
        ),
        activeRunToken=realToken,
    )
    ctrl = types.SimpleNamespace(
        _recordingScanSource=owner,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=coordinator
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is realToken
    assert failures == [
        (
            'Scan controller reported a run identity that does not match '
            'its active reservation.',
            {'abortManager': True},
        )
    ]


def test_broken_acceptance_envelope_fails_recording_with_abort_authority():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Owner:
        isRunning = True

    owner = Owner()
    runToken = object()

    class BrokenResult:
        @property
        def handled(self):
            raise RuntimeError('broken handled property')

        acceptedTokens = ((owner, runToken),)

    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=owner,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: BrokenResult()
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                runForOwner=lambda candidate: (
                    runToken if candidate is owner else None
                )
            )
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert failures == [
        (
            'Scan controller returned a malformed acceptance envelope: '
            'broken handled property',
            {'abortManager': True},
        )
    ]


def test_broken_completion_descriptor_fails_recording_closed():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Owner:
        supportsExactScanRequestCompletion = True
        isRunning = True

    owner = Owner()
    runToken = object()

    class BrokenCompletion:
        @property
        def owner(self):
            raise RuntimeError('broken completion owner')

    completion = BrokenCompletion()
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, runToken),),
        acceptedCompletions=((owner, runToken, completion),),
        reports=((owner, True, ''),),
    )
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=owner,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                runForOwner=lambda candidate: (
                    runToken if candidate is owner else None
                )
            )
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert failures == [
        (
            'Could not inspect exact scan completion terminal: '
            'broken completion owner',
            {'abortManager': True},
        )
    ]


def test_exceptional_exact_completion_wait_fails_recording_closed():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class BrokenCompletion:
        def wait(self, timeout=None):
            raise RuntimeError('broken completion wait')

    completion = BrokenCompletion()
    failures = []
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=completion,
        _exactScanCompletionHandled=False,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    RecordingController._exactScanRequestCompleted(ctrl, completion)

    assert ctrl._exactScanCompletionHandled is True
    assert failures == [
        (
            'Could not consume exact scan completion: broken completion wait',
            {'abortManager': True},
        )
    ]


def test_raising_rejection_message_still_fails_recording_closed():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class BrokenResult:
        handled = True
        accepted = False

        @property
        def rejectionMessage(self):
            raise RuntimeError('broken rejection message')

    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=None,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: BrokenResult()
            )
        ),
        _master=types.SimpleNamespace(),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert failures == [
        (
            'Scan controller returned a malformed rejection report: '
            'broken rejection message',
            {'abortManager': True},
        )
    ]


def test_raising_targeted_source_state_retains_abort_authority():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class BrokenSource:
        @property
        def isRunning(self):
            raise RuntimeError('broken running state')

    source = BrokenSource()
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=source,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: types.SimpleNamespace(
                    handled=False,
                    accepted=False,
                )
            )
        ),
        _master=types.SimpleNamespace(),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert failures == [
        (
            'Could not inspect whether the selected scan source started: '
            'broken running state',
            {'abortManager': True},
        )
    ]


def test_exact_completion_callback_is_bound_to_validated_terminal():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Owner:
        supportsExactScanRequestCompletion = True

    owner = Owner()
    runToken = object()

    class Completion:
        successful = True
        message = ''

        def __init__(self):
            self.owner = owner
            self.runToken = runToken

        def wait(self, timeout=None):
            return True

        def add_done_callback(self, callback):
            # A malformed adapter supplies a foreign payload. Recording must
            # still inspect only the already validated accepted terminal.
            callback(object())

    completion = Completion()
    result = types.SimpleNamespace(
        handled=True,
        accepted=True,
        acceptedTokens=((owner, runToken),),
        acceptedCompletions=((owner, runToken, completion),),
        reports=((owner, True, ''),),
    )
    queued = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=None,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: result
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            )
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _sigExactScanRequestCompleted=types.SimpleNamespace(
            emit=lambda terminal: queued.append(terminal)
        ),
        _handleRecordingFailure=lambda *_args, **_kwargs: None,
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is True
    assert queued == [completion]


def test_failure_cleanup_aborts_writer_when_source_state_getter_raises():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class BrokenSource:
        @property
        def isRunning(self):
            raise RuntimeError('running state unavailable')

    source = BrokenSource()
    runToken = object()
    events = []
    manager = types.SimpleNamespace(
        abortRecording=lambda **kwargs: events.append(
            ('abort-writer', kwargs)
        )
    )
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanOnce,
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _recordingOperationActive=False,
        _recordingManagerGeneration=None,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _recordingScanSource=source,
        _scanLifecycleEndedObserved=False,
        _shutdownRequested=False,
        stopRequested=False,
        endedRecording=False,
        doneScan=False,
        recording=True,
        _master=types.SimpleNamespace(recordingManager=manager),
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan_from=lambda target, token: events.append(
                    ('abort-scan', target, token)
                )
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recordingCycleEnded=lambda: events.append(('cycle-ended',)),
    )
    for name in (
        '_scanRunIsActive',
        '_abortOwnedScanSequence',
        '_notifyScanEndedIfPending',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    RecordingController._handleRecordingFailure(
        ctrl, 'terminal inspection failed', abortManager=True
    )

    assert ('abort-scan', source, runToken) in events
    assert (
        'abort-writer',
        {'emitSignal': False, 'wait': True},
    ) in events
    assert ctrl._recordingFailureAwaitingScanEnd is True


def test_nonfinal_exact_scanlapse_advances_with_run_lifecycle_open():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class Completion:
        successful = True
        message = ''

        def wait(self, timeout=None):
            return True

    completion = Completion()
    cycles = []
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=completion,
        _exactScanCompletionHandled=False,
        _scanStartPublished=True,
        _recordingFailureAwaitingScanEnd=False,
        _scanLifecycleEndedObserved=False,
        _shutdownRequested=False,
        _usesDetailedRecordingSignals=True,
        stopRequested=False,
        recMode=RecMode.ScanLapse,
        lapseCurrent=0,
        lapseTotal=2,
        doneScan=False,
        endedRecording=True,
        recordingCycleEnded=lambda: cycles.append(True),
    )

    RecordingController._exactScanRequestCompleted(ctrl, completion)

    assert ctrl.doneScan is True
    assert ctrl._exactScanCompletionHandled is True
    assert ctrl._scanStartPublished is True
    assert cycles == [True]


def test_final_exact_success_waits_for_paired_scan_lifecycle_end():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    runToken = object()

    class Completion:
        successful = True
        message = ''

        def wait(self, timeout=None):
            return True

    completion = Completion()
    cycles = []
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=completion,
        _acceptedScanRunToken=runToken,
        _exactScanCompletionHandled=False,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _scanLifecycleEndedObserved=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailureHandled=False,
        _recordingScanSource=None,
        _shutdownRequested=False,
        _usesDetailedRecordingSignals=True,
        stopRequested=False,
        recMode=RecMode.ScanLapse,
        lapseCurrent=1,
        lapseTotal=2,
        doneScan=False,
        endedRecording=True,
        timer=None,
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=None
            )
        ),
        recordingCycleEnded=lambda: cycles.append(True),
    )

    RecordingController._exactScanRequestCompleted(ctrl, completion)

    assert ctrl.doneScan is True
    assert ctrl._exactScanCompletionHandled is True
    assert cycles == []

    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanStartPublished is False
    assert ctrl._scanLifecycleEndedObserved is True
    assert cycles == [True]


def test_writer_terminal_cannot_use_stale_done_while_exact_is_pending():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    cycles = []
    ctrl = types.SimpleNamespace(
        _acceptedScanCompletion=object(),
        _exactScanCompletionHandled=False,
        _scanStartPublished=True,
        _recordingFailureAwaitingScanEnd=False,
        _scanLifecycleEndedObserved=False,
        doneScan=True,
        endedRecording=False,
        recMode=RecMode.ScanOnce,
        recordingCycleEnded=lambda: cycles.append(True),
    )

    RecordingController.recordingEnded(ctrl)

    assert ctrl.endedRecording is True
    assert cycles == []


def test_recording_cycle_cleanup_survives_deleted_widget():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class DeletedWidget:
        def isRecButtonChecked(self):
            raise RuntimeError('widget was deleted')

    ctrl = types.SimpleNamespace(
        _recordingCycleTerminalHandled=False,
        _shutdownRequested=False,
        _widget=DeletedWidget(),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recMode=RecMode.ScanOnce,
        stopRequested=False,
        recording=True,
        lapseCurrent=0,
        lapseTotal=1,
        timer=None,
        _finalizingRecCycle=False,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=object(),
        _acceptedScanCompletion=object(),
        _exactScanCompletionHandled=True,
        _scanLifecycleEndedObserved=True,
        _recordingScanSource=object(),
        _recordingOperationActive=True,
        _recordingManagerGeneration=1,
        _recordingGenerationBeforeOperation=0,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
    )

    RecordingController.recordingCycleEnded(ctrl)

    assert ctrl.recording is False
    assert ctrl._recordingCycleTerminalHandled is True
    assert ctrl._scanRequestAccepted is False
    assert ctrl._acceptedScanRunToken is None
    assert ctrl._acceptedScanCompletion is None
    assert ctrl._recordingScanSource is None
    assert ctrl._recordingOperationActive is False


@pytest.mark.parametrize('getterRaises', [False, True])
def test_nonfinal_lapse_cadence_cancellation_retains_owner_until_run_end(
        getterRaises):
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class Widget:
        def isRecButtonChecked(self):
            if getterRaises:
                raise RuntimeError('cadence widget unavailable')
            return False

    source = object()
    runToken = object()
    events = []
    manager = types.SimpleNamespace(
        abortRecording=lambda **kwargs: events.append(
            ('abort-writer', kwargs)
        )
    )
    ctrl = types.SimpleNamespace(
        _recordingCycleTerminalHandled=False,
        _shutdownRequested=False,
        _widget=Widget(),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recMode=RecMode.ScanLapse,
        stopRequested=False,
        recording=True,
        lapseCurrent=0,
        lapseTotal=2,
        timer=None,
        endedRecording=True,
        doneScan=True,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=object(),
        _exactScanCompletionHandled=True,
        _scanLifecycleEndedObserved=False,
        _recordingScanSource=source,
        _recordingOperationActive=False,
        _recordingManagerGeneration=None,
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _master=types.SimpleNamespace(
            recordingManager=manager,
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            ),
        ),
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan_from=lambda target, token: events.append(
                    ('abort-scan', target, token)
                )
            )
        ),
    )
    for name in (
        '_handleRecordingFailure',
        '_scanRunIsActive',
        '_abortOwnedScanSequence',
        '_notifyScanEndedIfPending',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    RecordingController.recordingCycleEnded(ctrl)

    assert ('abort-scan', source, runToken) in events
    assert (
        'abort-writer',
        {'emitSignal': False, 'wait': True},
    ) in events
    assert ctrl._recordingFailureAwaitingScanEnd is True
    assert ctrl._scanStartPublished is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert ctrl._recordingScanSource is source


@pytest.mark.parametrize('failurePoint', ['unchecked', 'widget', 'timer'])
def test_next_lapse_entry_failure_aborts_retained_run(
        monkeypatch, failurePoint):
    import types

    import imswitch.imcontrol.controller.controllers.RecordingController as rc_mod
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    class Widget:
        def isRecButtonChecked(self):
            if failurePoint == 'widget':
                raise RuntimeError('cadence widget unavailable')
            return failurePoint != 'unchecked'

    if failurePoint == 'timer':
        class BrokenTimer:
            def __init__(self, singleShot=False):
                self.timeout = types.SimpleNamespace(
                    connect=lambda _callback: None
                )

            def start(self, _delay):
                raise RuntimeError('drain timer unavailable')

            def stop(self):
                pass

        monkeypatch.setattr(rc_mod, 'Timer', BrokenTimer)

    source = object()
    runToken = object()
    events = []
    manager = types.SimpleNamespace(
        record=failurePoint == 'timer',
        abortRecording=lambda **kwargs: events.append(
            ('abort-writer', kwargs)
        ),
    )
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        stopRequested=False,
        _widget=Widget(),
        _recordingCycleTerminalHandled=True,
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _recordingOperationActive=False,
        _recordingManagerGeneration=None,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _scanLifecycleEndedObserved=False,
        _recordingScanSource=source,
        recMode=RecMode.ScanLapse,
        recording=True,
        endedRecording=True,
        doneScan=True,
        lapseCurrent=1,
        lapseTotal=3,
        timer=None,
        _master=types.SimpleNamespace(
            recordingManager=manager,
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=runToken
            ),
        ),
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan_from=lambda target, token: events.append(
                    ('abort-scan', target, token)
                )
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
        recordingCycleEnded=lambda: events.append(('cycle-ended',)),
    )
    for name in (
        '_handleRecordingFailure',
        '_scanRunIsActive',
        '_abortOwnedScanSequence',
        '_notifyScanEndedIfPending',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    assert RecordingController.nextLapse(ctrl) is False

    assert ('abort-scan', source, runToken) in events
    assert (
        'abort-writer',
        {'emitSignal': False, 'wait': True},
    ) in events
    assert ctrl._recordingFailureAwaitingScanEnd is True
    assert ctrl._scanStartPublished is True
    assert ctrl._acceptedScanRunToken is runToken
    assert ctrl._recordingScanSource is source


def test_failed_scan_end_notification_remains_pending_for_retry():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    attempts = []

    def notify():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError('temporary signal failure')

    ctrl = types.SimpleNamespace(
        _scanStartPublished=True,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                notify_scan_ended=notify
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
    )

    RecordingController._notifyScanEndedIfPending(ctrl)
    assert ctrl._scanStartPublished is True

    RecordingController._notifyScanEndedIfPending(ctrl)
    assert ctrl._scanStartPublished is False
    assert len(attempts) == 2


def test_unpaired_scan_start_blocks_recording_shutdown():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    runToken = object()
    source = types.SimpleNamespace(isRunning=False)
    ctrl = types.SimpleNamespace(
        _shutdownRequested=True,
        timer=None,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=object(),
        _exactScanCompletionHandled=True,
        _recordingScanSource=source,
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(
                record=False,
                shutdownComplete=lambda: True,
            )
        ),
    )
    ctrl._scanRunIsActive = (
        lambda: RecordingController._scanRunIsActive(ctrl)
    )

    assert RecordingController.shutdownComplete(ctrl) is False

    ctrl._scanStartPublished = False
    assert RecordingController.shutdownComplete(ctrl) is True


def test_pending_no_coordinator_exact_terminal_blocks_recording_shutdown():
    import types

    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    source = types.SimpleNamespace(isRunning=False)
    runToken = object()
    completion = ScanRequestCompletion(source)
    completion.bind(runToken)
    recordingManager = types.SimpleNamespace(
        record=False,
        shutdownComplete=lambda: True,
    )
    ctrl = types.SimpleNamespace(
        _master=types.SimpleNamespace(
            recordingManager=recordingManager
        ),
        _recordingScanSource=source,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=runToken,
        _acceptedScanCompletion=completion,
        _exactScanCompletionHandled=False,
        _shutdownRequested=True,
        timer=None,
    )
    ctrl._scanRunIsActive = (
        lambda: RecordingController._scanRunIsActive(ctrl)
    )

    assert RecordingController._scanRunIsActive(ctrl) is True
    assert RecordingController.shutdownComplete(ctrl) is False

    completion.resolve(runToken, True)
    assert RecordingController.shutdownComplete(ctrl) is False

    ctrl._exactScanCompletionHandled = True
    assert RecordingController._scanRunIsActive(ctrl) is False
    assert RecordingController.shutdownComplete(ctrl) is True


def test_raised_targeted_dispatch_retains_owner_token_from_attached_result():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    owner = types.SimpleNamespace(isRunning=True)
    runToken = object()
    completion = object()
    requestResult = types.SimpleNamespace(
        acceptedTokens=((owner, runToken),),
        acceptedCompletions=((owner, runToken, completion),),
    )
    error = RuntimeError('source raised after accepting')
    error.scanRequestResult = requestResult
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=owner,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: (_ for _ in ()).throw(error)
            )
        ),
        # Exercise the attached-result fallback used by adapters without the
        # shared NI coordinator.
        _master=types.SimpleNamespace(),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert RecordingController._scanRunIsActive(ctrl) is True
    assert failures == [
        ('source raised after accepting', {'abortManager': True})
    ]


def test_unhandled_scan_request_gets_no_abort_authority():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    unrelatedToken = object()
    ctrl = types.SimpleNamespace(
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan=lambda *_args: types.SimpleNamespace(
                    handled=False,
                    accepted=False,
                )
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=unrelatedToken
            )
        ),
        _RecordingController__logger=types.SimpleNamespace(
            warning=lambda *_args, **_kwargs: None
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
    )

    accepted = RecordingController._requestScanStart(
        ctrl, True, False
    )

    assert accepted is True
    assert ctrl._scanRequestAccepted is False
    assert ctrl._acceptedScanRunToken is None
    assert RecordingController._scanRunIsActive(ctrl) is False


def test_targeted_legacy_scan_source_has_identity_safe_start_and_abort():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    events = []
    source = types.SimpleNamespace(isRunning=True)
    workflow = types.SimpleNamespace(
        run_scan_from=lambda target, *_args: (
            events.append(('run', target)),
            types.SimpleNamespace(handled=False, accepted=False),
        )[1],
        run_scan=lambda *_args: events.append('broadcast'),
        abort_scan_from=lambda target: events.append(('abort', target)),
    )
    ctrl = types.SimpleNamespace(
        _recordingScanSource=source,
        _commChannel=types.SimpleNamespace(scanWorkflow=workflow),
        _master=types.SimpleNamespace(),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is None
    assert RecordingController._scanRunIsActive(ctrl) is True

    assert RecordingController._requestScanStart(
        ctrl, False, False
    ) is True
    RecordingController._abortOwnedScanSequence(ctrl)

    assert events == [
        ('run', source),
        ('run', source),
        ('abort', source),
    ]


def test_targeted_legacy_scan_source_must_enter_running_state():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    source = types.SimpleNamespace(isRunning=False)
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=source,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: types.SimpleNamespace(
                    handled=False,
                    accepted=False,
                )
            )
        ),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert failures == [
        (
            'The selected scan source did not start.',
            {'abortManager': True},
        )
    ]


def test_idle_exact_source_retains_draining_run_abort_authority():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    class Source:
        supportsExactScanRequestCompletion = True
        isRunning = False

    source = Source()
    runToken = object()
    failures = []
    ctrl = types.SimpleNamespace(
        _recordingScanSource=source,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                run_scan_from=lambda *_args: types.SimpleNamespace(
                    handled=False,
                    accepted=False,
                )
            )
        ),
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                runForOwner=lambda owner: (
                    runToken if owner is source else None
                )
            )
        ),
        _scanRequestAccepted=False,
        _acceptedScanRunToken=None,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._requestScanStart(
        ctrl, True, False
    ) is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is runToken
    assert failures == [
        (
            'The selected scan source did not start.',
            {'abortManager': True},
        )
    ]


def test_close_in_targeted_legacy_cadence_gap_uses_source_terminal_once():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    events = []

    class Timer:
        def isActive(self):
            return True

        def stop(self):
            events.append('timer-stop')

    class Source:
        isRunning = False
        doingNonFinalPartOfSequence = True

        def abortScan(self):
            events.append('source-abort')
            self.doingNonFinalPartOfSequence = False
            events.append('source-end')
            ctrl._scanLifecycleEnded()

    source = Source()
    manager = types.SimpleNamespace(
        record=False,
        abortRecording=lambda **_kwargs: events.append('manager-abort'),
        shutdownComplete=lambda: True,
    )
    widget = types.SimpleNamespace(
        isRecButtonChecked=lambda: True,
        updateRecFrameNum=lambda _value: None,
        updateRecTime=lambda _value: None,
        updateRecLapseNum=lambda _value: None,
        setRecButtonChecked=lambda _value: None,
        setFieldsEnabled=lambda _value: None,
    )
    ctrl = types.SimpleNamespace(
        _shutdownRequested=False,
        _finalizingRecCycle=False,
        _recordingCycleTerminalHandled=True,
        _recordingFailureAwaitingScanEnd=False,
        _recordingFailedCurrent=False,
        _recordingOperationActive=True,
        _recordingManagerGeneration=1,
        _recordingGenerationBeforeOperation=0,
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=None,
        _acceptedScanCompletion=None,
        _exactScanCompletionHandled=False,
        _scanLifecycleEndedObserved=False,
        _recordingScanSource=source,
        _usesDetailedRecordingSignals=True,
        recMode=RecMode.ScanLapse,
        lapseCurrent=1,
        lapseTotal=3,
        recording=True,
        stopRequested=False,
        endedRecording=True,
        doneScan=True,
        timer=Timer(),
        _widget=widget,
        _commChannel=types.SimpleNamespace(
            scanWorkflow=types.SimpleNamespace(
                abort_scan_from=lambda target: target.abortScan(),
                notify_scan_ended=lambda: events.append('synthetic-end'),
            ),
            sigRecordingEnded=types.SimpleNamespace(
                emit=lambda: events.append('recording-ended')
            ),
        ),
        _master=types.SimpleNamespace(
            recordingManager=manager,
            scanExecutionCoordinator=None,
        ),
        _RecordingController__logger=types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None
        ),
    )
    for name in (
        '_scanLifecycleEnded',
        '_scanRunIsActive',
        '_abortOwnedScanSequence',
        '_notifyScanEndedIfPending',
        'recordingCycleEnded',
        'shutdownComplete',
    ):
        setattr(
            ctrl,
            name,
            types.MethodType(getattr(RecordingController, name), ctrl),
        )

    assert RecordingController.closeEvent(ctrl) is True
    assert source.doingNonFinalPartOfSequence is False
    assert events.count('source-end') == 1
    assert 'synthetic-end' not in events
    assert ctrl._scanStartPublished is False
    assert ctrl._scanRequestAccepted is False
    assert ctrl._recordingScanSource is None


def test_scan_recording_preflight_rejects_active_standalone_source():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    failures = []
    activeSource = object()
    ctrl = types.SimpleNamespace(
        _commChannel=types.SimpleNamespace(
            getActiveScanSource=lambda: activeSource
        ),
        _master=types.SimpleNamespace(),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    assert RecordingController._preflightNewScanRequest(ctrl) is False
    assert failures == [
        (
            'Cannot start scan recording while another scan source is active.',
            {'abortManager': False},
        )
    ]


def test_scanlapse_source_resolution_fails_before_recording_is_armed():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    events = []
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanLapse,
        stopRequested=False,
        timer=None,
        lapseCurrent=0,
        lapseTotal=2,
        _recordingCycleTerminalHandled=False,
        _recordingScanSource=None,
        _widget=types.SimpleNamespace(
            isRecButtonChecked=lambda: True
        ),
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(record=False)
        ),
        _commChannel=types.SimpleNamespace(
            getRecordingScanSource=lambda _preferred=None: (
                _ for _ in ()
            ).throw(RuntimeError('ambiguous scan source'))
        ),
        _selectedScanSourceKey=lambda: '',
        # No getRecordingScanSourceNames on the fake channel: a setup that
        # offers no choice resolves automatically and still surfaces the
        # ambiguity error from the resolver.
        _scanSourceChoiceRequired=lambda: False,
        _preflightNewScanRequest=lambda: True,
        _handleRecordingFailure=lambda message, **kwargs: events.append(
            (message, kwargs)
        ),
        _notifyScanStarting=lambda: events.append('scan-starting'),
        _startManagerRecording=lambda: events.append('recording-started'),
    )

    assert RecordingController.nextLapse(ctrl) is False
    assert events == [
        (
            'ambiguous scan source',
            # Classified: a caller that survives some failures must be told
            # this one was the scan, not the writer, so it cannot continue.
            {'abortManager': False, 'kind': FailureKind.SCAN},
        )
    ]


def test_scan_end_without_scan_done_aborts_armed_recording():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    failures = []
    ctrl = types.SimpleNamespace(
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=object(),
        recMode=RecMode.ScanOnce,
        doneScan=False,
        _recordingFailureHandled=False,
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanStartPublished is False
    assert ctrl._scanRequestAccepted is False
    assert ctrl._acceptedScanRunToken is None
    assert failures == [
        (
            'Scan ended before acquisition completed.',
            {'abortManager': True},
        )
    ]


def test_stale_global_scan_end_cannot_clear_active_recording_run_token():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    token = object()
    failures = []
    ctrl = types.SimpleNamespace(
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=token,
        _recordingScanSource=None,
        recMode=RecMode.ScanOnce,
        doneScan=False,
        _recordingFailureHandled=False,
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=token
            )
        ),
        _handleRecordingFailure=lambda message, **kwargs: failures.append(
            (message, kwargs)
        ),
    )

    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanStartPublished is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is token
    assert failures == []


def test_stale_scan_end_cannot_clear_no_coordinator_exact_cadence_gap():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    token = object()
    source = types.SimpleNamespace(isRunning=False)
    ctrl = types.SimpleNamespace(
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=token,
        _acceptedScanCompletion=None,
        _recordingScanSource=source,
        _scanLifecycleEndedObserved=False,
        _recordingFailureHandled=False,
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=None
        ),
        timer=types.SimpleNamespace(isActive=lambda: True),
        recMode=RecMode.ScanLapse,
        stopRequested=False,
        doneScan=True,
        endedRecording=False,
        _usesDetailedRecordingSignals=True,
    )

    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanStartPublished is True
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is token

    # A targeted stop makes the next source terminal authoritative even though
    # the cadence timer object is still retained by this lightweight fixture.
    ctrl.stopRequested = True
    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanStartPublished is False
    assert ctrl._scanRequestAccepted is False
    assert ctrl._acceptedScanRunToken is None


def test_held_barrier_cleared_run_accepts_its_real_recording_lifecycle_end():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    token = types.SimpleNamespace(
        releaseRequested=True,
        releaseBarrierCleared=True,
        holdReleaseUntilFinalized=True,
    )
    ctrl = types.SimpleNamespace(
        _scanStartPublished=True,
        _scanRequestAccepted=True,
        _acceptedScanRunToken=token,
        _acceptedScanCompletion=object(),
        _exactScanCompletionHandled=False,
        _scanLifecycleEndedObserved=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingScanSource=None,
        _master=types.SimpleNamespace(
            scanExecutionCoordinator=types.SimpleNamespace(
                activeRunToken=token
            )
        ),
    )

    RecordingController._scanLifecycleEnded(ctrl)

    assert ctrl._scanLifecycleEndedObserved is True
    assert ctrl._scanStartPublished is False
    assert ctrl._scanRequestAccepted is True
    assert ctrl._acceptedScanRunToken is token


def test_stale_detailed_recording_failure_is_ignored_by_new_operation():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    failures = []
    ctrl = types.SimpleNamespace(
        _recordingOperationActive=True,
        _recordingManagerGeneration=2,
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(recordingGeneration=2)
        ),
        recordingFailed=lambda message: failures.append(message),
    )
    ctrl._recordingSignalMatches = types.MethodType(
        RecordingController._recordingSignalMatches, ctrl
    )

    RecordingController._recordingFailedDetailed(
        ctrl, 'old failure', 1
    )
    RecordingController._recordingFailedDetailed(
        ctrl, 'current failure', 2
    )

    assert failures == ['current failure']


def test_scan_done_keeps_recording_identity_until_writer_terminal():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )
    from imswitch.imcontrol.model import RecMode

    failures = []
    ctrl = types.SimpleNamespace(
        recMode=RecMode.ScanOnce,
        doneScan=False,
        endedRecording=False,
        _recordingOperationActive=True,
        _recordingManagerGeneration=5,
        _recordingGenerationBeforeOperation=4,
        _usesDetailedRecordingSignals=True,
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(recordingGeneration=5)
        ),
        recordingCycleEnded=lambda: failures.append('premature-success'),
        recordingFailed=lambda message: failures.append(message),
    )
    ctrl._recordingSignalMatches = types.MethodType(
        RecordingController._recordingSignalMatches, ctrl
    )

    RecordingController.scanDone(ctrl)
    RecordingController._recordingFailedDetailed(
        ctrl, 'writer finalization failed', 5
    )

    assert ctrl.doneScan is True
    assert failures == ['writer finalization failed']


def test_prestart_generation_window_rejects_queued_previous_terminal():
    import types

    from imswitch.imcontrol.controller.controllers.RecordingController import (
        RecordingController,
    )

    ctrl = types.SimpleNamespace(
        _recordingOperationActive=True,
        _recordingManagerGeneration=None,
        _recordingGenerationBeforeOperation=8,
        _master=types.SimpleNamespace(
            recordingManager=types.SimpleNamespace(recordingGeneration=8)
        ),
    )

    assert RecordingController._recordingSignalMatches(ctrl, 8) is False
    ctrl._master.recordingManager.recordingGeneration = 9
    assert RecordingController._recordingSignalMatches(ctrl, 9) is True
    assert ctrl._recordingManagerGeneration == 9


def test_recording_dtype_preservation(qtbot):
    """HDF5 datasets must derive their dtype from the detector frames.

    Regression test: the old code hardcoded dtype='i2' (signed int16), which
    corrupted unsigned-16-bit values >32767 (wrap-around to negative) and
    needlessly widened 8-bit data. The dataset dtype must instead match the
    dtype of the frames the detector actually produces.

    Note: this asserts the *invariant* (dtype derived from frames, never the
    hardcoded 'i2'). Verifying a specific bit depth (e.g. uint8 stays uint8)
    would require a mock camera whose output dtype is configurable — see the
    recording-manager milestone in ROADMAP.md.
    """
    detectorInfos = detectorInfosBasic
    detectorName = next(iter(detectorInfos))

    filePerDetector, _ = record(
        qtbot,
        detectorInfos,
        detectorNames=[detectorName],
        recMode=RecMode.SpecFrames,
        savename='test_dtype',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {'testAttr1': 2, 'testAttr2': 'value'}},
        recFrames=5
    )

    file = filePerDetector[detectorName]
    with h5py.File(file) as h5pyFile:
        # Phase 4: structured layout - dataset at /<detector>/data
        detector_group = h5pyFile.get(detectorName)
        assert detector_group is not None, 'recorded detector group missing'
        dataset = detector_group.get('data')
        assert dataset is not None, 'recorded dataset missing'
        # The bug: dtype was hardcoded to signed int16 ('i2'). The fix derives
        # it from the frame array, so it must NOT be 'i2' here (the mock
        # detector yields wider integer frames).
        assert dataset.dtype != np.dtype('i2'), \
            "HDF5 dtype must be derived from detector frames, not hardcoded 'i2'"
        assert np.issubdtype(dataset.dtype, np.integer), \
            f'expected an integer dataset dtype, got {dataset.dtype}'
    file.close()


def test_snap_hdf5_structured_layout(tmp_path):
    """Test that HDF5 snapshots use the structured layout with detector groups and metadata."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager, SaveMode, SaveFormat
    
    # Create a simple detector setup
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image and attrs
    detectorName = list(detectorInfosBasic.keys())[0]
    test_image = np.random.randint(0, 1000, (10, 128, 128), dtype=np.uint16)
    
    # Create attrs with categorized keys
    test_attrs = {
        detectorName: {
            'detector:exposure': 0.01,
            'detector:gain': 1.5,
            'lasers:laser1:power': 50,
            'lasers:laser1:wavelength': 488,
            'scan:size_um': 100.0,
            'uncategorized_key': 'value'
        }
    }
    
    # Snap the image
    savepath = str(tmp_path / 'test_snap')
    recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
    
    # Verify structured layout
    snap_file = f'{savepath}_{detectorName}.h5'
    assert os.path.exists(snap_file), f"Snapshot file not created: {snap_file}"
    
    with h5py.File(snap_file, 'r') as f:
        # Check file-level attrs
        assert 'timestamp' in f.attrs
        assert 'rec_mode' in f.attrs
        assert f.attrs['rec_mode'] == 'snap'
        
        # Check detector group exists
        assert detectorName in f, f"Detector group '{detectorName}' not found"
        det_group = f[detectorName]
        
        # Check data dataset
        assert 'data' in det_group, "data dataset not found in detector group"
        dataset = det_group['data']
        
        # Check dataset attrs
        assert 'detector_name' in dataset.attrs
        assert 'element_size_um' in dataset.attrs
        assert dataset.attrs['detector_name'] == detectorName
        
        # Check dtype preservation
        assert dataset.dtype == np.uint16, f"Expected uint16, got {dataset.dtype}"
        
        # Check shape (should be T, Y, X)
        assert dataset.shape == test_image.shape, f"Shape mismatch: {dataset.shape} != {test_image.shape}"
        
        # Check data content
        np.testing.assert_array_equal(dataset[:], test_image)
        
        # Check compression (gzip is the default since lzf caused problems
        # on some setups; see RecordingManager)
        assert dataset.compression is not None, "Dataset should be compressed"
        assert dataset.compression == 'gzip', f"Expected gzip compression, got {dataset.compression}"
        assert dataset.shuffle, "Shuffle filter should be enabled"
        
        # Check metadata group structure
        assert 'metadata' in det_group, "metadata group not found"
        meta_group = det_group['metadata']
        
        # Check category subgroups
        assert 'detector' in meta_group, "detector category not found in metadata"
        assert 'lasers' in meta_group, "lasers category not found in metadata"
        assert 'scan' in meta_group, "scan category not found in metadata"
        
        # Check detector category attrs
        det_meta = meta_group['detector']
        assert 'exposure' in det_meta.attrs
        assert 'gain' in det_meta.attrs
        assert det_meta.attrs['exposure'] == 0.01
        assert det_meta.attrs['gain'] == 1.5
        
        # Check lasers category attrs
        laser_meta = meta_group['lasers']
        assert 'laser1:power' in laser_meta.attrs
        assert 'laser1:wavelength' in laser_meta.attrs
        assert laser_meta.attrs['laser1:power'] == 50
        assert laser_meta.attrs['laser1:wavelength'] == 488
        
        # Check scan category attrs
        scan_meta = meta_group['scan']
        assert 'size_um' in scan_meta.attrs
        assert scan_meta.attrs['size_um'] == 100.0
        
        # Check uncategorized attrs (flat in metadata group)
        assert 'uncategorized_key' in meta_group.attrs
        assert meta_group.attrs['uncategorized_key'] == 'value'


def test_snap_hdf5_existing_file_uses_numbered_name(tmp_path):
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    detectorName = list(detectorInfosBasic.keys())[0]
    savepath = str(tmp_path / 'specified_snap')
    existing_file = f'{savepath}_{detectorName}.h5'

    with h5py.File(existing_file, 'w') as file:
        file.attrs['sentinel'] = 'keep'

    image = np.zeros((1, 8, 8), dtype=np.uint16)
    recordingManager.snapImagePrev(
        detectorName,
        savepath,
        SaveFormat.HDF5,
        image,
        {detectorName: {}},
    )

    numbered_file = f'{savepath}_1_{detectorName}.h5'
    assert os.path.exists(existing_file)
    assert os.path.exists(numbered_file)

    with h5py.File(existing_file, 'r') as file:
        assert file.attrs['sentinel'] == 'keep'
    with h5py.File(numbered_file, 'r') as file:
        np.testing.assert_array_equal(file[detectorName]['data'][:], image)


def test_recording_path_existing_file_uses_numbered_suffix(tmp_path):
    recordingManager = RecordingManager(_OmeMetaDetectors())
    path = tmp_path / 'specified_rec_CAM.hdf5'
    path.touch()
    (tmp_path / 'specified_rec_CAM_1.hdf5').touch()

    assert recordingManager.getSaveFilePath(str(path)) == str(
        tmp_path / 'specified_rec_CAM_2.hdf5'
    )


def test_snap_axis_ordering():
    """Test that snapshots use (T, Y, X) axis convention without reversal."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image with known pattern to detect axis swaps
    # Shape: (3, 100, 50) = (T, Y, X)
    test_image = np.zeros((3, 100, 50), dtype=np.uint16)
    test_image[0, :, 0] = 1  # Mark first frame, first column
    test_image[1, 0, :] = 2  # Mark second frame, first row
    test_image[2, :, :] = 3  # Mark third frame, all pixels
    
    detectorName = list(detectorInfosBasic.keys())[0]
    test_attrs = {detectorName: {'test': 'value'}}
    
    # Use a unique temporary filename
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        savepath = os.path.join(tmpdir, 'test_axis')
        recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
        
        # Read back and verify no axis transposition
        snap_file = f'{savepath}_{detectorName}.h5'
        with h5py.File(snap_file, 'r') as f:
            dataset = f[detectorName]['data']
            saved_data = dataset[:]
            
            # Should match exactly (T, Y, X) with no reversal
            assert saved_data.shape == test_image.shape
            np.testing.assert_array_equal(saved_data, test_image)
            
            # Verify the pattern markers are in correct positions
            assert saved_data[0, :, 0].sum() == 100  # First column of frame 0
            assert saved_data[1, 0, :].sum() == 100  # First row of frame 1
            assert saved_data[2, :, :].sum() == 3 * 100 * 50  # All of frame 2


def test_snap_zarr_structured_layout(tmp_path) -> None:
    """Test that Zarr snapshots mirror the structured HDF5 layout."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    detectorName = list(detectorInfosBasic.keys())[0]
    test_image = np.zeros((3, 100, 50), dtype=np.uint16)
    test_image[0, :, 0] = 1
    test_image[1, 0, :] = 2
    test_image[2, :, :] = 3

    test_attrs = {
        detectorName: {
            'detector:exposure': 0.01,
            'detector:gain': 1.5,
            'lasers:laser1:power': 50,
            'scan:size_um': 100.0,
            'uncategorized_key': 'value',
        }
    }

    savepath = str(tmp_path / 'test_zarr_snap')
    recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.ZARR, test_image, test_attrs)

    snap_store = f'{savepath}.zarr'
    assert os.path.exists(snap_store), f"Zarr snapshot not created: {snap_store}"

    root = zarr.open(snap_store, mode='r')
    assert root.attrs['rec_mode'] == 'snap'
    assert detectorName in root

    det_group = root[detectorName]
    assert 'data' in det_group
    dataset = det_group['data']
    assert dataset.shape == test_image.shape
    assert dataset.dtype == np.uint16
    assert dataset.attrs['detector_name'] == detectorName
    assert dataset.attrs['axes'] == ['T', 'Y', 'X']
    assert dataset.attrs['writing'] is False
    np.testing.assert_array_equal(dataset[:], test_image)

    assert 'metadata' in det_group
    meta_group = det_group['metadata']
    assert meta_group.attrs['uncategorized_key'] == 'value'
    assert meta_group['detector'].attrs['exposure'] == 0.01
    assert meta_group['detector'].attrs['gain'] == 1.5
    assert meta_group['lasers'].attrs['laser1:power'] == 50
    assert meta_group['scan'].attrs['size_um'] == 100.0


def test_zarr_streaming_structured_layout(tmp_path) -> None:
    """Test that Zarr streaming creates a structured, dtype-preserving array."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    store_path = str(tmp_path / 'test_zarr_stream.zarr')
    attrs = {
        detectorName: {
            'detector:exposure': 0.02,
            'scan:frames': 3,
        }
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={detectorName: store_path},
        detectorNames=[detectorName],
        shapes={detectorName: (50, 100)},
        attrs=attrs,
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )

    frames_a = np.arange(2 * 7 * 5, dtype=np.uint16).reshape(2, 7, 5)
    frames_b = np.full((1, 7, 5), 50000, dtype=np.uint16)
    storer.writeFrames(detectorName, frames_a)
    storer.writeFrames(detectorName, frames_b)
    storer.finalizeStream(
        currentFrames={detectorName: 3},
        filePaths={detectorName: store_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(store_path, mode='r')
    dataset = root[detectorName]['data']
    expected = np.concatenate([frames_a, frames_b], axis=0)
    assert dataset.shape == expected.shape
    assert dataset.dtype == np.uint16
    assert dataset.attrs['writing'] is False
    assert dataset.attrs['axes'] == ['T', 'Y', 'X']
    np.testing.assert_array_equal(dataset[:], expected)

    meta_group = root[detectorName]['metadata']
    assert meta_group['detector'].attrs['exposure'] == 0.02
    assert meta_group['scan'].attrs['frames'] == 3


def test_zarr_streaming_multi_detector_single_file(tmp_path) -> None:
    """Test that multiple detectors can stream into one structured Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosMulti, updatePeriod=100)
    detectorNames = list(detectorInfosMulti.keys())
    store_path = str(tmp_path / 'test_zarr_multidetector.zarr')
    attrs = {name: {'detector:index': index} for index, name in enumerate(detectorNames)}

    frames = {
        detectorNames[0]: np.full((2, 4, 5), 11, dtype=np.uint16),
        detectorNames[1]: np.full((3, 6, 7), 22, dtype=np.uint16),
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={name: store_path for name in detectorNames},
        detectorNames=detectorNames,
        shapes={name: frames[name].shape[-2:] for name in detectorNames},
        attrs=attrs,
        singleMultiDetectorFile=True,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for name in detectorNames:
        storer.writeFrames(name, frames[name])
    storer.finalizeStream(
        currentFrames={name: frames[name].shape[0] for name in detectorNames},
        filePaths={name: store_path for name in detectorNames},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(store_path, mode='r')
    assert sorted(root.keys()) == sorted(detectorNames)
    for index, name in enumerate(detectorNames):
        dataset = root[name]['data']
        assert dataset.shape == frames[name].shape
        assert dataset.dtype == np.uint16
        assert dataset.attrs['writing'] is False
        assert root[name]['metadata']['detector'].attrs['index'] == index
        np.testing.assert_array_equal(dataset[:], frames[name])


def test_zarr_streaming_per_detector_files(tmp_path) -> None:
    """Test that per-detector mode writes each detector to a separate Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosMulti, updatePeriod=100)
    detectorNames = list(detectorInfosMulti.keys())
    fileDests = {
        name: str(tmp_path / f'test_zarr_{index}.zarr')
        for index, name in enumerate(detectorNames)
    }
    frames = {
        detectorNames[0]: np.full((2, 3, 4), 7, dtype=np.uint16),
        detectorNames[1]: np.full((2, 5, 6), 9, dtype=np.uint16),
    }

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests=fileDests,
        detectorNames=detectorNames,
        shapes={name: frames[name].shape[-2:] for name in detectorNames},
        attrs={name: {} for name in detectorNames},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for name in detectorNames:
        storer.writeFrames(name, frames[name])
    storer.finalizeStream(
        currentFrames={name: frames[name].shape[0] for name in detectorNames},
        filePaths=fileDests,
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    for name in detectorNames:
        root = zarr.open(fileDests[name], mode='r')
        assert list(root.keys()) == [name]
        np.testing.assert_array_equal(root[name]['data'][:], frames[name])


def test_zarr_streaming_frames_committed_barrier(tmp_path) -> None:
    """ZarrStorer maintains the recording:frames_committed live-reader barrier.

    Zarr resizes the array BEFORE writing frame data, so a live reader that
    trusts array.shape reads uninitialised chunks. frames_committed is updated
    AFTER each batch write so readers that honour it never run ahead of the
    data on disk (docs/live_reconstruction_audit.md).
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    store_path = str(tmp_path / 'committed.zarr')

    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={detectorName: store_path},
        detectorNames=[detectorName],
        shapes={detectorName: (4, 5)},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )

    storer.writeFrames(detectorName, np.ones((3, 4, 5), dtype=np.uint16))
    dataset = zarr.open(store_path, mode='r')[detectorName]['data']
    assert int(dataset.attrs['recording:frames_committed']) == 3
    assert bool(dataset.attrs['writing']) is True

    storer.writeFrames(detectorName, np.ones((2, 4, 5), dtype=np.uint16))
    dataset = zarr.open(store_path, mode='r')[detectorName]['data']
    assert int(dataset.attrs['recording:frames_committed']) == 5

    storer.finalizeStream(
        currentFrames={detectorName: 5}, filePaths={detectorName: store_path},
        recordingManager=None, saveMode=SaveMode.Disk,
    )
    dataset = zarr.open(store_path, mode='r')[detectorName]['data']
    assert int(dataset.attrs['recording:frames_committed']) == 5
    assert bool(dataset.attrs['writing']) is False


def test_hdf5_streaming_frames_committed_and_complete_marker(tmp_path) -> None:
    """HDF5Storer maintains frames_committed / stream_complete side datasets.

    SWMR forbids attribute writes after swmr_mode=True, so the barrier and
    the completion marker live as 1-element datasets next to 'data'. The
    marker is written while the SWMR handle is still open — unlike the
    writing=False attr (rewritten via post-close r+ reopen), a live SWMR
    reader can actually see it, which is what lets SpecTime/UntilStop live
    reconstructions terminate.
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    file_path = str(tmp_path / 'committed.hdf5')

    storer = HDF5Storer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={detectorName: file_path},
        detectorNames=[detectorName],
        shapes={detectorName: (4, 5)},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )

    storer.writeFrames(detectorName, np.ones((3, 4, 5), dtype=np.uint16))
    assert storer._files[detectorName].libver == HDF5_STREAM_LIBVER
    with h5py.File(file_path, 'r', libver='latest', swmr=True) as f:
        group = f[detectorName]
        assert int(group['frames_committed'][0]) == 3
        assert int(group['stream_complete'][0]) == 0

    storer.writeFrames(detectorName, np.ones((2, 4, 5), dtype=np.uint16))
    with h5py.File(file_path, 'r', libver='latest', swmr=True) as f:
        assert int(f[detectorName]['frames_committed'][0]) == 5

    storer.finalizeStream(
        currentFrames={detectorName: 5}, filePaths={detectorName: file_path},
        recordingManager=None, saveMode=SaveMode.Disk,
    )
    with h5py.File(file_path, 'r') as f:
        group = f[detectorName]
        assert int(group['frames_committed'][0]) == 5
        assert int(group['stream_complete'][0]) == 1
        assert not bool(group['data'].attrs['writing'])


def test_hdf5_disk_and_ram_closes_writer_and_hands_off_readonly(tmp_path) -> None:
    """DiskAndRAM finalization must leave a normal closed file for external viewers."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    file_path = str(tmp_path / 'disk_and_ram.hdf5')

    class _SignalSink:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    class _RecordingManager:
        def __init__(self):
            self.sigMemoryRecordingAvailable = _SignalSink()

    storer = HDF5Storer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={detectorName: file_path},
        detectorNames=[detectorName],
        shapes={detectorName: (4, 5)},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.DiskAndRAM,
    )
    storer.writeFrames(detectorName, np.ones((2, 4, 5), dtype=np.uint16))

    manager = _RecordingManager()
    writer_file = storer._files[detectorName]
    assert writer_file.libver == HDF5_STREAM_LIBVER
    storer.finalizeStream(
        currentFrames={detectorName: 2}, filePaths={detectorName: file_path},
        recordingManager=manager, saveMode=SaveMode.DiskAndRAM,
    )

    assert not writer_file.id.valid
    assert len(manager.sigMemoryRecordingAvailable.calls) == 1
    name, handoff_file, path, saved_to_disk = manager.sigMemoryRecordingAvailable.calls[0]
    assert name == os.path.basename(file_path)
    assert path == file_path
    assert saved_to_disk is True
    assert isinstance(handoff_file, h5py.File)
    assert handoff_file.mode == 'r'
    assert handoff_file[detectorName]['data'].shape == (2, 4, 5)

    with h5py.File(file_path, 'r', libver=HDF5_STREAM_LIBVER) as f:
        assert not bool(f[detectorName]['data'].attrs['writing'])
        np.testing.assert_array_equal(f[detectorName]['data'][:], np.ones((2, 4, 5), dtype=np.uint16))

    handoff_file.close()
    with h5py.File(file_path, 'r+') as f:
        assert detectorName in f


def test_hdf5_single_multidetector_finalize_updates_all_datasets(tmp_path) -> None:
    """Shared HDF5 files must finalize every detector before closing the file."""
    detectorsManager = DetectorsManager(detectorInfosMulti, updatePeriod=100)
    detectorNames = list(detectorInfosMulti.keys())
    file_path = str(tmp_path / 'multi_detector.hdf5')

    storer = HDF5Storer(str(tmp_path / 'unused'), detectorsManager)
    storer.openStream(
        fileDests={name: file_path for name in detectorNames},
        detectorNames=detectorNames,
        shapes={name: (4, 5) for name in detectorNames},
        attrs={name: {} for name in detectorNames},
        singleMultiDetectorFile=True,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for index, detectorName in enumerate(detectorNames):
        storer.writeFrames(
            detectorName,
            np.full((2, 4, 5), index + 1, dtype=np.uint16),
        )
    assert storer._files[detectorNames[0]].libver == HDF5_STREAM_LIBVER

    storer.finalizeStream(
        currentFrames={name: 2 for name in detectorNames},
        filePaths={name: file_path for name in detectorNames},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    with h5py.File(file_path, 'r', libver=HDF5_STREAM_LIBVER) as f:
        for index, detectorName in enumerate(detectorNames):
            group = f[detectorName]
            assert int(group['frames_committed'][0]) == 2
            assert int(group['stream_complete'][0]) == 1
            assert not bool(group['data'].attrs['writing'])
            np.testing.assert_array_equal(
                group['data'][:],
                np.full((2, 4, 5), index + 1, dtype=np.uint16),
            )


def test_zarr_streaming_single_lapse_adds_scan_groups(tmp_path) -> None:
    """Test repeated single-lapse streams append scan groups in one Zarr store."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    store_path = str(tmp_path / 'test_zarr_lapse.zarr')

    for scan_index in range(2):
        frames = np.full((1, 4, 5), scan_index + 1, dtype=np.uint16)
        storer = ZarrStorer(str(tmp_path / f'unused_{scan_index}'), detectorsManager)
        storer.openStream(
            fileDests={detectorName: store_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames.shape[-2:]},
            attrs={detectorName: {'scan:index': scan_index}},
            singleMultiDetectorFile=False,
            singleLapseFile=True,
            saveMode=SaveMode.Disk,
        )
        storer.writeFrames(detectorName, frames)
        storer.finalizeStream(
            currentFrames={detectorName: frames.shape[0]},
            filePaths={detectorName: store_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )

    root = zarr.open(store_path, mode='r')
    assert sorted(root.keys()) == ['scan0', 'scan1']
    for scan_index in range(2):
        dataset = root[f'scan{scan_index}'][detectorName]['data']
        assert dataset.shape == (1, 4, 5)
        assert dataset.attrs['writing'] is False
        assert root[f'scan{scan_index}'][detectorName]['metadata']['scan'].attrs['index'] == scan_index
        np.testing.assert_array_equal(
            dataset[:], np.full((1, 4, 5), scan_index + 1, dtype=np.uint16)
        )


def test_zarr_ram_streaming_is_explicitly_unsupported(tmp_path) -> None:
    """Document current Zarr RAM-mode boundary until MemoryStore support is added."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    storer = ZarrStorer(str(tmp_path / 'unused'), detectorsManager)

    with pytest.raises(NotImplementedError):
        storer.openStream(
            fileDests={detectorName: str(tmp_path / 'memory.zarr')},
            detectorNames=[detectorName],
            shapes={detectorName: (4, 5)},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.RAM,
        )


def test_recording_stall_watchdog(qtbot, caplog, monkeypatch):
    """Test that the stall watchdog detects and aborts recordings when no frames arrive."""
    import time
    import logging
    
    # Use basic detector configuration
    detectorInfos = detectorInfosBasic
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Mock the detector's getChunk method to return no frames (simulating a stalled camera)
    detectorName = list(detectorInfos.keys())[0]
    original_getChunk = detectorsManager[detectorName].getChunk
    def mock_getChunk():
        return []  # Return empty list to simulate no frames arriving
    monkeypatch.setattr(detectorsManager[detectorName], 'getChunk', mock_getChunk)
    
    # Track signals
    stalled_detector = None
    failures = []
    
    def on_stalled(detector_name):
        nonlocal stalled_detector
        stalled_detector = detector_name
    
    recordingManager.sigRecordingStalled.connect(on_stalled)
    recordingManager.sigRecordingFailed.connect(failures.append)
    
    # Start recording with a very short stall timeout
    short_timeout = 0.5  # 0.5 seconds for faster test
    start_time = time.time()
    
    with caplog.at_level(logging.ERROR):
        recordingManager.startRecording(
            detectorNames=[detectorName],
            recMode=RecMode.SpecFrames,
            savename='test_stall',
            saveMode=SaveMode.RAM,
            attrs={detectorName: {}},
            recFrames=100,  # Request many frames that will never arrive
            stallTimeout=short_timeout
        )
        
        # Wait for stall signal with a reasonable timeout (stall timeout + margin)
        try:
            with qtbot.waitSignal(recordingManager.sigRecordingStalled, timeout=int((short_timeout + 2) * 1000)):
                pass
        except Exception:
            # If signal not emitted, the test will check the state below
            pass
    
    elapsed = time.time() - start_time
    
    # Verify the watchdog triggered
    assert stalled_detector is not None, "Stall signal should have been emitted"
    assert stalled_detector == detectorName, f"Stalled detector '{stalled_detector}' should match '{detectorName}'"
    
    # Verify error was logged
    error_logs = [r for r in caplog.records if r.levelno == logging.ERROR and 'stalled' in r.message]
    assert len(error_logs) > 0, "Expected ERROR log message about stall"
    assert 'no frames received' in error_logs[0].message
    
    # Verify it triggered within reasonable time (stall timeout + some margin for processing)
    assert elapsed >= short_timeout, f"Watchdog triggered too early: {elapsed:.2f}s < {short_timeout}s"
    assert elapsed < short_timeout + 2, f"Watchdog took too long: {elapsed:.2f}s > {short_timeout + 2}s"
    
    # Verify recording failed closed (thread not hung and no ordinary-success
    # interpretation for the truncated stream).
    qtbot.wait(200)  # Small delay to let thread finish
    assert not recordingManager.record, "Recording should have stopped after stall"
    assert failures and 'stalled' in failures[0]


def test_scan_point_detector_watchdog_starts_after_scan_end(qtbot):
    """Long scans get their full runtime before an APD final-frame timeout."""
    class _PointDetector:
        shape = (2, 2)
        dtype = np.dtype(np.uint16)
        pixelSizeUm = [1.0, 1.0, 1.0]
        isScanDriven = True

        def startAcquisition(self):
            pass

        def stopAcquisition(self):
            pass

        def startChunkConsumer(self, _consumerKey):
            pass

        def releaseChunkConsumer(self, _consumerKey):
            pass

        def readChunk(self, _consumerKey):
            return []

    class _PointDetectors:
        def __init__(self):
            self.detector = _PointDetector()

        def __getitem__(self, name):
            assert name == 'APDred'
            return self.detector

        def acquire(self, detectorNames, purpose):
            assert tuple(detectorNames) == ('APDred',)
            self.detector.startAcquisition()
            return object()

        def release(self, _handle):
            self.detector.stopAcquisition()

    class _NoopStorer:
        def __init__(self, _filepath, _detectorsManager):
            pass

        def openStream(self, **_kwargs):
            pass

        def writeFrames(self, _detectorName, _frames):
            pass

        def finalizeStream(self, *_args, **_kwargs):
            pass

        def abortStream(self, *_args, **_kwargs):
            pass

    recordingManager = RecordingManager(
        _PointDetectors(),
        storerMap={SaveFormat.HDF5: _NoopStorer},
    )
    stalled = []
    failures = []
    recordingManager.sigRecordingStalled.connect(stalled.append)
    recordingManager.sigRecordingFailed.connect(failures.append)

    generation = recordingManager.startRecording(
        detectorNames=['APDred'],
        recMode=RecMode.ScanOnce,
        savename='long_two_linestep_scan',
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'APDred': {}},
        recFrames=1,
        stallTimeout=0.05,
    )
    try:
        assert recordingManager.waitForAcquisitionStarted(2.0)
        assert recordingManager.markScanStarted(
            {
                'scan_samples_total': 500,
                'scan_time_step': 0.001,
                'n_linesteps': 2,
            },
            generation,
        )

        # This is three times the ordinary frame timeout, but still well
        # inside the generated 0.5 s scan.
        qtbot.wait(150)
        assert recordingManager.record
        assert stalled == []

        assert recordingManager.markScanCompleted(generation)
        with qtbot.waitSignal(
            recordingManager.sigRecordingStalled, timeout=1000
        ):
            pass
        assert stalled == ['APDred']
        qtbot.waitUntil(lambda: bool(failures), timeout=1000)
        assert failures
        assert 'after scan completion' in failures[0]
    finally:
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_hdf5_stream_preserves_linestep_axis(tmp_path):
    """One APD frame retains both line-step planes as (T, C, Y, X)."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = next(iter(detectorInfosBasic))
    path = str(tmp_path / 'apd_linesteps.h5')
    frames = np.arange(1 * 2 * 4 * 5, dtype=np.uint16).reshape(1, 2, 4, 5)

    storer = HDF5Storer(str(tmp_path / 'unused'), detectorsManager)
    metadataManager = RecordingManager(detectorsManager)
    storer.omeMeta = {
        detectorName: metadataManager.buildOmeMeta(
            detectorName, MODE_SCAN, 1
        )
    }
    storer.openStream(
        fileDests={detectorName: path},
        detectorNames=[detectorName],
        shapes={detectorName: frames.shape[1:]},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream(
        {detectorName: 1},
        {detectorName: path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    with h5py.File(path, 'r') as file:
        dataset = file[detectorName]['data']
        assert dataset.shape == (1, 2, 4, 5)
        assert dataset.attrs['axes'] == 'TCYX'
        np.testing.assert_array_equal(dataset[:], frames)
        # The OME-XML must survive SizeC > 1: build_ome_xml used to supply
        # one channel name against SizeC = n_linesteps, and the resulting
        # IndexError silently dropped this attribute.
        xml = file[detectorName].attrs['ome_xml']
        assert 'SizeC="2"' in xml
        assert xml.count(f'Name="{detectorName}"') == 2


def test_detector_dtype_contract(tmp_path):
    """Test that storers create datasets from detector's declared dtype (the contract).
    
    Phase 1, Task 1: The detector's dtype property is the single authoritative 
    source of truth for recording. HDF5 and Zarr storers must create datasets 
    from this declared dtype, not from frames.dtype.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer
    
    # Create detector with uint16 dtype (default)
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # Verify the detector declares uint16
    declared_dtype = detectorsManager[detectorName].dtype
    assert declared_dtype == np.dtype(np.uint16), \
        f"Expected uint16 declared dtype, got {declared_dtype}"
    
    # Test HDF5Storer
    hdf5_path = str(tmp_path / 'test_dtype_contract.h5')
    hdf5_storer = HDF5Storer(hdf5_path, detectorsManager)
    frames = np.random.randint(0, 1000, (5, 128, 128), dtype=np.uint16)
    
    hdf5_storer.openStream(
        fileDests={detectorName: hdf5_path},
        detectorNames=[detectorName],
        shapes={detectorName: frames.shape[-2:]},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    hdf5_storer.writeFrames(detectorName, frames)
    hdf5_storer.finalizeStream(
        currentFrames={detectorName: frames.shape[0]},
        filePaths={detectorName: hdf5_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Assert on-disk HDF5 dataset dtype matches declared dtype
    with h5py.File(hdf5_path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.dtype == declared_dtype, \
            f"HDF5 dataset dtype {dataset.dtype} != declared dtype {declared_dtype}"
    
    # Test ZarrStorer
    zarr_path = str(tmp_path / 'test_dtype_contract.zarr')
    zarr_storer = ZarrStorer(zarr_path, detectorsManager)
    
    zarr_storer.openStream(
        fileDests={detectorName: zarr_path},
        detectorNames=[detectorName],
        shapes={detectorName: frames.shape[-2:]},
        attrs={detectorName: {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    zarr_storer.writeFrames(detectorName, frames)
    zarr_storer.finalizeStream(
        currentFrames={detectorName: frames.shape[0]},
        filePaths={detectorName: zarr_path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Assert on-disk Zarr dataset dtype matches declared dtype
    root = zarr.open(zarr_path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.dtype == declared_dtype, \
        f"Zarr dataset dtype {dataset.dtype} != declared dtype {declared_dtype}"


def test_dtype_mismatch_warning(tmp_path, caplog):
    """Test that storers warn loudly once per detector on dtype mismatch.
    
    Phase 1, Task 1: When frames.dtype != declared dtype, storers must emit a
    prominent warning ONCE per detector, then proceed with a logged cast (not silent).
    The on-disk dtype must remain the declared dtype.
    """
    import logging
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer, TiffStorer
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # Detector declares uint16
    declared_dtype = detectorsManager[detectorName].dtype
    assert declared_dtype == np.dtype(np.uint16)
    
    # Create frames with a DIFFERENT dtype (float32) to trigger mismatch
    frames_wrong_dtype = np.random.rand(3, 128, 128).astype(np.float32)
    
    # Test HDF5Storer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        hdf5_path = str(tmp_path / 'test_mismatch.h5')
        hdf5_storer = HDF5Storer(hdf5_path, detectorsManager)
        
        hdf5_storer.openStream(
            fileDests={detectorName: hdf5_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        hdf5_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        hdf5_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: hdf5_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for HDF5Storer
    hdf5_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'HDF5Storer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(hdf5_warnings) == 1, \
        f"Expected exactly 1 HDF5 dtype mismatch warning, got {len(hdf5_warnings)}"
    assert 'declared=uint16' in hdf5_warnings[0].message
    assert 'actual frame=float32' in hdf5_warnings[0].message
    
    # Assert on-disk dtype is the DECLARED dtype (uint16), not the frame dtype (float32)
    with h5py.File(hdf5_path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.dtype == declared_dtype, \
            f"HDF5 dataset should use declared dtype {declared_dtype}, got {dataset.dtype}"
        assert dataset.shape[0] == 3, "All frames should be recorded despite mismatch"
    
    # Clear caplog for Zarr test
    caplog.clear()
    
    # Test ZarrStorer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        zarr_path = str(tmp_path / 'test_mismatch.zarr')
        zarr_storer = ZarrStorer(zarr_path, detectorsManager)
        
        zarr_storer.openStream(
            fileDests={detectorName: zarr_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        zarr_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        zarr_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: zarr_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for ZarrStorer
    zarr_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'ZarrStorer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(zarr_warnings) == 1, \
        f"Expected exactly 1 Zarr dtype mismatch warning, got {len(zarr_warnings)}"
    
    # Assert on-disk dtype is the DECLARED dtype
    root = zarr.open(zarr_path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.dtype == declared_dtype, \
        f"Zarr dataset should use declared dtype {declared_dtype}, got {dataset.dtype}"
    assert dataset.shape[0] == 3, "All frames should be recorded despite mismatch"
    
    # Clear caplog for Tiff test
    caplog.clear()
    
    # Test TiffStorer with dtype mismatch
    with caplog.at_level(logging.WARNING):
        tiff_path = str(tmp_path / 'test_mismatch.tiff')
        tiff_storer = TiffStorer(tiff_path, detectorsManager)
        
        tiff_storer.openStream(
            fileDests={detectorName: tiff_path},
            detectorNames=[detectorName],
            shapes={detectorName: frames_wrong_dtype.shape[-2:]},
            attrs={detectorName: {}},
            singleMultiDetectorFile=False,
            singleLapseFile=False,
            saveMode=SaveMode.Disk,
        )
        
        # Write multiple chunks - warning should appear only ONCE
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[:1])
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[1:2])
        tiff_storer.writeFrames(detectorName, frames_wrong_dtype[2:])
        
        tiff_storer.finalizeStream(
            currentFrames={detectorName: frames_wrong_dtype.shape[0]},
            filePaths={detectorName: tiff_path},
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )
    
    # Assert exactly ONE warning for TiffStorer
    tiff_warnings = [r for r in caplog.records 
                     if r.levelno == logging.WARNING 
                     and 'TiffStorer dtype mismatch' in r.message
                     and detectorName in r.message]
    assert len(tiff_warnings) == 1, \
        f"Expected exactly 1 Tiff dtype mismatch warning, got {len(tiff_warnings)}"


def test_detector_bitDepth_property():
    """Test that detector bitDepth property returns correct values.
    
    Phase 1, Task 1: bitDepth should return int(dtype.itemsize * 8).
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    # uint16 detector should report 16 bits
    assert detectorsManager[detectorName].bitDepth == 16, \
        "uint16 detector should report bitDepth=16"
    
    # Create a mock float32 detector by patching dtype
    detector = detectorsManager[detectorName]
    original_dtype = detector.dtype
    
    # Temporarily override dtype to test bitDepth calculation
    class MockFloat32Detector:
        @property
        def dtype(self):
            return np.dtype(np.float32)
        
        @property
        def bitDepth(self):
            return int(np.dtype(self.dtype).itemsize * 8)
    
    mock_detector = MockFloat32Detector()
    assert mock_detector.bitDepth == 32, \
        "float32 detector should report bitDepth=32"


# ---------------------------------------------------------------------------
# Off-thread writer (Phase 1 Task 3) tests.
#
# Design note: the off-thread writer is now in the DEFAULT recording path, so
# the existing recording integration tests (test_recording_spec_frames,
# test_recording_spec_time, lapse, multi-detector, ...) already exercise it
# end-to-end. We therefore avoid adding new slow qtbot/mock-driven integration
# tests here (they run on every CI push). Instead:
#   - one fast direct-storer test that compression survives the batched-chunk
#     change, and
#   - deterministic WriterThread unit tests for the concurrency guarantees
#     (FIFO order, no-drop, backpressure, openStream-failure propagation) using
#     a controllable fake storer. The mock detector emits RANDOM frames at a
#     time-driven rate and the real writer outpaces it, so order/backpressure
#     are not observable through the integration path anyway.
# ---------------------------------------------------------------------------

from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer, StreamPayloadInfo, TiffStorer, WriterThread,
    WRITER_QUEUE_MAX_BYTES,
    WRITE_BATCH_FRAMES,
)


def test_offthread_writer_compression_enabled(tmp_path):
    """Compression is still applied after the batched multi-frame chunk change.

    Direct-storer test (no qtbot / no time-driven mock) so it stays CI-fast.
    """
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'test_compression.h5')

    storer = HDF5Storer(path, detectorsManager)
    frames = np.random.randint(0, 1000, (5, 64, 64), dtype=np.uint16)
    storer.openStream(
        fileDests={detectorName: path}, detectorNames=[detectorName],
        shapes={detectorName: (64, 64)}, attrs={detectorName: {}},
        singleMultiDetectorFile=False, singleLapseFile=False, saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream({detectorName: 5}, {detectorName: path}, None, SaveMode.Disk)

    with h5py.File(path, 'r') as f:
        dataset = f[detectorName]['data']
        assert dataset.compression is not None, "Compression must remain enabled for disk mode"
        assert dataset.shape[0] == 5


class _FakeStorer:
    """Controllable storer for deterministic WriterThread unit tests.

    Records, in arrival order, every batch handed to writeFrames so tests can
    assert FIFO ordering and that no frame is dropped. Optionally raises on
    openStream (failure-propagation test) or sleeps per write (backpressure).
    """
    def __init__(self, openStreamError=None, writeDelay=0.0, payloadInfo=None):
        self._openStreamError = openStreamError
        self._writeDelay = writeDelay
        self.opened = False
        self.finalized = False
        self.aborted = False
        self.writes = {}  # detectorName -> list of received batch arrays
        self.payloadInfo = payloadInfo

    def openStream(self, **kwargs):
        if self._openStreamError is not None:
            raise self._openStreamError
        self.opened = True

    def writeFrames(self, detectorName, frames):
        if self._writeDelay:
            time.sleep(self._writeDelay)
        self.writes.setdefault(detectorName, []).append(np.asarray(frames))

    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        self.finalized = True

    def streamPayloadInfo(self, detectorName, currentFrames):
        return self.payloadInfo

    def abortStream(self, filePaths, fileDests, saveMode):
        self.aborted = True


def _make_writer(storer, detectorNames=('CAM',)):
    detectorNames = list(detectorNames)
    return WriterThread(
        storer=storer,
        fileDests={d: f'{d}.h5' for d in detectorNames},
        detectorNames=detectorNames,
        shapes={d: (2, 2) for d in detectorNames},
        attrs={d: {} for d in detectorNames},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={d: f'{d}.h5' for d in detectorNames},
        recordingManager=None,
    )


def test_writerthread_openstream_failure_propagates():
    """openStream failure is re-raised on the caller via wait_for_open, no hang."""
    storer = _FakeStorer(openStreamError=OSError("boom"))
    writer = _make_writer(storer)
    writer.start()
    with pytest.raises(OSError, match="boom"):
        writer.wait_for_open()
    writer.join(timeout=5.0)
    assert not writer.is_alive(), "Writer thread must terminate after openStream failure"
    assert not storer.finalized, "finalizeStream must not run if openStream failed"


def test_writerthread_preserves_order_and_count():
    """All enqueued frames reach the storer exactly once, in FIFO order."""
    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    n = WRITE_BATCH_FRAMES * 3 + 5  # multiple full batches + partial final
    for i in range(n):
        # Tag each frame with its index so order is verifiable.
        writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
    writer.finish()

    assert storer.finalized, "finalizeStream must run after the sentinel"
    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == n, f"Expected {n} frames, got {len(received)} (drops/dupes)"
    for i in range(n):
        assert (received[i] == i).all(), f"Frame {i} out of order"


def test_writerthread_publishes_exact_locator_after_finalization():
    """The terminal locator describes the saved raw scan, not its preview."""
    storer = _FakeStorer(payloadInfo=StreamPayloadInfo(
        group='scan0/APDred',
        stored_shape=(1, 2, 5, 7, 11),
        frame_axis_stored=True,
    ))

    class _Manager:
        def __init__(self):
            self.published = None

        def registerPayloadLocators(self, generation, locators):
            assert storer.finalized
            self.published = (generation, locators)

    manager = _Manager()
    writer = WriterThread(
        storer=storer,
        fileDests={'APDred': 'tile.h5'},
        detectorNames=['APDred'],
        shapes={'APDred': (7, 11)},
        attrs={'APDred': {}},
        singleMultiDetectorFile=False,
        singleLapseFile=True,
        saveMode=SaveMode.Disk,
        filePaths={'APDred': 'tile.h5'},
        recordingManager=manager,
        recordingGeneration=23,
        recordingMode=MODE_SCAN,
        scanDims=(11, 7, 5),
        scanDrivenDetectors={'APDred': True},
    )
    writer.start()
    writer.wait_for_open()
    writer.enqueue_frames(
        'APDred', np.zeros((1, 2, 5, 7, 11), dtype=np.uint16)
    )
    writer.finish()

    generation, locators = manager.published
    locator = locators['APDred']
    assert generation == 23
    assert locator.group == 'scan0/APDred'
    assert locator.axes == 'CZYX'
    assert locator.stored_axes == 'TCZYX'
    assert locator.shape == (2, 5, 7, 11)
    assert locator.stored_shape == (1, 2, 5, 7, 11)


def test_writerthread_keeps_normal_single_camera_recording_logically_yx():
    """A container's singleton frame wrapper must not become captured time."""
    storer = _FakeStorer(payloadInfo=StreamPayloadInfo(
        group='Camera', stored_shape=(1, 7, 11), frame_axis_stored=True,
    ))

    class _Manager:
        def registerPayloadLocators(self, generation, locators):
            self.locator = locators['Camera']

    manager = _Manager()
    writer = WriterThread(
        storer=storer,
        fileDests={'Camera': 'camera.h5'},
        detectorNames=['Camera'],
        shapes={'Camera': (7, 11)},
        attrs={'Camera': {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={'Camera': 'camera.h5'},
        recordingManager=manager,
        recordingGeneration=24,
        recordingMode=MODE_TIMELAPSE,
    )
    writer.start()
    writer.wait_for_open()
    writer.enqueue_frames(
        'Camera', np.zeros((1, 7, 11), dtype=np.uint16)
    )
    writer.finish()

    assert manager.locator.axes == 'YX'
    assert manager.locator.stored_axes == 'TYX'
    assert manager.locator.shape == (7, 11)
    assert manager.locator.stored_shape == (1, 7, 11)


def test_writerthread_backpressure_no_drop():
    """A slow storer fills the queue's budget; blocking must not drop frames.

    The budget is bytes, so the frames are sized to fill it rather than
    counted: bounding by queue *items* meant the buffer was whatever one poll
    happened to return -- about one frame on a fast camera, so seven
    milliseconds where the constant implied seconds.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import _framesNBytes

    storer = _FakeStorer(writeDelay=0.003)  # writer slower than producer
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    frame = np.zeros((1, 512, 512), dtype=np.uint16)
    # Twice the budget's worth, so enqueue must block and wait for the writer.
    n = 2 * (WRITER_QUEUE_MAX_BYTES // _framesNBytes(frame)) + 10
    for i in range(n):
        writer.enqueue_frames('CAM', np.full((1, 512, 512), i % 4096, dtype=np.uint16))
    writer.finish()

    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == n, f"Backpressure dropped frames: expected {n}, got {len(received)}"
    for i in range(n):
        assert (received[i] == i % 4096).all(), f"Frame {i} out of order under backpressure"


def test_the_writer_buffer_is_a_memory_bound_not_an_item_count():
    """One queued item is one poll's worth of frames, which is not a size."""
    from imswitch.imcontrol.model.managers.RecordingManager import _framesNBytes

    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()
    try:
        # A single chunk larger than the whole budget is admitted rather than
        # deadlocking on room that can never appear.
        huge = np.zeros((1, 1, WRITER_QUEUE_MAX_BYTES // 2 + 1024), dtype=np.uint16)
        assert _framesNBytes(huge) > WRITER_QUEUE_MAX_BYTES
        writer.enqueue_frames('CAM', huge)
    finally:
        writer.finish()

    assert len(storer.writes['CAM']) == 1


def test_writerthread_abort_calls_abortstream_not_finalize():
    """abort() discards via abortStream and never finalizes the stream."""
    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    for i in range(WRITE_BATCH_FRAMES * 2):
        writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
    writer.abort()

    writer.join(timeout=5.0)
    assert not writer.is_alive(), "Writer must terminate after abort"
    assert storer.aborted is True, "abortStream must be called on abort"
    assert storer.finalized is False, "finalizeStream must NOT be called on abort"


def test_writerthread_late_abort_after_finalize_removes_output():
    class _AbortDuringFinalizeStorer(_FakeStorer):
        def __init__(self):
            super().__init__()
            self.writer = None

        def finalizeStream(
                self, currentFrames, filePaths, recordingManager, saveMode):
            self.finalized = True
            # Deterministically model abort() arriving while a slow backend is
            # returning from finalizeStream.
            self.writer._abort_event.set()

    storer = _AbortDuringFinalizeStorer()
    writer = _make_writer(storer)
    storer.writer = writer
    writer.start()
    writer.wait_for_open()
    writer.finish()

    assert storer.finalized is True
    assert storer.aborted is True
    assert writer._stream_cleanup_done.is_set()


def test_tiff_finalize_attempts_every_close_and_metadata_write(monkeypatch):
    """One TIFF failure must not prevent cleanup/finalization attempts for peers."""
    import importlib
    from types import SimpleNamespace

    recording_module = importlib.import_module(
        'imswitch.imcontrol.model.managers.RecordingManager'
    )
    close_attempts = []
    metadata_attempts = []

    class _Writer:
        def __init__(self, name, error=None):
            self.name = name
            self.error = error

        def close(self):
            close_attempts.append(self.name)
            if self.error is not None:
                raise self.error

    def write_metadata(path, _xml):
        metadata_attempts.append(path)
        if path == 'B.ome.tiff':
            raise OSError('metadata flush failed')

    monkeypatch.setattr(
        recording_module._ome, 'build_ome_xml',
        lambda _meta, _shape: '<OME/>',
    )
    monkeypatch.setattr(recording_module.tiff, 'tiffcomment', write_metadata)

    storer = TiffStorer.__new__(TiffStorer)
    storer._writers = {
        'A': _Writer('A', OSError('close failed')),
        'B': _Writer('B'),
    }
    storer._paths = {
        'A': 'A.ome.tiff',
        'B': 'B.ome.tiff',
    }
    storer._spatial = {
        'A': (2, 2),
        'B': (2, 2),
    }
    storer._meta_for = lambda _name, n_frames: SimpleNamespace(axes='YX')

    with pytest.raises(RuntimeError) as exc_info:
        storer.finalizeStream(
            {'A': 1, 'B': 1},
            storer._paths,
            recordingManager=None,
            saveMode=SaveMode.Disk,
        )

    assert close_attempts == ['A', 'B']
    assert metadata_attempts == ['A.ome.tiff', 'B.ome.tiff']
    message = str(exc_info.value)
    assert 'close failed' in message
    assert 'metadata flush failed' in message
    assert '2 error(s)' in message


def test_tiff_writer_finalize_failure_aborts_partial_output(tmp_path):
    """A swallowed TiffWriter.close error must become writer failure + abort."""
    partial_path = tmp_path / 'partial.ome.tiff'

    class _FailingWriter:
        def __init__(self):
            self.closeCalls = 0

        def close(self):
            self.closeCalls += 1
            raise OSError('disk flush failed')

    class _FailingTiffStorer(TiffStorer):
        def __init__(self):
            self.writer = _FailingWriter()
            self._writers = {}
            self._paths = {}
            self._spatial = {}

        def openStream(self, **_kwargs):
            partial_path.write_bytes(b'partial TIFF')
            self._writers = {'CAM': self.writer}

        def writeFrames(self, detectorName, frames):
            pass

    storer = _FailingTiffStorer()
    writer = WriterThread(
        storer=storer,
        fileDests={'CAM': str(partial_path)},
        detectorNames=['CAM'],
        shapes={'CAM': (2, 2)},
        attrs={'CAM': {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
        filePaths={'CAM': str(partial_path)},
        recordingManager=None,
    )
    writer.start()
    writer.wait_for_open()

    with pytest.raises(RuntimeError, match='TIFF stream finalization failed'):
        writer.finish()

    assert not writer.is_alive()
    assert storer.writer.closeCalls >= 2
    assert not partial_path.exists()


def test_finalize_failure_emits_failure_without_success_and_removes_output(
        qtbot, tmp_path):
    """RecordingManager publishes only failure after writer finalization fails."""
    class _ImmediateDetector:
        shape = (2, 2)
        dtype = np.dtype(np.uint16)
        pixelSizeUm = [1.0, 1.0, 1.0]

        def __init__(self):
            self._delivered = False

        def startChunkConsumer(self, _consumerKey):
            self._delivered = False

        def releaseChunkConsumer(self, _consumerKey):
            pass

        def readChunk(self, _consumerKey):
            if self._delivered:
                return []
            self._delivered = True
            return [np.ones((2, 2), dtype=self.dtype)]

    class _ImmediateDetectors:
        def __init__(self):
            self.detector = _ImmediateDetector()

        def __getitem__(self, detectorName):
            assert detectorName == 'CAM'
            return self.detector

        def acquire(self, detectorNames, purpose):
            assert tuple(detectorNames) == ('CAM',)
            return object()

        def release(self, _handle):
            pass

    class _FinalizeFailingStorer:
        instances = []

        def __init__(self, _filepath, _detectorManager):
            self.paths = ()
            self.aborted = False
            self.__class__.instances.append(self)

        def openStream(self, *, fileDests, **_kwargs):
            self.paths = tuple(fileDests.values())
            for path in self.paths:
                with open(path, 'wb') as partial:
                    partial.write(b'partial recording')

        def writeFrames(self, detectorName, frames):
            pass

        def finalizeStream(self, currentFrames, filePaths,
                           recordingManager, saveMode):
            raise OSError('finalize failed')

        def abortStream(self, filePaths, fileDests, saveMode):
            self.aborted = True
            for path in set(filePaths.values()) | set(fileDests.values()):
                if isinstance(path, str) and os.path.exists(path):
                    os.remove(path)

    manager = RecordingManager(
        _ImmediateDetectors(),
        storerMap={SaveFormat.TIFF: _FinalizeFailingStorer},
    )
    successes = []
    legacySuccesses = []
    failures = []
    manager.sigRecordingEndedDetailed.connect(successes.append)
    manager.sigRecordingEnded.connect(lambda: legacySuccesses.append(True))
    manager.sigRecordingFailedDetailed.connect(
        lambda message, generation: failures.append((message, generation))
    )

    with qtbot.waitSignal(
        manager.sigRecordingFailedDetailed, timeout=5000
    ):
        generation = manager.startRecording(
            detectorNames=['CAM'],
            recMode=RecMode.SpecFrames,
            savename=str(tmp_path / 'finalize_failure'),
            saveMode=SaveMode.Disk,
            saveFormat=SaveFormat.TIFF,
            attrs={'CAM': {}},
            recFrames=1,
        )

    manager.endRecording(emitSignal=False, wait=True)
    storer = _FinalizeFailingStorer.instances[-1]
    assert len(failures) == 1
    assert failures[0][1] == generation
    assert 'partial output was aborted' in failures[0][0]
    assert successes == []
    assert legacySuccesses == []
    assert storer.aborted is True
    assert all(not os.path.exists(path) for path in storer.paths)


def test_hdf5storer_abort_removes_partial_file(tmp_path):
    """HDF5Storer.abortStream closes handles and deletes the partial file."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'aborted.h5')

    storer = HDF5Storer(path, detectorsManager)
    storer.openStream(
        fileDests={detectorName: path}, detectorNames=[detectorName],
        shapes={detectorName: (8, 8)}, attrs={detectorName: {}},
        singleMultiDetectorFile=False, singleLapseFile=False, saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, np.random.randint(0, 100, (3, 8, 8), dtype=np.uint16))
    assert os.path.exists(path), "file should exist before abort"

    storer.abortStream({detectorName: path}, {detectorName: path}, SaveMode.Disk)
    assert not os.path.exists(path), "abortStream must delete the partial file"


def test_recording_abort_discards_file(qtbot, tmp_path):
    """End-to-end: aborting an UntilStop recording leaves no file on disk."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    savename = str(tmp_path / 'aborted')
    detectorName = list(detectorInfosBasic.keys())[0]

    recordingManager.startRecording(
        detectorNames=[detectorName], recMode=RecMode.UntilStop, savename=savename,
        saveMode=SaveMode.Disk, saveFormat=SaveFormat.HDF5, attrs={detectorName: {}},
    )
    qtbot.wait(300)  # let some frames stream through the writer
    recordingManager.abortRecording(wait=True)

    assert not os.path.exists(f'{savename}_{detectorName}.hdf5'), \
        "aborted recording must not leave a file on disk"


def test_zarr_streaming_recording_metadata(tmp_path):
    """Zarr streaming datasets expose live-source recording metadata."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    detectorName = list(detectorInfosBasic.keys())[0]
    path = str(tmp_path / 'live_metadata.zarr')
    storer = ZarrStorer(path, detectorsManager)
    frames = np.random.randint(0, 100, (4, 8, 8), dtype=np.uint16)

    storer.openStream(
        fileDests={detectorName: path},
        detectorNames=[detectorName],
        shapes={detectorName: (8, 8)},
        attrs={detectorName: {
            'recording:expected_frames': 4,
            'recording:frames_per_stack': 4,
            'recording:num_timepoints': 1,
            'recording:lapse_index': 0,
            'recording:single_lapse_file': False,
            'custom:note': 'kept-in-metadata-group',
        }},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames(detectorName, frames)
    storer.finalizeStream(
        currentFrames={detectorName: 4},
        filePaths={detectorName: path},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )

    root = zarr.open(path, mode='r')
    dataset = root[detectorName]['data']
    assert dataset.attrs['recording:detector_name'] == detectorName
    assert dataset.attrs['recording:source_format'] == 'ZARR'
    assert dataset.attrs['recording:expected_frames'] == 4
    assert dataset.attrs['recording:frames_per_stack'] == 4
    assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
    # Non-lapse recordings should have default lapse metadata
    assert dataset.attrs['recording:num_timepoints'] == 1
    assert dataset.attrs['recording:lapse_index'] == 0
    assert dataset.attrs['recording:single_lapse_file'] == False
    assert root[detectorName]['metadata']['custom'].attrs['note'] == 'kept-in-metadata-group'


def test_hdf5_streaming_swmr_readable(tmp_path):
    """Test that HDF5 streaming recordings are SWMR-readable with recording:* metadata."""
    from imswitch.improcess.live import Hdf5LiveSource
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer
    
    detectorName = 'TestCam'
    detectorInfos = {detectorName: DetectorInfo(
        analogChannel=None,
        digitalLine=3,
        managerName='HamamatsuManager',
        managerProperties={
            'cameraListIndex': 'mock',
            'hamamatsu': {
                'readout_speed': 3,
                'trigger_global_exposure': 5,
                'trigger_active': 2,
                'trigger_polarity': 2,
                'exposure_time': 0.01,
                'trigger_source': 1,
                'subarray_hpos': 0,
                'subarray_vpos': 0,
                'subarray_hsize': 5,
                'subarray_vsize': 4,
                'image_width': 5,
                'image_height': 4
            }
        },
        forAcquisition=True
    )}
    
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    path = tmp_path / f'swmr_test_{detectorName}.h5'
    
    storer = HDF5Storer(str(path), detectorsManager)
    
    attrs = {
        detectorName: {
            'recording:frames_per_stack': 10,
            'recording:expected_frames': 10,
            'custom:metadata': 'test_value'
        }
    }
    
    storer.openStream(
        fileDests={detectorName: str(path)},
        detectorNames=[detectorName],
        shapes={detectorName: (4, 5)},
        attrs=attrs,
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    
    # Write frames in batches
    frames1 = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    storer.writeFrames(detectorName, frames1)
    
    frames2 = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5) + 100
    storer.writeFrames(detectorName, frames2)
    
    # Verify SWMR-readable before finalize
    with h5py.File(path, 'r', libver='latest', swmr=True) as f:
        assert f.swmr_mode is True
        dataset = f[detectorName]['data']
        assert dataset.shape[0] == 6
        assert dataset.attrs['writing'] == True
        assert dataset.attrs['recording:detector_name'] == detectorName
        assert dataset.attrs['recording:source_format'] == 'HDF5'
        assert dataset.attrs['recording:expected_frames'] == 10
        assert dataset.attrs['recording:frames_per_stack'] == 10
        assert dataset.attrs['recording:dataset_path'] == f'/{detectorName}/data'
    
    # Test with Hdf5LiveSource while writing=True
    source = Hdf5LiveSource(detector_name=detectorName)
    info = source.open(path)
    assert info.expected_frames == 10
    assert info.detector_name == detectorName
    assert info.source_format == 'HDF5'
    
    chunks = source.poll()
    assert len(chunks) > 0
    total_frames = sum(c.end - c.start for c in chunks)
    assert total_frames == 6
    assert not source.is_complete()  # writing=True and cursor < expected_frames
    source.close()
    
    # Finalize
    storer.finalizeStream(
        currentFrames={detectorName: 6},
        filePaths={detectorName: str(path)},
        recordingManager=None,
        saveMode=SaveMode.Disk,
    )
    
    # Verify writing=False after finalize
    with h5py.File(path, 'r', libver='latest', swmr=True) as f:
        dataset = f[detectorName]['data']
        assert dataset.attrs['writing'] == False
        assert dataset.shape[0] == 6
    
    # Test with Hdf5LiveSource after finalize
    source2 = Hdf5LiveSource(detector_name=detectorName)
    source2.open(path)
    chunks2 = source2.poll()
    total_frames2 = sum(c.end - c.start for c in chunks2)
    assert total_frames2 == 6
    assert source2.is_complete()  # writing=False and all frames read
    source2.close()


def test_worker_lapse_metadata_augmentation():
    """Test that RecordingWorker._augment_attrs_with_recording_metadata adds lapse fields."""
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    worker = recordingManager._RecordingManager__recordingWorker
    
    # Configure worker as if it were a ScanLapse recording
    worker.saveFormat = SaveFormat.ZARR
    worker.recLapseTotal = 3
    worker.recLapseIndex = 1
    worker.singleLapseFile = True
    
    detectorName = list(detectorInfosBasic.keys())[0]
    input_attrs = {detectorName: {'user_key': 'user_value'}}
    expected_frames = {detectorName: 10}
    
    augmented = worker._augment_attrs_with_recording_metadata(input_attrs, expected_frames)
    
    assert detectorName in augmented
    attrs = augmented[detectorName]
    
    # Check that lapse metadata was added
    assert attrs['recording:num_timepoints'] == 3
    assert attrs['recording:lapse_index'] == 1
    assert attrs['recording:single_lapse_file'] == True
    
    # Check that other metadata was also added
    assert attrs['recording:detector_name'] == detectorName
    assert attrs['recording:source_format'] == 'ZARR'
    assert attrs['recording:expected_frames'] == 10
    assert attrs['recording:frames_per_stack'] == 10
    
    # Check that user attrs were preserved
    assert attrs['user_key'] == 'user_value'
    
    # Test with default values (non-lapse)
    worker.recLapseTotal = None
    worker.recLapseIndex = None
    worker.singleLapseFile = False
    
    augmented_default = worker._augment_attrs_with_recording_metadata(input_attrs, expected_frames)
    attrs_default = augmented_default[detectorName]
    
    assert attrs_default['recording:num_timepoints'] == 1
    assert attrs_default['recording:lapse_index'] == 0
    assert attrs_default['recording:single_lapse_file'] == False


def test_lapse_metadata_end_to_end(qtbot):
    """Test that lapse metadata flows through to HDF5 file via RecordingManager."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    detectorName = list(detectorInfosBasic.keys())[0]
    
    filePerDetector, savedToDiskPerDetector = {}, {}

    def memoryRecordingAvailable(_, file, __, savedToDisk, detName):
        nonlocal filePerDetector, savedToDiskPerDetector
        filePerDetector[detName], savedToDiskPerDetector[detName] = file, savedToDisk
        return True

    # Start a recording with explicit lapse parameters
    recordingManager.startRecording(
        detectorNames=[detectorName],
        recMode=RecMode.SpecFrames,
        savename='test_lapse_metadata',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {'test_attr': 'test_value'}},
        recFrames=5,
        recLapseTotal=4,
        recLapseIndex=2,
        singleLapseFile=True,
    )
    
    with qtbot.waitSignals(
        [recordingManager.sigMemoryRecordingAvailable],
        check_params_cbs=[lambda *args, **kwargs: memoryRecordingAvailable(*args, detName=detectorName, **kwargs)],
        timeout=30000
    ):
        pass

    assert detectorName in filePerDetector
    file = filePerDetector[detectorName]
    h5pyFile = h5py.File(file)
    
    detector_group = h5pyFile.get(detectorName)
    assert detector_group is not None
    dataset = detector_group.get('data')
    assert dataset is not None
    
    # Verify lapse metadata
    assert dataset.attrs['recording:num_timepoints'] == 4
    assert dataset.attrs['recording:lapse_index'] == 2
    assert dataset.attrs['recording:single_lapse_file'] == True
    
    # Verify other recording metadata still works
    assert dataset.attrs['recording:detector_name'] == detectorName
    assert dataset.attrs['recording:expected_frames'] == 5
    
    h5pyFile.close()
    file.close()


def test_waitForAcquisitionStarted_blocks_until_armed(qtbot):
    """Verify that waitForAcquisitionStarted blocks until arming completes."""
    import threading
    from unittest.mock import patch
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    block_event = threading.Event()
    original_acquire = detectorsManager.acquire

    def blocking_acquire(detectorNames, purpose):
        block_event.wait()
        return original_acquire(detectorNames, purpose)

    try:
        with patch.object(detectorsManager, 'acquire', side_effect=blocking_acquire):
            recordingManager.startRecording(
                detectorNames=list(detectorInfosBasic.keys()),
                recMode=RecMode.SpecFrames,
                savename='test_arming_block',
                saveMode=SaveMode.RAM,
                attrs={name: {} for name in detectorInfosBasic.keys()},
                recFrames=5
            )
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=0.2)
            assert armed is False, "Should timeout while detector is blocked"
            
            block_event.set()
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=2.0)
            assert armed is True, "Should return True after detector is unblocked"
    finally:
        block_event.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_waitForAcquisitionStarted_waits_for_stream_open():
    """Scan gating must wait until the writer stream and chunk consumer exist."""
    import threading

    open_started = threading.Event()
    release_open = threading.Event()

    class BlockingStorer:
        def __init__(self, filepath, detectorManager):
            self.filepath = filepath
            self.detectorManager = detectorManager

        def openStream(self, *args, **kwargs):
            open_started.set()
            release_open.wait()

        def writeFrames(self, detectorName, frames):
            pass

        def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
            pass

        def abortStream(self, filePaths, fileDests, saveMode):
            pass

    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(
        detectorsManager,
        storerMap={SaveFormat.HDF5: BlockingStorer},
    )

    try:
        recordingManager.startRecording(
            detectorNames=list(detectorInfosBasic.keys()),
            recMode=RecMode.SpecFrames,
            savename='test_stream_ready_block',
            saveMode=SaveMode.RAM,
            attrs={name: {} for name in detectorInfosBasic.keys()},
            recFrames=1,
        )

        assert open_started.wait(2.0)
        assert recordingManager.waitForAcquisitionStarted(timeout=0.1) is False

        release_open.set()
        assert recordingManager.waitForAcquisitionStarted(timeout=2.0) is True
    finally:
        release_open.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_abortRecording_releases_open_writer_without_frames():
    """Abort cleanup must wake a writer waiting on an empty frame queue."""
    import numpy as np

    class EmptyDetector:
        forAcquisition = True
        shape = (4, 4)
        dtype = np.dtype(np.uint16)
        pixelSizeUm = [1, 1, 1]

        def startAcquisition(self):
            pass

        def stopAcquisition(self):
            pass

        def flushBuffers(self):
            pass

        def releaseChunkConsumer(self, consumerKey):
            pass

        def readChunk(self, consumerKey):
            return []

    class EmptyDetectorsManager:
        def __init__(self):
            self.detector = EmptyDetector()

        def __getitem__(self, detectorName):
            return self.detector

        def execOnAll(self, func, *, condition=None):
            condition = condition or (lambda detector: True)
            if condition(self.detector):
                return {"Empty": func(self.detector)}
            return {}

        def getAllDeviceNames(self, condition=None):
            if condition is None or condition(self.detector):
                return ["Empty"]
            return []

        def acquire(self, detectorNames, purpose):
            self.detector.startAcquisition()
            return object()

        def release(self, handle):
            self.detector.stopAcquisition()

    class NoopStorer:
        def __init__(self, filepath, detectorManager):
            self.filepath = filepath
            self.detectorManager = detectorManager

        def openStream(self, *args, **kwargs):
            pass

        def writeFrames(self, detectorName, frames):
            pass

        def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
            pass

        def abortStream(self, filePaths, fileDests, saveMode):
            pass

    recordingManager = RecordingManager(
        EmptyDetectorsManager(),
        storerMap={SaveFormat.HDF5: NoopStorer},
    )

    recordingManager.startRecording(
        detectorNames=["Empty"],
        recMode=RecMode.SpecFrames,
        savename="test_abort_open_writer",
        saveMode=SaveMode.RAM,
        attrs={"Empty": {}},
        recFrames=1,
        stallTimeout=5.0,
    )
    assert recordingManager.waitForAcquisitionStarted(timeout=2.0) is True

    recordingManager.abortRecording(emitSignal=False, wait=True)

    assert recordingManager.record is False


def test_waitForAcquisitionStarted_fast_arming(qtbot):
    """Verify that waitForAcquisitionStarted returns True in normal fast-arming path."""
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    try:
        recordingManager.startRecording(
            detectorNames=list(detectorInfosBasic.keys()),
            recMode=RecMode.SpecFrames,
            savename='test_arming_fast',
            saveMode=SaveMode.RAM,
            attrs={name: {} for name in detectorInfosBasic.keys()},
            recFrames=5
        )
        
        armed = recordingManager.waitForAcquisitionStarted(timeout=2.0)
        assert armed is True, "Should return True for fast-arming detector"
    finally:
        recordingManager.abortRecording(emitSignal=False, wait=True)


def test_waitForAcquisitionStarted_timeout(qtbot):
    """Verify that waitForAcquisitionStarted returns False on timeout."""
    import threading
    from unittest.mock import patch
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    block_event = threading.Event()
    original_acquire = detectorsManager.acquire

    def never_finishing_acquire(detectorNames, purpose):
        block_event.wait()
        return original_acquire(detectorNames, purpose)

    try:
        with patch.object(detectorsManager, 'acquire', side_effect=never_finishing_acquire):
            recordingManager.startRecording(
                detectorNames=list(detectorInfosBasic.keys()),
                recMode=RecMode.SpecFrames,
                savename='test_arming_timeout',
                saveMode=SaveMode.RAM,
                attrs={name: {} for name in detectorInfosBasic.keys()},
                recFrames=5
            )
            
            armed = recordingManager.waitForAcquisitionStarted(timeout=0.1)
            assert armed is False, "Should return False on timeout"
    finally:
        block_event.set()
        recordingManager.abortRecording(emitSignal=False, wait=True)


# Copyright (C) 2020-2021 ImSwitch developers
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


def test_the_writer_is_waited_on_while_it_is_still_draining():
    """A fixed shutdown deadline asserts a disk speed nobody declared.

    With a 512 MiB queue budget, "finish within 30 seconds" means "write at
    least 17 MiB/s" -- and missing it did not merely warn: finish() raised,
    the worker's cleanup aborted, and abortStream deleted a recording whose
    frames had all been captured. A writer that is still draining is given as
    long as it needs; only one that writes nothing at all is declared stuck.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import (
        WRITER_STALL_TIMEOUT_S,
    )

    # Slow enough that a fixed 30 s deadline would have been a coin flip, but
    # always making progress.
    storer = _FakeStorer(writeDelay=0.002)
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()

    for i in range(400):
        writer.enqueue_frames('CAM', np.full((1, 64, 64), i % 4096, dtype=np.uint16))
    writer.finish()

    assert not writer.is_alive()
    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == 400, 'a slow but progressing writer lost frames'
    assert WRITER_STALL_TIMEOUT_S > 0


# ----------------------------------------------------------------------
# The stall watchdog reads the detector's own cadence
# ----------------------------------------------------------------------

class _PacedCamera:
    """A healthy camera that delivers one frame per ``period`` seconds.

    ``declare`` publishes that period the way a real manager does, as a
    detector parameter with a unit -- the same parameter the OME metadata
    reads -- so the watchdog can know what cadence to expect.
    """
    shape = (4, 4)
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.1, 0.1]
    isScanDriven = False

    def __init__(self, period, declare):
        from types import SimpleNamespace

        self._period = period
        self._t0 = None
        self._delivered = 0
        self.parameters = (
            {'Set exposure time': SimpleNamespace(value=period, valueUnits='s')}
            if declare else {}
        )

    def startAcquisition(self):
        self._t0 = time.time()

    def stopAcquisition(self):
        pass

    def startChunkConsumer(self, _key, kind=None):
        pass

    def releaseChunkConsumer(self, _key):
        pass

    def readChunk(self, _key):
        if self._t0 is None:
            return []
        due = int((time.time() - self._t0) // self._period)
        if due > self._delivered:
            self._delivered += 1
            return [np.zeros(self.shape, dtype=self.dtype)]
        return []


class _OneCamera:
    def __init__(self, camera):
        self.cam = camera

    def __getitem__(self, name):
        return self.cam

    def acquire(self, detectorNames, purpose):
        self.cam.startAcquisition()
        return object()

    def release(self, _handle):
        self.cam.stopAcquisition()


def _record_paced_camera(qtbot, tmp_path, camera, stallTimeout):
    from imswitch.imcontrol.model.managers.RecordingManager import (
        RecMode, RecordingManager, SaveFormat, SaveMode,
    )

    manager = RecordingManager(_OneCamera(camera))
    failures, ended = [], []
    manager.sigRecordingFailed.connect(failures.append)
    manager.sigRecordingEndedDetailed.connect(ended.append)
    manager.startRecording(
        detectorNames=['Cam'],
        recMode=RecMode.SpecFrames,
        savename=str(tmp_path / 'paced'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'Cam': {}},
        recFrames=3,
        stallTimeout=stallTimeout,
    )
    # The worker reports on its own thread; its signals reach this one only
    # while events are being processed, which qtbot's wait does and a sleep
    # loop does not.
    try:
        qtbot.waitUntil(lambda: bool(failures or ended), timeout=6000)
    except Exception:
        pass
    manager.endRecording(emitSignal=False, wait=True)
    return failures, ended


def test_a_slow_camera_that_declares_its_exposure_is_not_a_stall(qtbot, tmp_path):
    """The allowance follows the cadence the detector itself publishes.

    A flat wall-clock timeout killed any exposure longer than itself at frame
    zero, deleted the file, and blamed scan TTL wiring in a mode with no scan.
    The camera here frames slower than the floor and says so; it must finish.
    """
    # Floor 0.2 s, exposure 0.4 s: allowance becomes 3 x 0.4 = 1.2 s.
    failures, ended = _record_paced_camera(
        qtbot, tmp_path, _PacedCamera(period=0.4, declare=True), stallTimeout=0.2
    )

    assert not failures, failures
    assert ended


def test_a_slow_camera_that_declares_nothing_keeps_the_floor(qtbot, tmp_path):
    """Nothing declared, nothing derived: the flat floor applies as before.

    This is the same camera without the parameter, and it is the control that
    shows the declaration is what made the difference above.
    """
    failures, _ended = _record_paced_camera(
        qtbot, tmp_path, _PacedCamera(period=0.4, declare=False), stallTimeout=0.2
    )

    assert failures and 'stalled' in failures[0]
    # And a non-scan mode is told about exposure, not about scan wiring.
    assert 'numCamTTL' not in failures[0]
    assert 'exposure' in failures[0]


# ----------------------------------------------------------------------
# Frames reach disk on time, not only on count
# ----------------------------------------------------------------------

def test_fewer_frames_than_a_batch_are_written_before_finalize():
    """A batch flushes on age as well as on count.

    With count as the only trigger, the file held nothing until either 32
    frames had arrived or the recording finalized: 32 seconds of staleness at
    one frame per second, and an empty file for the whole of any recording
    shorter than the batch.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import (
        WRITE_BATCH_FRAMES, WRITE_FLUSH_MAX_LATENCY_S,
    )

    storer = _FakeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()
    try:
        for i in range(5):
            writer.enqueue_frames('CAM', np.full((1, 2, 2), i, dtype=np.uint16))
        assert 5 < WRITE_BATCH_FRAMES

        deadline = time.time() + 4 * WRITE_FLUSH_MAX_LATENCY_S + 1.0
        while time.time() < deadline and not storer.writes.get('CAM'):
            time.sleep(0.02)

        assert storer.writes.get('CAM'), (
            'five frames sat in the batch with nothing on disk; only count '
            'was flushing'
        )
        assert not storer.finalized
    finally:
        writer.finish()

    received = np.concatenate(storer.writes['CAM'], axis=0)
    assert len(received) == 5

# ---------------------------------------------------------------------------
# Progress-aware writer drain: a slow disk is not a stall (rig regression:
# stopping a long scan recording aborted and deleted the file after 30 s).
# ---------------------------------------------------------------------------

def _enqueue_chunks(writer, nChunks, framesPerChunk=4):
    for _ in range(nChunks):
        writer.enqueue_frames('CAM', np.zeros((framesPerChunk, 2, 2), dtype=np.uint16))


def test_writer_finish_waits_for_a_slow_but_progressing_drain(monkeypatch):
    import importlib
    rm = importlib.import_module('imswitch.imcontrol.model.managers.RecordingManager')
    monkeypatch.setattr(rm, 'WRITER_STALL_TIMEOUT_S', 0.3)
    storer = _FakeStorer(writeDelay=0.12)          # each batch write takes 120 ms
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()
    _enqueue_chunks(writer, nChunks=16, framesPerChunk=32)   # 16 batches ≈ 1.9 s total
    t0 = time.monotonic()
    writer.finish()                                 # must not time out at 0.3 s
    assert time.monotonic() - t0 > 1.0
    assert not writer.is_alive()
    assert storer.finalized and not storer.aborted
    assert sum(len(b) for b in storer.writes['CAM']) == 16 * 32


def test_writer_finish_gives_up_only_on_a_real_stall(monkeypatch):
    import importlib
    rm = importlib.import_module('imswitch.imcontrol.model.managers.RecordingManager')
    monkeypatch.setattr(rm, 'WRITER_STALL_TIMEOUT_S', 0.3)
    import threading
    release = threading.Event()

    class _StuckStorer(_FakeStorer):
        def writeFrames(self, detectorName, frames):
            release.wait(5.0)                       # a hung storage backend
            super().writeFrames(detectorName, frames)

    storer = _StuckStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()
    _enqueue_chunks(writer, nChunks=2, framesPerChunk=32)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match='no progress for 0.3 s while writing'):
        writer.finish()
    assert 0.3 <= time.monotonic() - t0 < 2.0
    release.set()
    writer.join(timeout=5.0)


def test_writer_finalize_phase_has_its_own_longer_bound(monkeypatch):
    import importlib
    rm = importlib.import_module('imswitch.imcontrol.model.managers.RecordingManager')
    monkeypatch.setattr(rm, 'WRITER_STALL_TIMEOUT_S', 0.2)
    monkeypatch.setattr(rm, 'WRITER_FINALIZE_TIMEOUT_S', 2.0)

    class _SlowFinalizeStorer(_FakeStorer):
        def finalizeStream(self, *args, **kwargs):
            time.sleep(0.6)                         # longer than the stall bound
            super().finalizeStream(*args, **kwargs)

    storer = _SlowFinalizeStorer()
    writer = _make_writer(storer)
    writer.start()
    writer.wait_for_open()
    _enqueue_chunks(writer, nChunks=1, framesPerChunk=2)
    writer.finish()
    assert storer.finalized and not storer.aborted
