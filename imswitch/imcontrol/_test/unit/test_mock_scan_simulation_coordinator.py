import json
import time
from types import SimpleNamespace

import h5py
import numpy as np
from qtpy import QtCore

from imswitch.imcontrol.model import DetectorInfo, RecMode, SaveFormat, SaveMode, SetupInfo
from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.mockscan import SimulatedScanPlan


def _ensure_qcore_app():
    app = QtCore.QCoreApplication.instance()
    if app is None:
        app = QtCore.QCoreApplication([])
    return app


def _wait_for(predicate, timeout=1.0):
    app = _ensure_qcore_app()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _setup_from_user_default(name):
    with open(f'imswitch/_data/user_defaults/imcontrol_setups/{name}') as fh:
        return SetupInfo.from_json(fh.read(), infer_missing=True)


def _minimal_simulated_setup(detectors_json):
    setup_json = json.dumps({
        'detectors': detectors_json,
        'lasers': {},
        'positioners': {},
        'scan': {
            'scanWidgetType': 'PointScan',
            'scanDesigner': 'GalvoScanDesigner',
            'scanDesignerParams': {},
            'TTLCycleDesigner': 'PointScanTTLCycleDesigner',
            'TTLCycleDesignerParams': {},
            'sampleRate': 100000,
        },
        'nidaq': {
            'simulation': True,
            'timerCounterChannel': None,
            'startTrigger': False,
        },
    })
    return SetupInfo.from_json(setup_json, infer_missing=True)


def _minimal_scan_info():
    return {
        'img_dims': [2, 2],
        'img_axes_phys': ['x', 'y'],
        'pixel_sizes': [1.0, 1.0],
        'scan_samples': [1, 2, 4],
        'scan_samples_d2_period': 2,
        'scan_samples_total': 4,
        'dwell_time': 1e-6,
        'scan_time_step': 1e-6,
        'n_linesteps': 1,
        'scan_throw_startzero': 0,
        'scan_pads_initpos': [],
        'scan_throw_settling': 0,
        'scan_throw_startacc': 0,
        'phase_delay': 0,
        'smooth_axes': [False, False],
    }


def _slow_detector_scan_info():
    scan_info = _minimal_scan_info()
    scan_info.update({
        'scan_samples': [1, 20, 40],
        'scan_samples_d2_period': 20,
        'scan_samples_total': 40,
        'dwell_time': 10e-6,
        'scan_time_step': 1e-6,
    })
    return scan_info


class _ManualDetectorsManager:
    def __init__(self, detectors):
        self._detectors = detectors
        self._active = False

    def __getitem__(self, detectorName):
        return self._detectors[detectorName]

    def execOnAll(self, func, *, condition=None):
        if condition is None:
            condition = lambda detector: True
        return {
            name: func(detector)
            for name, detector in self._detectors.items()
            if condition(detector)
        }

    def getAllDeviceNames(self, condition=None):
        if condition is None:
            condition = lambda detector: True
        return [name for name, detector in self._detectors.items()
                if condition(detector)]

    def acquire(self, detectorNames, purpose):
        if not self._active:
            self.execOnAll(
                lambda detector: detector.startAcquisition(),
                condition=lambda detector: detector.forAcquisition,
            )
            self._active = True
        return object()

    def release(self, handle):
        if self._active:
            self.execOnAll(
                lambda detector: detector.stopAcquisition(),
                condition=lambda detector: detector.forAcquisition,
            )
            self._active = False


def _recorded_dataset(memory_recordings, detector_name):
    for name, file, _path, _saved in memory_recordings:
        if name.endswith(f'_{detector_name}.hdf5'):
            file.seek(0)
            h5file = h5py.File(file)
            return file, h5file, h5file[detector_name]['data']
    raise AssertionError(f'No memory recording for detector {detector_name}')


