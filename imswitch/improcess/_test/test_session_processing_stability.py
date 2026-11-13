"""Regression tests for Session watcher/processing queue stability."""

from types import SimpleNamespace

from imswitch.improcess.controller.ProcessingQueueController import (
    ProcessingQueueController,
)
from imswitch.improcess.controller.ProcessingSessionController import (
    ProcessingSessionController,
)
from imswitch.improcess.model.processing_queue import (
    ProcessingJob,
    ProcessingOrigin,
    ProcessingQueue,
    ExecutionRequirement,
)
from imswitch.improcess.execution.readiness import Readiness, ReadinessState


class _Timer:
    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def test_known_session_entry_change_does_not_schedule_reconcile():
    """Growing known HDF5/Zarr entries belong to LiveSource, not Session."""
    path = "/tmp/timelapse.hdf5"
    fake = SimpleNamespace(
        _session=object(),
        _sessionMode="follow",
        _watchUnresolvedPaths=set(),
        _watchPendingPaths=set(),
        _watchReconcileTimer=_Timer(),
        _pathBelongsToResults=lambda _path: False,
    )

    ProcessingSessionController._sessionPathChanged(fake, path)

    assert fake._watchPendingPaths == set()
    assert fake._watchReconcileTimer.starts == 0


def test_unresolved_new_entry_is_retried_when_its_stat_changes():
    """A file discovered before it is readable still gets readiness retries."""
    path = "/tmp/new_scan.hdf5"
    fake = SimpleNamespace(
        _session=object(),
        _sessionMode="follow",
        _watchUnresolvedPaths={path},
        _watchPendingPaths=set(),
        _watchReconcileTimer=_Timer(),
        _pathBelongsToResults=lambda _path: False,
    )

    ProcessingSessionController._sessionPathChanged(fake, path)

    assert fake._watchPendingPaths == {path}
    assert fake._watchReconcileTimer.starts == 1


def _auto_follow_queue_stub(queue, session, events):
    """Small queue fixture for active, readable Follow sources.

    Auto enters the streaming preflight only when a source is acquiring, the
    user requested Follow, and the physical reader is safe. Keep this gate
    explicit in tests instead of depending on whichever readiness probe runs.
    """
    return SimpleNamespace(
        _closing=False,
        queue=queue,
        _offlineThread=None,
        _incrementalRunner=None,
        _autoTimepointStates={},
        _sessionController=SimpleNamespace(session=session),
        _resolve_job_source=lambda job: session.dataset_by_id(job.dataset_id),
        _source_kind=lambda source: source.kind,
        _probe_auto_readiness=lambda *_args: Readiness(
            ReadinessState.ACQUIRING, "Acquisition in progress", live_readable=True,
        ),
        _streaming_job_path=lambda source: source.primary_path,
        _clear_readiness=lambda _job_id: None,
        _emit_changed=lambda: events.append("changed"),
        _emit_progress_changed=lambda: events.append("progress"),
    )


def test_auto_readiness_probe_does_not_toggle_queue_state(monkeypatch):
    """A pending Follow stream stays WAITING; its repeated probe is silent."""
    queue = ProcessingQueue()
    job = ProcessingJob(
        dataset_id="scan",
        label="scan",
        reconstructor_id="streaming",
        params={},
        origin=ProcessingOrigin.AUTO,
        execution_requirement=ExecutionRequirement.FOLLOW,
    )
    queue.enqueue(job)
    dataset = SimpleNamespace(
        id="scan", kind="scan", primary_path="/tmp/scan.hdf5",
    )
    reconstructor = SimpleNamespace(id="streaming", supports_streaming=True)
    session = SimpleNamespace(dataset_by_id=lambda _dataset_id: dataset)
    registry = SimpleNamespace(
        get_reconstructor=lambda _id, raise_on_missing=False: reconstructor,
    )

    import imswitch.improcess.controller.ProcessingQueueController as queue_module
    from imswitch.improcess.live.job import LiveJobResolution, LiveJobState

    monkeypatch.setattr(queue_module, "get_registry", lambda: registry)
    monkeypatch.setattr(
        queue_module,
        "prepare_entry_job",
        lambda _path, _reconstructor: LiveJobResolution(LiveJobState.PENDING),
    )

    events = []
    fake = _auto_follow_queue_stub(queue, session, events)

    ProcessingQueueController._process_next(fake)

    assert queue.current is None
    assert queue.waiting == (job,)
    assert job.readiness_status == "Waiting for committed frames — stream not initialized"
    assert events == ["changed"]  # One legitimate readiness-status update.

    ProcessingQueueController._process_next(fake)
    assert queue.current is None
    assert queue.waiting == (job,)
    assert events == ["changed"]  # No redundant UI updates on the next probe.


