"""Seam tests: producer layout -> recording gate -> worker -> storer -> resolver.

Audit condition 7. Every stage of the acquisition-layout contract had its
own unit tests, and the unit tests agreed with each other; what nobody ran
was one recording through all of them. That is how the RAW-frame cap
regressed unnoticed: each test faked the neighbour it did not own.

These tests drive the real Galvo designer for the scan, the real
producer builders for the layouts, ``RecordingManager.startRecording`` for
the gate, the real recording worker and HDF5 storer for the file, and the
ImProcess resolver to read the layout back. Detectors are the repository's
own mock managers under the simulated NI-DAQ, the same path the mock scan
setup exercises. Three families are covered:

* a plain camera point scan (frame-stream payload, one pulse per position),
* two scan-driven detectors on a two-line-step scan (assembled payload),
  plus the gate refusing a frame-stream layout for such a detector, and
* a single-file scan lapse, one HDF5 group per partition, each resolving
  back to its own partition index.

Run this file in its own pytest process; mixing the imcontrol and
improcess suites in one process hangs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayoutError,
    AcquisitionPartition,
    encode_acquisition_layout,
    recorded_frame_coordinates,
    scan_position_count,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
    build_controller_point_scan_layouts,
    build_point_scan_layouts,
    physical_kind_overrides,
    scan_devices,
    scan_directions,
    scan_driven_detector_names,
    validate_detector_edge_counts,
    with_time_partition,
)
from imswitch.imcontrol.model import RecMode, SaveFormat, SaveMode, SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.acquisition_layout_resolver import (
    resolve_acquisition_layout,
)

from .test_mock_scan_simulation_coordinator import (
    _ManualDetectorsManager,
    _ensure_qcore_app,
    _recorded_dataset,
    _wait_for,
)


COLS, ROWS = 3, 2
POSITIONS = COLS * ROWS
CAMERA_SIZE = 32

_POSITIONER = {
    'managerName': 'NidaqPositionerManager',
    'managerProperties': {
        'conversionFactor': 1, 'minVolt': -10, 'maxVolt': 10,
        'vel_max': 0.5, 'acc_max': 0.05,
    },
    'forScanning': True,
    'forPositioning': True,
}

CAMERA_JSON = {
    'analogChannel': None,
    'digitalLine': None,
    'managerName': 'HamamatsuManager',
    'managerProperties': {
        'cameraListIndex': 'mock',
        'hamamatsu': {
            'trigger_source': 2,
            'subarray_hsize': CAMERA_SIZE,
            'subarray_vsize': CAMERA_SIZE,
        },
    },
    'forAcquisition': True,
}


def _apd_json(index):
    return {
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


def _galvo_setup(detectors_json):
    """A Galvo-designer rig with X/Y/Z single-axis stages under a simulated DAQ."""
    return SetupInfo.from_json(
        json.dumps({
            'detectors': detectors_json,
            'lasers': {},
            'positioners': {
                axis: dict(_POSITIONER, analogChannel=index, axes=[axis])
                for index, axis in enumerate(('X', 'Y', 'Z'))
            },
            'scan': {
                'scanWidgetType': 'PointScan',
                'scanDesigner': 'GalvoScanDesigner',
                'scanDesignerParams': {},
                'TTLCycleDesigner': 'AdvancedScanTTLCycleDesigner',
                'TTLCycleDesignerParams': {},
                'sampleRate': 100000,
            },
            'nidaq': {
                'simulation': True,
                'timerCounterChannel': None,
                'startTrigger': False,
            },
        }),
        infer_missing=True,
    )


def _galvo_scan(setup, *, linesteps):
    """Ask the real designer for a 3x2 X/Y scan (Z listed but inactive)."""
    parameters = {
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [COLS, ROWS, 0],
        'axis_step_size': [1, 1, 1],
        'axis_centerpos': [0, 0, 0],
        'axis_startpos': [[0], [0], [0]],
        'sequence_time': 0.001,
        'phase_delay': 0,
        'd3step_delay': 100,
        'n_linesteps': linesteps,
    }
    sig_dict, _, scan_info = GalvoScanDesigner().make_signal(parameters, setup)
    assert list(scan_info['img_dims']) == [COLS, ROWS]
    return parameters, sig_dict, scan_info


def _camera_ttl(setup, scan_info, *, linesteps):
    """One camera pulse per position and line step, from the real TTL designer."""
    ttl_parameters = {
        'target_device': ['Camera'],
        'n_linesteps': linesteps,
        'linestep_enable': {'Camera': [True] * linesteps},
        'pulse_starts_s': {'Camera': [[0]] * linesteps},
        'pulse_ends_s': {'Camera': [[0.0005]] * linesteps},
        'sequence_time': 0.001,
        'advanced_mode': True,
    }
    signals, _ = AdvancedScanTTLCycleDesigner().make_signal(
        ttl_parameters, setup, scan_info
    )
    return signals


class _PointScanController:
    """The slice of a point-scan controller the layout accessor reads.

    The accessor asks the controller for its parameters, its scan manager's
    signals, its pulse declaration and its master's detectors manager; the
    rest of a controller is Qt. Signals come from the real designer.
    """

    def __init__(self, setup, detectors, parameters, sig_dict, scan_info, pulses):
        self._setupInfo = setup
        self._analogParameterDict = parameters
        self._digitalParameterDict = {}
        self._master = SimpleNamespace(
            detectorsManager=detectors,
            scanManager=SimpleNamespace(
                makeFullScan=lambda analog, digital: (sig_dict, scan_info)
            ),
        )
        self._pulses = dict(pulses)

    def getParameters(self):
        pass

    def getNumCamTTL(self):
        return dict(self._pulses)


@pytest.fixture
def qt_app():
    """Hold the QCoreApplication for the whole test.

    ``_ensure_qcore_app`` creates the application if none exists, and under
    PyQt5 dropping the last Python reference to it tears down every
    Python-owned QObject with it -- the managers included. A single-session
    test never notices because nothing emits after its last wait; the lapse
    test starts a second session and found both managers gone.
    """
    return _ensure_qcore_app()


def _collect_memory_recordings(recording):
    memory_recordings = []
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory_recordings.append(
            (name, file, path, saved)
        )
    )
    return memory_recordings


def _resolve(dataset, detector):
    return resolve_acquisition_layout(
        dict(dataset.attrs), shape=dataset.shape, detector=detector
    )


def _text(value):
    """h5py hands fixed-length string attrs back as bytes."""
    return value.decode() if isinstance(value, bytes) else str(value)


# ----------------------------------------------------------------------
# Camera point scan
# ----------------------------------------------------------------------


def test_camera_point_scan_layout_survives_producer_gate_worker_storer_resolver(qt_app, tmp_path):
    setup = _galvo_setup({'Camera': CAMERA_JSON})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)
    memory_recordings = _collect_memory_recordings(recording)

    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=1)
    signals = _camera_ttl(setup, scan_info, linesteps=1)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={'Camera': 1}
    )

    # Producer: the controller-level accessor, on the designer's own output.
    layouts = build_controller_point_scan_layouts(controller, ('Camera',))
    layout = layouts['Camera']
    assert layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM
    assert [loop.kind for loop in layout.event_loops] == ['scan_y', 'scan_x']
    assert {loop.kind: loop.device for loop in layout.event_loops} == {
        'scan_y': 'Y', 'scan_x': 'X'
    }
    assert layout.scan_source == '_PointScanController'
    assert scan_position_count(layout) == POSITIONS
    # The layout's frame plan agrees with the TTL the scan will actually run.
    validate_detector_edge_counts(layouts, signals)

    recording.startRecording(
        detectorNames=['Camera'],
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'camera_point_scan'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={'Camera': {}},
        recFrames=POSITIONS,
        numCamTTL=controller.getNumCamTTL(),
        stallTimeout=1.0,
        acquisitionLayouts=layouts,
    )
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        nidaq.runScan(
            {'scanSignalsDict': sig_dict, 'TTLCycleSignalsDict': signals},
            scan_info,
        )
        assert _wait_for(lambda: len(memory_recordings) == 1, timeout=3.0)
        mem_file, h5file, dataset = _recorded_dataset(memory_recordings, 'Camera')
        try:
            assert dataset.shape == (POSITIONS, CAMERA_SIZE, CAMERA_SIZE)
            assert dataset.attrs['recording:expected_frames'] == POSITIONS
            assert _text(dataset.attrs['recording:completion_outcome']) == 'complete'
            assert int(dataset.attrs.get('recording:discarded_frames', 0)) == 0

            resolved = _resolve(dataset, 'Camera')
            assert resolved.is_usable, [issue.code for issue in resolved.issues]
            assert resolved.is_authoritative
            assert encode_acquisition_layout(resolved.layout) == (
                encode_acquisition_layout(layout)
            )
            assert recorded_frame_coordinates(resolved.layout, POSITIONS - 1) == {
                'scan_y': ROWS - 1, 'scan_x': COLS - 1
            }
        finally:
            h5file.close()
            mem_file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()


# ----------------------------------------------------------------------
# Scan-driven detectors on a line-step scan
# ----------------------------------------------------------------------


def test_scan_driven_detectors_record_the_assembled_layout_and_refuse_a_frame_stream(qt_app, tmp_path):
    names = ('APDred', 'APDgreen')
    setup = _galvo_setup({name: _apd_json(i) for i, name in enumerate(names)})
    nidaq = NidaqManager(setup)
    apds = {name: APDManager(setup.detectors[name], name, nidaq) for name in names}
    detectors = _ManualDetectorsManager(apds)

    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=2)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={}
    )
    # The real APD managers say they assemble; that is what picks the payload.
    assert scan_driven_detector_names(controller, names) == names

    assembled = build_advanced_scan_layouts(
        scan_info,
        names,
        scan_source='ScanControllerAdvanced',
        detector_masks={},
        pulse_counts_by_condition={},
        scan_driven_detectors=scan_driven_detector_names(controller, names),
        directions=scan_directions(controller, scan_info),
        devices=scan_devices(controller, scan_info),
        kind_overrides=physical_kind_overrides(controller, scan_info),
    )
    for name in names:
        assert assembled[name].payload_kind == PAYLOAD_ASSEMBLED_IMAGE
        assert assembled[name].storage_axes == (
            'frame', 'condition', 'scan_y', 'scan_x'
        )
        assert scan_position_count(assembled[name]) == POSITIONS

    # Gate: a frame-stream layout for an assembling detector is refused before
    # any writer opens, with the code the rig will see.
    probe = RecordingManager(detectors)
    opened = []
    probe._RecordingManager__prepareRecordingThread = lambda: opened.append(True)
    frame_stream = build_point_scan_layouts(
        scan_info, ('APDred',), scan_source='ScanControllerAdvanced',
        pulse_counts={'APDred': 1},
    )
    with pytest.raises(AcquisitionLayoutError) as error:
        probe.startRecording(
            detectorNames=['APDred'],
            recMode=RecMode.ScanOnce,
            savename=str(tmp_path / 'must_not_open'),
            saveMode=SaveMode.RAM,
            saveFormat=SaveFormat.HDF5,
            attrs={'APDred': {}},
            recFrames=POSITIONS,
            stallTimeout=1.0,
            acquisitionLayouts=frame_stream,
        )
    assert 'DETECTOR_PAYLOAD_MISMATCH' in {issue.code for issue in error.value.issues}
    assert opened == []

    # The assembled layouts go all the way to the file and back.
    recording = RecordingManager(detectors)
    memory_recordings = _collect_memory_recordings(recording)
    recording.startRecording(
        detectorNames=list(names),
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'two_apd_linesteps'),
        saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5,
        attrs={name: {} for name in names},
        recFrames=POSITIONS,
        stallTimeout=1.0,
        acquisitionLayouts=assembled,
    )
    assert recording.waitForAcquisitionStarted(2.0)
    assert recording.markScanStarted(scan_info, recording.recordingGeneration)
    try:
        nidaq.runScan(
            {'scanSignalsDict': sig_dict, 'TTLCycleSignalsDict': {}}, scan_info
        )
        assert _wait_for(lambda: len(memory_recordings) == 2, timeout=3.0)
        open_files = []
        try:
            for name in names:
                mem_file, h5file, dataset = _recorded_dataset(memory_recordings, name)
                open_files.extend([h5file, mem_file])
                assert dataset.shape == (1, 2, ROWS, COLS)
                assert _text(dataset.attrs['axes']) == 'TCYX'
                assert dataset.attrs['recording:expected_frames'] == 1
                assert np.count_nonzero(dataset[:]) > 0

                resolved = _resolve(dataset, name)
                assert resolved.is_usable, [issue.code for issue in resolved.issues]
                assert resolved.is_authoritative
                assert encode_acquisition_layout(resolved.layout) == (
                    encode_acquisition_layout(assembled[name])
                )
        finally:
            for file in open_files:
                file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()


# ----------------------------------------------------------------------
# Single-file scan lapse: one group per partition
# ----------------------------------------------------------------------


def test_single_file_lapse_writes_one_group_per_partition_that_resolves_back(qt_app, tmp_path):
    setup = _galvo_setup({'Camera': CAMERA_JSON})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)
    ended = []
    recording.sigRecordingEndedDetailed.connect(lambda generation: ended.append(generation))

    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=1)
    signals = _camera_ttl(setup, scan_info, linesteps=1)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={'Camera': 1}
    )
    base = build_controller_point_scan_layouts(controller, ('Camera',))['Camera']
    assert base.partitions == ()

    savename = str(tmp_path / 'lapse')
    total = 2
    try:
        for index in range(total):
            # What RecordingController._applyAcquisitionLayoutPartitions hands
            # the manager for each lapse item.
            partitioned = with_time_partition(
                base, index=index, planned_count=total, single_file=True
            )
            recording.startRecording(
                detectorNames=['Camera'],
                recMode=RecMode.ScanLapse,
                savename=savename,
                saveMode=SaveMode.Disk,
                saveFormat=SaveFormat.HDF5,
                singleLapseFile=True,
                attrs={'Camera': {}},
                recFrames=POSITIONS,
                numCamTTL=controller.getNumCamTTL(),
                stallTimeout=1.0,
                recLapseTotal=total,
                recLapseIndex=index,
                acquisitionLayouts={'Camera': partitioned},
            )
            assert recording.waitForAcquisitionStarted(2.0)
            nidaq.runScan(
                {'scanSignalsDict': sig_dict, 'TTLCycleSignalsDict': signals},
                scan_info,
            )
            assert _wait_for(lambda: len(ended) == index + 1, timeout=3.0)
            recording.endRecording(emitSignal=False, wait=True)
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()

    path = tmp_path / 'lapse_Camera.hdf5'
    assert path.exists()
    assert not (tmp_path / 'lapse_Camera_1.hdf5').exists(), 'lapse items must share one file'

    # Through the reader a user actually opens the file with, not only through
    # h5py. Reading the attributes of a dataset found by hand proves the
    # resolver understands them; it does not prove ImProcess can find the
    # dataset at all, and for a single-file lapse it could not -- discovery
    # looked only at the container root, where a lapse recording has no
    # datasets, and reported every one of these files as empty.
    assert DataObj.getDatasetNames(str(path)) == ['scan0/Camera', 'scan1/Camera']
    with h5py.File(path, 'r') as h5file:
        assert sorted(h5file.keys()) == ['scan0', 'scan1']
        for index in range(total):
            dataset = h5file[f'scan{index}']['Camera']['data']
            assert dataset.shape == (POSITIONS, CAMERA_SIZE, CAMERA_SIZE)
            assert dataset.attrs['recording:lapse_index'] == index
            assert dataset.attrs['recording:num_timepoints'] == total
            assert bool(dataset.attrs['recording:single_lapse_file']) is True
            assert _text(dataset.attrs['recording:completion_outcome']) == 'complete'

            resolved = _resolve(dataset, 'Camera')
            assert resolved.is_usable, [issue.code for issue in resolved.issues]
            assert resolved.is_authoritative
            assert resolved.layout.partitions == (
                AcquisitionPartition(
                    'time', index=index, planned_count=total,
                    storage='one-group-per-item',
                ),
            )
            assert replace(resolved.layout, partitions=()) == base

            opened = DataObj(
                str(path), f'scan{index}/Camera', path=str(path)
            ).acquisition_layout
            assert opened.is_authoritative
            assert opened.layout == resolved.layout


def test_a_disk_and_ram_lapse_keeps_every_timepoint(qt_app, tmp_path):
    """Handing the finished file to memory must not lock the next timepoint.

    "Save on disk and keep in memory" reopens each finished file read-only and
    hands the live handle to the app, which keeps it. For a lapse written as a
    single file that is the same path the next timepoint opens for append, and
    HDF5 refuses that while any read handle is open -- so timepoint 1 failed to
    open, the recording aborted, and the abort removed the file, destroying the
    timepoints already acquired. A ten-point lapse produced no file at all.

    The sink here is what makes the test real: the app stores the handle, so a
    test whose listener drops it cannot see the fault.
    """
    setup = _galvo_setup({'Camera': CAMERA_JSON})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)

    held = {}
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: held.__setitem__(name, file)
    )
    ended = []
    recording.sigRecordingEndedDetailed.connect(lambda generation: ended.append(generation))
    failures = []
    recording.sigRecordingFailed.connect(failures.append)

    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=1)
    signals = _camera_ttl(setup, scan_info, linesteps=1)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={'Camera': 1}
    )
    base = build_controller_point_scan_layouts(controller, ('Camera',))['Camera']

    total = 3
    try:
        for index in range(total):
            recording.startRecording(
                detectorNames=['Camera'],
                recMode=RecMode.ScanLapse,
                savename=str(tmp_path / 'lapse'),
                saveMode=SaveMode.DiskAndRAM,
                saveFormat=SaveFormat.HDF5,
                singleLapseFile=True,
                attrs={'Camera': {}},
                recFrames=POSITIONS,
                numCamTTL=controller.getNumCamTTL(),
                stallTimeout=1.0,
                recLapseTotal=total,
                recLapseIndex=index,
                acquisitionLayouts={'Camera': with_time_partition(
                    base, index=index, planned_count=total, single_file=True
                )},
            )
            assert recording.waitForAcquisitionStarted(2.0)
            nidaq.runScan(
                {'scanSignalsDict': sig_dict, 'TTLCycleSignalsDict': signals},
                scan_info,
            )
            assert _wait_for(
                lambda: len(ended) == index + 1 or failures, timeout=3.0
            )
            assert not failures, failures
            recording.endRecording(emitSignal=False, wait=True)
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()

    path = tmp_path / 'lapse_Camera.hdf5'
    assert path.exists()
    with h5py.File(path, 'r') as h5file:
        assert sorted(h5file.keys()) == ['scan0', 'scan1', 'scan2']
    assert DataObj.getDatasetNames(str(path)) == [
        'scan0/Camera', 'scan1/Camera', 'scan2/Camera'
    ]
    # What the app was handed is a readable copy, still open, and holding no
    # claim on the path the later timepoints had to write.
    assert held, 'the memory hand-off never happened'
    for handle in held.values():
        assert isinstance(handle, h5py.File)
        assert list(handle.keys())


def test_aborting_one_lapse_timepoint_keeps_the_ones_already_recorded(tmp_path):
    """A shared lapse file is not this session's to delete.

    The abort path removed the on-disk file outright, which for a single-file
    lapse is the file holding every timepoint recorded so far.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer

    existing = tmp_path / 'lapse_Camera.hdf5'
    with h5py.File(existing, 'w') as h5file:
        h5file.create_group('scan0').create_group('Camera').create_dataset(
            'data', data=np.zeros((2, 4, 4), dtype=np.uint16)
        )
    fresh = tmp_path / 'other_Camera.hdf5'

    storer = HDF5Storer(str(tmp_path / 'lapse'), _ManualDetectorsManager({}))
    storer.openStream(
        {'Camera': str(existing), 'Other': str(fresh)},
        [], {}, {},
        singleMultiDetectorFile=False, singleLapseFile=True, saveMode=SaveMode.Disk,
    )
    with open(fresh, 'wb') as handle:
        handle.write(b'partial')

    storer.abortStream(
        {'Camera': str(existing), 'Other': str(fresh)}, {}, SaveMode.Disk
    )

    assert existing.exists(), 'an abort destroyed earlier timepoints'
    assert not fresh.exists(), 'this session\'s own partial file should go'


