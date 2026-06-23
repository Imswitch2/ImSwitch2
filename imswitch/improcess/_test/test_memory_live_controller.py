"""Tests for MemoryLiveController (Phase P5: in-RAM HDF5 hand-off)."""

from io import BytesIO
from unittest.mock import MagicMock

import h5py
import numpy as np
import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.MemoryLiveController import MemoryLiveController
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.reconstructors.base import Reconstructor


class _Result(ProcessingResult):
    def save(self, path, fmt):
        return None


class _TestReconstructor(Reconstructor):
    """Stub batch reconstructor for testing."""
    name = "Test RAM Reconstructor"
    id = "test-ram-recon"

    def __init__(self):
        self.process_called = False
        self.last_data_obj = None
        self.last_params = None

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        self.process_called = True
        self.last_data_obj = data_obj
        self.last_params = params
        return _Result("ram-result", np.asarray(data_obj.data), ["T", "Y", "X"])


class _FakeVFileItem:
    """Fake VFile item for testing."""
    def __init__(self, data):
        self.data = data
        self.savedToDisk = False
        self.filePath = None


class _FakeMemoryRecordings(SignalInterface):
    """Fake memory recordings collection."""
    sigDataSet = Signal(str, object)

    def emit_dataset(self, name: str, vfile_item):
        self.sigDataSet.emit(name, vfile_item)


class _FakeModuleCommChannel:
    """Fake module communication channel."""
    def __init__(self):
        self.memoryRecordings = _FakeMemoryRecordings()


class _FakeMainController:
    """Fake main controller with active reconstructor."""
    def __init__(self, reconstructor=None):
        self._activeReconstructor = reconstructor
        self._widget = MagicMock()
        self._widget.getReconstructionParams.return_value = {"param1": "value1"}
        self._widget.parTree.get_param_dict.return_value = {"param1": "value1"}


class _FakeWidget:
    """Minimal widget stub."""
    pass


class _FakeFactory:
    """Minimal factory stub."""
    def createController(self, controller_class, widget, **kwargs):
        pass


def test_memory_live_controller_disabled_by_default():
    """MemoryLiveController is disabled by default and does not process recordings."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    # Create HDF5 in-RAM recording
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("data", data=np.arange(12).reshape(3, 2, 2))
    
    vfile_item = _FakeVFileItem(buf)
    
    # Emit memory recording while disabled
    module_comm_channel.memoryRecordings.emit_dataset("test_recording", vfile_item)
    
    # Reconstructor should not have been called
    assert not reconstructor.process_called


def test_memory_live_controller_enabled_processes_hdf5():
    """When enabled, MemoryLiveController processes HDF5 RAM recordings."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    # Enable the controller
    controller.setEnabled(True)
    
    # Create HDF5 in-RAM recording with a dataset
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        dataset = f.create_dataset("data", data=np.arange(12).reshape(3, 2, 2))
        dataset.attrs["test_attr"] = "test_value"
    
    vfile_item = _FakeVFileItem(buf)
    
    # Track sigResultProduced emissions
    results = []
    comm_channel.sigResultProduced.connect(lambda result, name: results.append((result, name)))
    
    # Emit memory recording while enabled
    module_comm_channel.memoryRecordings.emit_dataset("test_recording", vfile_item)
    
    # Reconstructor should have been called
    assert reconstructor.process_called
    assert reconstructor.last_data_obj is not None
    assert reconstructor.last_data_obj.name == "test_recording"
    assert reconstructor.last_data_obj.datasetName == "data"
    assert np.array_equal(reconstructor.last_data_obj.data, np.arange(12).reshape(3, 2, 2))
    
    # Result should have been emitted
    assert len(results) == 1
    assert results[0][1] == "Live (RAM)"
    assert results[0][0].name == "ram-result"


def test_memory_live_controller_processes_multiple_datasets():
    """MemoryLiveController processes all datasets in an HDF5 file."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    controller.setEnabled(True)
    
    # Create HDF5 with multiple datasets
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("data1", data=np.arange(6).reshape(2, 3))
        f.create_dataset("data2", data=np.arange(12).reshape(3, 2, 2))
    
    vfile_item = _FakeVFileItem(buf)
    
    results = []
    comm_channel.sigResultProduced.connect(lambda result, name: results.append((result, name)))
    
    module_comm_channel.memoryRecordings.emit_dataset("multi_dataset", vfile_item)
    
    # Should have processed both datasets
    assert len(results) == 2


def test_memory_live_controller_skips_non_hdf5():
    """MemoryLiveController skips non-HDF5 recordings with a log message."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    controller.setEnabled(True)
    
    # Create non-HDF5 data (just bytes that can't be opened as HDF5)
    vfile_item = _FakeVFileItem(BytesIO(b"not an hdf5 file"))
    
    results = []
    comm_channel.sigResultProduced.connect(lambda result, name: results.append((result, name)))
    
    # Emit non-HDF5 recording
    module_comm_channel.memoryRecordings.emit_dataset("not_hdf5", vfile_item)
    
    # Reconstructor should not have been called
    assert not reconstructor.process_called
    # No results should have been emitted
    assert len(results) == 0


def test_memory_live_controller_no_active_reconstructor():
    """MemoryLiveController handles missing active reconstructor gracefully."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    main_controller = _FakeMainController(reconstructor=None)  # No reconstructor
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    controller.setEnabled(True)
    
    # Create HDF5 recording
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("data", data=np.arange(6).reshape(2, 3))
    
    vfile_item = _FakeVFileItem(buf)
    
    results = []
    comm_channel.sigResultProduced.connect(lambda result, name: results.append((result, name)))
    
    # Emit recording with no active reconstructor
    module_comm_channel.memoryRecordings.emit_dataset("test", vfile_item)
    
    # No results should have been emitted
    assert len(results) == 0


def test_memory_live_controller_passes_params():
    """MemoryLiveController passes reconstructor parameters correctly."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    controller.setEnabled(True)
    
    # Create HDF5 recording
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("data", data=np.arange(6).reshape(2, 3))
    
    vfile_item = _FakeVFileItem(buf)
    
    module_comm_channel.memoryRecordings.emit_dataset("test", vfile_item)
    
    # Check that params were passed
    assert reconstructor.last_params == {"param1": "value1"}


def test_memory_live_controller_handles_structured_layout():
    """MemoryLiveController handles structured detector/data layout."""
    comm_channel = CommunicationChannel()
    module_comm_channel = _FakeModuleCommChannel()
    reconstructor = _TestReconstructor()
    main_controller = _FakeMainController(reconstructor)
    
    controller = MemoryLiveController(
        comm_channel,
        _FakeWidget(),
        _FakeFactory(),
        moduleCommChannel=module_comm_channel,
        mainController=main_controller
    )
    
    controller.setEnabled(True)
    
    # Create HDF5 with structured layout (detector group with data dataset)
    buf = BytesIO()
    with h5py.File(buf, "w") as f:
        grp = f.create_group("CAM")
        grp.create_dataset("data", data=np.arange(12).reshape(3, 2, 2))
    
    vfile_item = _FakeVFileItem(buf)
    
    results = []
    comm_channel.sigResultProduced.connect(lambda result, name: results.append((result, name)))
    
    module_comm_channel.memoryRecordings.emit_dataset("structured", vfile_item)
    
    # Should have processed the detector group
    assert len(results) == 1
    assert reconstructor.process_called


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
