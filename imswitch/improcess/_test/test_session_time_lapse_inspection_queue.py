"""Time lapse is a configurable inspection job, not a derived reconstruction."""

from types import SimpleNamespace

import pytest

from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement,
    ProcessingJob,
    ProcessingOrigin,
    ProcessingQueue,
    supports_session_auto,
    supports_session_manual,
)
from imswitch.improcess.model.processing_session import SessionDataset, SourceRef
from imswitch.improcess.model.timepoint_scope import TimepointScope
from imswitch.improcess.reconstructors.time_lapse import TimeLapseReconstructor
from imswitch.improcess.reconstructors.view_only.reconstructor import ViewOnlyReconstructor
from imswitch.improcess._test._lapse_recordings import record_point


def _session_lapse(tmp_path):
    path = record_point(tmp_path, fmt="zarr", detector="Camera", index=0,
                        total=3, single_file=True, frames=2)
    return SessionDataset(
        id="raw", label="raw", kind="time-lapse",
        primary_path=str(path), primary_dataset="scan0/Camera",
        source_refs=[SourceRef(str(path), "scan0/Camera", timepoint_index=0)],
        planned_timepoints=3, timepoint_origin=0,
    )


def test_inspection_roles_and_queue_eligibility_are_independent():
    lapse = TimeLapseReconstructor()
    assert lapse.result_role == "inspection"
    assert supports_session_manual(lapse)
    assert supports_session_auto(lapse)
    assert not getattr(lapse, "supports_streaming", False)
    assert not supports_session_manual(ViewOnlyReconstructor())
    assert supports_session_auto(ViewOnlyReconstructor())
    assert not supports_session_manual(SimpleNamespace(result_role="inspection"))
    assert not supports_session_auto(SimpleNamespace(result_role="inspection"))
    assert supports_session_manual(SimpleNamespace(result_role="derived"))


def test_time_lapse_queues_a_single_logical_source_using_fresh_members(tmp_path):
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )

    source = _session_lapse(tmp_path)
    record_point(tmp_path, fmt="zarr", detector="Camera", index=1,
                 total=3, single_file=True, frames=2)
    record_point(tmp_path, fmt="zarr", detector="Camera", index=2,
                 total=3, single_file=True, frames=2)
    assert len(source.source_refs) == 1

    obj = ProcessingQueueController._whole_lapse_inspection_source(
        source, TimepointScope.all(),
    )
    assert obj.sourceKind == "time-lapse"
    assert obj.sourceMetadata.planned_count == 3
    assert len(obj.sourceMetadata.channel(obj.sourceMetadata.anchor_detector).candidates) == 3
    assert len(source.source_refs) == 1  # no implicit catalog mutation

    # Selected reconstruction settings are not replaced by inspection defaults.
    result = TimeLapseReconstructor().process(
        obj, {"time_axis": "actual", "incomplete": "skip", "detector": "Camera"}
    )
    assert result.metadata["time_lapse"]["incomplete_points"] == "skip"
    assert result.metadata["time_lapse"]["time_axis_requested"] == "actual"
    assert result.data.shape[0] == 3


def test_quick_load_item_can_assemble_its_lapse(tmp_path):
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )
    from imswitch.improcess.model.DataObj import DataObj

    source = _session_lapse(tmp_path)
    item = DataObj("scan0", "scan0/Camera", path=source.primary_path)
    opened = ProcessingQueueController._whole_lapse_inspection_source(
        item, TimepointScope.all(),
    )
    assert opened.sourceKind == "time-lapse"
    assert opened.sourceMetadata.planned_count == 3


@pytest.mark.parametrize("scope", [TimepointScope.single(0), TimepointScope.range(0, 1)])
def test_inspection_does_not_silently_ignore_bounded_scope(tmp_path, scope):
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )
    source = _session_lapse(tmp_path)
    with pytest.raises(ValueError, match="select All T"):
        ProcessingQueueController._whole_lapse_inspection_source(source, scope)