def test_detector_state_provider_setter_does_not_tear_down_task_waiters():
    """Binding simulator state is configuration, not manager shutdown."""
    class _Waiter:
        def __init__(self):
            self.quitCalls = 0
            self.waitCalls = 0
            self.running = True
            self.threadRunning = True

        def quit(self):
            self.quitCalls += 1

        def wait(self, _timeout):
            self.waitCalls += 1
            self.threadRunning = False
            return True

        def isRunning(self):
            return self.threadRunning

    class _Simulator:
        def __init__(self):
            self.provider = None
            self.stopCalls = 0

        def setDetectorStateProvider(self, provider):
            self.provider = provider

        def stop(self, wait):
            assert wait is False
            self.stopCalls += 1

    manager = NidaqManager.__new__(NidaqManager)
    simulator = _Simulator()
    waiter = _Waiter()
    manager._NidaqManager__scanSimulator = simulator
    manager.doTaskWaiter = waiter
    manager.aoTaskWaiter = None
    manager.timerTaskWaiter = None
    provider = lambda _name: True

    manager.setScanSimulationDetectorStateProvider(provider)

    assert simulator.provider is provider
    assert waiter.quitCalls == 0
    assert waiter.waitCalls == 0

    manager.__del__()
    assert simulator.stopCalls == 1
    assert waiter.quitCalls == 1
    assert waiter.waitCalls == 1

    # Prevent a second observable cleanup if Python invokes __del__ at GC.
    manager._NidaqManager__scanSimulator = None
    manager.doTaskWaiter = None


def test_simulated_scan_plan_counts_ttl_edges_and_fallback_frames():
    setup_info = SimpleNamespace(
        scan=SimpleNamespace(sampleRate=1000),
        detectors={
            'Camera': SimpleNamespace(forAcquisition=True),
            'APD': SimpleNamespace(forAcquisition=True),
            'FocusOnly': SimpleNamespace(forAcquisition=False),
        },
    )
    signal_dict = {
        'TTLCycleSignalsDict': {
            'Camera': np.array([0, 1, 1, 0, 1, 0], dtype=bool),
        },
    }
    scan_info = {'img_dims': [3, 4], 'scan_samples_total': 500}

    plan = SimulatedScanPlan.fromScan(setup_info, signal_dict, scan_info)

    assert plan.nPositions == 12
    assert plan.samplesTotal == 500
    assert plan.duration == 0.5
    assert plan.frameCounts == {
        'Camera': 2,
        'APD': 12,
    }


def test_simulated_scan_plan_only_emits_for_currently_armed_detectors():
    setup_info = SimpleNamespace(
        scan=SimpleNamespace(sampleRate=1000),
        detectors={
            'Camera': SimpleNamespace(forAcquisition=True),
            'UnusedCamera': SimpleNamespace(forAcquisition=True),
        },
    )

    plan = SimulatedScanPlan.fromScan(
        setup_info,
        {'TTLCycleSignalsDict': {}},
        {'img_dims': [2, 2], 'scan_samples_total': 100},
        detectorIsArmed=lambda name: name == 'Camera',
    )

    assert plan.frameCounts == {'Camera': 4}


def test_simulated_scan_worker_stop_aborts_before_duration_cap():
    """stop() must cut the tick loop short instead of running out the
    duration cap, and must suppress sigDone when aborted mid-run.

    Regression for a stale claim in docs/mock-infrastructure.rst that
    aborting a simulated scan blocks until SimulatedScanWorker._MAX_DURATION
    (5s) elapses - verified false against current code: stop() sets a flag
    checked once per ~30ms tick, so an external stop() aborts within one
    tick, not the duration cap.
    """
    from imswitch.imcontrol.model.managers.mockscan.ScanSimulationCoordinator import (
        SimulatedScanWorker,
    )

    plan = SimulatedScanPlan(
        frameCounts={'Camera': 1000}, duration=10.0, nPositions=1000, samplesTotal=1000,
    )
    worker = SimulatedScanWorker(plan)
    doneEmitted = []
    worker.sigDone.connect(lambda: doneEmitted.append(True))
    worker.start()

    _wait_for(lambda: worker.isRunning(), timeout=1.0)
    worker.stop()

    stoppedPromptly = _wait_for(lambda: not worker.isRunning(), timeout=1.0)
    assert stoppedPromptly, (
        'worker did not stop well within its 5s duration cap; abort is no '
        'longer prompt'
    )
    assert doneEmitted == []  # aborted mid-run: no natural-completion signal


