"""Real-manager scan participation and TimeTagger finish-barrier regressions."""

from __future__ import annotations

import importlib
import threading

import numpy as np
import pytest

from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self.slots):
            slot(*args)


class _Nidaq:
    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()
        self.isSimulated = False
        self.scanBuildFailures = []

    def reportScanBuildFailure(self, source, error):
        self.scanBuildFailures.append((source, error))
        return True


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, managerProperties):
        self.managerProperties = managerProperties


class _ScanWorker:
    def __init__(self, manager, scanInfoDict, signalDict):
        self.manager = manager
        self.scanInfoDict = scanInfoDict
        self.signalDict = signalDict
        self.d2Step = _Signal()
        self.d3Step = _Signal()
        self.acqDoneSignal = _Signal()
        self.scanning = False
        self._linestep = 1

    def moveToThread(self, thread):
        self.thread = thread

    def run(self):
        pass

    def close(self):
        pass


class _Thread:
    def __init__(self):
        self.started = _Signal()
        self.finished = _Signal()
        self.startCalls = 0
        self._running = False

    def start(self):
        self.startCalls += 1
        self._running = True

    def quit(self):
        self._running = False

    def wait(self, _timeout=None):
        return True

    def isRunning(self):
        return self._running

    def deleteLater(self):
        pass


def _make_apd():
    return APDManager(
        _DetectorInfo({"ctrInputLine": 0, "terminal": "PFI0"}),
        "APD",
        _Nidaq(),
    )


def _make_pmt():
    return PMTManager(
        _DetectorInfo({"analogInputLine": 0}),
        "PMT",
        _Nidaq(),
    )


@pytest.mark.parametrize(
    ("moduleName", "factory"),
    [
        (
            "imswitch.imcontrol.model.managers.detectors.APDManager",
            _make_apd,
        ),
        (
            "imswitch.imcontrol.model.managers.detectors.PMTManager",
            _make_pmt,
        ),
    ],
)
def test_scan_participant_snapshot_overrides_stale_acquisition_flag(
        monkeypatch, moduleName, factory):
    module = importlib.import_module(moduleName)
    monkeypatch.setattr(module, "ScanWorker", _ScanWorker)
    monkeypatch.setattr(module, "Thread", _Thread)
    manager = factory()

    # This is the stale flag state that existed after last-lease release.
    # Membership in the immutable per-scan snapshot must still arm the worker.
    manager.acquisition = False
    manager.initiateScan(
        {PARTICIPANTS_KEY: [manager.name]},
        {},
    )

    assert manager._scanParticipating is True
    assert isinstance(manager._scanWorker, _ScanWorker)
    manager.startScan()
    assert manager._scanThread.startCalls == 1


@pytest.mark.parametrize(
    ("moduleName", "factory"),
    [
        (
            "imswitch.imcontrol.model.managers.detectors.APDManager",
            _make_apd,
        ),
        (
            "imswitch.imcontrol.model.managers.detectors.PMTManager",
            _make_pmt,
        ),
    ],
)
def test_excluded_point_detector_does_not_build_or_start_worker(
        monkeypatch, moduleName, factory):
    module = importlib.import_module(moduleName)
    monkeypatch.setattr(module, "ScanWorker", _ScanWorker)
    monkeypatch.setattr(module, "Thread", _Thread)
    manager = factory()
    staleThread = _Thread()
    manager._scanThread = staleThread
    manager.acquisition = True

    manager.initiateScan({PARTICIPANTS_KEY: ["another-detector"]}, {})
    manager.startScan()

    assert manager._scanParticipating is False
    assert staleThread.startCalls == 0


@pytest.mark.parametrize(
    ("moduleName", "factory"),
    [
        (
            "imswitch.imcontrol.model.managers.detectors.APDManager",
            _make_apd,
        ),
        (
            "imswitch.imcontrol.model.managers.detectors.PMTManager",
            _make_pmt,
        ),
    ],
)
def test_absent_participant_snapshot_preserves_legacy_point_detector_scan(
        monkeypatch, moduleName, factory):
    module = importlib.import_module(moduleName)
    monkeypatch.setattr(module, "ScanWorker", _ScanWorker)
    monkeypatch.setattr(module, "Thread", _Thread)
    manager = factory()
    manager.acquisition = False

    manager.initiateScan({}, {})

    assert manager._scanParticipating is True
    assert isinstance(manager._scanWorker, _ScanWorker)


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_stale_point_worker_pixels_cannot_mutate_new_scan_generation(
        monkeypatch, factory):
    manager = factory()
    updates = []
    monkeypatch.setattr(
        manager,
        "updateImage",
        lambda pixels, pos: updates.append((pixels.copy(), pos)),
    )
    manager._activeScanGeneration = 2
    manager._preparedScanGeneration = 2
    manager._completedScanGenerations.add(1)

    manager._onScanPixels(np.array([11]), (0, 0), 1)
    manager._onScanPixels(np.array([22]), (0, 1), 2)

    assert len(updates) == 1
    assert updates[0][0].tolist() == [22]
    assert updates[0][1] == (0, 1)


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_stale_point_worker_frame_boundary_cannot_publish_new_scan_frame(
        factory):
    manager = factory()
    manager._image = np.ones((1, 2, 2), dtype=np.uint16)
    manager._image_display = np.zeros((2, 2), dtype=np.uint16)
    manager._activeScanGeneration = 4
    manager._preparedScanGeneration = 4
    manager._completedScanGenerations.add(3)

    manager._onFrameBoundary(3)
    assert np.count_nonzero(manager._image_display) == 0

    manager._onFrameBoundary(4)
    assert np.array_equal(manager._image_display, np.ones((2, 2)))


