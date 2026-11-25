"""A scan-once recording and a frame that is not from the scan.

The recording's chunk consumer is registered when the recording is armed
(before the scan is announced), and it keeps the first ``recFrames`` frames it
sees. A frame that lands between that boundary and the scan's first trigger
(a leftover trigger, a snap, a trailing pulse of the previous scan) is
therefore frame 0 of the file and the last real frame is not recorded: a
complete, but shifted, recording. These tests pin that behaviour with
index-coded frames; the stale frame is value 1000, the scan's k-th frame k.

This is what the recorder does today (reported, not changed in the BeadRec
fix); the tests document it so a change to the recorder shows up here.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model import RecMode, SaveFormat, SaveMode
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import (
    HamamatsuManager,
)

from .test_mock_scan_simulation_coordinator import (
    _ManualDetectorsManager,
    _ensure_qcore_app,
    _minimal_scan_info,
    _minimal_simulated_setup,
    _recorded_dataset,
    _wait_for,
)

N = 4
STALE = 1000


class IndexFrames:
    def __init__(self):
        self.real = 0
        self.special = []

    def nextFrames(self, n, shape):
        out = []
        for _ in range(n):
            if self.special:
                value = self.special.pop(0)
            else:
                self.real += 1
                value = self.real
            out.append(np.full(shape, value, dtype=np.uint16))
        return out

    def liveFrame(self, shape):
        return np.zeros(shape, dtype=np.uint16)


def _record(tmp_path, beforeScan=None):
    _ensure_qcore_app()
    setup = _minimal_simulated_setup({'Camera': {
        'analogChannel': None, 'digitalLine': None, 'managerName': 'HamamatsuManager',
        'managerProperties': {'cameraListIndex': 'mock', 'hamamatsu': {
            'trigger_source': 2, 'subarray_hsize': 32, 'subarray_vsize': 32}},
        'forAcquisition': True}})
    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    source = IndexFrames()
    camera._camera.mock_frame_source = source
    recording = RecordingManager(_ManualDetectorsManager({'Camera': camera}))
    saved = []
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, ok: saved.append((name, file, path, ok)))
    recording.startRecording(
        detectorNames=['Camera'], recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'stale'), saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5, attrs={'Camera': {}}, recFrames=N,
        numCamTTL={'Camera': 1}, stallTimeout=1.0)
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        if beforeScan is not None:
            beforeScan(camera._camera, source)
        nidaq.runScan({'scanSignalsDict': {}, 'TTLCycleSignalsDict': {}},
                      _minimal_scan_info())
        assert _wait_for(lambda: len(saved) == 1, timeout=3.0)
        file, h5file, dataset = _recorded_dataset(saved, 'Camera')
        try:
            return dataset[:, 0, 0].tolist(), int(dataset.attrs['recording:expected_frames'])
        finally:
            h5file.close()
            file.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)


def test_a_clean_scan_records_its_frames_in_order(tmp_path):
    frames, expected = _record(tmp_path)
    assert frames == [1, 2, 3, 4] and expected == N


def test_a_stale_frame_before_the_first_trigger_is_recorded_as_frame_zero(tmp_path):
    """The recording is complete (N frames) and shifted by one: the stale frame
    is frame 0 and the scan's last frame is not in the file."""
    def inject(camera, source):
        source.special.append(STALE)
        camera.mockTrigger(1)

    frames, expected = _record(tmp_path, beforeScan=inject)
    assert expected == N
    assert frames == [STALE, 1, 2, 3]
