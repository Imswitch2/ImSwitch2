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
)


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
        _watchUnresolvedPaths={path},
        _watchPendingPaths=set(),
        _watchReconcileTimer=_Timer(),
        _pathBelongsToResults=lambda _path: False,
    )

    ProcessingSessionController._sessionPathChanged(fake, path)

    assert fake._watchPendingPaths == {path}
    assert fake._watchReconcileTimer.starts == 1


def test_auto_readiness_probe_does_not_toggle_queue_state(monkeypatch):
    """Unreadable Auto sources remain WAITING without Session UI churn."""
    queue = ProcessingQueue()
    queue.enqueue(ProcessingJob(
        dataset_id="scan",
        label="scan",
        reconstructor_id="streaming",
        params={},
        origin=ProcessingOrigin.AUTO,
    ))
    dataset = SimpleNamespace(id="scan", primary_path="/tmp/scan.hdf5")
    reconstructor = SimpleNamespace(id="streaming", supports_streaming=True)
    session = SimpleNamespace(
        dataset_by_id=lambda _dataset_id: dataset,
        raw_root="/tmp",
    )
    registry = SimpleNamespace(
        get_reconstructor=lambda _id, raise_on_missing=False: reconstructor
    )

    import imswitch.improcess.controller.ProcessingQueueController as queue_module
    from imswitch.improcess.live.job import LiveJobResolution, LiveJobState

    monkeypatch.setattr(queue_module, "get_registry", lambda: registry)
    monkeypatch.setattr(
        queue_module,
        "prepare_entry_job",
        lambda _path, _reconstructor: LiveJobResolution(LiveJobState.PENDING),
    )

    emitted = []
    fake = SimpleNamespace(
        _closing=False,
        queue=queue,
        _offlineThread=None,
        _live=SimpleNamespace(is_running=False),
        _sessionController=SimpleNamespace(session=session),
        _emit_changed=lambda: emitted.append(True),
    )

    ProcessingQueueController._process_next(fake)

    assert queue.current is None
    assert [job.dataset_id for job in queue.waiting] == ["scan"]
    assert emitted == []


def test_persistent_live_access_error_fails_queue_instead_of_waiting(monkeypatch):
    """A non-retryable source conflict becomes terminal queue history."""
    from imswitch.improcess.live.job import LiveJobResolution, LiveJobState
    from imswitch.improcess.model.processing_queue import ProcessingJobState

    queue = ProcessingQueue()
    queue.enqueue(ProcessingJob(
        dataset_id="scan",
        label="scan",
        reconstructor_id="streaming",
        params={},
        origin=ProcessingOrigin.AUTO,
    ))
    dataset = SimpleNamespace(id="scan", kind="scan", primary_path="/tmp/scan.hdf5")
    reconstructor = SimpleNamespace(id="streaming", supports_streaming=True)
    session = SimpleNamespace(
        dataset_by_id=lambda _dataset_id: dataset,
        raw_root="/tmp",
    )
    registry = SimpleNamespace(
        get_reconstructor=lambda _id, raise_on_missing=False: reconstructor
    )

    import imswitch.improcess.controller.ProcessingQueueController as queue_module

    monkeypatch.setattr(queue_module, "get_registry", lambda: registry)
    monkeypatch.setattr(
        queue_module,
        "prepare_entry_job",
        lambda _path, _reconstructor: LiveJobResolution(
            LiveJobState.FAILED, reason="persistent raw HDF5 access conflict"
        ),
    )

    errors = []
    fake = SimpleNamespace(
        _closing=False,
        queue=queue,
        _offlineThread=None,
        _live=SimpleNamespace(is_running=False),
        _sessionController=SimpleNamespace(session=session),
        _logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        _emit_changed=lambda: None,
        _finish_current=lambda *, error="", skipped=False: (
            errors.append(error), queue.fail_current(error)
        ),
    )

    ProcessingQueueController._process_next(fake)

    assert queue.current is None
    assert queue.waiting == ()
    assert queue.history[-1].state == ProcessingJobState.FAILED
    assert errors == ["persistent raw HDF5 access conflict"]


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


def test_dataset_added_event_is_the_steady_state_auto_trigger():
    seen = []
    fake = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        enqueueAutoEligible=lambda dataset_ids=None: seen.append(tuple(dataset_ids or ())),
    )

    ProcessingQueueController._datasets_added(fake, ("new-a", "new-b"))

    assert seen == [("new-a", "new-b")]


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


def test_manual_timelapse_uses_streaming_runner_when_supported():
    """Runner choice follows logical dataset semantics, not submission origin."""
    dataset = SimpleNamespace(kind="time-lapse")
    streaming = SimpleNamespace(supports_streaming=True)
    offline = SimpleNamespace(supports_streaming=False)

    manual = ProcessingJob(
        dataset_id="lapse", label="lapse", reconstructor_id="monalisa",
        params={}, origin=ProcessingOrigin.MANUAL,
    )
    working_set = ProcessingJob(
        dataset_id="lapse", label="lapse", reconstructor_id="monalisa",
        params={}, origin=ProcessingOrigin.WORKING_SET,
    )

    assert ProcessingQueueController._uses_streaming_runner(manual, dataset, streaming)
    assert ProcessingQueueController._uses_streaming_runner(working_set, dataset, streaming)
    assert not ProcessingQueueController._uses_streaming_runner(manual, dataset, offline)


def test_manual_single_scan_stays_offline_but_auto_single_scan_streams():
    dataset = SimpleNamespace(kind="scan")
    reconstructor = SimpleNamespace(supports_streaming=True)
    manual = ProcessingJob(
        dataset_id="scan", label="scan", reconstructor_id="monalisa",
        params={}, origin=ProcessingOrigin.MANUAL,
    )
    auto = ProcessingJob(
        dataset_id="scan", label="scan", reconstructor_id="monalisa",
        params={}, origin=ProcessingOrigin.AUTO,
    )

    assert not ProcessingQueueController._uses_streaming_runner(manual, dataset, reconstructor)
    assert ProcessingQueueController._uses_streaming_runner(auto, dataset, reconstructor)