def _make_timetagger():
    return SwabianTimeTaggerManager(
        _DetectorInfo(
            {
                "click_channel": 1,
                "start_channel": 2,
                "line_channel": 3,
            }
        ),
        "FLIM",
        _Nidaq(),
    )


class _FinishWorker:
    def __init__(self, generation):
        self.scanGeneration = generation
        self.signalDoneCalls = 0

    def signal_done(self):
        self.signalDoneCalls += 1

    def stop(self):
        pass


def _prepare_finish(manager, generation):
    worker = _FinishWorker(generation)
    manager._flim = object()
    manager._scanWorker = worker
    manager._activeScanGeneration = generation
    manager._scanParticipating = True
    return worker


def test_excluded_timetagger_does_not_touch_retained_prepared_hardware():
    manager = _make_timetagger()
    manager._flim = object()  # prepared object retained from an older scan
    teardownCalls = []
    manager._teardownScanThread = lambda: teardownCalls.append(True)

    manager.initiateScan({PARTICIPANTS_KEY: ["APD"]}, {})
    manager.startScan()
    manager._onScanDone()

    assert manager._scanParticipating is False
    assert manager._preparedScanGeneration is None
    assert teardownCalls == []


def test_timetagger_absent_participant_snapshot_preserves_legacy_gate():
    manager = _make_timetagger()
    manager._enabled = False

    manager.initiateScan({}, {})

    assert manager._scanParticipating is True
    assert manager._scanGeneration == 1
    assert manager._preparedScanGeneration is None


@pytest.mark.parametrize(
    ("moduleName", "factory"),
    [
        (
            "imswitch.imcontrol.model.managers.detectors.APDManager",
            _make_apd,
        ),
        (
            "imswitch.imcontrol.model.managers.detectors.PMTManager",
            _make_pmt,
        ),
    ],
)
def test_point_detector_preparation_exception_is_contained_and_reported(
        monkeypatch, moduleName, factory):
    module = importlib.import_module(moduleName)

    class _ExplodingWorker:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("invalid detector scan")

    monkeypatch.setattr(module, "ScanWorker", _ExplodingWorker)
    manager = factory()

    # This method is a Qt slot: the exception must not escape it.
    manager._onScanBuilt({PARTICIPANTS_KEY: [manager.name]}, {}, [])

    assert len(manager._nidaqManager.scanBuildFailures) == 1
    source, error = manager._nidaqManager.scanBuildFailures[0]
    assert source == f"{manager.name}.prepare"
    assert "invalid detector scan" in str(error)
    assert manager._scanWorker is None
    assert manager._preparedScanGeneration is None


class _TeardownWorker:
    def __init__(self, manager, generation, events):
        self.manager = manager
        self.scanGeneration = generation
        self.events = events
        self.scanning = True

    def close(self):
        self.events.append("close-start")
        self.manager.finishScan(
            "graceful", lambda: self.events.append("ack")
        )
        assert "ack" not in self.events
        self.events.append("close-end")


