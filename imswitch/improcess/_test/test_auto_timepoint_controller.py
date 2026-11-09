"""M4B.2: job reuse, publisher lifetime, and bounded resume with Qt slots."""

from types import SimpleNamespace

import pytest
pytest.importorskip("qtpy")

from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
from imswitch.improcess.execution.auto_timepoints import (
    AutoTimepointState, CompletedTimepointBatch,
)
from imswitch.improcess.model.processing_queue import (
    ProcessingJob, ProcessingOrigin, ProcessingQueue,
)
from imswitch.improcess.model.timepoint_scope import TimepointScope


class _Publisher:
    def __init__(self):
        self.appended = []
        self.finished = []
        self.plan = SimpleNamespace(coordinate_for_ordinal=lambda i: i)
        self.published = object()

    def append_completed(self, result, ordinal, *, run=None):
        self.appended.append((result, ordinal, run))

    def finish(self, status):
        self.finished.append(status)


def _ready_to_teardown(*, next_t=1, stop_t=3):
    queue = ProcessingQueue()
    job = ProcessingJob("lapse", "lapse", "monalisa", {},
                        origin=ProcessingOrigin.AUTO, timepoint_total=stop_t)
    queue.enqueue(job)
    queue.take_next()
    publisher = _Publisher()
    worker_thread = object()
    state = AutoTimepointState(next_t, stop_t, publisher)
    changes = []
    fake = SimpleNamespace(
        queue=queue, _offlineThread=worker_thread,
        _offlineDataObjs=[], _offlineResultScopes=[],
        _offlineWorker=object(), _offlinePendingOutcome=("completed", ""),
        _offlineAtomicLapse=True, _offlineProgressive=True,
        _offlineAutoBatch=True, _offlineBatchIndices=(next_t - 1,),
        _offlinePublisher=publisher, _offlinePublishedCount=next_t,
        _offlinePendingResult=None, _offlineReconstructor=None,
        _autoTimepointStates={job.id: state},
        _readinessDue={}, _autoTimepointDue={}, _autoTimepointCached={},
        _autoTimepointInspected={},
        _skipRequested=False, _closing=True,
        _currentProgress={"status": "processing"},
        _emit_changed=lambda: changes.append("changed"),
        _emit_progress_changed=lambda: changes.append("progress"),
        _release_data_objs=lambda objs: None,
        _finish_current=lambda **kwargs: changes.append(("terminal", kwargs)),
    )
    return fake, job, worker_thread, publisher, changes


def test_finite_auto_timepoint_parks_without_finalizing_result():
    fake, job, thread, publisher, changes = _ready_to_teardown()
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert fake.queue.current is None
    assert fake.queue.waiting == (job,)
    assert fake._autoTimepointStates[job.id].next_t == 1
    assert publisher.finished == []
    assert not fake.queue.history
    assert changes == ["changed", "progress"]


def test_last_timepoint_completes_one_publisher():
    fake, job, thread, publisher, changes = _ready_to_teardown(next_t=3)
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert publisher.finished == ["complete"]
    assert job.id not in fake._autoTimepointStates
    assert changes == [("terminal", {})]


def test_incomplete_batch_fails_rather_than_requeueing_duplicate_timepoint():
    fake, job, thread, publisher, changes = _ready_to_teardown(next_t=1)
    fake._offlineBatchIndices = (0, 1)
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert publisher.finished == ["failed"]
    assert job.id not in fake._autoTimepointStates
    assert changes[0][0] == "terminal"
    assert "omitted" in changes[0][1]["error"]


def test_first_result_preserves_auto_calibration_across_worker_batches():
    queue = ProcessingQueue()
    job = ProcessingJob("lapse", "lapse", "monalisa",
                        {"localization_mode": "Auto"},
                        origin=ProcessingOrigin.AUTO, timepoint_total=3)
    queue.enqueue(job)
    queue.take_next()
    publisher = _Publisher()
    state = AutoTimepointState(0, 3, publisher)
    fake = SimpleNamespace(
        _offlineThread=object(), queue=queue,
        _offlineResultScopes=[TimepointScope.single(0)],
        _offlineAtomicLapse=True, _offlineProgressive=True,
        _offlinePendingOutcome=None, _offlinePublisher=publisher,
        _offlinePublishedCount=0, _offlineAutoBatch=True,
        _autoTimepointStates={job.id: state},
        _offlineReconstructor=SimpleNamespace(
            finite_timepoint_reuse_overrides=lambda result, params:
                {"auto_localization": {"pattern": "locked"}}),
    )
    ProcessingQueueController._on_offline_result_ready(fake, object(), object())
    assert state.next_t == 1
    assert state.calibration_resolved
    assert state.reuse_overrides == {"auto_localization": {"pattern": "locked"}}
    assert publisher.appended[0][1] == 0


def test_auto_job_can_start_on_first_completed_t_without_full_scope(monkeypatch):
    registry = SimpleNamespace(get_reconstructor=lambda *args, **kwargs:
                               SimpleNamespace(id="monalisa", supports_progressive_finite=True))
    # Import the controller module, not the identically named re-export.
    import importlib
    module = importlib.import_module("imswitch.improcess.controller.ProcessingQueueController")
    monkeypatch.setattr(module, "get_registry", lambda: registry)
    queue = ProcessingQueue()
    job = ProcessingJob("lapse", "lapse", "monalisa", {},
                        origin=ProcessingOrigin.AUTO, timepoint_total=5)
    queue.enqueue(job)
    started = []
    fake = SimpleNamespace(
        queue=queue, _closing=False, _offlineThread=None,
        _incrementalRunner=None,
        _reconstructorManager=SimpleNamespace(isReconstructionRunning=lambda: False),
        _resolve_job_source=lambda job: SimpleNamespace(kind="time-lapse"),
        _source_kind=lambda source: source.kind,
        _probe_auto_timepoints=lambda job, source: CompletedTimepointBatch((0,), "Waiting for T2"),
        _probe_auto_readiness=lambda *args: pytest.fail("Should not require full finite readiness"),
        _emit_changed=lambda: None, _emit_progress_changed=lambda: None,
        _clear_readiness=lambda job_id: None,
        _start_offline=lambda job, source, reconstructor, **kw: started.append(kw),
        _skipRequested=False, _currentProgress={},
        _currentPlannedTimepoints=None, _currentSourceTimepoints=None,
    )
    ProcessingQueueController._process_next(fake)
    assert started == [{"ready_timepoints": (0,), "ready_references": None}]
    assert queue.current is job