def test_nidaq_simulated_scan_allows_no_physical_outputs():
    setup_info = _setup_from_user_default('mock_scan_setup.json')
    manager = NidaqManager(setup_info)
    events = []

    manager.sigScanBuildFailed.connect(lambda: events.append('failed'))
    manager.sigScanStarted.connect(lambda: events.append('started'))
    manager.sigScanDone.connect(lambda: events.append('done'))

    manager.runScan(
        {
            'scanSignalsDict': {
                'X': np.zeros(10),
                'Y': np.zeros(10),
                'Z': np.zeros(10),
            },
            'TTLCycleSignalsDict': {},
        },
        {'img_dims': [2, 2], 'scan_samples_total': 10},
    )

    assert 'failed' not in events
    assert 'started' in events
    assert _wait_for(lambda: 'done' in events)


def test_external_mock_driver_registration_does_not_suppress_camera_triggers():
    setup_info = _minimal_simulated_setup({
        'Camera': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'HamamatsuManager',
            'managerProperties': {
                'cameraListIndex': 'mock',
                'hamamatsu': {'trigger_source': 2},
            },
            'forAcquisition': True,
        },
    })
    manager = NidaqManager(setup_info)
    triggers = []
    done = []

    manager.registerExternalScanDriver()
    manager.sigSimScanFrameTrigger.connect(
        lambda detector, n: triggers.append((detector, n))
    )
    manager.sigScanDone.connect(lambda: done.append(True))

    manager.runScan(
        {
            'scanSignalsDict': {},
            'TTLCycleSignalsDict': {
                'Camera': np.array([0, 1, 1, 0, 1, 0], dtype=bool),
            },
        },
        {'img_dims': [4, 1], 'scan_samples_total': 10},
    )

    assert _wait_for(lambda: sum(n for _, n in triggers) == 2 and done)
    assert triggers
    assert {detector for detector, _ in triggers} == {'Camera'}


def test_finish_external_mock_does_not_abort_active_simulated_camera_triggers():
    setup_info = _minimal_simulated_setup({
        'Camera': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'HamamatsuManager',
            'managerProperties': {
                'cameraListIndex': 'mock',
                'hamamatsu': {'trigger_source': 2},
            },
            'forAcquisition': True,
        },
    })
    manager = NidaqManager(setup_info)
    triggers = []
    done = []

    manager.sigSimScanFrameTrigger.connect(
        lambda detector, n: triggers.append((detector, n))
    )
    manager.sigScanDone.connect(lambda: done.append(True))

    manager.runScan(
        {
            'scanSignalsDict': {},
            'TTLCycleSignalsDict': {
                'Camera': np.array([0, 1, 0, 1, 0, 1, 0], dtype=bool),
            },
        },
        {'img_dims': [3, 1], 'scan_samples_total': 10},
    )
    manager.finishExternalMock()

    assert _wait_for(lambda: sum(n for _, n in triggers) == 3 and done)


def test_mixed_hamamatsu_and_apd_mock_scan_keeps_camera_triggers():
    setup_info = _minimal_simulated_setup({
        'Camera': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'HamamatsuManager',
            'managerProperties': {
                'cameraListIndex': 'mock',
                'hamamatsu': {'trigger_source': 2},
            },
            'forAcquisition': True,
        },
        'APD': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'APDManager',
            'managerProperties': {
                'ctrInputLine': 'Dev1/ctr0',
                'terminal': '/Dev1/PFI0',
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    camera_info = DetectorInfo(
        managerName='HamamatsuManager',
        analogChannel=None,
        digitalLine=None,
        managerProperties={
            'cameraListIndex': 'mock',
            'hamamatsu': {
                'trigger_source': 2,
                'subarray_hsize': 32,
                'subarray_vsize': 32,
            },
        },
        forAcquisition=True,
    )
    apd_info = DetectorInfo(
        managerName='APDManager',
        analogChannel=None,
        digitalLine=None,
        managerProperties={
            'ctrInputLine': 'Dev1/ctr0',
            'terminal': '/Dev1/PFI0',
        },
        forAcquisition=True,
    )
    camera = HamamatsuManager(camera_info, 'Camera', nidaqManager=nidaq)
    apd = APDManager(apd_info, 'APD', nidaq)
    done = []
    nidaq.sigScanDone.connect(lambda: done.append(True))

    scan_info = _minimal_scan_info()

    camera.startAcquisition()
    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {
                    'Camera': np.array([0, 1, 0, 1, 0], dtype=bool),
                },
            },
            scan_info,
        )

        assert _wait_for(lambda: done)
        camera_frames = camera.getChunk()
        assert len(camera_frames) == 2
        assert all(frame.shape == (32, 32) for frame in camera_frames)

        apd_chunk = apd.getChunk()
        assert apd_chunk.size > 0
        assert apd_chunk.shape == (1, 2, 2)
        assert apd_chunk.dtype == np.uint16
        assert np.max(apd_chunk) < np.iinfo(np.uint16).max
    finally:
        camera.stopAcquisition()
        apd.stopAcquisition()


