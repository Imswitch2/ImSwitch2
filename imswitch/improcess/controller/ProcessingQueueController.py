"""Shared Session processing queue and serial runner.

Patch-2 backend: manual Session batches and automatic Session processing feed the
same lazy queue.  The legacy DirectoryWatcher UI is intentionally left intact in
this patch; the integrated Session UI can switch to this controller later once
the execution model has been validated.
"""

from __future__ import annotations

import copy
from pathlib import Path

from qtpy import QtCore

from imswitch.improcess.controller.basecontrollers import ImProcessWidgetController
from imswitch.improcess.controller.LiveReconstructionController import (
    LiveReconstructionController,
)
from imswitch.improcess.controller.reconstruction_worker import (
    ReconstructionWorker,
    ReconstructionWorkerFailure,
    ReconstructionWorkerJob,
)
from imswitch.improcess.live.job import (
    LiveJobState,
    prepare_entry_job,
    start_prepared_live_job,
    watcher_entry_for_source,
)
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.processing_queue import (
    AutoProcessPolicy,
    ProcessingJob,
    ProcessingOrigin,
    ProcessingQueue,
)
from imswitch.improcess.reconstructors.registry import get_registry


class ProcessingQueueController(ImProcessWidgetController):
    """One serial processing lane shared by manual and automatic Session work."""

    sigQueueChanged = QtCore.Signal(object)

    def __init__(
        self,
        *args,
        mainController=None,
        sessionController=None,
        reconstructorManager=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._sessionController = sessionController
        self._reconstructorManager = reconstructorManager

        self.queue = ProcessingQueue()
        self.autoPolicy = AutoProcessPolicy()

        self._offlineThread = None
        self._offlineWorker = None
        self._offlineDataObjs: list[DataObj] = []
        # Worker terminal signals arrive before QThread.finished. Keep the queue
        # slot occupied until the thread itself is gone so a following job can
        # never overwrite the only strong references to a still-running QThread.
        self._offlinePendingOutcome: tuple[str, str] | None = None
        self._closing = False
        self._live = LiveReconstructionController(self._commChannel)
        self._live.sigFinalResult.connect(self._on_live_final_result)
        self._live.sigFinished.connect(self._on_live_finished)
        self._liveFinalResult = None
        self._skipRequested = False

        # Retry an auto job whose newly-created store is not readable yet.
        self._retryTimer = QtCore.QTimer(self)
        self._retryTimer.setInterval(500)
        self._retryTimer.timeout.connect(self._process_next)
        self._retryTimer.start()

        if self._sessionController is not None:
            added_signal = getattr(self._sessionController, "sigDatasetsAdded", None)
            if added_signal is not None:
                added_signal.connect(self._datasets_added)

    # ------------------------------------------------------------------ public API

    def queueSnapshot(self):
        snapshot = self.queue.view_snapshot(waiting_limit=100)
        snapshot["auto_enabled"] = bool(self.autoPolicy.enabled)
        return snapshot

    def queueDatasetStates(self) -> dict[str, str]:
        return self.queue.dataset_states()

    def setAutoEnabled(self, enabled: bool) -> None:
        """Enable/disable automatic submission; discovery itself is unaffected."""
        self.autoPolicy.enabled = bool(enabled)
        if self.autoPolicy.enabled:
            self.enqueueAutoEligible()
        self._emit_changed()

    def enqueueAutoEligible(self, dataset_ids=None) -> int:
        session = getattr(self._sessionController, "session", None)
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        if session is None or reconstructor is None or not self.autoPolicy.enabled:
            return 0
        params = self._snapshot_params(reconstructor)
        if dataset_ids is None:
            datasets = list(session.datasets)
        else:
            datasets = [
                dataset
                for dataset_id in dataset_ids
                if (dataset := session.dataset_by_id(str(dataset_id))) is not None
            ]
        jobs = []
        for dataset in datasets:
            processed = bool(self._sessionController.isDatasetProcessed(dataset.id))
            if not self.autoPolicy.eligible(
                dataset,
                queue=self.queue,
                reconstructor=reconstructor,
                processed=processed,
            ):
                continue
            jobs.append(self._job_for_dataset(
                dataset,
                reconstructor,
                params,
                origin=ProcessingOrigin.AUTO,
            ))
        added = self.queue.enqueue_many(jobs, position="end")
        if added:
            self._emit_changed()
            self._process_next()
        return added

    def enqueueSessionDatasets(
        self,
        dataset_ids,
        *,
        origin: ProcessingOrigin | str = ProcessingOrigin.MANUAL,
        position: str = "end",
    ) -> int:
        """Queue logical Session datasets without loading them first."""
        session = getattr(self._sessionController, "session", None)
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        if session is None or reconstructor is None:
            return 0
        if str(getattr(reconstructor, "result_role", "derived")) == "inspection":
            self._logger.warning("Inspection reconstructors are not processing-queue jobs")
            return 0
        try:
            origin = ProcessingOrigin(origin)
        except ValueError:
            raise ValueError(f"Unknown processing origin: {origin!r}")

        params = self._snapshot_params(reconstructor)
        jobs = []
        for dataset_id in dataset_ids:
            dataset = session.dataset_by_id(str(dataset_id))
            if dataset is None or getattr(dataset, "raw_deleted", False):
                continue
            jobs.append(self._job_for_dataset(dataset, reconstructor, params, origin=origin))
        added = self.queue.enqueue_many(jobs, position=position)
        if added:
            self._emit_changed()
            self._process_next()
        return added

    def pause(self) -> None:
        """Pause queue advancement and a live run at its next TP boundary."""
        self.queue.pause()
        if self._live.is_running:
            self._live.pause()
        self._emit_changed()

    def resume(self) -> None:
        self.queue.resume()
        if self._live.is_running and self._live.is_paused:
            self._live.resume()
        self._emit_changed()
        self._process_next()

    def canCloseSession(self) -> bool:
        """An active job must remain attached to its Session until it finishes."""
        return self.queue.current is None

    def sessionClosed(self) -> None:
        """Discard pending work only after Session close has been confirmed."""
        self.autoPolicy.enabled = False
        self.queue.clear_waiting()
        self.queue.resume()
        self._emit_changed()

    def skipCurrent(self) -> None:
        """Skip the current job at the safest boundary the active runner offers."""
        if self.queue.current is None:
            return
        self._skipRequested = True
        if self._live.is_running:
            self._live.stop(graceful=True, notify_finished=True)
            return
        worker = self._offlineWorker
        if worker is not None:
            worker.request_cancel()
            return
        self._finish_current(skipped=True)

    # ------------------------------------------------------------------ queue feed

    @QtCore.Slot(object)
    def _datasets_added(self, dataset_ids) -> None:
        if self.autoPolicy.enabled:
            self.enqueueAutoEligible(dataset_ids or ())

    def _snapshot_params(self, reconstructor) -> dict:
        getter = getattr(self._reconstructorManager, "snapshotProcessingParams", None)
        if callable(getter):
            return copy.deepcopy(getter(reconstructor))
        return {}

    @staticmethod
    def _job_for_dataset(dataset, reconstructor, params, *, origin):
        return ProcessingJob(
            dataset_id=str(dataset.id),
            label=str(dataset.label),
            reconstructor_id=str(reconstructor.id),
            params=copy.deepcopy(params),
            origin=origin,
        )

    # ------------------------------------------------------------------ execution

    @QtCore.Slot()
    def _process_next(self) -> None:
        if (
            self._closing
            or self.queue.paused
            or self.queue.current is not None
            or self._offlineThread is not None
            or self._live.is_running
        ):
            return

        waiting = self.queue.waiting
        if not waiting:
            return

        # Match the original DirectoryWatcher/JobQueue behavior: a newly
        # discovered Auto source stays WAITING while it is still becoming
        # readable. Previously we took/deferred the job every 500 ms, emitting
        # RUNNING -> WAITING twice per probe and forcing the Session UI to
        # rebuild on every retry.
        candidate = waiting[0]
        prepared_live_job = None
        live_resolution = None
        session = getattr(self._sessionController, "session", None)
        candidate_dataset = (
            session.dataset_by_id(candidate.dataset_id) if session is not None else None
        )
        candidate_reconstructor = get_registry().get_reconstructor(
            candidate.reconstructor_id, raise_on_missing=False
        )
        if (
            candidate_dataset is not None
            and candidate_reconstructor is not None
            and ProcessingQueueController._uses_streaming_runner(
                candidate, candidate_dataset, candidate_reconstructor
            )
        ):
            # Re-enter the exact DirectoryWatcher/JobQueue boundary. Session
            # discovery may have descended into a container and stored the
            # child dataset as primary_path; the old live path starts from the
            # immediate watched entry and lets JobQueue choose the seed.
            raw_root = getattr(session, "raw_root", Path(candidate_dataset.primary_path).parent)
            job_path = watcher_entry_for_source(raw_root, candidate_dataset.primary_path)
            live_resolution = prepare_entry_job(
                job_path, candidate_reconstructor
            )
            if live_resolution.state == LiveJobState.PENDING:
                return
            prepared_live_job = live_resolution.prepared

        job = self.queue.take_next()
        if job is None:
            return
        self._skipRequested = False
        self._liveFinalResult = None
        self._emit_changed()

        dataset = session.dataset_by_id(job.dataset_id) if session is not None else None
        reconstructor = get_registry().get_reconstructor(
            job.reconstructor_id, raise_on_missing=False
        )
        if dataset is None:
            self._finish_current(error="Session dataset no longer exists")
            return
        if reconstructor is None:
            self._finish_current(error=f"Reconstructor {job.reconstructor_id!r} is unavailable")
            return

        if ProcessingQueueController._uses_streaming_runner(
            job, dataset, reconstructor
        ):
            if live_resolution is not None and live_resolution.state == LiveJobState.FAILED:
                reason = live_resolution.reason or "Streaming source access failed"
                self._logger.error(
                    "Failing queued dataset %s: %s",
                    getattr(dataset, "label", job.label),
                    reason,
                )
                self._finish_current(error=reason)
                return
            if live_resolution is not None and live_resolution.state == LiveJobState.UNSUPPORTED:
                reason = live_resolution.reason or "Streaming source is unsupported"
                self._logger.warning(
                    "Skipping queued dataset %s: %s",
                    getattr(dataset, "label", job.label),
                    reason,
                )
                self._finish_current(error=reason)
                return
            if self._start_live(
                job, dataset, reconstructor, prepared=prepared_live_job
            ):
                return
            self._finish_current(error="Streaming source is not compatible with this dataset")
            return

        self._start_offline(job, dataset, reconstructor)

    @staticmethod
    def _uses_streaming_runner(job, dataset, reconstructor) -> bool:
        """Whether this logical dataset must stay one streaming reconstruction.

        Automatic jobs follow growing sources through the streaming runner, as
        before.  More importantly, a logical Session time-lapse is *always* one
        processing unit when the selected reconstructor supports streaming --
        independent of whether it was submitted manually, automatically, or
        from Multi Data Management.  Exploding its ``source_refs`` into offline
        jobs would turn one T-series into one Result per timepoint.
        """
        if dataset is None or reconstructor is None:
            return False
        if not getattr(reconstructor, "supports_streaming", False):
            return False
        return (
            job.origin == ProcessingOrigin.AUTO
            or str(getattr(dataset, "kind", "")) == "time-lapse"
        )

    def _start_live(self, job, dataset, reconstructor, *, prepared=None) -> bool:
        if not getattr(reconstructor, "supports_streaming", False):
            return False
        if prepared is None:
            session = getattr(self._sessionController, "session", None)
            raw_root = getattr(session, "raw_root", Path(dataset.primary_path).parent)
            job_path = watcher_entry_for_source(raw_root, dataset.primary_path)
            resolution = prepare_entry_job(job_path, reconstructor)
            if resolution.state != LiveJobState.READY:
                return False
            prepared = resolution.prepared

        source_type = prepared.source_type
        self._logger.info(
            "Processing dataset %s: kind=%s origin=%s runner=streaming "
            "job=%s seed=%s source=%s/%s timepoints=%s",
            getattr(dataset, "label", job.label),
            getattr(dataset, "kind", ""),
            getattr(job.origin, "value", str(job.origin)),
            prepared.job_path,
            prepared.seed_path,
            getattr(source_type, "format_id", "?"),
            getattr(source_type, "layout", "?"),
            getattr(dataset, "planned_timepoints", None),
        )
        try:
            started = start_prepared_live_job(
                self._live,
                prepared,
                reconstructor,
                dict(job.params),
                name=job.label,
            )
        except Exception as exc:
            self._logger.exception("Could not start queued live reconstruction")
            self._finish_current(error=str(exc))
            return True
        if not started:
            self._finish_current(error="Live reconstruction did not start")
        return True

    def _start_offline(self, job, dataset, reconstructor) -> None:
        self._logger.info(
            "Processing dataset %s: kind=%s origin=%s runner=offline timepoints=%s",
            getattr(dataset, "label", job.label),
            getattr(dataset, "kind", ""),
            getattr(job.origin, "value", str(job.origin)),
            getattr(dataset, "planned_timepoints", None),
        )
        if reconstructor.id == "monalisa-legacy":
            self._finish_current(
                error="MoNaLISA Legacy is not yet supported by the shared processing queue"
            )
            return

        data_objs = self._data_objs_for_dataset(dataset)
        if not data_objs:
            self._finish_current(error="No readable source units found")
            return

        budget = self._reconstructorManager._default_memory_budget_bytes()
        worker_jobs = []
        for data_obj in data_objs:
            params = copy.deepcopy(job.params)
            try:
                validator = getattr(reconstructor, "validate_source", None)
                if callable(validator):
                    validator(data_obj)
                estimate = reconstructor.estimate_resources(data_obj, params)
            except Exception as exc:
                self._release_data_objs(data_objs)
                self._finish_current(error=f"Reconstruction preflight failed: {exc}")
                return
            confirmed = False
            if estimate is not None and estimate.required_bytes > budget:
                confirmer = getattr(self._widget, "confirmReconstructionMemory", None)
                confirmed = bool(callable(confirmer) and confirmer(estimate, budget))
                if not confirmed:
                    self._release_data_objs(data_objs)
                    self._finish_current(error="Reconstruction memory estimate was declined")
                    return
            worker_jobs.append(ReconstructionWorkerJob(
                data_obj=data_obj,
                params=params,
                memory_budget_bytes=budget,
                confirmed_over_budget=confirmed,
            ))

        thread = QtCore.QThread()
        self._offlinePendingOutcome = None
        worker = ReconstructionWorker(
            reconstructor,
            worker_jobs,
            consolidate=False,
            retain_results=False,
            release_after_each=True,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resultReady.connect(
            lambda result, _run, r=reconstructor: self._on_offline_result(result, r)
        )
        worker.finished.connect(self._on_offline_finished)
        worker.failed.connect(self._on_offline_failed)
        worker.cancelled.connect(self._on_offline_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(lambda: self._offline_thread_finished(thread))
        thread.finished.connect(thread.deleteLater)

        self._offlineDataObjs = data_objs
        self._offlineThread = thread
        self._offlineWorker = worker
        thread.start()

    def _data_objs_for_dataset(self, dataset) -> list[DataObj]:
        refs = list(getattr(dataset, "source_refs", ()) or ())
        if dataset.kind != "time-lapse":
            refs = refs[:1]
        out = []
        for ref in refs:
            dataset_name = ref.dataset
            if dataset_name in (None, ""):
                try:
                    names = list(DataObj.getDatasetNames(str(ref.path)))
                except Exception:
                    continue
                if len(names) != 1:
                    continue
                dataset_name = names[0]
            out.append(DataObj(Path(ref.path).name, str(dataset_name), path=str(ref.path)))
        return out

    def _claim_current_result(self, result) -> None:
        job = self.queue.current
        claimer = getattr(self._sessionController, "claimResultForDataset", None)
        if job is not None and callable(claimer):
            claimer(result, job.dataset_id)

    @QtCore.Slot(object, object)
    def _on_offline_result(self, result, reconstructor) -> None:
        self._claim_current_result(result)
        self._reconstructorManager.publishQueuedResult(
            result,
            getattr(result, "name", "reconstruction"),
            reconstructor=reconstructor,
        )

    @QtCore.Slot(object)
    def _on_offline_finished(self, _outcome) -> None:
        self._offlinePendingOutcome = (
            "skipped" if self._skipRequested else "completed", ""
        )

    @QtCore.Slot(object)
    def _on_offline_failed(self, failure: ReconstructionWorkerFailure) -> None:
        self._offlinePendingOutcome = ("failed", str(failure.message or ""))

    @QtCore.Slot(str)
    def _on_offline_cancelled(self, message: str) -> None:
        if self._skipRequested:
            self._offlinePendingOutcome = ("skipped", "")
        else:
            self._offlinePendingOutcome = (
                "failed", str(message or "Reconstruction cancelled")
            )

    def _offline_thread_finished(self, thread) -> None:
        """Release runner references before freeing the queue slot.

        ``ReconstructionWorker.finished`` means the worker function returned; it
        does *not* mean the owning QThread has finished its event loop. Advancing
        there allowed the next job to replace ``_offlineThread`` while the old
        QThread was still alive, which can lead to ``QThread: Destroyed while
        thread is still running``.
        """
        if self._offlineThread is not thread:
            return
        outcome = self._offlinePendingOutcome or (
            "failed", "Processing thread stopped without a terminal outcome"
        )
        self._release_data_objs(self._offlineDataObjs)
        self._offlineDataObjs = []
        self._offlineThread = None
        self._offlineWorker = None
        self._offlinePendingOutcome = None

        state, message = outcome
        if state == "skipped":
            self._finish_current(skipped=True)
        elif state == "failed":
            self._finish_current(error=message)
        else:
            self._finish_current()

    @staticmethod
    def _release_data_objs(data_objs) -> None:
        for data_obj in data_objs:
            releaser = getattr(data_obj, "checkAndUnloadData", None)
            if callable(releaser):
                try:
                    releaser()
                except Exception:
                    pass

    # ------------------------------------------------------------------ live completion

    @QtCore.Slot(object)
    def _on_live_final_result(self, result) -> None:
        # sigFinalResult is emitted before sigResultFinalized, so claim exact
        # Session ownership here while the queue still holds the current job.
        self._claim_current_result(result)
        self._liveFinalResult = result

    @QtCore.Slot()
    def _on_live_finished(self) -> None:
        if self.queue.current is None:
            return
        if self._skipRequested:
            self._finish_current(skipped=True)
        elif self._liveFinalResult is not None:
            self._finish_current()
        else:
            self._finish_current(error="Live processing ended without a final result")

    # ------------------------------------------------------------------ completion / lifecycle

    def _finish_current(self, *, error: str = "", skipped: bool = False) -> None:
        if self.queue.current is None:
            return
        if skipped:
            self.queue.skip_current()
        elif error:
            self.queue.fail_current(error)
        else:
            self.queue.complete_current()
        self._skipRequested = False
        self._liveFinalResult = None
        self._emit_changed()
        if not self._closing and not self.queue.paused:
            QtCore.QTimer.singleShot(0, self._process_next)

    def _emit_changed(self) -> None:
        snapshot = self.queueSnapshot()
        snapshot["changed_dataset_ids"] = self.queue.consume_changed_dataset_ids()
        self.sigQueueChanged.emit(snapshot)

    def closeEvent(self):
        self._closing = True
        self._retryTimer.stop()
        self.autoPolicy.enabled = False
        self.queue.clear_waiting()
        self.queue.pause()
        if self._live.is_running:
            self._live.stop(graceful=False)
        if self._offlineWorker is not None:
            self._offlineWorker.request_cancel()
        # Do not unload DataObjs from the GUI thread while the worker may still
        # be reading them. _offline_thread_finished owns that cleanup.
        return self.shutdownComplete()

    def shutdownComplete(self):
        return self._offlineThread is None and not self._live.is_running


__all__ = ["ProcessingQueueController"]
