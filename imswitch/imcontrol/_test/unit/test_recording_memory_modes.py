"""Recording in memory: refused where a storer cannot honour it, released when dropped."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from imswitch.imcommon.controller.ModuleCommunicationChannel import ModuleCommunicationChannel
from imswitch.imcommon.model.VFileCollection import VFileItem
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
    unsupported_memory_save_mode,
)
from imswitch.imcontrol.model import RecordingManager, SaveFormat, SaveMode


@pytest.mark.parametrize('saveMode,saveFormat,refused', [
    (SaveMode.Disk, SaveFormat.ZARR, False),
    (SaveMode.Disk, SaveFormat.TIFF, False),
    (SaveMode.RAM, SaveFormat.HDF5, False),
    (SaveMode.DiskAndRAM, SaveFormat.HDF5, False),
    (SaveMode.RAM, SaveFormat.ZARR, True),
    (SaveMode.RAM, SaveFormat.TIFF, True),
    (SaveMode.DiskAndRAM, SaveFormat.ZARR, True),
    (SaveMode.DiskAndRAM, SaveFormat.TIFF, True),
])
def test_memory_modes_need_hdf5(saveMode, saveFormat, refused):
    problem = unsupported_memory_save_mode(saveMode, saveFormat)
    assert (problem is not None) is refused
    if refused:
        assert 'HDF5' in problem and saveFormat.name in problem


def test_rec_is_refused_before_anything_is_armed():
    widget = Mock()
    widget.getRecSaveMode.return_value = SaveMode.RAM.value
    widget.getSaveFormat.return_value = SaveFormat.ZARR.value
    stub = SimpleNamespace(_widget=widget, recording=False, _finalizingRecCycle=False)
    stub._RecordingController__logger = Mock()

    RecordingController.toggleREC.__get__(stub)(True)

    widget.setRecButtonChecked.assert_called_once_with(False)
    widget.showRecordingRefused.assert_called_once()
    assert 'HDF5' in widget.showRecordingRefused.call_args.args[0]
    assert stub.recording is False
    assert stub._finalizingRecCycle is False


class _Detector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.1, 0.1]
    parameters = {}


class _Detectors:
    def __getitem__(self, name):
        return _Detector()

    def execOnAll(self, func, *, condition=None):
        return {}


def test_manager_forgets_a_released_memory_recording():
    manager = RecordingManager(_Detectors())
    manager._memRecordings['/tmp/a_rec.hdf5'] = object()

    assert manager.getSaveFilePath('/tmp/a_rec.hdf5') == '/tmp/a_rec_1.hdf5'
    assert manager.releaseMemoryRecording('/tmp/a_rec.hdf5') is True
    assert manager.releaseMemoryRecording('/tmp/a_rec.hdf5') is False
    assert manager.getSaveFilePath('/tmp/a_rec.hdf5') == '/tmp/a_rec.hdf5'


def test_master_releases_the_buffer_when_improcess_drops_the_recording():
    channel = ModuleCommunicationChannel()
    recordingManager = Mock()
    stub = SimpleNamespace(recordingManager=recordingManager)
    stub._MasterController__moduleCommChannel = channel
    channel.memoryRecordings.sigDataWillRemove.connect(
        lambda name: MasterController.memoryRecordingWillBeRemoved(stub, name)
    )
    payload = Mock()
    channel.memoryRecordings['a_rec.hdf5'] = VFileItem(
        data=payload, filePath='/tmp/a_rec.hdf5', savedToDisk=False
    )

    del channel.memoryRecordings['a_rec.hdf5']

    recordingManager.releaseMemoryRecording.assert_called_once_with('/tmp/a_rec.hdf5')
    payload.close.assert_called_once()
