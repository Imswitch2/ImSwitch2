"""Background worker for explicit/automatic MoNaLISA localization."""

from __future__ import annotations

import traceback
from dataclasses import dataclass

from qtpy import QtCore


@dataclass(frozen=True)
class LocalizationWorkerJob:
    request_id: int
    data_obj: object
    params: dict
    signature: str
    automatic: bool = False


@dataclass(frozen=True)
class LocalizationWorkerOutcome:
    job: LocalizationWorkerJob
    localization: object
    status: str
    details: dict


@dataclass(frozen=True)
class LocalizationWorkerFailure:
    job: LocalizationWorkerJob
    message: str
    traceback: str


class LocalizationWorker(QtCore.QObject):
    """Run one immutable localization request away from the GUI thread."""

    finished = QtCore.Signal(object)
    failed = QtCore.Signal(object)

    def __init__(self, reconstructor, job: LocalizationWorkerJob, parent=None):
        super().__init__(parent)
        self._reconstructor = reconstructor
        self._job = job

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self._reconstructor.localize(
                self._job.data_obj, dict(self._job.params)
            )
            localization, status = result[:2]
            details = result[2] if len(result) > 2 else {}
            self.finished.emit(LocalizationWorkerOutcome(
                job=self._job,
                localization=localization,
                status=str(status or "Auto localization ready"),
                details=dict(details or {}),
            ))
        except Exception as exc:
            self.failed.emit(LocalizationWorkerFailure(
                job=self._job,
                message=str(exc) or type(exc).__name__,
                traceback=traceback.format_exc(),
            ))