def test_persistent_live_access_error_fails_queue_instead_of_waiting(monkeypatch):
    """A non-retryable Follow reader conflict becomes terminal queue history."""
    from imswitch.improcess.live.job import LiveJobResolution, LiveJobState
    from imswitch.improcess.model.processing_queue import ProcessingJobState

    queue = ProcessingQueue()
    job = ProcessingJob(
        dataset_id="scan",
        label="scan",
        reconstructor_id="streaming",
        params={},
        origin=ProcessingOrigin.AUTO,
        execution_requirement=ExecutionRequirement.FOLLOW,
    )
    queue.enqueue(job)
    dataset = SimpleNamespace(
        id="scan", kind="scan", primary_path="/tmp/scan.hdf5",
    )
    reconstructor = SimpleNamespace(id="streaming", supports_streaming=True)
    session = SimpleNamespace(dataset_by_id=lambda _dataset_id: dataset)
    registry = SimpleNamespace(
        get_reconstructor=lambda _id, raise_on_missing=False: reconstructor,
    )

    import imswitch.improcess.controller.ProcessingQueueController as queue_module

    monkeypatch.setattr(queue_module, "get_registry", lambda: registry)
    monkeypatch.setattr(
        queue_module,
        "prepare_entry_job",
        lambda _path, _reconstructor: LiveJobResolution(
            LiveJobState.FAILED, reason="persistent raw HDF5 access conflict",
        ),
    )

    events = []
    fake = _auto_follow_queue_stub(queue, session, events)
    fake._display_error_for_source_failure = (
        ProcessingQueueController._display_error_for_source_failure
    )
    errors = []

    def finish(*, error="", display_error=""):
        errors.append((error, display_error))
        queue.fail_current(error)

    fake._finish_current = finish

    ProcessingQueueController._process_next(fake)

    assert queue.current is None
    assert queue.waiting == ()
    assert queue.history[-1].state == ProcessingJobState.FAILED
    assert errors == [(
        "persistent raw HDF5 access conflict",
        "Reconstruction failed: persistent raw HDF5 access conflict",
    )]
    assert events == ["changed", "progress"]


def test_hdf5_access_conflict_gets_concise_queue_ui_message():
    reason = (
        "Cannot open a new raw HDF5 reader for /tmp/timelapse.hdf5: "
        "the file already has 1 live in-process handle. "
        "Unload the existing data source or restart ImProcess before starting "
        "timelapse/session reconstruction."
    )

    message = ProcessingQueueController._display_error_for_source_failure(reason)

    assert message == (
        "Reconstruction failed: a timepoint from this timelapse is already loaded. "
        "Unload it and try again."
    )


def test_queue_failure_is_retained_for_footer_status():
    queue = ProcessingQueue()
    queue.enqueue(ProcessingJob(
        dataset_id="scan",
        label="timelapse",
        reconstructor_id="streaming",
        params={},
    ))
    assert queue.take_next() is not None

    fake = SimpleNamespace(
        queue=queue,
        _lastTerminalStatus={},
        _skipRequested=False,
        _autoTimepointStates={},
        _clear_readiness=lambda _job_id: None,
        _logger=SimpleNamespace(error=lambda *args: None),
        _currentProgress={},
        _currentPlannedTimepoints=None,
        _closing=True,
        _emit_changed=lambda: None,
        _emit_progress_changed=lambda: None,
    )
    technical = "Cannot open a new raw HDF5 reader for /tmp/timelapse.hdf5"
    display = "Reconstruction failed: a timepoint from this timelapse is already loaded."

    ProcessingQueueController._finish_current(
        fake, error=technical, display_error=display
    )

    assert queue.current is None
    assert fake._lastTerminalStatus == {
        "kind": "error",
        "label": "timelapse",
        "status": display,
        "detail": technical,
    }


