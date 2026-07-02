"""Tests for the live reconstruction completion gate.

Verifies that stores are only processed once they are complete (writing=False
or absent), and that incomplete stores do not block later complete ones.
"""

import os
from collections import deque
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import zarr
from qtpy import QtCore

from imswitch.improcess.controller.LiveModeController import LiveModeController


class _FakeExtension:
    def __init__(self, value='zarr'):
        self._value = value
        self.sigValueChanged = MagicMock()

    def value(self):
        return self._value


class _FakeCommChannel:
    def __init__(self, extension='zarr'):
        self.extension = _FakeExtension(extension)
        self.sigResultProduced = MagicMock()
        self.sigLiveResultUpdated = MagicMock()


class _FakeWidget:
    def __init__(self, path=None):
        self.path = path
        self.liveCheck = MagicMock()
        self.sigLiveChanged = MagicMock()


class _FakeReconstructor:
    def __init__(self, name='test_recon'):
        self.name = name
        self.supports_streaming = False


class _FakeMainController:
    def __init__(self, reconstructor=None):
        self._activeReconstructor = reconstructor or _FakeReconstructor()


class _FakeLiveReconstructionController(QtCore.QObject):
    """Fake LiveReconstructionController that records start() calls."""

    sigFinished = QtCore.Signal()

    def __init__(self, comm_channel):
        super().__init__()
        self._commChannel = comm_channel
        self.start_calls = []
        self.stop_calls = []
        self.start_return = True

    def start(self, reconstructor, source, params, source_arg=None):
        self.start_calls.append({'source': source, 'source_arg': source_arg})
        return self.start_return

    def stop(self):
        self.stop_calls.append(True)


def _make_controller(folder_path='/tmp/test', extension='zarr'):
    """Create a LiveModeController with fakes using __new__ bypass."""
    controller = LiveModeController.__new__(LiveModeController)
    controller._widget = _FakeWidget(path=folder_path)
    controller._commChannel = _FakeCommChannel(extension=extension)
    controller._mainController = _FakeMainController()
    controller._logger = MagicMock()

    controller._liveController = _FakeLiveReconstructionController(controller._commChannel)
    controller._scanTimer = None
    controller._storeQueue = deque()
    controller._seenKeys = set()
    controller._currentlyProcessing = False
    controller._watchedFolder = folder_path
    controller._extension = extension
    return controller


def _make_structured_zarr_store(path, writing=None):
    """Create a structured Zarr store with detector/data layout."""
    root = zarr.open(path, mode='w')
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    
    if writing is not None:
        data.attrs['writing'] = writing
    
    return path


