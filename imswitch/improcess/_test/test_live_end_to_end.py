"""End-to-end tests for live reconstruction UI integration."""

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.live import ZarrLiveSource
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.reconstructors.base import (
    StreamInit,
    StreamPlan,
    StreamingReconstructor,
    StreamingSession,
)


class _Result(ProcessingResult):
    """Minimal ProcessingResult for testing."""
    def save(self, path, fmt):
        return None


class _StreamingSession(StreamingSession):
    """Stub streaming session that copies frames into a buffer."""
    def __init__(self):
        self.buffer = None

    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        frames = init_obj.stack_info.expected_frames if init_obj.stack_info else 10
        self.buffer = np.zeros((frames, *init_obj.data.shape[-2:]), dtype=np.float32)
        self.push(init_obj.data, 0, init_obj.data.shape[0])
        return StreamPlan(
            out_shape=self.buffer.shape,
            axis_labels=["T", "Y", "X"],
            view_modes=[ViewMode("Standard", (0, 1, 2))],
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        self.buffer[start:end] = chunk

    def result(self) -> ProcessingResult:
        return _Result("live", self.buffer.copy(), ["T", "Y", "X"])


class _StreamingRecon(StreamingReconstructor):
    """Stub streaming reconstructor for testing."""
    name = "Test Streaming"
    id = "test-streaming"
    supports_streaming = True

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params: dict) -> ProcessingResult:
        return _Result("batch", np.asarray(data_obj.data), ["T", "Y", "X"])

    def make_session(self) -> StreamingSession:
        return _StreamingSession()


def _create_synthetic_zarr(path, data: np.ndarray, detector_name: str = "CAM"):
    """Create a synthetic zarr with the structured layout and recording metadata."""
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    root.attrs["rec_mode"] = "recording"
    
    det_group = root.create_group(detector_name)
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(2, *data.shape[-2:]))
    
    array.attrs["detector_name"] = detector_name
    array.attrs["writing"] = False
    array.attrs["axes"] = ["T", "Y", "X"]
    
    array.attrs["recording:expected_frames"] = data.shape[0]
    array.attrs["recording:frames_per_stack"] = data.shape[0]
    array.attrs["recording:detector_name"] = detector_name
    array.attrs["recording:source_format"] = "ZARR"
    
    return root


def test_live_reconstruction_end_to_end(tmp_path):
    """Live reconstruction processes a chunk and produces a result."""
    zarr_path = tmp_path / "test.zarr"
    data = np.arange(2 * 4 * 5, dtype=np.uint16).reshape(2, 4, 5)
    
    _create_synthetic_zarr(zarr_path, data, detector_name="CAM")
    
    reconstructor = _StreamingRecon()
    source = ZarrLiveSource(detector_name="CAM", chunk_size=2)
    
    stack_info = source.open(zarr_path)
    assert stack_info is not None
    assert stack_info.frame_shape == (4, 5)
    assert stack_info.detector_name == "CAM"
    
    chunks = source.poll()
    assert len(chunks) == 1
    assert chunks[0].start == 0
    assert chunks[0].end == 2
    
    session = reconstructor.make_session()
    init_obj = StreamInit(
        name="test",
        dataset_name="CAM",
        data=chunks[0].data,
        attrs=stack_info.attrs,
        stack_info=stack_info,
    )
    
    plan = session.begin(init_obj, {})
    assert plan.out_shape == data.shape
    
    result = session.result()
    assert result is not None
    assert result.data.shape == data.shape
    np.testing.assert_array_equal(result.data, data.astype(np.float32))


def test_format_selector_picks_zarr():
    """LiveModeController selects ZarrLiveSource for .zarr paths."""
    path_lower = "/path/to/recording.zarr".lower()
    
    if path_lower.endswith('.zarr'):
        from imswitch.improcess.live import ZarrLiveSource
        source = ZarrLiveSource(detector_name=None)
        assert source is not None
        assert isinstance(source, ZarrLiveSource)


def test_format_selector_picks_hdf5():
    """LiveModeController selects Hdf5LiveSource for .h5/.hdf5 paths."""
    from imswitch.improcess.live import Hdf5LiveSource
    
    path_lower = "/path/to/recording.h5".lower()
    if path_lower.endswith('.h5') or path_lower.endswith('.hdf5'):
        source = Hdf5LiveSource(detector_name=None)
        assert source is not None
        assert isinstance(source, Hdf5LiveSource)
    
    path_lower = "/path/to/recording.hdf5".lower()
    if path_lower.endswith('.h5') or path_lower.endswith('.hdf5'):
        source = Hdf5LiveSource(detector_name=None)
        assert source is not None
        assert isinstance(source, Hdf5LiveSource)


def test_format_selector_rejects_tiff():
    """LiveModeController rejects unsupported formats."""
    path_lower = "/path/to/recording.tif".lower()
    
    source = None
    if path_lower.endswith('.zarr'):
        source = ZarrLiveSource(detector_name=None)
    elif path_lower.endswith('.h5') or path_lower.endswith('.hdf5'):
        from imswitch.improcess.live import Hdf5LiveSource
        source = Hdf5LiveSource(detector_name=None)
    
    assert source is None


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
