"""The physical terminal closes an Auto job, not just its Session UI row."""

from types import SimpleNamespace

import pytest

pytest.importorskip('qtpy')

from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
from imswitch.improcess.model.acquisition_progress import AcquisitionProgress
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement, ProcessingJob, ProcessingOrigin, ProcessingQueue,
)
from imswitch.improcess.model.timepoint_scope import TimepointScope
from imswitch.improcess.execution.auto_timepoints import AutoTimepointState


def _pending(*, processed=0):
    queue = ProcessingQueue()
    job = ProcessingJob(
        dataset_id='lapse', label='lapse', reconstructor_id='monalisa',
        params={}, origin=ProcessingOrigin.AUTO,
        timepoint_scope=TimepointScope.all(), timepoint_total=5,
        execution_requirement=ExecutionRequirement.FOLLOW,
    )
    assert queue.enqueue(job)
    finished = []
    stages = {}
    if processed:
        stages[job.id] = AutoTimepointState(
            next_t=processed, stop_t=5,
            publisher=SimpleNamespace(finish=lambda status: finished.append(status)),
        )
    clear = []
    changed = []
    started = []
    fake = SimpleNamespace(
        queue=queue, _autoTimepointStates=stages, _closing=False,
        _cancel_waiting_auto_timepoints=lambda job_id:
            ProcessingQueueController._cancel_waiting_auto_timepoints(fake, job_id),
        _clear_readiness=clear.append, _emit_changed=lambda: changed.append(1),
        _process_next=lambda: started.append(1),
    )
    return fake, job, finished, clear, started


def _terminal(count):
    return AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=tuple(range(count)), status='finished_early',
    )


def test_pending_auto_is_shortened_to_verified_prefix():
    fake, job, finished, cleared, started = _pending()
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(2))
    assert len(fake.queue.waiting) == 1
    assert job.timepoint_scope.bounds(5) == (0, 2)
    assert job.timepoint_total == 5
    assert job.execution_requirement == ExecutionRequirement.FINITE
    assert cleared == [job.id]
    assert len(started) == 1
    assert not finished
    # Re-delivering the same watcher terminal is idempotent.
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(2))
    assert len(fake.queue.waiting) == 1


def test_published_prefix_is_retained_and_waiter_removed():
    fake, job, finished, cleared, started = _pending(processed=2)
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(2))
    assert fake.queue.waiting == ()
    assert finished == ['cancelled']
    assert job.id not in fake._autoTimepointStates
    assert len(started) == 1
    assert job.id in cleared


def test_progressive_auto_can_finish_remaining_sealed_prefix():
    fake, job, finished, cleared, started = _pending(processed=1)
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(2))
    assert len(fake.queue.waiting) == 1
    stage = fake._autoTimepointStates[job.id]
    assert stage.next_t == 1
    assert stage.stop_t == 2
    assert stage.terminal_early
    assert job.timepoint_scope.bounds(5) == (0, 2)
    assert not finished


def test_zero_valid_prefix_does_not_leave_permanent_auto_wait():
    fake, job, finished, _, _ = _pending()
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(0))
    assert fake.queue.waiting == ()
    assert job.id not in fake._autoTimepointStates


def test_unverified_terminal_does_not_change_auto_wait():
    fake, job, _, _, started = _pending()
    gap = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 2), status='finished_early',
    )
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', gap)
    assert job.timepoint_scope == TimepointScope.all()
    assert not started


def test_session_probe_passes_verified_terminal_to_queue(tmp_path):
    from imswitch.imcommon.model.hdf5_lapse_terminal import write_early_stop
    from imswitch.improcess.execution.readiness import Readiness, ReadinessState
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    anchor = tmp_path / 'lapse.zarr'
    anchor.mkdir()
    write_early_stop(anchor, completed=2, planned=5)
    refs = [SimpleNamespace(path=str(anchor), dataset=f'scan{t}/CAM',
                            timepoint_index=t) for t in range(2)]
    dataset = SimpleNamespace(id='lapse', kind='time-lapse',
                              primary_path=str(anchor), source_refs=refs)
    verified = AcquisitionProgress(
        planned_timepoints=5, scope=(0, 5),
        completed_timepoints=(0, 1), status='acquiring',
    )
    received = []
    fake = SimpleNamespace(
        _session=SimpleNamespace(dataset_by_id=lambda _: dataset),
        _acquisitionProbeGeneration=2, _acquisitionProgress={},
        _watchAcquisitionRetryPaths=set(),
        _watchRetrySchedule=SimpleNamespace(discard=lambda _: None),
        _source_paths=lambda _: (str(anchor),),
        _merge_acquisition_progress=ProcessingSessionController._merge_acquisition_progress,
        _queue_controller=lambda: SimpleNamespace(
            acquisitionTerminated=lambda did, p: received.append((did, p)),
        ),
        _update_watcher_detail_paths=lambda: None,
        _syncWatchRetryTimer=lambda: None,
        _queue_states=lambda: {},
        _selectedDatasetId=None,
        _renderDataset=lambda *args, **kwargs: None,
    )
    ProcessingSessionController._acquisition_probe_finished(
        fake, [('lapse', Readiness(ReadinessState.UNKNOWN, progress=verified),
                tuple(refs), False, '')], 2,
    )
    assert len(received) == 1
    assert received[0][1].status == 'finished_early'
    assert received[0][1].completed_timepoints == (0, 1)
    assert received[0][1].planned_timepoints == 5


def test_active_follow_stops_after_verified_final_timepoint():
    fake, job, _, _, _ = _pending()
    assert fake.queue.take_next() is job
    stops = []
    fake._incrementalRunner = SimpleNamespace(cancel=lambda: stops.append('cancel'))
    fake._incrementalCompletedTimepoints = 1
    fake._incrementalTerminalCount = None
    ProcessingQueueController.acquisitionTerminated(fake, 'lapse', _terminal(2))
    assert fake._incrementalTerminalCount == 2
    assert stops == []  # T1 has not yet been reconstructed
    fake._resultPublisher = SimpleNamespace(timepoint_completed=lambda n: None)
    fake._currentPlannedTimepoints = 5
    fake._currentProgress = {}
    fake._emit_progress_changed = lambda: None
    ProcessingQueueController._on_incremental_timepoint(fake, 2)
    assert stops == ['cancel']
    assert fake._incrementalCompletedTimepoints == 2


def test_active_follow_finalizes_partial_science_without_worker_error():
    fake, job, _, _, _ = _pending()
    assert fake.queue.take_next() is job
    statuses = []
    outputs = []
    publisher = SimpleNamespace(
        published=object(),
        finish=lambda status: statuses.append(status),
    )
    fake._incrementalRunner = object()
    fake._incrementalTerminalCount = 2
    fake._incrementalResult = None
    fake._incrementalError = ''
    fake._resultPublisher = publisher
    fake._incrementalReconstructor = object()
    fake._incrementalPublishedResult = publisher.published
    fake._skipRequested = False
    fake._finish_current = lambda **kw: outputs.append(kw)
    ProcessingQueueController._on_incremental_finished(fake, False)
    assert statuses == ['cancelled']
    assert outputs == [{}]
    assert fake._incrementalTerminalCount is None
