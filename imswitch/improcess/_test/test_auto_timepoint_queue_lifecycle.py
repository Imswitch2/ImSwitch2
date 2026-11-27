"""M4B.2 queue contract: parked work retains one identity and does not block others."""

from imswitch.improcess.model.processing_queue import (
    ProcessingJob, ProcessingOrigin, ProcessingQueue, ProcessingJobState,
)


def _job(name):
    return ProcessingJob(name, name, "monalisa", {}, origin=ProcessingOrigin.AUTO)


def test_park_and_resume_does_not_finish_or_release_dataset_ownership():
    queue = ProcessingQueue()
    long_lapse, another = _job("lapse"), _job("other")
    assert queue.enqueue_many([long_lapse, another]) == 2
    assert queue.take_ready(0) is long_lapse
    assert queue.defer_current(position="end") is long_lapse
    assert queue.contains_dataset("lapse")
    assert long_lapse.state == ProcessingJobState.WAITING
    assert not queue.history
    assert queue.take_next() is another
    queue.complete_current()
    assert queue.take_next() is long_lapse
    queue.complete_current()
    assert [job.id for job in queue.history] == [another.id, long_lapse.id]
    assert not queue.contains_dataset("lapse")


def test_remove_parked_job_is_explicit_cancellation():
    queue = ProcessingQueue()
    job = _job("lapse")
    queue.enqueue(job)
    queue.take_next()
    queue.defer_current()
    assert queue.remove_waiting([job.id]) == (job,)
    assert job.state == ProcessingJobState.SKIPPED
    assert not queue.contains_dataset("lapse")
