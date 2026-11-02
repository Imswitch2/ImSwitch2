"""Shared lazy processing queue for manual, Working Set and automatic Session jobs.

The queue stores *references* to logical Session datasets, never loaded image data.
Loading and execution belong to the controller/runner layer.  Keeping this model
Qt-free makes ordering and state transitions deterministic and easy to test.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable
from uuid import uuid4


class ProcessingOrigin(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"
    WORKING_SET = "working-set"


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
        jobs = list(jobs)
        if position == "next":
            # Preserve caller order while repeatedly inserting at the front.
            jobs = list(reversed(jobs))
        added = 0
        for job in jobs:
            added += int(self.enqueue(job, position=position))
        return added

    def take_next(self) -> ProcessingJob | None:
        if self._paused or self._current is not None or not self._waiting:
            return None
        job = self._waiting.pop(0)
        job.state = ProcessingJobState.RUNNING
        self._current = job
        self._changed_dataset_ids.add(str(job.dataset_id))
        return job

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
        if processed is None:
            processed = bool(getattr(dataset, "saved_reconstructions", None))
        if processed:
            return False
        if queue.contains_dataset(getattr(dataset, "id", "")):
            return False
        # Inspection-only plugins are views, not automatic processing recipes.
        if str(getattr(reconstructor, "result_role", "derived")) == "inspection":
            return False
        # Automatic processing is initially limited to reconstructors that can
        # safely follow a source that may still be growing.  Manual queued jobs
        # have no such restriction and work for ordinary reconstructors too.
        return bool(getattr(reconstructor, "supports_streaming", False))


__all__ = [
    "AutoProcessPolicy",
    "ProcessingJob",
    "ProcessingJobView",
    "ProcessingJobState",
    "ProcessingOrigin",
    "ProcessingQueue",
]
