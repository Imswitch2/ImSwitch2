"""M2D-A semantic assembly and shared publication lifecycle; no Qt required."""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.execution.result_assembly import (
    AssemblyError, ResultAssemblyPlan, ResultCoverage,
)
from imswitch.improcess.execution.result_publication import (
    PublicationHooks, ResultPublication,
)
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _result(name="recon", *, shape=(1, 1, 8, 8), axes=None, **attrs):
    result = SimpleNamespace(
        name=name, data=np.zeros(shape, np.float32),
        axis_labels=list(axes or ("T", "Z", "Y", "X")),
        axis_scales=[1.] * len(shape), scale_unit="nm", kind="image",
        metadata={},
    )
    for key, value in attrs.items():
        setattr(result, key, value)
    return result


def _publication(scope=None, *, total=10):
    events = []
    hooks = PublicationHooks(
        claim=lambda result: events.append(("claim", result)),
        annotate_scope=lambda result, scope, total: events.append(("scope", scope, total)),
        preview=lambda result: events.append(("preview", result)),
        plane=lambda index, pixels: events.append(("plane", index, pixels)),
        timepoint_done=lambda index: events.append(("done", index)),
        finalized=lambda result, name: events.append(("finalized", result, name)),
        current_changed=lambda result: events.append(("selected", result)),
        offline_complete=lambda result, name, reconstructor: events.append(
            ("offline", result, name, reconstructor)
        ),
    )
    plan = ResultAssemblyPlan.for_timepoints(
        "job-17", "scan-03", scope=scope or TimepointScope.range(4, 6), total=total,
    )
    return ResultPublication(plan, hooks), events


def test_source_identity_and_scope_are_explicit_not_derived_from_shape():
    plan = ResultAssemblyPlan.for_timepoints("job", "lapse", scope=TimepointScope.range(2, 4), total=8)
    assert plan.axis == "T"
    assert plan.expected_count == 3
    assert [plan.coordinate_for_ordinal(i) for i in range(3)] == [2, 3, 4]
    with pytest.raises(AssemblyError):
        plan.coordinate_for_ordinal(3)
    with pytest.raises(AssemblyError):
        ResultAssemblyPlan.for_timepoints("", "lapse")
    with pytest.raises(IndexError):
        ResultAssemblyPlan.for_timepoints("job", "lapse", scope=TimepointScope.range(9, 10), total=8)


def test_dataset_assembly_needs_explicit_unique_identities():
    plan = ResultAssemblyPlan.for_datasets("job", "selection", ["recA", "recB"])
    assert plan.axis == "Dataset"
    assert plan.coordinate_for_ordinal(1) == "recB"
    with pytest.raises(AssemblyError):
        ResultAssemblyPlan.for_datasets("job", "selection", ["recA", "recA"])
    with pytest.raises(AssemblyError):
        ResultAssemblyPlan.for_datasets("job", "selection", [])


def test_gap_and_scope_coverage_are_not_renumbered():
    coverage = ResultCoverage(ResultAssemblyPlan.for_timepoints(
        "job", "lapse", scope=TimepointScope.range(1, 5), total=9,
    ))
    coverage.mark_ordinal(0)
    coverage.mark_ordinal(2)
    assert coverage.to_dict()["published_t_ranges"] == [[1, 2], [3, 4]]
    assert coverage.to_dict()["published_steps"] == 2
    with pytest.raises(AssemblyError):
        coverage.finalize("complete")


def test_completed_count_closes_an_earlier_gap_without_renumbering():
    coverage = ResultCoverage(ResultAssemblyPlan.for_timepoints(
        "job", "lapse", scope=TimepointScope.range(5, 7), total=10,
    ))
    coverage.mark_ordinal(0)
    coverage.mark_ordinal(2)
    coverage.mark_completed_count(3)
    assert coverage.to_dict()["published_t_ranges"] == [[5, 8]]


def test_missing_frame_ranges_are_not_mistaken_for_missing_timepoints():
    publisher, events = _publication()
    publisher.missing_frames([(14, 22)])
    publisher.timepoint_completed(3)
    result = _result()
    publisher.finish("complete", result)
    payload = result.metadata["result_assembly"]
    assert payload["status"] == "partial"
    assert payload["published_t_ranges"] == [[4, 7]]
    assert payload["missing_frame_ranges"] == [[14, 22]]