def test_offline_queue_finishes_only_after_qthread_finished():
    """A worker terminal signal records outcome but does not free the queue slot."""
    thread = object()
    finish_calls = []
    fake = SimpleNamespace(
        _skipRequested=False,
        _offlinePendingOutcome=None,
        _offlineThread=thread,
        _offlineWorker=object(),
        _offlineDataObjs=[],
        _offlineResultScopes=[],
        _offlineAtomicLapse=False,
        _offlinePendingResult=None,
        _offlineReconstructor=object(),
        _offlineAutoBatch=False,
        _offlineBatchIndices=(),
        _autoTimepointStates={},
        queue=SimpleNamespace(current=None),
        _release_data_objs=lambda _objs: None,
        _finish_current=lambda **kwargs: finish_calls.append(kwargs),
    )

    ProcessingQueueController._on_offline_finished(fake, object())
    assert fake._offlinePendingOutcome == ("completed", "")
    assert finish_calls == []
    assert fake._offlineThread is thread

    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert fake._offlineThread is None
    assert fake._offlineWorker is None
    assert finish_calls == [{}]


def test_queue_change_updates_only_changed_session_rows():
    rendered = []
    fake = SimpleNamespace(
        _session=object(),
        _selectedDatasetId="selected",
        _widget=SimpleNamespace(setProcessingQueue=lambda _snapshot: None),
        _queue_states=lambda: {"a": "waiting", "selected": "running"},
        _renderDataset=lambda dataset_id, render_selected=False, queue_state=None: rendered.append(
            (dataset_id, render_selected, queue_state)
        ),
    )

    ProcessingSessionController._processingQueueChanged(
        fake, {"changed_dataset_ids": ("a", "selected")}
    )

    assert rendered == [
        ("a", False, "waiting"),
        ("selected", True, "running"),
    ]


def test_catalog_added_event_no_longer_triggers_historical_auto_backfill():
    seen = []
    fake = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        enqueueAutoEligible=lambda dataset_ids=None: seen.append(tuple(dataset_ids or ())),
    )

    ProcessingQueueController._datasets_added(fake, ("new-a", "new-b"))

    assert seen == []


def test_session_save_worker_delegates_save_without_session_catalog_work():
    from imswitch.improcess.controller.session_save_worker import SessionSaveWorker

    dataset = SimpleNamespace(id="scan")
    result = object()
    receipt = SimpleNamespace(primary="/tmp/result.ome.tif")
    calls = []
    store = SimpleNamespace(
        save=lambda got_result, got_dataset, display_name: (
            calls.append((got_result, got_dataset, display_name)) or receipt
        )
    )
    worker = SessionSaveWorker(store, result, dataset, "display")
    succeeded = []
    worker.succeeded.connect(lambda *args: succeeded.append(args))

    worker.run()

    assert calls == [(result, dataset, "display")]
    assert succeeded == [(result, "scan", "display", receipt)]


def test_loaded_manual_batch_enqueues_as_external_source_without_session_ownership(tmp_path):
    path = tmp_path / "old_control.h5"
    path.touch()
    data_obj = SimpleNamespace(
        name="old_control.h5",
        datasetName="Camera",
        dataPath=str(path),
        sourceOriginalPath=str(path),
        sourceKind="image",
        sourceMetadata=None,
    )
    reconstructor = SimpleNamespace(id="monalisa", result_role="derived")
    queue = ProcessingQueue()
    emitted = []
    fake = SimpleNamespace(
        queue=queue,
        _main=SimpleNamespace(_activeReconstructor=reconstructor),
        _sessionController=SimpleNamespace(session=None),
        _externalSources={},
        _external_source_id=ProcessingQueueController._external_source_id,
        _snapshot_params=lambda _reconstructor: {"preset": "Fast"},
        _emit_changed=lambda: emitted.append(True),
        _process_next=lambda: None,
    )

    added = ProcessingQueueController.enqueueLoadedDataObjs(fake, [data_obj])

    assert added == 1
    job = queue.waiting[0]
    assert job.source == "loaded"
    assert job.origin == ProcessingOrigin.MANUAL
    assert fake._externalSources[job.dataset_id] is data_obj
    assert emitted == [True]


