"""View-only Session Auto is a raw inspection workflow, not reconstruction.

The tests exercise eligibility and the existing finite/live publication contracts;
no new worker, reader or result-saving path is introduced for inspection.
"""
from types import SimpleNamespace

import importlib

import numpy as np

from imswitch.improcess.model.processing_queue import (
    AutoProcessPolicy,
    ExecutionRequirement,
    ProcessingOrigin,
    ProcessingQueue,
    supports_session_auto,
)
from imswitch.improcess.model.processing_session import result_role_for_result
from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
from imswitch.improcess.execution.progressive_results import ProgressiveResultAssembler
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
    ViewOnlyResult,
)
from imswitch.improcess.reconstructors.time_lapse.reconstructor import TimeLapseReconstructor
from imswitch.improcess.model.result import ViewMode


def test_inspection_plugins_opt_into_session_auto_individually():
    inspection = ViewOnlyReconstructor()
    assert inspection.result_role == "inspection"
    assert inspection.supports_streaming
    assert inspection.supports_progressive_finite
    assert supports_session_auto(inspection)
    assert supports_session_auto(TimeLapseReconstructor())
    assert not supports_session_auto(SimpleNamespace(result_role="inspection"))
    assert supports_session_auto(SimpleNamespace(result_role="derived"))


def test_auto_policy_accepts_view_only_but_does_not_count_inspection_as_processed():
    policy = AutoProcessPolicy()
    policy.enabled = True
    dataset = SimpleNamespace(
        id="new-raw", raw_deleted=False, auto_process_ignored=False,
        saved_reconstructions=[],
    )
    queue = ProcessingQueue()
    assert policy.eligible(
        dataset, queue=queue, reconstructor=ViewOnlyReconstructor(), processed=False,
    )
    assert policy.eligible(
        dataset, queue=queue, reconstructor=TimeLapseReconstructor(), processed=False,
    )
    # A genuinely derived reconstruction still suppresses duplicate Auto jobs.
    assert not policy.eligible(
        dataset, queue=queue, reconstructor=ViewOnlyReconstructor(), processed=True,
    )


def test_view_only_auto_recipe_respects_live_toggle():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    reconstructor = ViewOnlyReconstructor()
    stub = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        _processWhileAcquiring=True,
        _main=SimpleNamespace(_activeReconstructor=reconstructor),
        _snapshot_params=lambda _: {},
    )
    recipe = ProcessingQueueController.captureAutoRecipe(stub)
    assert recipe == {
        "reconstructor_id": "view-only", "params": {},
        "execution_requirement": ExecutionRequirement.FOLLOW.value,
    }
    stub._processWhileAcquiring = False
    assert (ProcessingQueueController.captureAutoRecipe(stub)
            ["execution_requirement"] == ExecutionRequirement.FINITE.value)
    stub._main._activeReconstructor = TimeLapseReconstructor()
    assert ProcessingQueueController.captureAutoRecipe(stub) == {
        "reconstructor_id": "time-lapse", "params": {},
        "execution_requirement": ExecutionRequirement.FINITE.value,
    }


def test_queue_claim_tags_view_only_before_session_publication(monkeypatch):
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    controller_module = importlib.import_module(
        "imswitch.improcess.controller.ProcessingQueueController"
    )

    plugin = ViewOnlyReconstructor()
    monkeypatch.setattr(
        controller_module, "get_registry",
        lambda: SimpleNamespace(get_reconstructor=lambda *_args, **_kwargs: plugin),
    )
    claimed = []
    job = SimpleNamespace(
        source="session", reconstructor_id="view-only", dataset_id="raw-1",
        origin=ProcessingOrigin.AUTO,
    )
    stub = SimpleNamespace(
        queue=SimpleNamespace(current=job),
        _sessionController=SimpleNamespace(
            claimResultForDataset=lambda result, did: claimed.append(
                (result._improcess_result_role, did)
            )
        ),
    )
    result = SimpleNamespace(metadata={})
    ProcessingQueueController._claim_current_result(stub, result)
    assert claimed == [("inspection", "raw-1")]
    assert result_role_for_result(result) == "inspection"


def _point(value):
    raw = np.full((2, 3, 4), value, dtype=np.uint16)
    return ViewOnlyResult(
        name="raw", data=raw, axis_labels=["Frame", "Y", "X"],
        axis_scales=[1.0, 0.2, 0.2], scale_unit="um",
        view_modes=[ViewMode("Standard", (0, 1, 2))],
    )


def test_view_only_completed_timepoints_can_be_assembled_without_processing_pixels():
    plan = ResultAssemblyPlan.for_timepoints("job", "raw", total=3)
    assembler = ProgressiveResultAssembler(plan)
    snapshots = [assembler.append(_point(i + 1), i) for i in range(3)]
    result = snapshots[-1]
    assert all(item is result for item in snapshots)
    assert result.axis_labels == ["T", "Frame", "Y", "X"]
    assert result.data.shape == (3, 2, 3, 4)
    assert result.data.dtype == np.uint16
    assert np.array_equal(result.data[:, 0, 0, 0], [1, 2, 3])
    assert result.axis_scales == [1.0, 1.0, 0.2, 0.2]


