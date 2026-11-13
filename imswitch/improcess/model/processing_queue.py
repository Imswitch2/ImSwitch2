"""Shared lazy processing queue for manual, Multi-data and automatic Session jobs.

The queue stores lightweight logical source references, never loaded image pixels.
Loading and execution belong to the controller/runner layer.  Keeping this model
Qt-free makes ordering and state transitions deterministic and easy to test.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from imswitch.improcess.model.timepoint_scope import (
    TimepointScope,
    coerce_timepoint_scope,
)
from uuid import uuid4


class ProcessingOrigin(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"
    WORKING_SET = "working-set"


class ExecutionRequirement(str, Enum):
    """How a job must execute; independent of who submitted it.

    POLICY retains pre-M2 behavior for older queued jobs. FOLLOW is reserved
    for the explicit readiness scheduler in M2B.
    """

    POLICY = "policy"
    FINITE = "finite"
    FOLLOW = "follow"


class ProcessingJobState(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"




@dataclass(frozen=True)
class ProcessingJobView:
    """Lightweight UI projection of a queued job; deliberately excludes params."""

    id: str
    dataset_id: str
    label: str
    reconstructor_id: str
    origin: ProcessingOrigin
    state: ProcessingJobState
    error: str = ""
    source: str = "session"
    batch_index: int = 1
    batch_total: int = 1
    timepoint_scope: TimepointScope = field(default_factory=TimepointScope.all)
    timepoint_total: int | None = None
    execution_requirement: ExecutionRequirement = ExecutionRequirement.POLICY
    readiness_status: str = ""

@dataclass
class ProcessingJob:
    """One logical dataset waiting to be processed.

    ``params`` is a queue-time snapshot.  Changing the parameter widget after a
    job was submitted therefore cannot silently change what that job means.
    """

    dataset_id: str
    label: str
    reconstructor_id: str
    params: dict
    origin: ProcessingOrigin = ProcessingOrigin.MANUAL
    id: str = field(default_factory=lambda: uuid4().hex)
    state: ProcessingJobState = ProcessingJobState.WAITING
    error: str = ""
    source: str = "session"
    batch_index: int = 1
    batch_total: int = 1
    timepoint_scope: TimepointScope = field(default_factory=TimepointScope.all)
    timepoint_total: int | None = None
    execution_requirement: ExecutionRequirement = ExecutionRequirement.POLICY
    readiness_status: str = ""

    def __post_init__(self) -> None:
        self.execution_requirement = ExecutionRequirement(self.execution_requirement)
        self.timepoint_scope = coerce_timepoint_scope(self.timepoint_scope)
        if self.timepoint_total not in (None, 0):
            self.timepoint_total = max(1, int(self.timepoint_total))
        else:
            self.timepoint_total = None

    def snapshot(self) -> "ProcessingJob":
        return ProcessingJob(
            id=self.id,
            dataset_id=self.dataset_id,
            label=self.label,
            reconstructor_id=self.reconstructor_id,
            params=copy.deepcopy(self.params),
            origin=self.origin,
            state=self.state,
            error=self.error,
            source=self.source,
            batch_index=self.batch_index,
            batch_total=self.batch_total,
            timepoint_scope=self.timepoint_scope,
            timepoint_total=self.timepoint_total,
            execution_requirement=self.execution_requirement,
            readiness_status=self.readiness_status,
        )

    def view(self) -> ProcessingJobView:
        return ProcessingJobView(
            id=self.id,
            dataset_id=self.dataset_id,
            label=self.label,
            reconstructor_id=self.reconstructor_id,
            origin=self.origin,
            state=self.state,
            error=self.error,
            source=self.source,
            batch_index=self.batch_index,
            batch_total=self.batch_total,
            timepoint_scope=self.timepoint_scope,
            timepoint_total=self.timepoint_total,
            execution_requirement=self.execution_requirement,
            readiness_status=self.readiness_status,
        )


class ProcessingQueue:
    """Single serial queue shared by every processing entry point."""

    def __init__(self) -> None:
        self._waiting: list[ProcessingJob] = []
        self._current: ProcessingJob | None = None
        self._history: list[ProcessingJob] = []
        self._active_dataset_ids: set[str] = set()
        self._changed_dataset_ids: set[str] = set()
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def current(self) -> ProcessingJob | None:
        return self._current

    @property
    def waiting(self) -> tuple[ProcessingJob, ...]:
        return tuple(self._waiting)

    @property
    def history(self) -> tuple[ProcessingJob, ...]:
        return tuple(self._history)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def contains_dataset(self, dataset_id: str) -> bool:
        return str(dataset_id) in self._active_dataset_ids

    def enqueue(self, job: ProcessingJob, *, position: str = "end") -> bool:
        """Add one job unless the same logical dataset is already active/waiting.

        ``position='next'`` inserts immediately after the running job.  The
        running job itself is never pre-empted.
        """
        if position not in {"next", "end"}:
            raise ValueError("position must be 'next' or 'end'")
        if self.contains_dataset(job.dataset_id):
            return False
        job.state = ProcessingJobState.WAITING
        job.error = ""
        if position == "next":
            self._waiting.insert(0, job)
        else:
            self._waiting.append(job)
        self._active_dataset_ids.add(str(job.dataset_id))
        self._changed_dataset_ids.add(str(job.dataset_id))
        return True

    def enqueue_many(
        self, jobs: Iterable[ProcessingJob], *, position: str = "end"
    ) -> int:
        if position not in {"next", "end"}:
            raise ValueError("position must be 'next' or 'end'")

        # Resolve deduplication before assigning batch metadata.  This keeps
        # Item x/y truthful even when callers submit an already-active dataset
        # alongside new work. Duplicate dataset ids inside one submission are
        # also collapsed deterministically.
        accepted: list[ProcessingJob] = []
        seen = set(self._active_dataset_ids)
        for job in list(jobs):
            dataset_id = str(job.dataset_id)
            if dataset_id in seen:
                continue
            seen.add(dataset_id)
            accepted.append(job)

        total = len(accepted)
        for index, job in enumerate(accepted, start=1):
            job.batch_index = index
            job.batch_total = max(1, total)

        ordered = list(reversed(accepted)) if position == "next" else accepted
        added = 0
        for job in ordered:
            added += int(self.enqueue(job, position=position))
        return added

    def take_ready(self, index: int = 0) -> ProcessingJob | None:
        """Run the first ready item without making older pending jobs block it."""
        if self._paused or self._current is not None or not 0 <= index < len(self._waiting):
            return None
        job = self._waiting.pop(index)
        job.state = ProcessingJobState.RUNNING
        job.readiness_status = ""
        self._current = job
        self._changed_dataset_ids.add(str(job.dataset_id))
        return job

    def take_next(self) -> ProcessingJob | None:
        return self.take_ready(0)

    def _finish_current(self, state: ProcessingJobState, error: str = "") -> ProcessingJob | None:
        job = self._current
        if job is None:
            return None
        job.state = state
        job.error = str(error or "")
        self._history.append(job)
        self._active_dataset_ids.discard(str(job.dataset_id))
        self._changed_dataset_ids.add(str(job.dataset_id))
        self._current = None
        return job

    def defer_current(self, *, position: str = "end") -> ProcessingJob | None:
        """Return the running job to the queue without adding history.

        A newly-created store may exist before it is readable.  Deferring it to
        the end prevents that ordinary transient state from blocking later jobs
        that are already ready, matching the legacy ``JobQueue`` behaviour.
        """
        if position not in {"next", "end"}:
            raise ValueError("position must be 'next' or 'end'")
        job = self._current
        if job is None:
            return None
        job.state = ProcessingJobState.WAITING
        job.error = ""
        if position == "next":
            self._waiting.insert(0, job)
        else:
            self._waiting.append(job)
        self._changed_dataset_ids.add(str(job.dataset_id))
        self._current = None
        return job

    def complete_current(self) -> ProcessingJob | None:
        return self._finish_current(ProcessingJobState.COMPLETED)

    def fail_current(self, error: str) -> ProcessingJob | None:
        return self._finish_current(ProcessingJobState.FAILED, error)

    def skip_current(self) -> ProcessingJob | None:
        return self._finish_current(ProcessingJobState.SKIPPED)

    def clear_waiting(self) -> tuple[ProcessingJob, ...]:
        removed = tuple(self._waiting)
        for job in removed:
            job.state = ProcessingJobState.SKIPPED
            self._history.append(job)
            self._active_dataset_ids.discard(str(job.dataset_id))
            self._changed_dataset_ids.add(str(job.dataset_id))
        self._waiting.clear()
        return removed

    def remove_waiting(self, job_ids: Iterable[str]) -> tuple[ProcessingJob, ...]:
        """Remove selected *waiting* jobs without touching the running job.

        Jobs are addressed by stable queue IDs rather than UI row positions.
        Removed jobs are recorded as skipped so queue history remains truthful,
        and their logical datasets immediately become eligible for a later
        explicit/manual submission.
        """
        wanted = {str(job_id) for job_id in job_ids}
        if not wanted:
            return ()

        kept: list[ProcessingJob] = []
        removed: list[ProcessingJob] = []
        for job in self._waiting:
            if str(job.id) not in wanted:
                kept.append(job)
                continue
            job.state = ProcessingJobState.SKIPPED
            self._history.append(job)
            self._active_dataset_ids.discard(str(job.dataset_id))
            self._changed_dataset_ids.add(str(job.dataset_id))
            removed.append(job)
        self._waiting = kept
        return tuple(removed)

    def move_waiting(self, job_ids: Iterable[str], *, direction: int) -> bool:
        """Move selected waiting jobs by one slot while preserving their order.

        ``direction`` must be ``-1`` (up / earlier) or ``+1`` (down / later).
        The running job is stored separately and can therefore never be moved.
        """
        if direction not in {-1, 1}:
            raise ValueError("direction must be -1 or +1")
        wanted = {str(job_id) for job_id in job_ids}
        if not wanted or len(self._waiting) < 2:
            return False

        changed = False
        if direction < 0:
            indices = range(1, len(self._waiting))
            for index in indices:
                current = self._waiting[index]
                previous = self._waiting[index - 1]
                if str(current.id) in wanted and str(previous.id) not in wanted:
                    self._waiting[index - 1], self._waiting[index] = current, previous
                    changed = True
        else:
            indices = range(len(self._waiting) - 2, -1, -1)
            for index in indices:
                current = self._waiting[index]
                following = self._waiting[index + 1]
                if str(current.id) in wanted and str(following.id) not in wanted:
                    self._waiting[index], self._waiting[index + 1] = following, current
                    changed = True
        return changed

    def dataset_states(self) -> dict[str, str]:
        states = {str(job.dataset_id): "waiting" for job in self._waiting}
        if self._current is not None:
            states[str(self._current.dataset_id)] = "running"
        return states

    def consume_changed_dataset_ids(self) -> tuple[str, ...]:
        changed = tuple(sorted(self._changed_dataset_ids))
        self._changed_dataset_ids.clear()
        return changed

    def view_snapshot(self, *, waiting_limit: int = 100) -> dict:
        """Cheap UI snapshot: no params and no completed history."""
        limit = max(0, int(waiting_limit))
        return {
            "paused": self._paused,
            "current": self._current.view() if self._current is not None else None,
            "waiting_count": len(self._waiting),
            "waiting": tuple(job.view() for job in self._waiting[:limit]),
        }

    def snapshot(self) -> dict:
        return {
            "paused": self._paused,
            "current": self._current.snapshot() if self._current is not None else None,
            "waiting": tuple(job.snapshot() for job in self._waiting),
            "history": tuple(job.snapshot() for job in self._history),
        }


def supports_session_manual(reconstructor) -> bool:
    """Manual queue eligibility is independent of the result's semantic role."""
    if reconstructor is None:
        return False
    return (str(getattr(reconstructor, "result_role", "derived")) != "inspection"
            or bool(getattr(reconstructor, "supports_session_manual", False)))