def test_loaded_source_matching_active_session_keeps_session_ownership(tmp_path):
    path = tmp_path / "today.h5"
    path.touch()
    data_obj = SimpleNamespace(
        name="today.h5", datasetName="Camera", dataPath=str(path),
        sourceOriginalPath=str(path), sourceKind="image", sourceMetadata=None,
    )
    dataset = SimpleNamespace(
        id="session-id", label="today.h5", raw_deleted=False,
        matches_source=lambda candidate_path, dataset_name: (
            str(candidate_path) == str(path) and dataset_name == "Camera"
        ),
    )
    session = SimpleNamespace(datasets=[dataset])
    reconstructor = SimpleNamespace(id="monalisa", result_role="derived")
    queue = ProcessingQueue()
    fake = SimpleNamespace(
        queue=queue,
        _main=SimpleNamespace(_activeReconstructor=reconstructor),
        _sessionController=SimpleNamespace(session=session),
        _externalSources={},
        _job_for_dataset=ProcessingQueueController._job_for_dataset,
        _snapshot_params=lambda _reconstructor: {},
        _emit_changed=lambda: None,
        _process_next=lambda: None,
    )

    added = ProcessingQueueController.enqueueLoadedDataObjs(fake, [data_obj])

    assert added == 1
    job = queue.waiting[0]
    assert job.dataset_id == "session-id"
    assert job.source == "session"
    assert fake._externalSources == {}


def test_external_source_registry_prunes_finished_loaded_jobs():
    queue = ProcessingQueue()
    job = ProcessingJob(
        dataset_id="loaded:image:/tmp/a.h5:Camera:",
        label="a.h5", reconstructor_id="monalisa", params={}, source="loaded",
    )
    queue.enqueue(job)
    source = object()
    fake = SimpleNamespace(queue=queue, _externalSources={job.dataset_id: source})

    ProcessingQueueController._prune_external_sources(fake)
    assert fake._externalSources == {job.dataset_id: source}

    queue.take_next()
    queue.complete_current()
    ProcessingQueueController._prune_external_sources(fake)
    assert fake._externalSources == {}


def test_closing_session_removes_only_session_waiting_jobs():
    queue = ProcessingQueue()
    session_job = ProcessingJob(
        dataset_id="session-a", label="session-a", reconstructor_id="monalisa",
        params={}, source="session",
    )
    loaded_job = ProcessingJob(
        dataset_id="loaded:image:/tmp/a.h5:Camera:", label="old-a",
        reconstructor_id="monalisa", params={}, source="loaded",
    )
    queue.enqueue_many([session_job, loaded_job])
    fake = SimpleNamespace(
        queue=queue,
        autoPolicy=SimpleNamespace(enabled=True),
        _cancel_waiting_auto_timepoints=lambda _job_id: None,
        _clear_readiness=lambda _job_id: None,
        _emit_changed=lambda: None,
        _process_next=lambda: None,
    )

    ProcessingQueueController.sessionClosed(fake)

    assert fake.autoPolicy.enabled is False
    assert [job.dataset_id for job in queue.waiting] == [loaded_job.dataset_id]


def test_loaded_running_job_does_not_block_session_close():
    queue = ProcessingQueue()
    queue.enqueue(ProcessingJob(
        dataset_id="loaded:image:/tmp/a.h5:Camera:", label="old-a",
        reconstructor_id="monalisa", params={}, source="loaded",
    ))
    queue.take_next()
    fake = SimpleNamespace(queue=queue)

    assert ProcessingQueueController.canCloseSession(fake)

    queue.complete_current()
    queue.enqueue(ProcessingJob(
        dataset_id="session-a", label="session-a", reconstructor_id="monalisa",
        params={}, source="session",
    ))
    queue.take_next()
    assert not ProcessingQueueController.canCloseSession(fake)


def test_partial_timelapse_result_does_not_mark_session_dataset_fully_processed():
    dataset = SimpleNamespace(
        kind="time-lapse",
        planned_timepoints=20,
        saved_reconstructions=[
            SimpleNamespace(timepoint_scope={"mode": "single", "first": 6, "last": 6})
        ],
    )
    fake = SimpleNamespace(
        _session=SimpleNamespace(dataset_by_id=lambda _dataset_id: dataset),
        _loaded_reconstructions_for_dataset=lambda _dataset: [],
    )
    assert not ProcessingSessionController.isDatasetProcessed(fake, "lapse")

    dataset.saved_reconstructions[0].timepoint_scope = {"mode": "all", "total": 20}
    assert ProcessingSessionController.isDatasetProcessed(fake, "lapse")


def test_legacy_timelapse_result_still_counts_as_fully_processed():
    dataset = SimpleNamespace(
        kind="time-lapse",
        planned_timepoints=20,
        saved_reconstructions=[SimpleNamespace(timepoint_scope=None)],
    )
    fake = SimpleNamespace(
        _session=SimpleNamespace(dataset_by_id=lambda _dataset_id: dataset),
        _loaded_reconstructions_for_dataset=lambda _dataset: [],
    )
    assert ProcessingSessionController.isDatasetProcessed(fake, "lapse")
