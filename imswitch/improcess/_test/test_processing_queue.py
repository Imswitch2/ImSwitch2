"""Pure backend tests for the shared ImProcess processing queue."""

from types import SimpleNamespace

from imswitch.improcess.model.timepoint_scope import TimepointScope
from imswitch.improcess.model.processing_queue import (
    AutoProcessPolicy,
    ProcessingJob,
    ProcessingJobState,
    ProcessingOrigin,
    ProcessingQueue,
)


def _job(dataset_id, *, origin=ProcessingOrigin.MANUAL):
    return ProcessingJob(
        dataset_id=dataset_id,
        label=dataset_id,
        reconstructor_id="monalisa",
        params={"preset": "Fast"},
        origin=origin,
    )


def test_manual_next_preserves_batch_order_ahead_of_auto_backlog():
    queue = ProcessingQueue()
    queue.enqueue_many([
        _job("auto-1", origin=ProcessingOrigin.AUTO),
        _job("auto-2", origin=ProcessingOrigin.AUTO),
    ])

    assert queue.enqueue_many([_job("manual-1"), _job("manual-2")], position="next") == 2
    assert [job.dataset_id for job in queue.waiting] == [
        "manual-1", "manual-2", "auto-1", "auto-2"
    ]


def test_manual_can_also_append_after_auto_backlog():
    queue = ProcessingQueue()
    queue.enqueue(_job("auto-1", origin=ProcessingOrigin.AUTO))
    queue.enqueue(_job("manual-1"), position="end")
    assert [job.dataset_id for job in queue.waiting] == ["auto-1", "manual-1"]


def test_queue_is_serial_and_pause_blocks_advancement():
    queue = ProcessingQueue()
    queue.enqueue_many([_job("a"), _job("b")])

    current = queue.take_next()
    assert current.dataset_id == "a"
    assert current.state == ProcessingJobState.RUNNING
    assert queue.take_next() is None

    queue.complete_current()
    queue.pause()
    assert queue.take_next() is None
    queue.resume()
    assert queue.take_next().dataset_id == "b"


def test_defer_current_retries_source_without_blocking_later_ready_jobs():
    queue = ProcessingQueue()
    queue.enqueue_many([
        _job("new-store", origin=ProcessingOrigin.AUTO),
        _job("ready-store", origin=ProcessingOrigin.AUTO),
    ])
    running = queue.take_next()
    assert running.dataset_id == "new-store"

    deferred = queue.defer_current()
    assert deferred.dataset_id == "new-store"
    assert queue.current is None
    assert [job.dataset_id for job in queue.waiting] == ["ready-store", "new-store"]
    assert queue.history == ()


def test_duplicate_dataset_is_not_active_twice():
    queue = ProcessingQueue()
    assert queue.enqueue(_job("same")) is True
    assert queue.enqueue(_job("same", origin=ProcessingOrigin.AUTO)) is False
    queue.take_next()
    assert queue.enqueue(_job("same")) is False


def test_auto_policy_uses_shared_processed_state_and_queue_state():
    queue = ProcessingQueue()
    policy = AutoProcessPolicy()
    policy.enabled = True
    dataset = SimpleNamespace(id="scan", raw_deleted=False, saved_reconstructions=[])
    reconstructor = SimpleNamespace(result_role="derived", supports_streaming=True)

    assert policy.eligible(dataset, queue=queue, reconstructor=reconstructor, processed=False)
    assert not policy.eligible(dataset, queue=queue, reconstructor=reconstructor, processed=True)

    queue.enqueue(_job("scan"))
    assert not policy.eligible(dataset, queue=queue, reconstructor=reconstructor, processed=False)


def test_auto_policy_respects_explicit_session_exclusion():
    queue = ProcessingQueue()
    policy = AutoProcessPolicy()
    policy.enabled = True
    dataset = SimpleNamespace(
        id="scan",
        raw_deleted=False,
        auto_process_ignored=True,
        saved_reconstructions=[],
    )
    reconstructor = SimpleNamespace(result_role="derived", supports_streaming=True)

    assert not policy.eligible(dataset, queue=queue, reconstructor=reconstructor, processed=False)


def test_auto_policy_rejects_inspection_and_non_streaming_v1_plugins():
    queue = ProcessingQueue()
    policy = AutoProcessPolicy()
    policy.enabled = True
    dataset = SimpleNamespace(id="scan", raw_deleted=False, saved_reconstructions=[])

    inspection = SimpleNamespace(result_role="inspection", supports_streaming=True)
    assert not policy.eligible(dataset, queue=queue, reconstructor=inspection, processed=False)

    offline_only = SimpleNamespace(result_role="derived", supports_streaming=False)
    assert not policy.eligible(dataset, queue=queue, reconstructor=offline_only, processed=False)


