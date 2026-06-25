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

def test_incomplete_lapse_not_all_files_present(tmp_path):
    """A lapse with only 2 of 3 files present is not processed."""
    # Create first timepoint with num_timepoints=3
    tp0_path = str(tmp_path / 'rec_scan__00__CAM.zarr')
    root = zarr.open(tp0_path, mode='w')
    root.attrs['recording:num_timepoints'] = 3
    detector = root.create_group('CAM')
    data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
    data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
    data.attrs['writing'] = False
    
    # Create second timepoint
    tp1_path = str(tmp_path / 'rec_scan__01__CAM.zarr')
    _make_structured_zarr_store(tp1_path, writing=False)
    
    # Third file is missing
    
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((tp0_path, True))
    
    with patch('imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should not have been processed (incomplete lapse)
    assert len(controller._liveController.start_calls) == 0


def test_complete_lapse_all_files_present_and_last_complete(tmp_path):
    """A lapse with all 3 files present and last complete is processed."""
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


def test_lapse_last_file_still_writing_not_processed(tmp_path):
    """A lapse with all files present but last still writing is not processed."""
    # Create all timepoints
    for i in range(3):
        tp_path = str(tmp_path / f'rec_scan__0{i}__CAM.zarr')
        root = zarr.open(tp_path, mode='w')
        if i == 0:
            root.attrs['recording:num_timepoints'] = 3
        detector = root.create_group('CAM')
        data = detector.create_array('data', shape=(10, 256, 256), dtype='uint16', chunks=(1, 256, 256))
        data[:] = np.random.randint(0, 1000, size=(10, 256, 256), dtype='uint16')
        # Last file is still writing
        data.attrs['writing'] = True if i == 2 else False
    
    tp0_path = str(tmp_path / 'rec_scan__00__CAM.zarr')
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((tp0_path, True))
    
    with patch('imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource',
               return_value=MagicMock()):
        controller._processNextStore()
    
    # Should not have been processed
    assert len(controller._liveController.start_calls) == 0


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