def _make_structured_hdf5_store(path, writing=None):
    """Create a structured HDF5 store with detector/data layout."""
    with h5py.File(path, 'w') as f:
        detector = f.create_group('CAM')
        data = detector.create_dataset('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
        data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
        
        if writing is not None:
            data.attrs['writing'] = writing


# --- Single-file store tests --------------------------------------------------

def test_incomplete_zarr_store_not_processed(tmp_path):
    """A Zarr store with writing=True is not processed."""
    store_path = str(tmp_path / 'rec.zarr')
    _make_structured_zarr_store(store_path, writing=True)
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should not have been processed
    assert len(controller._liveController.start_calls) == 0
    # Should still be in queue
    assert len(controller._storeQueue) == 1


def test_complete_zarr_store_is_processed(tmp_path):
    """A Zarr store with writing=False is processed."""
    store_path = str(tmp_path / 'rec.zarr')
    _make_structured_zarr_store(store_path, writing=False)
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed
    assert len(controller._liveController.start_calls) == 1
    assert controller._liveController.start_calls[0]['source_arg'] == store_path


def test_becomes_processed_once_complete(tmp_path):
    """A store becomes processed when writing changes from True to False."""
    store_path = str(tmp_path / 'rec.zarr')
    _make_structured_zarr_store(store_path, writing=True)
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        # First attempt: still writing
        controller._processNextStore()
        assert len(controller._liveController.start_calls) == 0
        
        # Mark as complete
        root = zarr.open(store_path, mode='r+')
        data = root['CAM/data']
        data.attrs['writing'] = False
        
        # Second attempt: now complete
        controller._processNextStore()
        assert len(controller._liveController.start_calls) == 1


def test_legacy_zarr_store_no_writing_attr_is_complete(tmp_path):
    """A Zarr store with no writing attribute is treated as complete."""
    store_path = str(tmp_path / 'rec.zarr')
    _make_structured_zarr_store(store_path)  # No writing attribute
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed (legacy store)
    assert len(controller._liveController.start_calls) == 1


def test_incomplete_hdf5_store_not_processed(tmp_path):
    """An HDF5 store with writing=True is not processed."""
    store_path = str(tmp_path / 'rec.h5')
    _make_structured_hdf5_store(store_path, writing=True)
    
    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should not have been processed
    assert len(controller._liveController.start_calls) == 0


def test_complete_hdf5_store_is_processed(tmp_path):
    """An HDF5 store with writing=False is processed."""
    store_path = str(tmp_path / 'rec.h5')
    _make_structured_hdf5_store(store_path, writing=False)
    
    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed
    assert len(controller._liveController.start_calls) == 1


def test_legacy_hdf5_store_no_writing_attr_is_complete(tmp_path):
    """An HDF5 store with no writing attribute is treated as complete."""
    store_path = str(tmp_path / 'rec.h5')
    _make_structured_hdf5_store(store_path)  # No writing attribute
    
    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed (legacy store)
    assert len(controller._liveController.start_calls) == 1


# --- Multi-file lapse tests ---------------------------------------------------

def test_lapse_seed_complete_starts_even_with_later_files_missing(tmp_path):
    """A multi-file lapse job starts as soon as its seed file is complete.

    Later timepoint files need not exist yet: the multi-file sources
    tail-follow them as they land, which is what gives per-timepoint live
    updates during a running lapse. Waiting for all files would show nothing
    until the lapse ends.
    """
    tp0_path = str(tmp_path / 'rec_scan__00__CAM.zarr')
    root = zarr.open(tp0_path, mode='w')
    root.attrs['recording:num_timepoints'] = 3
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = False

    # Timepoints 1 and 2 do not exist yet (lapse still running).

    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((tp0_path, True))

    with patch('imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()

    assert len(controller._liveController.start_calls) == 1


def test_lapse_seed_still_writing_not_processed(tmp_path):
    """A multi-file lapse whose seed file is still being written must wait."""
    tp0_path = str(tmp_path / 'rec_scan__00__CAM.zarr')
    root = zarr.open(tp0_path, mode='w')
    root.attrs['recording:num_timepoints'] = 3
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = True

    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((tp0_path, True))

    with patch('imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()

    assert len(controller._liveController.start_calls) == 0


def test_complete_lapse_all_files_present_and_last_complete(tmp_path):
    """A finished lapse (all files complete) is processed."""
    # Create all timepoints
    for i in range(3):
        tp_path = str(tmp_path / f'rec_scan__0{i}__CAM.zarr')
        root = zarr.open(tp_path, mode='w')
        if i == 0:
            root.attrs['recording:num_timepoints'] = 3
        detector = root.create_group('CAM')
        data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
        data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
        data.attrs['writing'] = False

    tp0_path = str(tmp_path / 'rec_scan__00__CAM.zarr')
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((tp0_path, True))

    with patch('imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()

    # Should have been processed
    assert len(controller._liveController.start_calls) == 1


def test_lapse_hdf5_complete_is_processed(tmp_path):
    """An HDF5 lapse with all files complete is processed."""
    # Create all timepoints
    for i in range(2):
        tp_path = str(tmp_path / f'rec_scan__0{i}__CAM.h5')
        with h5py.File(tp_path, 'w') as f:
            if i == 0:
                f.attrs['recording:num_timepoints'] = 2
            detector = f.create_group('CAM')
            data = detector.create_dataset('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
            data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
            data.attrs['writing'] = False
    
    tp0_path = str(tmp_path / 'rec_scan__00__CAM.h5')
    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._storeQueue.append((tp0_path, True))
    
    with patch('imswitch.improcess.controller.LiveModeController.Hdf5MultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed
    assert len(controller._liveController.start_calls) == 1


# --- Single-file (scan{N}) timelapse stores ------------------------------------

def _run_single_file_lapse_cycle(storer_cls, path, lapse_index, num_timepoints):
    """Drive one real storer recording cycle of a single-file lapse.

    Uses the actual HDF5Storer/ZarrStorer streaming path (openStream +
    writeFrames with singleLapseFile=True) so the on-disk layout — scan{N}
    groups, dataset-level recording:* attrs, writing flags — is exactly what
    the recorder produces. Returns the storer; call finalizeStream on it to
    finish the cycle.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import SaveMode

    det = MagicMock()
    det.dtype = np.uint16
    det.pixelSizeUm = [1, 0.1, 0.1]
    detMgr = MagicMock()
    detMgr.__getitem__ = MagicMock(return_value=det)

    storer = storer_cls(path, detMgr)
    storer.omeMeta = {}
    attrs = {'CAM': {
        'recording:num_timepoints': num_timepoints,
        'recording:lapse_index': lapse_index,
        'recording:single_lapse_file': True,
        'recording:expected_frames': 4,
    }}
    storer.openStream(fileDests={'CAM': path}, detectorNames=['CAM'],
                      shapes={'CAM': (8, 8)}, attrs=attrs,
                      singleMultiDetectorFile=False, singleLapseFile=True,
                      saveMode=SaveMode.Disk)
    storer.writeFrames('CAM', np.ones((4, 8, 8), dtype=np.uint16))
    return storer


def _finish_cycle(storer, path):
    from imswitch.imcontrol.model.managers.RecordingManager import SaveMode
    storer.finalizeStream({'CAM': 4}, {'CAM': path}, MagicMock(), SaveMode.Disk)


def test_single_file_hdf5_lapse_completes_only_when_all_timepoints_present(tmp_path):
    """A single-file HDF5 lapse (scan{N} groups) is complete only at the end.

    Mid-cycle (writing=True) and between cycles (all present groups complete
    but fewer than recording:num_timepoints) must both stay incomplete —
    the recorder reopens the file in append mode for each next timepoint, so
    processing it early would race the recording.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer

    path = str(tmp_path / 'lapse.hdf5')
    controller = _make_controller(folder_path=str(tmp_path), extension='hdf5')

    storer = _run_single_file_lapse_cycle(HDF5Storer, path, 0, 2)
    assert controller._is_single_file_complete(path) is False  # mid cycle 0
    _finish_cycle(storer, path)
    assert controller._is_single_file_complete(path) is False  # between cycles

    storer = _run_single_file_lapse_cycle(HDF5Storer, path, 1, 2)
    assert controller._is_single_file_complete(path) is False  # mid cycle 1
    _finish_cycle(storer, path)
    assert controller._is_single_file_complete(path) is True   # lapse finished


def test_single_file_zarr_lapse_completes_only_when_all_timepoints_present(tmp_path):
    """Zarr twin of the single-file lapse completion progression."""
    from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer

    path = str(tmp_path / 'lapse.zarr')
    controller = _make_controller(folder_path=str(tmp_path))

    storer = _run_single_file_lapse_cycle(ZarrStorer, path, 0, 2)
    assert controller._is_single_file_complete(path) is False  # mid cycle 0
    _finish_cycle(storer, path)
    assert controller._is_single_file_complete(path) is False  # between cycles

    storer = _run_single_file_lapse_cycle(ZarrStorer, path, 1, 2)
    assert controller._is_single_file_complete(path) is False  # mid cycle 1
    _finish_cycle(storer, path)
    assert controller._is_single_file_complete(path) is True   # lapse finished


def test_single_file_lapse_unknown_num_timepoints_stays_incomplete(tmp_path):
    """Without recording:num_timepoints a scan{N} file cannot prove it is done.

    Between lapse cycles every present scan group is momentarily complete, so
    the expected count is the only way to distinguish "between cycles" from
    "finished" — unknown count must be treated as incomplete rather than
    risking a read that races the recorder's append reopen.
    """
    path = str(tmp_path / 'lapse.hdf5')
    with h5py.File(path, 'w') as f:
        for n in range(2):
            group = f.create_group(f'scan{n}/CAM')
            data = group.create_dataset('data', data=np.zeros((4, 8, 8), dtype=np.uint16))
            data.attrs['writing'] = False  # no recording:num_timepoints anywhere

    controller = _make_controller(folder_path=str(tmp_path), extension='hdf5')
    assert controller._is_single_file_complete(path) is False


# --- Mid-write admission via the frames_committed barrier ----------------------

def _make_midwrite_zarr_store_with_barrier(path, committed=4):
    """A store the recorder is still writing, carrying the committed barrier."""
    root = zarr.open(path, mode='w')
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(committed, 8, 8), dtype='uint16',
                                 chunks=(1, 8, 8))
    data[:] = 7
    data.attrs['writing'] = True
    data.attrs['recording:frames_committed'] = committed
    return path


def test_midwrite_store_with_barrier_admitted_for_streaming_reconstructor(tmp_path):
    """A still-writing store with the barrier is processed when streaming.

    With frames_committed the live sources never read past flushed data, so a
    streaming reconstructor can follow the recording as it grows — this is
    what makes reconstruction live DURING a recording instead of after it.
    """
    store_path = str(tmp_path / 'rec.zarr')
    _make_midwrite_zarr_store_with_barrier(store_path)

    controller = _make_controller(folder_path=str(tmp_path))
    controller._mainController._activeReconstructor.supports_streaming = True
    controller._storeQueue.append((store_path, False))

    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()

    assert len(controller._liveController.start_calls) == 1


def test_midwrite_store_with_barrier_not_admitted_for_batch_reconstructor(tmp_path):
    """Batch reconstructors read a store once — they need it complete."""
    store_path = str(tmp_path / 'rec.zarr')
    _make_midwrite_zarr_store_with_barrier(store_path)

    controller = _make_controller(folder_path=str(tmp_path))
    controller._mainController._activeReconstructor.supports_streaming = False
    controller._storeQueue.append((store_path, False))

    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()

    assert len(controller._liveController.start_calls) == 0


def test_midwrite_store_without_barrier_not_admitted(tmp_path):
    """A legacy mid-write store (no barrier) must wait for completion.

    Without frames_committed a reader would trust array.shape, which Zarr
    resizes before writing the data (audit root cause 1).
    """
    store_path = str(tmp_path / 'rec.zarr')
    _make_structured_zarr_store(store_path, writing=True)

    controller = _make_controller(folder_path=str(tmp_path))
    controller._mainController._activeReconstructor.supports_streaming = True
    controller._storeQueue.append((store_path, False))

    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()

    assert len(controller._liveController.start_calls) == 0


def test_midwrite_single_file_lapse_never_barrier_admitted(tmp_path):
    """scan{N} single-file lapses are excluded from barrier admission.

    The recorder reopens the file in append mode for every next timepoint;
    a reader holding it open would race the recording, so these are only
    processed once the whole lapse is complete — streaming or not.
    """
    from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer

    path = str(tmp_path / 'lapse.hdf5')
    storer = _run_single_file_lapse_cycle(HDF5Storer, path, 0, 2)
    _finish_cycle(storer, path)  # between cycles: 1 of 2 timepoints present

    controller = _make_controller(folder_path=str(tmp_path), extension='hdf5')
    controller._mainController._activeReconstructor.supports_streaming = True

    assert controller._has_streaming_barrier(path) is False
    assert controller._is_store_ready(path, False) is False


# --- No head-of-line blocking -------------------------------------------------

def test_no_head_of_line_blocking_complete_bypasses_incomplete(tmp_path):
    """A complete store in the queue is processed even if an earlier one is incomplete."""
    # First store: incomplete
    incomplete_path = str(tmp_path / 'rec_incomplete.zarr')
    _make_structured_zarr_store(incomplete_path, writing=True)
    
    # Second store: complete
    complete_path = str(tmp_path / 'rec_complete.zarr')
    _make_structured_zarr_store(complete_path, writing=False)
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.extend([
        (incomplete_path, False),
        (complete_path, False),
    ])
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have processed the complete one, skipping the incomplete
    assert len(controller._liveController.start_calls) == 1
    assert controller._liveController.start_calls[0]['source_arg'] == complete_path
    
    # Incomplete should still be in queue
    assert len(controller._storeQueue) == 1
    assert controller._storeQueue[0][0] == incomplete_path


# --- String bool coercion tests -----------------------------------------------

def test_writing_attr_string_false_is_complete(tmp_path):
    """A store with writing='false' (string) is treated as complete."""
    store_path = str(tmp_path / 'rec.zarr')
    root = zarr.open(store_path, mode='w')
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = 'false'  # String "false"
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed
    assert len(controller._liveController.start_calls) == 1


def test_writing_attr_string_0_is_complete(tmp_path):
    """A store with writing='0' (string) is treated as complete."""
    store_path = str(tmp_path / 'rec.zarr')
    root = zarr.open(store_path, mode='w')
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = '0'  # String "0"
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should have been processed
    assert len(controller._liveController.start_calls) == 1


def test_writing_attr_string_true_is_incomplete(tmp_path):
    """A store with writing='true' (string) is treated as incomplete."""
    store_path = str(tmp_path / 'rec.zarr')
    root = zarr.open(store_path, mode='w')
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = 'true'  # String "true"
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((store_path, False))
    
    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should not have been processed
    assert len(controller._liveController.start_calls) == 0


# Copyright (C) 2020-2026 ImSwitch developers
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
