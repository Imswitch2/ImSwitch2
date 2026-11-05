"""M2B: marker-only Auto readiness, queue fairness, and finite/Follow policy."""

from types import SimpleNamespace

import h5py
import numpy as np

from imswitch.imcommon.model.acquisition_metadata import RecordingLifecycleMarkers
from imswitch.improcess.execution.policy import (
    ExecutionPath, choose_execution, supports_streaming_settings,
)
from imswitch.improcess.execution.readiness import (
    PhysicalEvidence, PhysicalReference, ReadinessState, evaluate_evidence,
    inspect_physical_reference, probe_scope,
)
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement, ProcessingJob, ProcessingOrigin, ProcessingQueue,
)
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _job(name="rec", *, origin=ProcessingOrigin.AUTO, mode=ExecutionRequirement.POLICY,
         total=3, scope=None, params=None):
    return ProcessingJob(
        dataset_id=name, label=name, reconstructor_id="monalisa",
        params=params or {}, origin=origin, execution_requirement=mode,
        timepoint_total=total, timepoint_scope=scope or TimepointScope.all(),
    )


def _plugin(*, streaming=True, compatible=True):
    return SimpleNamespace(
        supports_streaming=streaming,
        supports_streaming_params=lambda params: compatible,
    )


def _evidence(*, fmt="zarr", writing=None, committed=None, complete=None,
              attrs=None, size=8):
    return PhysicalEvidence(fmt, dict(attrs or {}), RecordingLifecycleMarkers(
        writing=writing, frames_committed=committed, stream_complete=complete,
    ), size)


def test_completed_timelapse_uses_finite_worker_for_fast_and_full():
    job = _job(origin=ProcessingOrigin.MANUAL, mode=ExecutionRequirement.FINITE,
               params={"preset": "Full", "reassignment": "auto"})
    assert choose_execution(job, "time-lapse", _plugin(compatible=False)).path == ExecutionPath.OFFLINE_FINITE
    assert choose_execution(job, "time-lapse", _plugin(compatible=True)).path == ExecutionPath.OFFLINE_FINITE
    assert not supports_streaming_settings(_plugin(streaming=False), job.params)


def test_auto_nonstreaming_waits_while_growing_then_runs_offline():
    job = _job(mode=ExecutionRequirement.FOLLOW)
    assert choose_execution(job, "time-lapse", _plugin(compatible=False),
                            growing=True, live_reader=True).path == ExecutionPath.WAIT_FINITE
    assert choose_execution(job, "time-lapse", _plugin(compatible=False)).path == ExecutionPath.OFFLINE_FINITE
    assert choose_execution(job, "time-lapse", _plugin(),
                            growing=True, live_reader=True).path == ExecutionPath.INCREMENTAL_FOLLOW
    assert choose_execution(job, "time-lapse", _plugin(),
                            growing=True, live_reader=False).path == ExecutionPath.WAIT_FINITE


def test_pending_queue_entry_does_not_block_later_ready_job():
    queue = ProcessingQueue()
    a, b = _job("pending"), _job("ready")
    queue.enqueue_many([a, b])
    a.readiness_status = "Waiting for acquisition"
    assert queue.view_snapshot()["waiting"][0].readiness_status == a.readiness_status
    assert queue.take_ready(1) is b
    queue.complete_current()
    assert queue.take_next() is a


def test_zarr_missing_marker_never_inferred_from_size():
    assert evaluate_evidence(_evidence(writing=None, committed=None)).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(_evidence(writing=False, committed=None)).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(_evidence(writing=True, committed=5)).state == ReadinessState.ACQUIRING
    assert evaluate_evidence(_evidence(writing=True, committed=5)).live_readable
    assert evaluate_evidence(_evidence(writing=False, committed=8)).state == ReadinessState.READY
    assert evaluate_evidence(_evidence(writing=False, committed=5)).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(_evidence(writing=False, committed=9)).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(_evidence(writing=False, committed=8, attrs={
        "recording:completion_outcome": "stopped_early"})).state == ReadinessState.UNKNOWN