class _OrderedTeardownThread(_Thread):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def quit(self):
        self.events.append("quit")
        super().quit()

    def wait(self, _timeout=None):
        self.events.append("wait")
        return True


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_point_detector_ack_waits_for_nested_local_teardown(factory):
    manager = factory()
    events = []
    generation = 7
    worker = _TeardownWorker(manager, generation, events)
    manager._scanWorker = worker
    manager._scanThread = _Thread()
    manager._preparedScanGeneration = generation
    manager._activeScanGeneration = generation

    manager.stopAcquisitionLocal()

    assert events == ["close-start", "close-end", "ack"]
    assert manager._scanWorker is None
    assert manager._scanThread is None


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_point_detector_abort_cleans_partial_worker_before_ack(factory):
    manager = factory()
    events = []
    generation = 8

    class _AbortWorker:
        scanGeneration = generation
        scanning = True

        def close(self):
            events.append("closed")

    thread = _Thread()
    manager._scanWorker = _AbortWorker()
    manager._scanThread = thread
    manager._preparedScanGeneration = generation

    manager.finishScan("abort", lambda: events.append("ack"))

    assert events == ["closed", "ack"]
    assert manager._scanWorker is None
    assert manager._scanThread is None


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_point_detector_abort_waits_for_active_teardown_before_ack(factory):
    manager = factory()
    generation = 18
    closeEntered = threading.Event()
    releaseClose = threading.Event()
    acknowledgements = []

    class _BlockingWorker:
        scanGeneration = generation
        scanning = True

        def close(self):
            closeEntered.set()
            assert releaseClose.wait(1)

    manager._scanWorker = _BlockingWorker()
    manager._scanThread = _Thread()
    manager._activeScanGeneration = generation

    teardownThread = threading.Thread(target=manager.stopAcquisitionLocal)
    teardownThread.start()
    assert closeEntered.wait(1)

    finishThread = threading.Thread(
        target=lambda: manager.finishScan(
            "abort", lambda: acknowledgements.append("ack")
        )
    )
    finishThread.start()
    assert acknowledgements == []

    releaseClose.set()
    teardownThread.join(1)
    finishThread.join(1)

    assert not teardownThread.is_alive()
    assert not finishThread.is_alive()
    assert acknowledgements == ["ack"]
    assert manager._scanWorker is None
    assert manager._scanThread is None


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_point_detector_closes_input_task_before_joining_worker(factory):
    manager = factory()
    events = []
    generation = 71

    class _BlockingWorker:
        scanGeneration = generation
        scanning = True

        def close(self):
            events.append("close")

    manager._scanWorker = _BlockingWorker()
    manager._scanThread = _OrderedTeardownThread(events)
    manager._activeScanGeneration = generation

    manager.stopAcquisitionLocal(generation)

    assert events == ["close", "quit", "wait"]


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_point_detector_finish_callback_is_cancelled_by_identity(factory):
    manager = factory()
    generation = 9
    manager._scanWorker = _TeardownWorker(manager, generation, [])
    manager._preparedScanGeneration = generation
    manager._activeScanGeneration = generation
    acknowledgements = []

    class _EqualCallback:
        def __init__(self, label):
            self.label = label

        def __call__(self):
            acknowledgements.append(self.label)

        def __eq__(self, _other):
            return True

    cancelled = _EqualCallback("cancelled")
    retained = _EqualCallback("retained")
    manager.finishScan("graceful", cancelled)
    manager.finishScan("graceful", retained)

    assert manager.cancelFinishScan(cancelled) is True
    manager._completeScanGeneration(generation)

    assert acknowledgements == ["retained"]


@pytest.mark.parametrize("factory", [_make_apd, _make_pmt])
def test_stale_point_worker_completion_cannot_teardown_new_generation(factory):
    manager = factory()
    currentWorker = _ScanWorker(manager, {}, {})
    currentWorker.scanGeneration = 16
    currentThread = _Thread()
    manager._scanWorker = currentWorker
    manager._scanThread = currentThread
    manager._preparedScanGeneration = 16
    manager._activeScanGeneration = 16

    manager.stopAcquisitionLocal(15)

    assert manager._scanWorker is currentWorker
    assert manager._scanThread is currentThread
    assert currentThread.startCalls == 0
    assert 15 in manager._completedScanGenerations


def test_timetagger_invalid_preparation_invalidates_old_flim_and_reports(
        monkeypatch):
    module = importlib.import_module(
        "imswitch.imcontrol.model.managers.detectors."
        "SwabianTimeTaggerManager"
    )
    monkeypatch.setattr(module, "_TIMETAGGER_AVAILABLE", True)
    manager = _make_timetagger()
    oldFlim = object()
    manager._flim = oldFlim
    manager._preparedScanGeneration = 99

    manager._onScanBuilt(
        {
            PARTICIPANTS_KEY: [manager.name],
            "img_dims": [2, 2],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [1.0, 1.0],
            "dwell_time": 0,
        },
        {},
        [],
    )

    assert manager._flim is None
    assert manager._preparedScanGeneration is None
    assert manager._activeScanGeneration is None
    assert len(manager._nidaqManager.scanBuildFailures) == 1
    assert "dwell_time" in str(
        manager._nidaqManager.scanBuildFailures[0][1]
    )


