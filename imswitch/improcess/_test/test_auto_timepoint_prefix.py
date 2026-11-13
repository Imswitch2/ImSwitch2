"""M4B.2: only explicitly sealed timepoints may run ahead of their acquisition."""

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_metadata import RecordingLifecycleMarkers
from imswitch.improcess.execution.auto_timepoints import probe_completed_prefix
from imswitch.improcess.execution.readiness import PhysicalEvidence
from imswitch.improcess.model.processing_queue import ProcessingJob
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _job(total=4):
    return ProcessingJob("dataset", "lapse", "monalisa", {}, timepoint_total=total)


def _source(refs):
    return SimpleNamespace(source_refs=[
        SimpleNamespace(path=path, dataset=detector, timepoint_index=t)
        for t, path, detector in refs
    ])


def _hdf5(path, *, index, total=4, single=False, postclosed=True):
    with h5py.File(path, "w", libver="latest") as file:
        group = file.create_group("CAM")
        data = group.create_dataset("data", data=np.ones((3, 4, 4), np.uint16))
        data.attrs.update({
            "recording:num_timepoints": total,
            "recording:lapse_index": index,
            "recording:single_lapse_file": single,
            "recording:planned_frames": 3,
            "recording:completion_outcome": "complete" if postclosed else "",
            "writing": False,
        })
        group.create_dataset("frames_committed", data=[3])
        group.create_dataset("stream_complete", data=[1])


def test_multifile_hdf5_can_be_processed_before_later_timepoints(tmp_path):
    a = tmp_path / "lapse_scan0_CAM.hdf5"
    b = tmp_path / "lapse_scan1_CAM.hdf5"
    _hdf5(a, index=0)
    _hdf5(b, index=1)
    source = _source([(0, str(a), "CAM"), (1, str(b), "CAM")])
    batch = probe_completed_prefix(source, _job(), next_t=0)
    assert batch.indices == (0, 1)
    assert "T3" in batch.reason
    assert probe_completed_prefix(source, _job(), next_t=2).indices == ()


def test_shared_single_file_hdf5_is_never_processed_early(tmp_path):
    path = tmp_path / "lapse.hdf5"
    _hdf5(path, index=0, single=True)
    batch = probe_completed_prefix(_source([(0, str(path), "CAM")]), _job(), next_t=0)
    assert batch.indices == ()
    assert "HDF5" in batch.reason


def test_postclose_completion_outcome_required_for_early_hdf5(tmp_path):
    path = tmp_path / "lapse_scan0_CAM.hdf5"
    _hdf5(path, index=0, postclosed=False)
    assert probe_completed_prefix(_source([(0, str(path), "CAM")]), _job(), 0).indices == ()


def test_hdf5_shared_physical_file_rejected_even_if_metadata_claims_multifile(tmp_path):
    path = tmp_path / "lapse_scan0_CAM.hdf5"
    _hdf5(path, index=0)
    source = _source([(0, str(path), "CAM"), (1, str(path), "CAM")])
    assert probe_completed_prefix(source, _job(), 0).indices == ()


def test_zarr_prefix_keeps_gaps_and_does_not_read_future(monkeypatch):
    import imswitch.improcess.execution.auto_timepoints as target
    available = {0, 2}
    examined = []

    def read(ref):
        examined.append(ref.logical_t)
        return PhysicalEvidence(
            "zarr", {
                "recording:num_timepoints": 4,
                "recording:lapse_index": ref.logical_t,
                "writing": False,
            }, RecordingLifecycleMarkers(writing=False, frames_committed=3), 3,
        )

    monkeypatch.setattr(target, "inspect_physical_reference", read)
    source = _source([(t, f"/raw/scan{t}.zarr", "CAM") for t in available])
    batch = probe_completed_prefix(source, _job(), 0)
    assert batch.indices == (0,)
    assert examined == [0]
    assert "T2" in batch.reason
    # A later existing T2 must never be renumbered as T1.
    assert probe_completed_prefix(source, _job(), 1).indices == ()


def test_prefix_caches_only_confirmed_completed_refs(monkeypatch):
    import imswitch.improcess.execution.auto_timepoints as target
    reads = []

    def read(ref):
        reads.append(ref.logical_t)
        return PhysicalEvidence("zarr", {
            "recording:num_timepoints": 4, "recording:lapse_index": ref.logical_t,
        }, RecordingLifecycleMarkers(writing=False, frames_committed=3), 3)

    monkeypatch.setattr(target, "inspect_physical_reference", read)
    cache = {}
    source = _source([(t, f"/raw/scan{t}.zarr", "CAM") for t in range(4)])
    assert probe_completed_prefix(source, _job(), 0, inspected=cache, max_new_files=2).indices == (0, 1)
    assert probe_completed_prefix(source, _job(), 0, inspected=cache, max_new_files=2).indices == (0, 1)
    assert reads == [0, 1]


def test_new_hdf5_sibling_is_discovered_without_session_monitor(tmp_path):
    first = tmp_path / "lapse_scan0_CAM.hdf5"
    second = tmp_path / "lapse_scan1_CAM.hdf5"
    _hdf5(first, index=0)
    source = _source([(0, str(first), "CAM")])
    source.primary_path = str(first)
    source.primary_dataset = "CAM"
    source.timepoint_origin = 0
    assert probe_completed_prefix(source, _job(), next_t=0).indices == (0,)
    _hdf5(second, index=1)
    batch = probe_completed_prefix(source, _job(), next_t=1)
    assert batch.indices == (1,)
    assert batch.references[0].path == str(second)


def test_job_local_sibling_lookup_does_not_accept_other_detectors(tmp_path):
    first = tmp_path / "lapse_scan0_CAM.hdf5"
    different_detector = tmp_path / "lapse_scan1_Detector2.hdf5"
    _hdf5(first, index=0)
    _hdf5(different_detector, index=1)
    source = _source([(0, str(first), "CAM")])
    source.primary_path = str(first)
    source.primary_dataset = "CAM"
    source.timepoint_origin = 0
    assert probe_completed_prefix(source, _job(), next_t=1).indices == ()


def test_zero_padded_lapse_index_uses_recording_naming_width(tmp_path):
    # ImControl pads indexes to len(str(total)), not len(str(total-1)).
    first = tmp_path / "lapse_scan00_CAM.hdf5"
    next_file = tmp_path / "lapse_scan01_CAM.hdf5"
    _hdf5(first, index=0, total=10)
    _hdf5(next_file, index=1, total=10)
    source = _source([(0, str(first), "CAM")])
    source.primary_path = str(first)
    source.primary_dataset = "CAM"
    source.timepoint_origin = 0
    batch = probe_completed_prefix(source, _job(total=10), next_t=1)
    assert batch.indices == (1,)