def test_hdf5_requires_stream_complete_even_with_writing_false():
    assert evaluate_evidence(_evidence(fmt="hdf5", writing=False, complete=False,
                                        committed=8)).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(_evidence(fmt="hdf5", writing=True, complete=True,
                                        committed=8)).state == ReadinessState.READY
    assert evaluate_evidence(_evidence(fmt="hdf5", writing=False, complete=None,
                                        committed=8)).state == ReadinessState.UNKNOWN


def test_lapse_scope_requires_explicit_membership_and_total():
    attrs = {"recording:num_timepoints": 3, "recording:lapse_index": 2}
    good = _evidence(writing=False, committed=8, attrs=attrs)
    assert evaluate_evidence(good, is_lapse=True, expected_total=3,
                             logical_index=2).state == ReadinessState.READY
    assert evaluate_evidence(good, is_lapse=True, expected_total=3,
                             logical_index=1).state == ReadinessState.UNKNOWN
    assert evaluate_evidence(good, is_lapse=True, expected_total=4,
                             logical_index=2).state == ReadinessState.UNKNOWN


def _h5_scan(path, *, index, total=3, writing=False, complete=True, committed=4):
    with h5py.File(path, "a", libver="latest") as root:
        group = root.require_group(f"scan{index}/CAM")
        if "data" not in group:
            data = group.create_dataset("data", data=np.zeros((4, 3, 3), np.uint16))
        else:
            data = group["data"]
        data.attrs["writing"] = writing
        data.attrs["recording:num_timepoints"] = total
        data.attrs["recording:lapse_index"] = index
        if "frames_committed" not in group:
            group.create_dataset("frames_committed", data=[committed], dtype="i8")
        if "stream_complete" not in group:
            group.create_dataset("stream_complete", data=[int(complete)], dtype="u1")


def test_real_hdf5_marker_and_selected_scope(tmp_path):
    path = tmp_path / "lapse.hdf5"
    _h5_scan(path, index=0)
    _h5_scan(path, index=2)
    source = SimpleNamespace(source_refs=[
        SimpleNamespace(path=str(path), dataset="scan0/CAM", timepoint_index=0),
        SimpleNamespace(path=str(path), dataset="scan2/CAM", timepoint_index=2),
    ])
    evidence = inspect_physical_reference(PhysicalReference(str(path), "scan2/CAM", 2))
    # SourceRefs also occur with the full physical dataset path.
    direct = inspect_physical_reference(PhysicalReference(str(path), "scan2/CAM/data", 2))
    assert direct.markers == evidence.markers
    assert evidence.markers.stream_complete is True
    assert evidence.markers.frames_committed == 4
    job = _job(total=3)
    assert probe_scope(source, job, is_lapse=True).state == ReadinessState.UNKNOWN
    job.timepoint_scope = TimepointScope.single(2)
    assert probe_scope(source, job, is_lapse=True).state == ReadinessState.READY


def test_bounded_probe_caches_completed_references(tmp_path):
    path = tmp_path / "scans.h5"
    for index in range(3):
        _h5_scan(path, index=index)
    source = SimpleNamespace(source_refs=[
        SimpleNamespace(path=str(path), dataset=f"scan{i}/CAM", timepoint_index=i)
        for i in range(3)
    ])
    cache = {}
    job = _job(total=3)
    assert probe_scope(source, job, is_lapse=True, inspected=cache,
                       max_new_files=1).state == ReadinessState.UNKNOWN
    assert len(cache) == 1
    assert probe_scope(source, job, is_lapse=True, inspected=cache,
                       max_new_files=1).state == ReadinessState.UNKNOWN
    assert len(cache) == 2
    assert probe_scope(source, job, is_lapse=True, inspected=cache,
                       max_new_files=1).state == ReadinessState.READY
    assert len(cache) == 3


def test_monalisa_full_params_are_not_fast_streaming_params():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    from imswitch.improcess.reconstructors.monalisa.params import apply_preset, default_params

    assert MonalisaReconstructor.supports_streaming_params(
        apply_preset(default_params(), "Fast"))
    assert not MonalisaReconstructor.supports_streaming_params(
        apply_preset(default_params(), "Full"))
    assert not MonalisaReconstructor.supports_streaming_params(
        apply_preset(default_params(), "Legacy DLL"))
