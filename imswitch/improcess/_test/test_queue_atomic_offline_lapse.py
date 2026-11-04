"""M2B: finite Full/Robust timelapse publishes only one finished result."""

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
        _offlineReconstructor=plugin, _skipRequested=False,
        _release_data_objs=lambda data: events.append(("release", data)),
        _currentSourceTimepoints=4, queue=queue,
        _claim_current_result=lambda r: events.append(("claim", r)),
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
        _offlineThread=thread, _offlinePendingOutcome=("completed", ""),
        _offlineDataObjs=[], _offlineResultScopes=[], _offlineWorker=object(),
        _offlineAtomicLapse=True, _offlinePendingResult=object(),
        _offlineReconstructor=object(), _skipRequested=True,
        _release_data_objs=lambda data: None,
        _finish_current=lambda **kwargs: events.append(kwargs),
    )
    ProcessingQueueController._offline_thread_finished(fake, thread)
    assert events == [{"skipped": True}]
    assert fake._offlinePendingResult is None