def test_time_lapse_auto_can_see_new_zarr_groups_without_false_readiness(tmp_path):
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )
    from imswitch.improcess.execution.readiness import ReadinessState, probe_scope

    source = _session_lapse(tmp_path)
    job = ProcessingJob(
        dataset_id=source.id, label=source.label,
        reconstructor_id="time-lapse", params=TimeLapseReconstructor.default_params(),
        origin=ProcessingOrigin.AUTO, timepoint_total=3,
        execution_requirement=ExecutionRequirement.FINITE,
    )
    # The watcher initially saw only T0. Auto must not mistake it for a
    # completed three-point recording.
    early = ProcessingQueueController._inspection_lapse_readiness_source(job, source)
    assert probe_scope(early, job, is_lapse=True).state != ReadinessState.READY

    record_point(tmp_path, fmt="zarr", detector="Camera", index=1,
                 total=3, single_file=True, frames=2)
    record_point(tmp_path, fmt="zarr", detector="Camera", index=2,
                 total=3, single_file=True, frames=2)
    refreshed = ProcessingQueueController._inspection_lapse_readiness_source(job, source)
    assert len(refreshed.sourceMetadata.channel("Camera").candidates) == 3
    assert len(source.source_refs) == 1
    assert probe_scope(refreshed, job, is_lapse=True).state == ReadinessState.READY


def test_auto_readiness_follows_selected_detector_not_anchor(tmp_path):
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )
    from imswitch.improcess.execution.readiness import ReadinessState, probe_scope

    camera_first = record_point(tmp_path, fmt="zarr", detector="Camera", index=0,
                                total=3, frames=2)
    for t in (1, 2):
        record_point(tmp_path, fmt="zarr", detector="Camera", index=t,
                     total=3, frames=2)
    # A second detector exists, but only its first point has finalized.
    record_point(tmp_path, fmt="zarr", detector="APD", index=0, total=3, frames=2)
    from imswitch.improcess.model.lapse_source import discover_time_lapse
    anchor_dataset = discover_time_lapse(camera_first).anchor.dataset
    source = SessionDataset(
        id="multi", label="multi", kind="time-lapse",
        primary_path=str(camera_first), primary_dataset=anchor_dataset,
        source_refs=[SourceRef(str(camera_first), anchor_dataset, timepoint_index=0)],
        planned_timepoints=3, timepoint_origin=0,
    )
    job = ProcessingJob(
        dataset_id=source.id, label=source.label, reconstructor_id="time-lapse",
        params={"detector": "APD"}, origin=ProcessingOrigin.AUTO,
        timepoint_total=3, execution_requirement=ExecutionRequirement.FINITE,
    )
    chosen = ProcessingQueueController._inspection_lapse_readiness_source(job, source)
    assert chosen.sourceMetadata.anchor_detector == "APD"
    assert probe_scope(chosen, job, is_lapse=True).state != ReadinessState.READY
    for t in (1, 2):
        record_point(tmp_path, fmt="zarr", detector="APD", index=t,
                     total=3, frames=2)
    complete = ProcessingQueueController._inspection_lapse_readiness_source(job, source)
    assert probe_scope(complete, job, is_lapse=True).state == ReadinessState.READY


def test_time_lapse_auto_never_requests_live_and_skips_non_lapse_sources(monkeypatch):
    import importlib
    from imswitch.improcess.controller.ProcessingQueueController import (
        ProcessingQueueController,
    )

    plugin = TimeLapseReconstructor()
    stub = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        _processWhileAcquiring=True,
        _main=SimpleNamespace(_activeReconstructor=plugin),
        _snapshot_params=lambda _plugin: {"time_axis": "actual", "incomplete": "skip"},
    )
    recipe = ProcessingQueueController.captureAutoRecipe(stub)
    assert recipe == {
        "reconstructor_id": "time-lapse",
        "params": {"time_axis": "actual", "incomplete": "skip"},
        "execution_requirement": ExecutionRequirement.FINITE.value,
    }
    module = importlib.import_module(
        "imswitch.improcess.controller.ProcessingQueueController"
    )
    monkeypatch.setattr(
        module, "get_registry",
        lambda: SimpleNamespace(get_reconstructor=lambda *_args, **_kwargs: plugin),
    )
    scan = SimpleNamespace(id="scan", label="scan", kind="scan", raw_deleted=False)
    queue = ProcessingQueue()
    job_runner = SimpleNamespace(
        queue=queue,
        _sessionController=SimpleNamespace(
            session=SimpleNamespace(dataset_by_id=lambda *_: scan),
            isDatasetProcessed=lambda *_: False,
        ),
        _job_for_dataset=ProcessingQueueController._job_for_dataset,
        _emit_changed=lambda: None,
        _process_next=lambda: None,
    )
    assert ProcessingQueueController.enqueueAutoArrivals(job_runner, ["scan"], recipe) == 0
    assert not queue.waiting