def _record_single_file_lapse(recording, nidaq, controller, base, tmp_path, total):
    """One complete single-file scan lapse into ``tmp_path/'lapse'``."""
    ended = []
    connection = recording.sigRecordingEndedDetailed.connect(
        lambda generation: ended.append(generation)
    )
    parameters, sig_dict, scan_info = controller._analogParameterDict, None, None
    for index in range(total):
        recording.startRecording(
            detectorNames=['Camera'],
            recMode=RecMode.ScanLapse,
            savename=str(tmp_path / 'lapse'),
            saveMode=SaveMode.Disk,
            saveFormat=SaveFormat.HDF5,
            singleLapseFile=True,
            attrs={'Camera': {}},
            recFrames=POSITIONS,
            numCamTTL=controller.getNumCamTTL(),
            stallTimeout=1.0,
            recLapseTotal=total,
            recLapseIndex=index,
            acquisitionLayouts={'Camera': with_time_partition(
                base, index=index, planned_count=total, single_file=True
            )},
        )
        assert recording.waitForAcquisitionStarted(2.0)
        nidaq.runScan(
            {
                'scanSignalsDict': controller._scan_signals,
                'TTLCycleSignalsDict': controller._ttl_signals,
            },
            controller._scan_info,
        )
        assert _wait_for(lambda: len(ended) == index + 1, timeout=3.0)
        recording.endRecording(emitSignal=False, wait=True)
    recording.sigRecordingEndedDetailed.disconnect(connection)


