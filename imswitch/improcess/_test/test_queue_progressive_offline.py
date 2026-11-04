"""M2D-C: finite worker publication remains in the queue's GUI-side result lane."""

from types import SimpleNamespace

from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
from imswitch.improcess.model.timepoint_scope import TimepointScope


class Publisher:
    def __init__(self, *, start=3):
        self.calls = []
        self.published = object()
        self.plan = SimpleNamespace(
            coordinate_for_ordinal=lambda i: start + i,
        )

    def append_completed(self, result, ordinal):
        self.calls.append(("append", result, ordinal))

    def finish(self, status):
        self.calls.append(("finish", status))


def fake_queue(count=2):
    pub = Publisher()
    events = []
    worker = SimpleNamespace(request_cancel=lambda: events.append("cancel"))
    fake = SimpleNamespace(
        _offlineThread=object(), _offlineWorker=worker, _offlineProgressive=True,
        _offlinePublisher=pub, _offlinePublishedCount=0,
        _offlineAtomicLapse=True, _offlinePendingOutcome=None,
        _offlinePendingResult=None, _offlineReconstructor=object(),
        _offlineDataObjs=[], _offlineResultScopes=[TimepointScope.single(3 + i) for i in range(count)],
        _skipRequested=False, queue=SimpleNamespace(current=object()),
        _logger=SimpleNamespace(exception=lambda *a: events.append("error")),
        _release_data_objs=lambda values: events.append("release"),
        _finish_current=lambda **kwargs: events.append(("finish-queue", kwargs)),
    )
    return fake, pub, events


def test_offline_per_timepoint_signals_create_one_progressive_result():
    fake, pub, events = fake_queue()
    a, b = object(), object()
    ProcessingQueueController._on_offline_result_ready(fake, a, object())
    ProcessingQueueController._on_offline_result_ready(fake, b, object())
    assert pub.calls == [("append", a, 0), ("append", b, 1)]
    ProcessingQueueController._on_offline_finished(fake, SimpleNamespace())
    assert fake._offlinePendingOutcome == ("completed", "")
    ProcessingQueueController._offline_thread_finished(fake, fake._offlineThread)
    assert ("finish", "complete") in pub.calls
    assert ("finish-queue", {}) in events
    assert fake._offlineThread is None


def test_skip_retains_published_timepoints_after_thread_exit():
    fake, pub, events = fake_queue()
    ProcessingQueueController._on_offline_result_ready(fake, object(), object())
    fake._skipRequested = True
    ProcessingQueueController._on_offline_finished(fake, SimpleNamespace())
    ProcessingQueueController._offline_thread_finished(fake, fake._offlineThread)
    assert ("finish", "cancelled") in pub.calls
    assert ("finish-queue", {"skipped": True}) in events


def test_mismatched_logical_t_cancels_and_marks_failure():
    fake, pub, events = fake_queue()
    fake._offlineResultScopes = [TimepointScope.single(5)]
    ProcessingQueueController._on_offline_result_ready(fake, object(), object())
    assert events == ["error", "cancel"]
    assert fake._offlinePendingOutcome[0] == "failed"
    ProcessingQueueController._on_offline_cancelled(fake, "cancelled")
    assert fake._offlinePendingOutcome[0] == "failed"
    ProcessingQueueController._offline_thread_finished(fake, fake._offlineThread)
    assert ("finish", "failed") in pub.calls
    assert any(x[0] == "finish-queue" and "error" in x[1] for x in events if isinstance(x, tuple))