def test_apd_mock_input_behaves_like_bounded_cumulative_counter():
    setup_info = _minimal_simulated_setup({
        'APD': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'APDManager',
            'managerProperties': {
                'ctrInputLine': 'Dev1/ctr0',
                'terminal': '/Dev1/PFI0',
                'mockPhotonCountMean': 1000,
                'mockPhotonCountMax': 3000,
                'mockRandomSeed': 1,
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    apd = APDManager(setup_info.detectors['APD'], 'APD', nidaq)

    from imswitch.imcontrol.model.managers.detectors.APDManager import (
        ScanWorker as APDScanWorker,
    )

    worker = APDScanWorker(apd, _slow_detector_scan_info(), {'TTLCycleSignalsDict': {}})
    data = worker.randomInput(worker._samples_d_scanstep[1])
    increments = np.concatenate(([data[0] - worker._last_value], np.diff(data)))
    pixels = worker.samples_to_pixels(increments)

    assert np.all(np.diff(data) >= 0)
    assert np.all(increments >= 0)
    assert np.all(pixels >= 0)
    assert np.max(pixels) <= 3000


def test_pmt_mock_input_behaves_like_bounded_voltage_average():
    setup_info = _minimal_simulated_setup({
        'PMT': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'PMTManager',
            'managerProperties': {
                'analogInputLine': 'Dev1/ai0',
                'mockVoltageMin': -5.0,
                'mockVoltageMax': 5.0,
                'mockVoltageNoiseStd': 10.0,
                'mockRandomSeed': 1,
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    pmt = PMTManager(setup_info.detectors['PMT'], 'PMT', nidaq)

    from imswitch.imcontrol.model.managers.detectors.PMTManager import (
        ScanWorker as PMTScanWorker,
    )

    worker = PMTScanWorker(pmt, _slow_detector_scan_info(), {'TTLCycleSignalsDict': {}})
    data = worker.randomInput(200) - pmt._offset_v

    assert data.dtype == np.float32
    assert np.min(data) >= -5.0
    assert np.max(data) <= 5.0

    line = np.array([5.0] * 10 + [-5.0] * 10, dtype=np.float32)
    np.testing.assert_allclose(worker.samples_to_pixels(line), [5.0, -5.0])


def test_apd_only_mock_scan_completes_once_without_input_task_cleanup():
    setup_info = _minimal_simulated_setup({
        'APD': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'APDManager',
            'managerProperties': {
                'ctrInputLine': 'Dev1/ctr0',
                'terminal': '/Dev1/PFI0',
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    apd_info = DetectorInfo(
        managerName='APDManager',
        analogChannel=None,
        digitalLine=None,
        managerProperties={
            'ctrInputLine': 'Dev1/ctr0',
            'terminal': '/Dev1/PFI0',
        },
        forAcquisition=True,
    )
    apd = APDManager(apd_info, 'APD', nidaq)
    done = []
    external_finishes = []
    mock_starts = []
    apd_chunks = []
    nidaq.sigScanDone.connect(lambda: done.append(True))
    nidaq.finishExternalMock = lambda: external_finishes.append(True)
    original_mock_start = apd.mockStartScan
    apd.mockStartScan = lambda scan_info, signal_dict: (
        mock_starts.append(True),
        original_mock_start(scan_info, signal_dict),
    )[-1]

    def apd_chunk_ready():
        if apd_chunks:
            return True
        chunk = apd.getChunk()
        if chunk.size > 0:
            apd_chunks.append(chunk)
        return bool(apd_chunks)

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {},
            },
            _minimal_scan_info(),
        )

        assert _wait_for(
            lambda: done and apd_chunk_ready() and apd.mockScanDone()
        )
        assert len(done) == 1
        assert external_finishes == []
        assert mock_starts == [True]
        assert apd.mockScanDone() is True
        assert _wait_for(
            lambda: apd._scanThread is None or not apd._scanThread.isRunning()
        )
        assert nidaq.tasks == {}
    finally:
        apd.stopAcquisition()


def test_pmt_only_mock_scan_completes_once_without_input_task_cleanup():
    setup_info = _minimal_simulated_setup({
        'PMT': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'PMTManager',
            'managerProperties': {
                'analogInputLine': 'Dev1/ai0',
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    pmt_info = DetectorInfo(
        managerName='PMTManager',
        analogChannel=None,
        digitalLine=None,
        managerProperties={
            'analogInputLine': 'Dev1/ai0',
        },
        forAcquisition=True,
    )
    pmt = PMTManager(pmt_info, 'PMT', nidaq)
    done = []
    external_finishes = []
    mock_starts = []
    pmt_chunks = []
    nidaq.sigScanDone.connect(lambda: done.append(True))
    nidaq.finishExternalMock = lambda: external_finishes.append(True)
    original_mock_start = pmt.mockStartScan
    pmt.mockStartScan = lambda scan_info, signal_dict: (
        mock_starts.append(True),
        original_mock_start(scan_info, signal_dict),
    )[-1]

    def pmt_chunk_ready():
        if pmt_chunks:
            return True
        chunk = pmt.getChunk()
        if chunk.size > 0:
            pmt_chunks.append(chunk)
        return bool(pmt_chunks)

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {},
            },
            _minimal_scan_info(),
        )

        assert _wait_for(
            lambda: done and pmt_chunk_ready() and pmt.mockScanDone()
        )
        assert len(done) == 1
        assert external_finishes == []
        assert mock_starts == [True]
        assert pmt.mockScanDone() is True
        assert _wait_for(
            lambda: pmt._scanThread is None or not pmt._scanThread.isRunning()
        )
        assert nidaq.tasks == {}
    finally:
        pmt.stopAcquisition()


def test_hamamatsu_mock_scan_once_recording_reaches_rec_frames(tmp_path):
    _ensure_qcore_app()
    setup_info = _minimal_simulated_setup({
        'Camera': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'HamamatsuManager',
            'managerProperties': {
                'cameraListIndex': 'mock',
                'hamamatsu': {
                    'trigger_source': 2,
                    'subarray_hsize': 32,
                    'subarray_vsize': 32,
                },
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    camera = HamamatsuManager(
        setup_info.detectors['Camera'], 'Camera', nidaqManager=nidaq
    )
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)
    memory_recordings = []

    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory_recordings.append(
            (name, file, path, saved)
        )
    )

    recording.startRecording(
        detectorNames=['Camera'],
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'hamamatsu_scan_once'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'Camera': {}},
        recFrames=4,
        # The scan source declares the pulses per position (getNumCamTTL);
        # this test drives the manager directly, so it declares them itself.
        numCamTTL={'Camera': 1},
        stallTimeout=1.0,
    )
    assert recording.waitForAcquisitionStarted(2.0)

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {},
            },
            _minimal_scan_info(),
        )

        assert _wait_for(lambda: len(memory_recordings) == 1, timeout=3.0)
        mem_file, h5file, dataset = _recorded_dataset(
            memory_recordings, 'Camera'
        )
        try:
            assert dataset.shape == (4, 32, 32)
            assert dataset.attrs['recording:expected_frames'] == 4
            assert dataset.attrs['recording:frames_per_stack'] == 4
        finally:
            h5file.close()
            mem_file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)


def test_two_apds_record_both_linesteps_as_one_frame_each(tmp_path):
    detector_json = {
        name: {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'APDManager',
            'managerProperties': {
                'ctrInputLine': f'Dev1/ctr{index}',
                'terminal': f'/Dev1/PFI{index}',
                'mockRandomSeed': index + 1,
            },
            'forAcquisition': True,
        }
        for index, name in enumerate(('APDred', 'APDgreen'))
    }
    setup_info = _minimal_simulated_setup(detector_json)
    nidaq = NidaqManager(setup_info)
    apds = {
        name: APDManager(info, name, nidaq)
        for name, info in setup_info.detectors.items()
    }
    recording = RecordingManager(_ManualDetectorsManager(apds))
    memory_recordings = []
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory_recordings.append(
            (name, file, path, saved)
        )
    )

    recording.startRecording(
        detectorNames=['APDred', 'APDgreen'],
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'two_apd_linesteps'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'APDred': {}, 'APDgreen': {}},
        recFrames=1,
        stallTimeout=1.0,
    )
    assert recording.waitForAcquisitionStarted(2.0)

    scan_info = _minimal_scan_info()
    scan_info.update({
        'n_linesteps': 2,
        'scan_samples': [1, 2, 8],
        'scan_samples_total': 8,
    })
    generation = recording.recordingGeneration
    assert recording.markScanStarted(scan_info, generation)

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {},
            },
            scan_info,
        )

        assert _wait_for(lambda: len(memory_recordings) == 2, timeout=3.0)
        open_files = []
        try:
            for detector_name in ('APDred', 'APDgreen'):
                mem_file, h5file, dataset = _recorded_dataset(
                    memory_recordings, detector_name
                )
                open_files.extend([h5file, mem_file])
                assert dataset.shape == (1, 2, 2, 2)
                assert dataset.attrs['axes'] == 'TCYX'
                assert np.count_nonzero(dataset[:]) > 0
        finally:
            for file in open_files:
                file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()


def test_mixed_hamamatsu_apd_mock_scan_once_records_each_target(tmp_path):
    _ensure_qcore_app()
    setup_info = _minimal_simulated_setup({
        'Camera': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'HamamatsuManager',
            'managerProperties': {
                'cameraListIndex': 'mock',
                'hamamatsu': {
                    'trigger_source': 2,
                    'subarray_hsize': 32,
                    'subarray_vsize': 32,
                },
            },
            'forAcquisition': True,
        },
        'APD': {
            'analogChannel': None,
            'digitalLine': None,
            'managerName': 'APDManager',
            'managerProperties': {
                'ctrInputLine': 'Dev1/ctr0',
                'terminal': '/Dev1/PFI0',
            },
            'forAcquisition': True,
        },
    })
    nidaq = NidaqManager(setup_info)
    camera = HamamatsuManager(
        setup_info.detectors['Camera'], 'Camera', nidaqManager=nidaq
    )
    apd = APDManager(setup_info.detectors['APD'], 'APD', nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera, 'APD': apd})
    recording = RecordingManager(detectors)
    memory_recordings = []

    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory_recordings.append(
            (name, file, path, saved)
        )
    )

    recording.startRecording(
        detectorNames=['Camera', 'APD'],
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'mixed_scan_once'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'Camera': {}, 'APD': {}},
        recFrames=1,
        numCamTTL={'Camera': 4, 'APD': 1},
        stallTimeout=1.0,
    )
    assert recording.waitForAcquisitionStarted(2.0)

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {},
            },
            _minimal_scan_info(),
        )

        assert _wait_for(lambda: len(memory_recordings) == 2, timeout=3.0)
        open_files = []
        try:
            for detector_name, expected_shape in {
                'Camera': (4, 32, 32),
                'APD': (1, 2, 2),
            }.items():
                mem_file, h5file, dataset = _recorded_dataset(
                    memory_recordings, detector_name
                )
                open_files.extend([h5file, mem_file])
                assert dataset.shape == expected_shape
                assert dataset.attrs['recording:expected_frames'] == expected_shape[0]
        finally:
            for file in open_files:
                file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)