def test_a_second_lapse_run_does_not_append_to_the_first_ones_file(qt_app, tmp_path):
    """Each run of a single-file lapse gets its own file.

    The exemption that lets timepoints 1..N-1 append to the file timepoint 0
    created was expressed as "this path may always be overwritten", which is
    also true of the first timepoint of the *next* run. A repeated lapse
    therefore appended into the previous run's file, with group numbering that
    continued from it -- scan2, scan3 -- while every recorded time-partition
    index restarted at 0, so two groups in one file claimed to be timepoint 0
    of the same lapse.
    """
    setup = _galvo_setup({'Camera': CAMERA_JSON})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=1)
    signals = _camera_ttl(setup, scan_info, linesteps=1)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={'Camera': 1}
    )
    controller._scan_signals = sig_dict
    controller._ttl_signals = signals
    controller._scan_info = scan_info
    base = build_controller_point_scan_layouts(controller, ('Camera',))['Camera']

    total = 2
    try:
        for _ in range(2):
            recording = RecordingManager(detectors)
            _record_single_file_lapse(
                recording, nidaq, controller, base, tmp_path, total
            )
            recording.endRecording(emitSignal=False, wait=True)
    finally:
        nidaq.finalize()

    files = sorted(path.name for path in tmp_path.iterdir() if path.is_file())
    assert files == ['lapse_Camera.hdf5', 'lapse_Camera_1.hdf5'], files
    for name in files:
        with h5py.File(tmp_path / name, 'r') as h5file:
            assert sorted(h5file.keys()) == ['scan0', 'scan1']
            for index, group in enumerate(['scan0', 'scan1']):
                dataset = h5file[group]['Camera']['data']
                assert dataset.attrs['recording:lapse_index'] == index