def test_late_old_final_frame_cannot_acknowledge_new_generation():
    manager = _make_timetagger()
    acknowledgements = []

    _prepare_finish(manager, 1)
    manager.finishScan("graceful", lambda: acknowledgements.append("old"))
    _prepare_finish(manager, 2)
    manager.finishScan("graceful", lambda: acknowledgements.append("new"))

    manager._fireFinalFrameAck(1)
    assert acknowledgements == ["old"]
    assert 2 in manager._finishAcks

    manager._fireFinalFrameAck(2)
    assert acknowledgements == ["old", "new"]


def test_finish_callback_can_be_cancelled_by_identity():
    manager = _make_timetagger()
    acknowledgements = []
    _prepare_finish(manager, 3)
    cancelled = lambda: acknowledgements.append("cancelled")
    retained = lambda: acknowledgements.append("retained")

    manager.finishScan("graceful", cancelled)
    manager.finishScan("graceful", retained)

    assert manager.cancelFinishScan(cancelled) is True
    assert manager.cancelFinishScan(cancelled) is False
    manager._fireFinalFrameAck(3)

    assert acknowledgements == ["retained"]


def test_final_frame_before_finish_acknowledges_immediately():
    manager = _make_timetagger()
    acknowledgements = []
    worker = _prepare_finish(manager, 4)

    manager._fireFinalFrameAck(4)
    manager.finishScan("graceful", lambda: acknowledgements.append("done"))

    assert acknowledgements == ["done"]
    assert worker.signalDoneCalls == 0


def test_stale_worker_frame_does_not_overwrite_current_image_or_ack_new_scan():
    manager = _make_timetagger()
    acknowledgements = []
    _prepare_finish(manager, 5)
    manager.finishScan("graceful", lambda: acknowledgements.append("old"))
    _prepare_finish(manager, 6)
    manager.finishScan("graceful", lambda: acknowledgements.append("new"))
    manager._image_intensity = np.zeros((1, 1, 1), dtype=np.float32)
    manager._image_display = np.zeros((1, 1, 1), dtype=np.float32)

    manager._on_frame_ready(
        np.ones((1, 1), dtype=np.float32),
        np.ones((1, 1), dtype=np.float32),
        True,
        np.ones(1, dtype=np.float32),
        np.ones(1, dtype=np.float32),
        1.0,
        scanGeneration=5,
    )

    assert acknowledgements == ["old"]
    assert 6 in manager._finishAcks
    assert manager._image_display[0, 0, 0] == 0


def test_timetagger_worker_exit_without_data_acknowledges_its_generation():
    manager = _make_timetagger()
    acknowledgements = []
    _prepare_finish(manager, 10)
    manager.finishScan(
        "graceful", lambda: acknowledgements.append("terminal")
    )

    manager._onScanWorkerFinished(10)

    assert acknowledgements == ["terminal"]
    assert 10 not in manager._finishAcks


def test_timetagger_old_worker_exit_cannot_ack_new_generation():
    manager = _make_timetagger()
    acknowledgements = []
    _prepare_finish(manager, 11)
    manager.finishScan("graceful", lambda: acknowledgements.append("old"))
    _prepare_finish(manager, 12)
    manager.finishScan("graceful", lambda: acknowledgements.append("new"))

    manager._onScanWorkerFinished(11)

    assert acknowledgements == ["old"]
    assert 12 in manager._finishAcks


def test_timetagger_final_frame_then_finished_fallback_acknowledges_once():
    manager = _make_timetagger()
    acknowledgements = []
    _prepare_finish(manager, 13)
    manager.finishScan("graceful", lambda: acknowledgements.append("done"))

    manager._fireFinalFrameAck(13)
    manager._onScanWorkerFinished(13)

    assert acknowledgements == ["done"]


def test_timetagger_worker_finished_before_finish_acknowledges_immediately():
    manager = _make_timetagger()
    acknowledgements = []
    worker = _prepare_finish(manager, 14)

    manager._onScanWorkerFinished(14)
    manager.finishScan("graceful", lambda: acknowledgements.append("done"))

    assert acknowledgements == ["done"]
    assert worker.signalDoneCalls == 0


def test_timetagger_abort_stops_iteration_with_retained_detector_lease():
    manager = _make_timetagger()
    worker = _prepare_finish(manager, 15)
    teardownCalls = []

    def teardown():
        teardownCalls.append(True)
        manager._scanWorker = None
        manager._scanThread = None

    manager._teardownScanThread = teardown
    acknowledgements = []

    manager.finishScan("abort", lambda: acknowledgements.append("done"))

    assert teardownCalls == [True]
    assert acknowledgements == ["done"]
    assert manager._activeScanGeneration is None
    assert manager.acquisition is False
