"""M2B: finite Full/Robust timelapse publishes only one finished result."""
import pytest
from types import SimpleNamespace

from imswitch.improcess.model.processing_queue import ProcessingJob, ProcessingQueue
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _running():
    queue = ProcessingQueue()
    job = ProcessingJob(dataset_id="lapse", label="lapse", reconstructor_id="monalisa",
                        params={"preset": "Full"},
                        timepoint_scope=TimepointScope.range(1, 2), timepoint_total=4)
    queue.enqueue(job)
    queue.take_next()
    return queue


def test_offline_lapse_result_is_held_until_thread_shutdown():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    result = SimpleNamespace(name="combined")
    fake = SimpleNamespace(_offlineAtomicLapse=True, _offlinePendingResult=None,
                           _offlinePendingOutcome=None, _skipRequested=False)
    outcome = SimpleNamespace(merged=result, results=(object(), object()),
                              consolidation_error=None)
    ProcessingQueueController._on_offline_finished(fake, outcome)
    assert fake._offlinePendingResult is result
    assert fake._offlinePendingOutcome == ("completed", "")


def test_offline_lapse_consolidation_failure_is_not_silently_published():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    fake = SimpleNamespace(_offlineAtomicLapse=True, _offlinePendingResult=None,
                           _offlinePendingOutcome=None, _skipRequested=False)
    outcome = SimpleNamespace(merged=None, results=(object(), object()),
                              consolidation_error="Cannot combine shapes")
    ProcessingQueueController._on_offline_finished(fake, outcome)
    assert fake._offlinePendingOutcome == ("failed", "Cannot combine shapes")


def test_offline_scope_publication_occurs_once_after_real_thread_end(monkeypatch):
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    queue = _running()
    result = SimpleNamespace(name="full result")
    events = []
    thread = object()
    plugin = SimpleNamespace(id="monalisa")
    monkeypatch.setattr(module, "annotate_result_timepoint_scope",
                        lambda result, scope, total: events.append(("scope", scope, total)))
    fake = SimpleNamespace(
        _offlineThread=thread, _offlinePendingOutcome=("completed", ""),
        _offlineDataObjs=[], _offlineResultScopes=[], _offlineWorker=object(),
        _offlineAtomicLapse=True, _offlinePendingResult=result,
        _offlineAutoBatch=False, _offlineBatchIndices=(), _autoTimepointStates={},
        _offlineReconstructor=plugin, _skipRequested=False,
        _release_data_objs=lambda data: events.append(("release", data)),
        _currentSourceTimepoints=4, queue=queue,
        _claim_current_result=lambda r: events.append(("claim", r)),
        _publish_offline_completed=lambda r, reconstructor, scope=None: (
            events.append(("scope", scope, 4)), events.append(("claim", r)),
            events.append(("publish", r, getattr(r, "name", "result"), reconstructor))
        ),
        _reconstructorManager=SimpleNamespace(
            publishQueuedResult=lambda r, label, reconstructor:
            events.append(("publish", r, label, reconstructor))),
        _finish_current=lambda **kwargs: events.append(("finish", kwargs)),
        _logger=SimpleNamespace(exception=lambda *args: None),
    )
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert [event[0] for event in events] == ["release", "scope", "claim", "publish", "finish"]
    assert events[1][1] == TimepointScope.range(1, 2)
    assert events[1][2] == 4
    assert events[3][1] is result
    assert fake._offlineThread is None
    assert fake._offlineAtomicLapse is False


def test_skip_offline_lapse_discards_provisional_consolidation():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    events = []
    thread = object()
    fake = SimpleNamespace(
        queue=_running(),
        _offlineThread=thread, _offlinePendingOutcome=("completed", ""),
        _offlineDataObjs=[], _offlineResultScopes=[], _offlineWorker=object(),
        _offlineAtomicLapse=True, _offlinePendingResult=object(),
        _offlineAutoBatch=False, _offlineBatchIndices=(), _autoTimepointStates={},
        _offlineReconstructor=object(), _skipRequested=True,
        _release_data_objs=lambda data: None,
        _finish_current=lambda **kwargs: events.append(kwargs),
    )
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert events == [{"skipped": True}]
    assert fake._offlinePendingResult is None


