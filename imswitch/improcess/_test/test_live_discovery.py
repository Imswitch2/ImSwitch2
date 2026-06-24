"""Tests for live reconstruction folder discovery and sequential queue processing."""

import os
from collections import deque
from unittest.mock import MagicMock, patch

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


def _make_zarr(path):
    """Create a directory that looks like a .zarr store."""
    os.makedirs(path, exist_ok=True)
    open(os.path.join(path, '.zgroup'), 'w').close()


def _make_hdf5(path):
    """Create a minimal HDF5 file."""
    import h5py
    with h5py.File(path, 'w') as f:
        f.attrs['dummy'] = 'test'


# --- Recursive discovery (fail a: select the parent folder) -------------------

def test_recursive_discovery_finds_stores_in_measurement_subfolders(tmp_path):
    """Selecting the parent folder discovers stores inside measurement subdirs."""
    measurement = tmp_path / '010_watching_folder'
    _make_zarr(str(measurement / 'rec_scan__00__CAM.zarr'))
    _make_zarr(str(measurement / 'rec_scan__01__CAM.zarr'))
    _make_zarr(str(tmp_path / 'top_level.zarr'))

    controller = _make_controller(folder_path=str(tmp_path))
    found = controller._discoverStores(str(tmp_path))

    names = {os.path.basename(p) for p in found}
    assert names == {'rec_scan__00__CAM.zarr', 'rec_scan__01__CAM.zarr', 'top_level.zarr'}


def test_discovery_excludes_output_dirs(tmp_path):
    """Reconstruction-output subdirs (e.g. rec/) are not ingested."""
    _make_zarr(str(tmp_path / 'data.zarr'))
    _make_zarr(str(tmp_path / 'rec' / 'data_rec.zarr'))

    controller = _make_controller(folder_path=str(tmp_path))
    found = controller._discoverStores(str(tmp_path))

    assert [os.path.basename(p) for p in found] == ['data.zarr']


# --- Lapse grouping -----------------------------------------------------------

def test_per_file_lapse_groups_into_one_job(tmp_path):
    """All ..._scan__NN__... timepoint files dedup into a single lapse job."""
    for i in range(3):
        _make_zarr(str(tmp_path / f'rec_scan__0{i}__CAM.zarr'))

    controller = _make_controller(folder_path=str(tmp_path))
    controller._currentlyProcessing = True  # don't auto-pop the queue
    controller._scanForStores()

    assert len(controller._storeQueue) == 1
    store_path, is_lapse = controller._storeQueue[0]
    assert is_lapse is True
    assert os.path.basename(store_path) == 'rec_scan__00__CAM.zarr'  # lowest index seeds


def test_non_lapse_stores_queued_individually(tmp_path):
    """Unrelated stores get one job each."""
    _make_zarr(str(tmp_path / 'sampleA.zarr'))
    _make_zarr(str(tmp_path / 'sampleB.zarr'))

    controller = _make_controller(folder_path=str(tmp_path))
    controller._currentlyProcessing = True  # don't auto-pop the queue
    controller._scanForStores()

    assert len(controller._storeQueue) == 2
    assert all(is_lapse is False for _, is_lapse in controller._storeQueue)


def test_rescan_does_not_requeue_seen_stores(tmp_path):
    """A second scan does not re-enqueue already-seen stores/lapses."""
    _make_zarr(str(tmp_path / 'rec_scan__00__CAM.zarr'))
    controller = _make_controller(folder_path=str(tmp_path))

    controller._scanForStores()
    first = len(controller._seenKeys)
    controller._scanForStores()  # nothing new
    assert len(controller._seenKeys) == first


# --- Queue processing ---------------------------------------------------------

def test_lapse_job_builds_multifile_source(tmp_path):
    """A lapse job is driven by a ZarrMultiFileLapseSource (accumulating)."""
    seed = str(tmp_path / 'rec_scan__00__CAM.zarr')
    _make_zarr(seed)
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((seed, True))

    with patch(
        'imswitch.improcess.controller.LiveModeController.ZarrMultiFileLapseSource'
    ) as mock_lapse:
        controller._processNextStore()

    mock_lapse.assert_called_once()
    assert mock_lapse.call_args.args[0] == seed


def test_non_lapse_job_uses_make_live_source(tmp_path):
    seed = str(tmp_path / 'sample.zarr')
    _make_zarr(seed)
    controller = _make_controller(folder_path=str(tmp_path))
    controller._storeQueue.append((seed, False))

    with patch(
        'imswitch.improcess.controller.LiveModeController.make_live_source'
    ) as mock_make:
        controller._processNextStore()

    mock_make.assert_called_once()
    assert mock_make.call_args.args[0] == seed