def test_classic_monalisa_folded_condition_axis_rejected_for_t_append():
    plan = ResultAssemblyPlan.for_timepoints("job", "lapse")
    a = _result(acquisition_projection={"components": ("time", "condition")})
    b = _result()
    with pytest.raises(AssemblyError, match="conditions"):
        plan.validate_append(a, b)


def test_dataset_append_does_not_require_t_to_be_elapsed_time():
    plan = ResultAssemblyPlan.for_datasets("job", "multi", ["A", "B"])
    a = _result(shape=(1, 1, 1, 8, 8), axes=["Dataset", "Base", "T", "Y", "X"],
                acquisition_projection={"components": ("time", "condition")})
    b = _result(shape=(1, 1, 1, 8, 8), axes=["Dataset", "Base", "T", "Y", "X"],
                acquisition_projection={"components": ("time", "condition")})
    plan.validate_append(a, b)


def test_append_checks_axis_kind_geometry_calibration_and_optional_outputs():
    plan = ResultAssemblyPlan.for_timepoints("job", "lapse")
    a = _result(shape=(1, 1, 8, 8))
    b = _result(shape=(2, 1, 8, 8))
    plan.validate_append(a, b)
    b.axis_scales[2] = 2
    with pytest.raises(AssemblyError, match="calibrations"):
        plan.validate_append(a, b)
    b.axis_scales[2] = 1
    b.background = np.zeros_like(b.data)
    with pytest.raises(AssemblyError, match="background"):
        plan.validate_append(a, b)
    b.background = None
    b.kind = "labels"
    with pytest.raises(AssemblyError, match="kinds"):
        plan.validate_append(a, b)


def test_initial_snapshot_and_planes_are_same_published_job():
    publisher, events = _publication()
    result = _result()
    publisher.snapshot(result)
    pixels = np.ones((1, 1, 8, 8), np.float32)
    publisher.plane(0, pixels)
    publisher.timepoint_completed(1)
    publisher.timepoint_completed(2)
    assert [event[0] for event in events] == [
        "scope", "claim", "preview", "plane", "done", "done",
    ]
    assert result.metadata["result_assembly"]["published_t_ranges"] == [[4, 6]]
    assert result.metadata["incremental_completion"]["completed_timepoints"] == 2
    assert result._improcess_queue_job_id == "job-17"


def test_every_new_snapshot_is_claimed_although_freed_ids_come_back():
    """Only the newest snapshot stays alive, so CPython reuses freed ids.

    Remembering annotated results by id() skipped the scope and the Session
    claim for a new snapshot that happened to get a freed one's address. The
    hooks keep no reference to a result, as the queue's do not.
    """
    claimed = []
    publisher = ResultPublication(
        ResultAssemblyPlan.for_timepoints(
            "job-17", "scan-03", scope=TimepointScope.range(0, 50), total=50,
        ),
        PublicationHooks(claim=lambda result: claimed.append(result.name)),
    )
    for index in range(50):
        publisher.snapshot(_result(f"snapshot-{index}"))
    assert claimed == [f"snapshot-{index}" for index in range(50)]


def test_complete_reuses_one_viewer_entry_and_finalizes_once():
    publisher, events = _publication()
    preview = _result()
    final = _result()
    publisher.snapshot(preview)
    publisher.timepoint_completed(3)
    assert publisher.finish("complete", final) is final
    assert publisher.finish("complete", final) is None
    assert [e[0] for e in events].count("finalized") == 1
    assert [e[0] for e in events].count("preview") == 2
    assert final.metadata["result_assembly"]["status"] == "complete"
    assert final.metadata["result_assembly"]["published_t_ranges"] == [[4, 7]]


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_cancel_or_fail_keeps_last_published_only(status):
    publisher, events = _publication()
    result = _result()
    publisher.snapshot(result)
    publisher.timepoint_completed(1)
    assert publisher.finish(status) is result
    assert result.metadata["result_assembly"]["status"] == status
    assert result.metadata["incremental_completion"]["status"] == "partial"
    assert [e[0] for e in events].count("finalized") == 1
    assert [e[0] for e in events].count("preview") == 1


