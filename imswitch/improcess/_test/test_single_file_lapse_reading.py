"""A lapse recorded as one file must be openable, not just writable.

Recording a scan lapse with *single file* storage writes one group per
timepoint -- ``scan0/Camera/data``, ``scan1/Camera/data`` -- so the detector
groups sit one level deeper than in every other recording. Dataset discovery
only looked at the container root, found neither an array nor a detector group
there, and reported "File does not contain any datasets": every such recording
was unopenable in ImProcess, in both HDF5 and Zarr, by any dataset name.

It survived because each side was tested against its own idea of the other.
The storer's tests assert the groups are written; the live-source tests read
``scan{N}`` groups through a *different* reader that always knew about them;
the layout resolver was tested on attribute dictionaries lifted straight out
of a dataset. Nothing opened a written lapse file the way a user does.

These tests fix the shape in place: discovery names each item, each opens, and
each carries its own ``time`` partition so a reader can tell which timepoint
it is holding.
"""

from __future__ import annotations

import h5py
import numpy as np
import pytest
import zarr

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    AcquisitionPartition,
    TraversalRule,
    encode_acquisition_layout,
)
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.image_sources import dataset_names

ROWS, COLS = 2, 3
FRAMES = ROWS * COLS
FRAME_SHAPE = (4, 5)
TIMEPOINTS = 2


def _layout(index):
    loops = (
        AcquisitionLoop("scan_y", "scan_y", ROWS, step=1.0, unit="um", direction=1),
        AcquisitionLoop("scan_x", "scan_x", COLS, step=1.0, unit="um", direction=1),
    )
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Camera",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=loops,
        traversal=tuple(TraversalRule(loop.id, "forward") for loop in loops),
        provenance="recorded",
        partitions=(
            AcquisitionPartition(
                "time", index=index, planned_count=TIMEPOINTS,
                storage="one-group-per-item",
            ),
        ),
    )


def _attrs(index):
    """The attributes the recording worker writes onto a lapse item."""
    return {
        "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
        "AcquisitionLayout:json": encode_acquisition_layout(_layout(index)),
        "recording:detector_name": "Camera",
        "recording:num_timepoints": TIMEPOINTS,
        "recording:lapse_index": index,
        "recording:single_lapse_file": True,
        "recording:expected_frames": FRAMES,
        "recording:completion_outcome": "complete",
        "writing": False,
    }


def _frames(index):
    return np.full((FRAMES, *FRAME_SHAPE), index, dtype=np.uint16)


@pytest.fixture
def hdf5_lapse(tmp_path):
    path = tmp_path / "lapse_Camera.hdf5"
    with h5py.File(path, "w") as file:
        for index in range(TIMEPOINTS):
            detector = file.create_group(f"scan{index}").create_group("Camera")
            dataset = detector.create_dataset("data", data=_frames(index))
            dataset.attrs.update(_attrs(index))
    return path


@pytest.fixture
def zarr_lapse(tmp_path):
    path = tmp_path / "lapse_Camera.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    for index in range(TIMEPOINTS):
        detector = root.create_group(f"scan{index}").create_group("Camera")
        array = detector.create_array(
            "data", shape=(FRAMES, *FRAME_SHAPE), dtype="uint16"
        )
        array[:] = _frames(index)
        array.attrs.update(_attrs(index))
    return path


def test_hdf5_single_file_lapse_names_every_timepoint(hdf5_lapse):
    with h5py.File(hdf5_lapse, "r") as file:
        assert dataset_names(file) == ["scan0/Camera", "scan1/Camera"]
    assert DataObj.getDatasetNames(str(hdf5_lapse)) == ["scan0/Camera", "scan1/Camera"]


def test_zarr_single_file_lapse_names_every_timepoint(zarr_lapse):
    assert DataObj.getDatasetNames(str(zarr_lapse)) == ["scan0/Camera", "scan1/Camera"]


@pytest.mark.parametrize("container", ["hdf5_lapse", "zarr_lapse"])
def test_each_lapse_item_opens_with_its_own_partition(container, request):
    path = str(request.getfixturevalue(container))

    for index in range(TIMEPOINTS):
        data_obj = DataObj(path, f"scan{index}/Camera", path=path)
        resolved = data_obj.acquisition_layout

        assert resolved.is_usable, [issue.code for issue in resolved.issues]
        assert resolved.is_authoritative
        assert resolved.layout.partitions == _layout(index).partitions
        # The pixels of that timepoint, not of the first one.
        assert data_obj.data.shape == (FRAMES, *FRAME_SHAPE)
        assert int(np.asarray(data_obj.data[0]).flat[0]) == index


def test_an_ordinary_single_scan_recording_is_unchanged(tmp_path):
    """Descending must be recognition of the lapse shape, not a tree walk."""
    path = tmp_path / "point_scan_Camera.hdf5"
    with h5py.File(path, "w") as file:
        detector = file.create_group("Camera")
        dataset = detector.create_dataset("data", data=_frames(0))
        dataset.attrs.update(_attrs(0))
        # A sibling group that is not a lapse item must not be descended into.
        file.create_group("notes").create_dataset("free_text", data=np.zeros(3))

    assert DataObj.getDatasetNames(str(path)) == ["Camera"]