def test_a_recording_that_produced_no_frames_still_says_what_it_was(qt_app, tmp_path):
    """A stub file must be recognisable as an interrupted recording.

    Stopping a recording before its first frame arrives still leaves a file:
    the storers create the dataset lazily, and finalize only walks datasets
    that exist. What was left behind said nothing at all -- not the detector,
    not that a recording had been stopped, not how much of the plan was
    missing -- so it read as a corrupt file rather than an empty one.
    """
    setup = _galvo_setup({'Camera': CAMERA_JSON})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)

    parameters, sig_dict, scan_info = _galvo_scan(setup, linesteps=1)
    controller = _PointScanController(
        setup, detectors, parameters, sig_dict, scan_info, pulses={'Camera': 1}
    )
    layouts = build_controller_point_scan_layouts(controller, ('Camera',))

    recording.startRecording(
        detectorNames=['Camera'],
        recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'stopped'),
        saveMode=SaveMode.Disk,
        saveFormat=SaveFormat.HDF5,
        attrs={'Camera': {}},
        recFrames=POSITIONS,
        numCamTTL=controller.getNumCamTTL(),
        stallTimeout=1.0,
        acquisitionLayouts=layouts,
    )
    assert recording.waitForAcquisitionStarted(2.0)
    # Stopped before the scan ever ran: no frame is ever delivered.
    recording.endRecording(emitSignal=False, wait=True)
    nidaq.finalize()

    path = tmp_path / 'stopped_Camera.hdf5'
    assert path.exists()
    with h5py.File(path, 'r') as h5file:
        assert list(h5file.keys()) == [], 'an empty recording has no dataset'
        attrs = dict(h5file.attrs)
    assert _text(attrs['recording:detector_name']) == 'Camera'
    assert _text(attrs['recording:completion_outcome']) == 'stopped_early'
    assert int(attrs['recording:actual_frames']) == 0
    assert int(attrs['recording:planned_frames']) == POSITIONS

    # And the reader says the honest thing rather than offering a dataset.
    with pytest.raises(RuntimeError, match='does not contain any datasets'):
        DataObj.getDatasetNames(str(path))
