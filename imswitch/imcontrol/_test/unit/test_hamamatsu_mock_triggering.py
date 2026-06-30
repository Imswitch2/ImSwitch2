import json
import time

import numpy as np
from qtpy import QtCore

from imswitch.imcontrol.model import DetectorInfo, SetupInfo
from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)


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


def _minimal_simulated_setup():
    return SetupInfo.from_json(json.dumps({
        'detectors': {
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
        },
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
    }), infer_missing=True)


def test_mock_hamamatsu_external_trigger_queue_is_exact_and_uint16():
    camera = MockHamamatsu()
    camera.startAcquisition()
    camera.setPropertyValue('trigger_source', 2)

    camera.mockTrigger(3)
    frames, shape = camera.getFrames()
    drained, _ = camera.getFrames()

    assert len(frames) == 3
    assert len(drained) == 0
    assert shape == (2048, 2048)
    assert all(frame.dtype == np.uint16 for frame in frames)


def test_mock_hamamatsu_internal_trigger_mode_free_runs():
    camera = MockHamamatsu()
    camera.setPropertyValue('subarray_hsize', 64)
    camera.setPropertyValue('subarray_vsize', 64)
    camera.setPropertyValue('internal_frame_rate', 500)
    camera.startAcquisition()
    camera.setPropertyValue('trigger_source', 1)

    time.sleep(0.02)
    frames, shape = camera.getFrames()

    assert len(frames) > 0
    assert shape == (64, 64)
    assert all(frame.shape == (64, 64) for frame in frames)
    assert all(frame.dtype == np.uint16 for frame in frames)


def test_mock_hamamatsu_flush_clears_external_trigger_queue():
    camera = MockHamamatsu()
    camera.startAcquisition()
    camera.setPropertyValue('trigger_source', 2)

    camera.mockTrigger(2)
    camera.updateIndices()
    frames, _ = camera.getFrames()

    assert frames == []


def test_hamamatsu_manager_exposes_mock_trigger_protocol():
    detector_info = DetectorInfo(
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
    manager = HamamatsuManager(detector_info, 'Camera')
    manager.startAcquisition()

    try:
        manager.mockTrigger(2)
        frames = manager.getChunk()

        assert len(frames) == 2
        assert all(frame.shape == (32, 32) for frame in frames)
        assert manager.mockScanDone() is True
    finally:
        manager.stopAcquisition()


def test_simulated_nidaq_triggers_hamamatsu_manager_mock_frames():
    setup_info = _minimal_simulated_setup()
    nidaq = NidaqManager(setup_info)
    detector_info = DetectorInfo(
        managerName='HamamatsuManager',
        analogChannel=None,
        digitalLine=None,
        managerProperties={
            'cameraListIndex': 'mock',
            'hamamatsu': {
                'trigger_source': 2,
                'subarray_hsize': 64,
                'subarray_vsize': 64,
            },
        },
        forAcquisition=True,
    )
    manager = HamamatsuManager(detector_info, 'Camera', nidaqManager=nidaq)
    manager.startAcquisition()
    done = []
    nidaq.sigScanDone.connect(lambda: done.append(True))

    try:
        nidaq.runScan(
            {
                'scanSignalsDict': {},
                'TTLCycleSignalsDict': {
                    'Camera': np.array([0, 1, 1, 0, 1, 0], dtype=bool),
                },
            },
            {'img_dims': [2, 1], 'scan_samples_total': 10},
        )

        assert _wait_for(lambda: done)
        frames = manager.getChunk()
        assert len(frames) == 2
        assert all(frame.shape == (64, 64) for frame in frames)
        assert all(frame.dtype == np.uint16 for frame in frames)
    finally:
        manager.stopAcquisition()
