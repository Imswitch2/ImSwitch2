"""Single-file HDF5 Auto readiness must survive a frozen Session catalog."""
from types import SimpleNamespace

import h5py
import numpy as np

from imswitch.improcess.model.timepoint_scope import TimepointScope
from imswitch.improcess.execution.readiness import (
    ReadinessState, probe_scope, references_for_scope,
)


def _source(path, group=0):
    return SimpleNamespace(
        sourceMetadata=None,
        source_refs=[
            SimpleNamespace(
                path=str(path), dataset=f"scan{group}/CAM",
                timepoint_index=0,
            )
        ],
    )


def _job(total=2):
    return SimpleNamespace(
        timepoint_scope=TimepointScope.all(),
        timepoint_total=total,
    )


def _write_point(path, physical_t, logical_t, total=2, complete=True):
    with h5py.File(path, "a") as file:
        group = file.create_group(f"scan{physical_t}/CAM")
        ds = group.create_dataset(
            "data", data=np.ones((4, 8, 8), dtype=np.uint16)
        )
        ds.attrs["writing"] = not complete
        ds.attrs["recording:source_format"] = "HDF5"
        ds.attrs["recording:num_timepoints"] = total
        ds.attrs["recording:lapse_index"] = logical_t
        ds.attrs["recording:single_lapse_file"] = True
        ds.attrs["recording:planned_frames"] = 4
        ds.attrs["recording:expected_frames"] = 4
        ds.attrs["recording:actual_frames"] = 4
        ds.attrs["recording:frames_per_stack"] = 4
        ds.attrs["recording:planned_partitions"] = 1
        ds.attrs["recording:actual_partitions"] = 1
        ds.attrs["recording:discarded_frames"] = 0
        ds.attrs["recording:completion_outcome"] = (
            np.bytes_(b"complete") if complete else np.bytes_(b"")
        )
        group.create_dataset("frames_committed", data=[4])
        group.create_dataset(
            "stream_complete", data=[int(complete)]
        )


def test_expected_groups_handle_nonzero_physical_origin(tmp_path):
    path = tmp_path / "lapse_CAM.hdf5"
    refs, _ = references_for_scope(
        _source(path, group=4), TimepointScope.all(), 3,
        is_lapse=True,
    )
    assert [ref.dataset for ref in refs] == [
        "scan4/CAM", "scan5/CAM", "scan6/CAM",
    ]


def test_single_file_becomes_ready_without_catalog_refresh(tmp_path):
    path = tmp_path / "lapse_CAM.hdf5"
    _write_point(path, 0, 0)
    source, job, cache = _source(path), _job(), {}

    waiting = probe_scope(source, job, is_lapse=True, inspected=cache)
    assert waiting.state != ReadinessState.READY
    assert len(waiting.progress.expected_references) == 2
    assert waiting.progress.catalog_count == 1

    # The Session source remains unchanged when the second group arrives.
    _write_point(path, 1, 1)
    ready = probe_scope(source, job, is_lapse=True, inspected=cache)

    assert ready.state == ReadinessState.READY
    assert ready.progress.completed_count == 2
    assert [r.dataset for r in ready.verified_references] == [
        "scan0/CAM", "scan1/CAM",
    ]


def test_wrong_lapse_identity_cannot_unlock_hdf5(tmp_path):
    path = tmp_path / "lapse_CAM.hdf5"
    _write_point(path, 0, 0)
    _write_point(path, 1, 0)  # Wrong logical membership.
    state = probe_scope(_source(path), _job(), is_lapse=True)
    assert state.state != ReadinessState.READY


def test_unfinished_last_group_cannot_unlock_hdf5(tmp_path):
    path = tmp_path / "lapse_CAM.hdf5"
    _write_point(path, 0, 0)
    _write_point(path, 1, 1, complete=False)
    state = probe_scope(_source(path), _job(), is_lapse=True)
    assert state.state != ReadinessState.READY