def test_sequential_processing_one_at_a_time(tmp_path):
    """Stores are processed one at a time, advancing on sigFinished."""
    controller = _make_controller(folder_path=str(tmp_path))
    fake = controller._liveController
    controller._storeQueue.extend([
        (str(tmp_path / 's0.zarr'), False),
        (str(tmp_path / 's1.zarr'), False),
    ])

    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        controller._processNextStore()
        assert len(fake.start_calls) == 1  # second waits
        controller._onStoreFinished()
        assert len(fake.start_calls) == 2


def test_start_failure_advances_queue(tmp_path):
    """A store whose start() returns False does not stall the queue."""
    controller = _make_controller(folder_path=str(tmp_path))
    fake = controller._liveController
    controller._storeQueue.extend([
        (str(tmp_path / 'empty.zarr'), False),
        (str(tmp_path / 'good.zarr'), False),
    ])

    def _start(*a, **k):
        fake.start_calls.append(k)
        return fake.start_return

    with patch('imswitch.improcess.controller.LiveModeController.make_live_source',
               return_value=MagicMock()):
        fake.start_return = False  # first store not ready -> must advance
        # flip to True after the first call so the second store "takes"
        original = fake.start

        def start_side_effect(*args, **kwargs):
            result = original(*args, **kwargs)
            fake.start_return = True
            return result

        fake.start = start_side_effect
        controller._processNextStore()

    assert len(fake.start_calls) == 2
    assert controller._currentlyProcessing is True


# --- Robust grouping tests ----------------------------------------------------

def test_hdf5_lapse_groups_into_one_job(tmp_path):
    """HDF5 per-file lapse files dedup into a single lapse job."""
    for i in range(3):
        _make_hdf5(str(tmp_path / f'rec_scan__0{i}__CAM.h5'))

    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._currentlyProcessing = True  # don't auto-pop the queue
    controller._scanForStores()

    assert len(controller._storeQueue) == 1
    store_path, is_lapse = controller._storeQueue[0]
    assert is_lapse is True
    assert os.path.basename(store_path) == 'rec_scan__00__CAM.h5'  # lowest index seeds


def test_hdf5_lapse_job_builds_hdf5_multifile_source(tmp_path):
    """An HDF5 lapse job uses Hdf5MultiFileLapseSource."""
    seed = str(tmp_path / 'rec_scan__00__CAM.h5')
    _make_hdf5(seed)
    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._storeQueue.append((seed, True))

    with patch(
        'imswitch.improcess.controller.LiveModeController.Hdf5MultiFileLapseSource'
    ) as mock_lapse:
        controller._processNextStore()

    mock_lapse.assert_called_once()
    assert mock_lapse.call_args.args[0] == seed


def test_lenient_trailing_integer_grouping(tmp_path):
    """Files with trailing integers (not just 'scan') group as lapse."""
    for i in range(3):
        _make_zarr(str(tmp_path / f'timelapse_0{i}.zarr'))

    controller = _make_controller(folder_path=str(tmp_path))
    controller._currentlyProcessing = True
    controller._scanForStores()

    # Should group into one lapse job
    assert len(controller._storeQueue) == 1
    _, is_lapse = controller._storeQueue[0]
    assert is_lapse is True


def test_metadata_based_grouping_for_multifile_lapse(tmp_path):
    """Files marked as multi-file lapse via metadata group correctly."""
    import h5py
    
    # Create files with metadata indicating multi-file lapse but no scan pattern
    for i in range(2):
        path = tmp_path / f'recording_{i}.h5'
        with h5py.File(path, 'w') as f:
            f.attrs['recording:single_lapse_file'] = False
            f.attrs['recording:num_timepoints'] = 2

    controller = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller._currentlyProcessing = True
    controller._scanForStores()

    # Should detect as lapse based on metadata
    assert len(controller._storeQueue) >= 1
    # At least one should be marked as lapse
    has_lapse = any(is_lapse for _, is_lapse in controller._storeQueue)
    assert has_lapse


def test_mixed_zarr_and_hdf5_lapse_detection(tmp_path):
    """Verify both Zarr and HDF5 lapse files are detected correctly."""
    # Create Zarr lapse
    for i in range(2):
        _make_zarr(str(tmp_path / f'zarr_scan_0{i}.zarr'))
    
    # Create HDF5 lapse
    for i in range(2):
        _make_hdf5(str(tmp_path / f'hdf5_scan_0{i}.h5'))

    controller_zarr = _make_controller(folder_path=str(tmp_path), extension='zarr')
    controller_zarr._currentlyProcessing = True
    controller_zarr._scanForStores()
    
    # Should find 1 Zarr lapse group (ignores .h5 files)
    zarr_lapses = [item for item in controller_zarr._storeQueue if item[1]]
    assert len(zarr_lapses) >= 1

    controller_hdf5 = _make_controller(folder_path=str(tmp_path), extension='h5')
    controller_hdf5._currentlyProcessing = True
    controller_hdf5._scanForStores()
    
    # Should find 1 HDF5 lapse group (ignores .zarr files)
    hdf5_lapses = [item for item in controller_hdf5._storeQueue if item[1]]
    assert len(hdf5_lapses) >= 1


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