def test_cancel_before_publication_produces_no_orphan():
    publisher, events = _publication()
    assert publisher.finish("cancelled") is None
    assert events == []
    publisher.snapshot(_result())
    assert events == []


def test_final_result_proves_completed_output_when_gui_progress_is_delayed():
    publisher, _events = _publication()
    result = _result()
    publisher.snapshot(result)
    publisher.timepoint_completed(1)
    publisher.finish("complete", result)
    assert result.metadata["result_assembly"]["status"] == "complete"
    assert result.metadata["result_assembly"]["published_t_ranges"] == [[4, 7]]


def test_two_runs_same_source_have_separate_job_identity():
    first, ev1 = _publication()
    second = ResultPublication(
        ResultAssemblyPlan.for_timepoints("job-18", "scan-03", scope=TimepointScope.range(4, 6), total=10),
        PublicationHooks(),
    )
    a, b = _result(name="same"), _result(name="same")
    first.snapshot(a)
    second.snapshot(b)
    assert a._improcess_queue_job_id != b._improcess_queue_job_id


def test_atomic_offline_publication_uses_same_coverage_and_scope_contract():
    publisher, events = _publication()
    result = _result()
    plugin = object()
    publisher.offline_final(result, plugin)
    assert [e[0] for e in events] == ["scope", "claim", "offline"]
    assert result.metadata["result_assembly"]["status"] == "complete"
    assert result.metadata["result_assembly"]["published_t_ranges"] == [[4, 7]]
    assert events[-1][3] is plugin
    with pytest.raises(RuntimeError):
        publisher.offline_final(_result(), plugin)


def test_long_contiguous_lapse_has_compact_coverage_payload():
    coverage = ResultCoverage(ResultAssemblyPlan.for_timepoints(
        "job", "lapse", total=10000,
    ))
    coverage.mark_completed_count(9999)
    payload = coverage.to_dict()
    assert payload["published_t_ranges"] == [[0, 9999]]
    assert len(str(payload)) < 500


def test_provisional_t0_never_claimed_and_removed_on_early_cancel():
    events = []
    hooks = PublicationHooks(
        claim=lambda result: events.append(("claim", result)),
        preview=lambda result: events.append(("preview", result)),
        provisional_plane=lambda t, pixels: events.append(("provisional", t, pixels)),
        discard_provisional=lambda job_id: events.append(("discard", job_id)),
        finalized=lambda result, name: events.append(("finalized", result)),
    )
    publisher = ResultPublication(
        ResultAssemblyPlan.for_timepoints("job-4", "lapse", total=3), hooks,
    )
    transient = _result(shape=(3, 8, 8), axes=["T", "Y", "X"])
    publisher.provisional_snapshot(transient)
    publisher.provisional_plane(0, np.ones((1, 8, 8), dtype=np.float32))
    assert publisher.published is None
    assert not any(e[0] == "claim" for e in events)
    assert publisher.finish("cancelled") is None
    assert events[-1] == ("discard", "job-4")
    assert not any(e[0] == "finalized" for e in events)


def test_provisional_t1_keeps_committed_session_array_unchanged_after_cancel():
    events = []
    hooks = PublicationHooks(
        claim=lambda result: events.append(("claim", result)),
        preview=lambda result: events.append(("preview", result)),
        provisional_plane=lambda t, pixels: events.append(("provisional", t, pixels)),
        finalized=lambda result, name: events.append(("finalized", result)),
    )
    publisher = ResultPublication(
        ResultAssemblyPlan.for_timepoints("job-5", "lapse", total=3), hooks,
    )
    committed = _result(shape=(3, 8, 8), axes=["T", "Y", "X"])
    committed.data[1:] = np.nan
    publisher.snapshot(committed)
    publisher.timepoint_completed(1)
    plane = np.ones((1, 8, 8), dtype=np.float32)
    publisher.provisional_plane(1, plane)
    assert np.isnan(committed.data[1:]).all()
    assert [e[0] for e in events].count("claim") == 1
    assert publisher.finish("cancelled") is committed
    assert events[-2][0] == "preview" and events[-2][1] is committed
    assert events[-1][0] == "finalized" and events[-1][1] is committed
    assert committed.metadata["result_assembly"]["status"] == "cancelled"