def test_view_snapshot_is_lightweight_and_limits_waiting_details():
    queue = ProcessingQueue()
    for index in range(5):
        queue.enqueue(ProcessingJob(
            dataset_id=f"scan-{index}",
            label=f"scan-{index}",
            reconstructor_id="monalisa",
            params={"large": list(range(100))},
        ))

    snapshot = queue.view_snapshot(waiting_limit=2)

    assert snapshot["waiting_count"] == 5
    assert len(snapshot["waiting"]) == 2
    assert not hasattr(snapshot["waiting"][0], "params")
    assert "history" not in snapshot




def test_job_view_preserves_source_without_copying_params():
    queue = ProcessingQueue()
    queue.enqueue(ProcessingJob(
        dataset_id="loaded:image:/tmp/a.h5:Camera:",
        label="a.h5",
        reconstructor_id="monalisa",
        params={"large": list(range(100))},
        source="loaded",
    ))

    snapshot = queue.view_snapshot(waiting_limit=10)

    assert snapshot["waiting"][0].source == "loaded"
    assert not hasattr(snapshot["waiting"][0], "params")


def test_active_dataset_index_tracks_finish_and_clear():
    queue = ProcessingQueue()
    queue.enqueue_many([_job("a"), _job("b")])
    assert queue.contains_dataset("a")
    assert queue.contains_dataset("b")

    queue.take_next()
    queue.complete_current()
    assert not queue.contains_dataset("a")
    assert queue.contains_dataset("b")

    queue.clear_waiting()
    assert not queue.contains_dataset("b")


def test_remove_waiting_uses_job_ids_and_never_touches_running_job():
    queue = ProcessingQueue()
    jobs = [_job("a"), _job("b"), _job("c")]
    queue.enqueue_many(jobs)
    running = queue.take_next()

    removed = queue.remove_waiting([running.id, jobs[1].id])

    assert [job.dataset_id for job in removed] == ["b"]
    assert queue.current is running
    assert [job.dataset_id for job in queue.waiting] == ["c"]
    assert not queue.contains_dataset("b")
    assert queue.contains_dataset("a")
    assert queue.contains_dataset("c")


def test_move_waiting_moves_selected_block_one_slot_preserving_relative_order():
    queue = ProcessingQueue()
    jobs = [_job("a"), _job("b"), _job("c"), _job("d")]
    queue.enqueue_many(jobs)

    assert queue.move_waiting([jobs[1].id, jobs[2].id], direction=1)
    assert [job.dataset_id for job in queue.waiting] == ["a", "d", "b", "c"]

    assert queue.move_waiting([jobs[1].id, jobs[2].id], direction=-1)
    assert [job.dataset_id for job in queue.waiting] == ["a", "b", "c", "d"]

    assert not queue.move_waiting([jobs[0].id], direction=-1)


def test_changed_dataset_ids_are_incremental_not_full_queue_snapshots():
    queue = ProcessingQueue()
    queue.enqueue_many([_job("a"), _job("b")])
    assert queue.consume_changed_dataset_ids() == ("a", "b")
    assert queue.consume_changed_dataset_ids() == ()

    queue.take_next()
    assert queue.consume_changed_dataset_ids() == ("a",)
    queue.complete_current()
    assert queue.consume_changed_dataset_ids() == ("a",)


def test_enqueue_many_assigns_truthful_batch_positions_after_deduplication():
    queue = ProcessingQueue()
    queue.enqueue(_job("already"))

    jobs = [_job("a"), _job("already"), _job("b"), _job("a")]
    assert queue.enqueue_many(jobs) == 2

    waiting = [job for job in queue.waiting if job.dataset_id in {"a", "b"}]
    assert [(job.dataset_id, job.batch_index, job.batch_total) for job in waiting] == [
        ("a", 1, 2),
        ("b", 2, 2),
    ]
    view = waiting[0].view()
    assert view.batch_index == 1
    assert view.batch_total == 2


def test_timepoint_scope_is_snapshotted_with_logical_job_and_not_part_of_dedup_identity():
    queue = ProcessingQueue()
    scoped = _job("lapse")
    scoped.timepoint_scope = TimepointScope.range(3, 11)
    assert queue.enqueue(scoped) is True

    view = queue.view_snapshot()["waiting"][0]
    assert view.timepoint_scope == TimepointScope.range(3, 11)
    assert queue.enqueue(ProcessingJob(
        dataset_id="lapse", label="lapse", reconstructor_id="test", params={},
        timepoint_scope=TimepointScope.single(15),
    )) is False
