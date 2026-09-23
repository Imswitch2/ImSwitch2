"""Reusable opt-in worker for expensive ImProcess reconstructors."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, replace

from qtpy import QtCore

from imswitch.improcess.reconstructors.base import (
    CancellationToken,
    ReconstructionCancelled,
    ReconstructionContext,
)
from imswitch.improcess.reconstructors.run import run_consolidation, run_reconstruction


@dataclass(frozen=True)
class ReconstructionWorkerJob:
    data_obj: object
    params: dict
    memory_budget_bytes: int | None
    confirmed_over_budget: bool = False


@dataclass(frozen=True)
class ReconstructionWorkerOutcome:
    results: tuple[object, ...]
    merged: object | None = None
    consolidation_error: str | None = None
    #: One ReconstructionRun per result, in order: the source description,
    #: parameters and provenance node each result was made with.
    runs: tuple[object, ...] = ()


@dataclass(frozen=True)
class ReconstructionWorkerFailure:
    message: str
    traceback: str


class ReconstructionWorker(QtCore.QObject):
    """Run immutable reconstruction jobs away from the GUI thread."""

    progress = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(object)
    cancelled = QtCore.Signal(str)

    def __init__(self, reconstructor, jobs, *, consolidate=False, parent=None):
        super().__init__(parent)
        self._reconstructor = reconstructor
        self._jobs = tuple(jobs)
        self._consolidate = bool(consolidate)
        self._token = CancellationToken()

    def request_cancel(self) -> None:
        """Thread-safe; intentionally callable while ``run`` owns the worker."""
        self._token.cancel()

    def _forward_progress(self, job_index, update):
        count = max(1, len(self._jobs))
        overall = min(1.0, (job_index + update.fraction) / count)
        message = update.message
        if count > 1:
            message = f"Item {job_index + 1}/{count}: {message}"
        self.progress.emit(
            replace(update, fraction=overall, message=message)
        )

    @QtCore.Slot()
    def run(self) -> None:
        results = []
        runs = []
        try:
            self._token.check()
            for index, job in enumerate(self._jobs):
                validator = getattr(self._reconstructor, "validate_source", None)
                if callable(validator):
                    validator(job.data_obj)
                context = ReconstructionContext(
                    progress_callback=lambda update, i=index: self._forward_progress(
                        i, update
                    ),
                    cancellation_token=self._token,
                    memory_budget_bytes=job.memory_budget_bytes,
                    confirmed_over_budget=job.confirmed_over_budget,
                )
                run = run_reconstruction(
                    self._reconstructor, job.data_obj, dict(job.params), context=context
                )
                context.check_cancelled()
                results.append(run.result)
                runs.append(run)

            merged = None
            consolidation_error = None
            if self._consolidate and results:
                self._token.check()
                try:
                    merged = run_consolidation(self._reconstructor, results)
                except Exception as exc:
                    consolidation_error = str(exc)
            self._token.check()
            self.finished.emit(ReconstructionWorkerOutcome(
                results=tuple(results),
                merged=merged,
                consolidation_error=consolidation_error,
                runs=tuple(runs),
            ))
        except ReconstructionCancelled as exc:
            self.cancelled.emit(str(exc) or "Reconstruction cancelled")
        except Exception as exc:
            self.failed.emit(ReconstructionWorkerFailure(
                message=str(exc) or type(exc).__name__,
                traceback=traceback.format_exc(),
            ))