def test_monitor_auto_availability_allows_view_only_and_time_lapse():
    from imswitch.improcess.controller.ProcessingSessionController import (
        ProcessingSessionController,
    )

    changes = []
    stub = SimpleNamespace(
        _session=object(), _sessionMode="follow",
        _main=SimpleNamespace(
            _activeReconstructor=ViewOnlyReconstructor(),
            _widget=SimpleNamespace(
                setSessionAutoProcessingAvailable=lambda enabled: changes.append(enabled),
            ),
        ),
        _sync_streaming_capability=lambda: None,
    )
    ProcessingSessionController._sync_auto_availability(stub)
    stub._main._activeReconstructor = TimeLapseReconstructor()
    ProcessingSessionController._sync_auto_availability(stub)
    assert changes == [True, True]


def test_view_only_auto_arrival_creates_one_job_without_creating_a_derived_result(monkeypatch):
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    module = importlib.import_module(
        "imswitch.improcess.controller.ProcessingQueueController"
    )
    view_only = ViewOnlyReconstructor()
    monkeypatch.setattr(
        module, "get_registry",
        lambda: SimpleNamespace(get_reconstructor=lambda *_args, **_kwargs: view_only),
    )
    dataset = SimpleNamespace(
        id="new", label="new recording", kind="time-lapse",
        planned_timepoints=3, raw_deleted=False,
    )
    queued = ProcessingQueue()
    events = []
    stub = SimpleNamespace(
        _sessionController=SimpleNamespace(
            session=SimpleNamespace(
                dataset_by_id=lambda dataset_id: dataset if dataset_id == "new" else None,
            ),
            isDatasetProcessed=lambda _dataset_id: False,
        ),
        queue=queued,
        _job_for_dataset=ProcessingQueueController._job_for_dataset,
        _emit_changed=lambda: events.append("queue changed"),
        _process_next=lambda: events.append("dispatch"),
    )
    assert ProcessingQueueController.enqueueAutoArrivals(stub, ["new", "new"], {
        "reconstructor_id": "view-only", "params": {},
        "execution_requirement": "follow",
    }) == 1
    assert len(queued.waiting) == 1
    job = queued.waiting[0]
    assert job.origin == ProcessingOrigin.AUTO
    assert job.reconstructor_id == "view-only"
    assert job.execution_requirement == ExecutionRequirement.FOLLOW
    assert events == ["queue changed", "dispatch"]


def test_inspection_result_is_never_indexed_or_autosaved_as_reconstruction(monkeypatch):
    from imswitch.improcess.controller.ProcessingSessionController import (
        ProcessingSessionController,
    )

    module = importlib.import_module(
        "imswitch.improcess.controller.ProcessingSessionController"
    )
    monkeypatch.setattr(module, "output_node", lambda _result: {"op": "reconstruct"})
    result = SimpleNamespace(_improcess_result_role="inspection")
    assert not ProcessingSessionController._is_session_reconstruction(result)
    calls = []
    stub = SimpleNamespace(
        _session=object(),
        _is_session_reconstruction=ProcessingSessionController._is_session_reconstruction,
        _queueAutoSave=lambda *_args: calls.append("unexpected autosave"),
    )
    ProcessingSessionController._resultFinalized(stub, result, "raw")
    assert calls == []


def test_per_timepoint_raw_t_axis_is_renamed_before_finite_assembly():
    from imswitch.improcess.model.timepoint_scope import TimepointScope

    raw = np.ones((2, 3, 4), dtype=np.uint16)
    obj = SimpleNamespace(
        name="raw", data=raw, dataLoaded=True, sourceLoaded=False,
        dataPath=None, _improcess_timepoint_scope=TimepointScope.single(0),
        axis_labels=["T", "Y", "X"], axis_scales=None, scale_unit="px",
        acquisition_layout=SimpleNamespace(layout=SimpleNamespace(
            provenance="recorded", storage_axes=("time", "detector_y", "detector_x"),
        )),
        checkAndLoadData=lambda: None, checkAndUnloadData=lambda: None,
    )
    result = ViewOnlyReconstructor().process(obj, {})
    assert result.axis_labels == ["Frame", "Y", "X"]
    assert result.axis_scales == [1.0, 1.0, 1.0]
    assembled = ProgressiveResultAssembler(
        ResultAssemblyPlan.for_timepoints("job", "raw", total=2)
    ).append(result, 0)
    assert assembled.axis_labels == ["T", "Frame", "Y", "X"]
    assert assembled.data.shape == (2, 2, 3, 4)