def test_manual_finite_refreshes_stale_single_file_lapse_catalog(tmp_path):
    """A scan0-only Session snapshot must not hide scan1/scan2 at execution."""
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    from imswitch.improcess.model.processing_session import SessionDataset, SourceRef
    from imswitch.improcess._test._lapse_recordings import record_point

    path = record_point(tmp_path, fmt="zarr", detector="Camera", index=0,
                        total=3, single_file=True, frames=2)
    source = SessionDataset(
        id="lapse", label="lapse", kind="time-lapse",
        primary_path=str(path), primary_dataset="scan0/Camera",
        source_refs=[SourceRef(str(path), "scan0/Camera", timepoint_index=0)],
        planned_timepoints=3, timepoint_origin=0,
    )
    # The watcher did not revisit the root, so the Session still knows T0 only.
    record_point(tmp_path, fmt="zarr", detector="Camera", index=1,
                 total=3, single_file=True, frames=2)
    record_point(tmp_path, fmt="zarr", detector="Camera", index=2,
                 total=3, single_file=True, frames=2)
    assert len(source.source_refs) == 1

    fake = SimpleNamespace(
        _source_kind=ProcessingQueueController._source_kind,
        _planned_timepoints=ProcessingQueueController._planned_timepoints,
        _fresh_manual_lapse_index=ProcessingQueueController._fresh_manual_lapse_index,
    )
    resolve = ProcessingQueueController._data_objs_for_source
    all_units = resolve(fake, source, timepoint_scope=TimepointScope.all())
    assert [unit.datasetName for unit in all_units] == [
        "scan0/Camera", "scan1/Camera", "scan2/Camera",
    ]
    assert [unit._improcess_timepoint_scope.first for unit in all_units] == [0, 1, 2]
    second = resolve(fake, source, timepoint_scope=TimepointScope.single(1))
    assert [unit.datasetName for unit in second] == ["scan1/Camera"]
    final_two = resolve(fake, source, timepoint_scope=TimepointScope.range(1, 2))
    assert [unit._improcess_timepoint_scope.first for unit in final_two] == [1, 2]
    assert len(source.source_refs) == 1  # execution must not mutate Session ownership


def test_verified_auto_references_bypass_manual_lapse_rediscovery(monkeypatch, tmp_path):
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    from imswitch.improcess.model.processing_session import SessionDataset

    source = SessionDataset(
        id="lapse", label="lapse", kind="time-lapse",
        primary_path=str(tmp_path / "never-opened.zarr"), primary_dataset="scan0/Camera",
        planned_timepoints=3,
    )
    fake = SimpleNamespace(
        _source_kind=ProcessingQueueController._source_kind,
        _planned_timepoints=ProcessingQueueController._planned_timepoints,
        _fresh_manual_lapse_index=lambda *_: pytest.fail("Auto must not rediscover"),
    )
    refs = [SimpleNamespace(path=tmp_path / "scan0.zarr", dataset="scan0/Camera",
                            logical_t=0)]
    units = ProcessingQueueController._data_objs_for_source(
        fake, source, timepoint_scope=TimepointScope.all(),
        allowed_timepoints={0}, verified_references=refs,
    )
    assert [unit._improcess_timepoint_scope.first for unit in units] == [0]


def test_manual_finite_rejects_lapse_identity_change(monkeypatch, tmp_path):
    import imswitch.improcess.controller.ProcessingQueueController as module
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
    from imswitch.improcess.model.processing_session import SessionDataset

    source = SessionDataset(
        id="lapse", label="lapse", kind="time-lapse",
        primary_path=str(tmp_path / "lapse.zarr"), primary_dataset="scan4/Camera",
        planned_timepoints=3, timepoint_origin=4,
    )
    fresh = SimpleNamespace(
        anchor=SimpleNamespace(ordinal=0), anchor_index=0,
        planned_count=3, anchor_detector="Camera",
    )
    monkeypatch.setattr(module, "discover_time_lapse", lambda *args: fresh)
    with pytest.raises(ValueError, match="origin changed"):
        ProcessingQueueController._fresh_manual_lapse_index(source)
