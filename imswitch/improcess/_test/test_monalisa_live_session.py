"""Tests for MoNaLISA live streaming reconstruction."""

import numpy as np
import pytest
import zarr

from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.live import ZarrLiveSource
from imswitch.improcess.reconstructors.base import StreamInit, StreamingSession
from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.gauss_processor import make_gauss_processor


@pytest.fixture
def synthetic_stack():
    """Create a synthetic MoNaLISA-like stack with scan geometry."""
    nx_s, ny_s = 10, 10
    num_rows, num_cols = 100, 100
    num_frames = nx_s * ny_s

    dx, dy = 50.0, 50.0
    x0, y0, z0 = 0.0, 0.0, 0.0
    x1, y1 = (nx_s - 1) * dx, (ny_s - 1) * dy

    rng = np.random.default_rng(12345)
    stack = rng.integers(50, 150, size=(num_frames, num_rows, num_cols), dtype=np.uint16)

    xp, yp = 10.0, 10.0
    xo, yo = 5.0, 5.0
    nx_c = int(np.ceil((num_cols - xo) / xp))
    ny_c = int(np.ceil((num_rows - yo) / yp))

    for i in range(num_frames):
        x_offset = (i % nx_s) * 0.5
        y_offset = (i // nx_s) * 0.5
        for ix in range(nx_c):
            for iy in range(ny_c):
                xc = int(xo + ix * xp + x_offset)
                yc = int(yo + iy * yp + y_offset)
                if 0 <= xc < num_cols and 0 <= yc < num_rows:
                    stack[i, yc, xc] += 100

    attrs = {
        "ImswitchData": {
            "ScanStage:axis_startpos": [x0, y0, z0],
            "ScanStage:axis_length": [x1, y1, 1.0],
            "ScanStage:axis_step_size": [dx, dy, 1.0],
            "Rec:LapseTime": 2,
        }
    }

    return stack, attrs, nx_s, ny_s, nx_c, ny_c


def _write_monalisa_zarr(path, stack: np.ndarray, attrs: dict, detector_name: str = "CAM"):
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    det_group = root.create_group(detector_name)
    array = ZarrStorer._create_array(
        det_group,
        "data",
        data=stack,
        chunks=(25, *stack.shape[-2:]),
    )
    array.attrs["detector_name"] = detector_name
    array.attrs["writing"] = False
    array.attrs["axes"] = ["T", "Y", "X"]
    array.attrs["recording:detector_name"] = detector_name
    array.attrs["recording:dataset_path"] = f"/{detector_name}/data"
    array.attrs["recording:source_format"] = "ZARR"
    array.attrs["recording:expected_frames"] = int(stack.shape[0])
    array.attrs["recording:frames_per_stack"] = int(stack.shape[0])

    metadata = det_group.create_group("metadata")
    imswitch_data = attrs["ImswitchData"]
    scan_stage = metadata.create_group("ScanStage")
    scan_stage.attrs["axis_startpos"] = imswitch_data["ScanStage:axis_startpos"]
    scan_stage.attrs["axis_length"] = imswitch_data["ScanStage:axis_length"]
    scan_stage.attrs["axis_step_size"] = imswitch_data["ScanStage:axis_step_size"]
    rec = metadata.create_group("Rec")
    rec.attrs["LapseTime"] = imswitch_data["Rec:LapseTime"]
    return root


def test_monalisa_reconstructor_supports_streaming():
    """Test that MonalisaReconstructor declares streaming support."""
    reconstructor = MonalisaReconstructor()
    assert hasattr(reconstructor, "supports_streaming")
    assert reconstructor.supports_streaming is True
    assert hasattr(reconstructor, "make_session")


def test_make_session_returns_streaming_session():
    """Test that make_session returns a StreamingSession instance."""
    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()
    assert isinstance(session, StreamingSession)


def test_live_session_begin(synthetic_stack):
    """Test StreamingSession.begin with synthetic data."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False, "num_rects": 3})

    assert plan.out_shape[0] == 1
    assert plan.out_shape[1] == 1
    assert plan.out_shape[2] == 2
    assert plan.out_shape[3] == 1
    assert plan.out_shape[4] > 0
    assert plan.out_shape[5] > 0
    assert plan.dtype == np.dtype(np.float32)
    assert "Dataset" in plan.axis_labels
    assert "Base" in plan.axis_labels
    assert "T" in plan.axis_labels


def test_live_session_from_zarr_live_source(tmp_path, synthetic_stack):
    """Drive a real MoNaLISA live session from a structured ZarrLiveSource."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack
    zarr_path = tmp_path / "monalisa-live.zarr"
    _write_monalisa_zarr(zarr_path, stack, attrs, detector_name="CAM")

    source = ZarrLiveSource(detector_name="CAM", chunk_size=25)
    stack_info = source.open(zarr_path)

    chunks = []
    total_frames = 0
    while total_frames < stack_info.frames_per_stack:
        new_chunks = source.poll()
        assert new_chunks
        chunks.extend(new_chunks)
        total_frames += sum(chunk.data.shape[0] for chunk in new_chunks)

    init_data = np.concatenate([chunk.data for chunk in chunks], axis=0)
    assert init_data.shape == stack.shape
    assert stack_info.attrs["ScanStage:axis_startpos"] == attrs["ImswitchData"]["ScanStage:axis_startpos"]
    assert stack_info.attrs["Rec:LapseTime"] == attrs["ImswitchData"]["Rec:LapseTime"]

    init_obj = StreamInit(
        name="monalisa-live",
        dataset_name=stack_info.detector_name,
        data=init_data,
        attrs=stack_info.attrs,
        stack_info=stack_info,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()
    plan = session.begin(init_obj, params={"use_gpu": False, "num_rects": 3})
    result = session.result()

    assert result.data.shape == plan.out_shape
    assert result.data.dtype == np.dtype(np.float32)
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)
    source.close()
    session.close()


def test_live_session_push_and_result(synthetic_stack):
    """Test StreamingSession push and result with chunked data."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    first_chunk = stack[:50]
    second_chunk = stack[50:]

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False})

    session.push(first_chunk, 0, 50)

    session.push(second_chunk, 50, 100)

    result = session.result()

    assert result is not None
    assert result.data.shape == plan.out_shape
    assert result.data.dtype == plan.dtype
    assert np.all(np.isfinite(result.data))
    assert not np.all(result.data == 0)


def test_live_session_finish(synthetic_stack):
    """Test StreamingSession finish returns final result."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    plan = session.begin(init_obj, params={"use_gpu": False})

    session.push(stack, 0, 100)

    final_result = session.finish()

    assert final_result is not None
    assert final_result.data.shape == plan.out_shape
    assert np.all(np.isfinite(final_result.data))


def test_live_session_close(synthetic_stack):
    """Test StreamingSession close cleans up resources."""
    stack, attrs, nx_s, ny_s, nx_c, ny_c = synthetic_stack

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs=attrs,
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    session.begin(init_obj, params={"use_gpu": False})

    session.close()


def test_gauss_processor_gpu_fallback():
    """Test that make_gauss_processor falls back to CPU when GPU unavailable."""
    processor = make_gauss_processor(
        xp=10.0,
        xo=5.0,
        yp=10.0,
        yo=5.0,
        nx_c=10,
        ny_c=10,
        nx_s=10,
        ny_s=10,
        num_rows=100,
        num_cols=100,
        num_rects=3,
        use_gpu=True,
    )

    assert processor is not None
    assert hasattr(processor, "process_chunk")


def test_live_session_missing_scan_geometry():
    """Test that begin raises clear error when scan geometry is missing."""
    stack = np.random.randint(50, 150, size=(100, 100, 100), dtype=np.uint16)

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs={},
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    with pytest.raises(ValueError, match="Missing ScanStage"):
        session.begin(init_obj, params={})


def test_live_session_invalid_data_shape():
    """Test that begin raises error for non-3D data."""
    stack = np.random.randint(50, 150, size=(100, 100), dtype=np.uint16)

    init_obj = StreamInit(
        name="test_stack",
        dataset_name="detector_0",
        data=stack,
        attrs={
            "ImswitchData": {
                "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
                "ScanStage:axis_length": [450.0, 450.0, 1.0],
                "ScanStage:axis_step_size": [50.0, 50.0, 1.0],
                "Rec:LapseTime": 1,
            }
        },
    )

    reconstructor = MonalisaReconstructor()
    session = reconstructor.make_session()

    with pytest.raises(ValueError, match="Expected 3D data"):
        session.begin(init_obj, params={})


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