def supports_session_auto(reconstructor) -> bool:
    """Auto admission is independent of whether a result is derived or raw.

    Derived plugins retain their existing Auto eligibility. Inspection plugins
    must opt in explicitly; View only and Time lapse do so independently,
    without allowing arbitrary inspection plugins into Auto jobs.
    """
    if reconstructor is None:
        return False
    return (str(getattr(reconstructor, "result_role", "derived")) != "inspection"
            or bool(getattr(reconstructor, "supports_session_auto", False)))


class AutoProcessPolicy:
    """V1 auto rule: queue Session datasets that have no derived result yet."""

    def __init__(self) -> None:
        self.enabled = False

    def eligible(
        self, dataset, *, queue: ProcessingQueue, reconstructor, processed: bool | None = None
    ) -> bool:
        if not self.enabled or dataset is None or reconstructor is None:
            return False
        if getattr(dataset, "raw_deleted", False):
            return False
        if getattr(dataset, "auto_process_ignored", False):
            return False
        if processed is None:
            processed = bool(getattr(dataset, "saved_reconstructions", None))
        if processed:
            return False
        if queue.contains_dataset(getattr(dataset, "id", "")):
            return False
        # An inspection view is Auto-eligible only when explicitly opted in.
        if not supports_session_auto(reconstructor):
            return False
        # Non-streaming plugins are eligible too. The source-readiness gate
        # holds their Auto job until explicit writer markers prove it finite.
        return True


__all__ = [
    "AutoProcessPolicy",
    "supports_session_auto",
    "supports_session_manual",
    "ProcessingJob",
    "ProcessingJobView",
    "ProcessingJobState",
    "ProcessingOrigin",
    "ExecutionRequirement",
    "ProcessingQueue",
]
