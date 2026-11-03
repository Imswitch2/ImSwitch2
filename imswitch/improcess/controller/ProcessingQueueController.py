"""Shared serial processing queue for Session and ad-hoc Loaded data.

Manual Session batches, Session auto-processing, and Quick Load batches feed the
same lazy queue. Sources remain lightweight until execution; Session ownership
and autosave semantics are retained only for Session-backed jobs.
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
from imswitch.improcess.model.lapse_source import TIME_LAPSE_SOURCE_KIND
from imswitch.improcess.model.timepoint_scope import (
    TimepointScope,
    coerce_timepoint_scope,
)
from imswitch.improcess.model.processing_queue import (
    AutoProcessPolicy,
    ProcessingJob,
    ProcessingOrigin,
    ProcessingQueue,
)
from imswitch.improcess.reconstructors.registry import get_registry


class ProcessingQueueController(ImProcessWidgetController):
    """One serial processing lane shared by manual and automatic processing."""

    sigQueueChanged = QtCore.Signal(object)
    sigProgressChanged = QtCore.Signal(object)

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
        # Runtime-only ad-hoc sources queued from Quick Load. The queue model
        # keeps only a stable logical id; this registry keeps the lightweight
        # DataObj reference needed to materialize the source lazily at execution.
        self._externalSources: dict[str, DataObj] = {}

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
        self._live.sigTimepointProgress.connect(self._on_live_timepoint_progress)
        self._liveFinalResult = None
        self._currentProgress = {}
        self._currentPlannedTimepoints = None
        self._lastTerminalStatus = {}
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
        snapshot["current_progress"] = dict(self._currentProgress or {})
        snapshot["last_terminal_status"] = dict(self._lastTerminalStatus or {})
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
                timepoint_scope=TimepointScope.all(),
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
        timepoint_scope=None,
    ) -> int:
        """Queue logical Session datasets without loading them first.

        ``timepoint_scope`` is meaningful only for a single logical lapse. It is
        snapped into the queue job and never follows later Current-T changes.
        """
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
        dataset_ids = list(dataset_ids or ())
        requested_scope = coerce_timepoint_scope(timepoint_scope)
        jobs = []
        for dataset_id in dataset_ids:
            dataset = session.dataset_by_id(str(dataset_id))
            if dataset is None or getattr(dataset, "raw_deleted", False):
                continue
            scope = (
                requested_scope
                if len(dataset_ids) == 1 and getattr(dataset, "kind", "") == "time-lapse"
                else TimepointScope.all()
            )
            jobs.append(self._job_for_dataset(
                dataset, reconstructor, params, origin=origin, timepoint_scope=scope
            ))
        added = self.queue.enqueue_many(jobs, position=position)
        if added:
            self._emit_changed()
            self._process_next()
        return added

    def enqueueLoadedDataObjs(
        self, data_objs, *, position: str = "end", timepoint_scope=None
    ) -> int:
        """Queue selected Quick Load sources through the shared serial runner.

        The catalog objects themselves remain unloaded until their job starts.
        If an object actually belongs to the active Session, submit the Session
        identity instead so result ownership/autosave semantics remain exact.
        """
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        if reconstructor is None:
            return 0
        if str(getattr(reconstructor, "result_role", "derived")) == "inspection":
            return 0
        params = self._snapshot_params(reconstructor)
        session = getattr(self._sessionController, "session", None)
        data_objs = list(data_objs or ())
        requested_scope = coerce_timepoint_scope(timepoint_scope)
        jobs = []
        for data_obj in data_objs:
            if data_obj is None:
                continue
            session_dataset = None
            if session is not None:
                path = getattr(data_obj, "dataPath", None)
                dataset_name = getattr(data_obj, "datasetName", None)
                for candidate in session.datasets:
                    if candidate.matches_source(path, dataset_name):
                        session_dataset = candidate
                        break
            if session_dataset is not None:
                scope = (
                    requested_scope
                    if len(data_objs) == 1 and getattr(session_dataset, "kind", "") == "time-lapse"
                    else TimepointScope.all()
                )
                jobs.append(self._job_for_dataset(
                    session_dataset, reconstructor, params,
                    origin=ProcessingOrigin.MANUAL, timepoint_scope=scope,
                ))
                continue

            source_kind = str(getattr(data_obj, "sourceKind", "image") or "image")
            if source_kind not in {"image", TIME_LAPSE_SOURCE_KIND}:
                continue
            # Batch jobs must be reopenable independently of the Current-data
            # object. Quick Load normally satisfies this; reject RAM-only
            # objects rather than letting the queue later mutate/unload them.
            if not (getattr(data_obj, "dataPath", None) or getattr(data_obj, "sourceOriginalPath", None)):
                self._logger.warning(
                    "Cannot queue %s: the source has no reopenable path",
                    getattr(data_obj, "name", "Loaded data"),
                )
                continue
            source_id = self._external_source_id(data_obj)
            self._externalSources[source_id] = data_obj
            scope = (
                requested_scope
                if len(data_objs) == 1 and source_kind == TIME_LAPSE_SOURCE_KIND
                else TimepointScope.all()
            )
            jobs.append(ProcessingJob(
                dataset_id=source_id,
                label=str(getattr(data_obj, "name", "") or getattr(data_obj, "datasetName", "") or "Data"),
                reconstructor_id=str(reconstructor.id),
                params=copy.deepcopy(params),
                origin=ProcessingOrigin.MANUAL,
                source="loaded",
                timepoint_scope=scope,
            ))
        added = self.queue.enqueue_many(jobs, position=position)
        if added:
            self._emit_changed()
            self._process_next()
        return added

    @staticmethod
    def _external_source_id(data_obj) -> str:
        path = str(
            getattr(data_obj, "sourceOriginalPath", "")
            or getattr(data_obj, "dataPath", "")
            or ""
        )
        dataset = str(getattr(data_obj, "datasetName", "") or "")
        kind = str(getattr(data_obj, "sourceKind", "image") or "image")
        index = getattr(data_obj, "sourceMetadata", None)
        anchor = getattr(index, "anchor", None)
        anchor_dataset = str(getattr(anchor, "dataset", "") or "")
        return f"loaded:{kind}:{path}:{dataset}:{anchor_dataset}"

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
        """Only a running Session-owned job blocks closing the Session."""
        current = self.queue.current
        return current is None or str(getattr(current, "source", "session")) != "session"

    def sessionClosed(self) -> None:
        """Discard Session-owned waiting work without disturbing Loaded jobs."""
        self.autoPolicy.enabled = False
        session_job_ids = [
            job.id for job in self.queue.waiting
            if str(getattr(job, "source", "session")) == "session"
        ]
        if session_job_ids:
            self.queue.remove_waiting(session_job_ids)
        self._emit_changed()
        self._process_next()

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

    def removeWaiting(self, job_ids) -> int:
        """Remove selected waiting jobs; the running job is never affected."""
        removed = self.queue.remove_waiting(job_ids or ())
        if not removed:
            return 0
        self._emit_changed()
        if not self.queue.paused:
            QtCore.QTimer.singleShot(0, self._process_next)
        return len(removed)

    def moveWaiting(self, job_ids, direction: int) -> bool:
        """Move selected waiting jobs one position earlier/later."""
        changed = self.queue.move_waiting(job_ids or (), direction=int(direction))
        if changed:
            self._emit_changed()
        return bool(changed)

    def removeAutoWaitingDatasets(self, dataset_ids) -> int:
        """Drop queued AUTO jobs for explicitly auto-excluded Session datasets.

        Manual jobs remain intentional requests and are deliberately untouched.
        A running AUTO job is also left alone; the user may use ``Skip current``
        if they want to stop that already-started work.
        """
        wanted = {str(dataset_id) for dataset_id in (dataset_ids or ())}
        job_ids = [
            job.id
            for job in self.queue.waiting
            if str(job.dataset_id) in wanted and job.origin == ProcessingOrigin.AUTO
        ]
        return self.removeWaiting(job_ids)

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
    def _job_for_dataset(
        dataset, reconstructor, params, *, origin, timepoint_scope=None
    ):
        return ProcessingJob(
            dataset_id=str(dataset.id),
            label=str(dataset.label),
            reconstructor_id=str(reconstructor.id),
            params=copy.deepcopy(params),
            origin=origin,
            source="session",
            timepoint_scope=coerce_timepoint_scope(timepoint_scope),
        )

    def _resolve_job_source(self, job):
        if str(getattr(job, "source", "session") or "session") == "loaded":
            return self._externalSources.get(str(job.dataset_id))
        session = getattr(self._sessionController, "session", None)
        return session.dataset_by_id(job.dataset_id) if session is not None else None

    @staticmethod
    def _is_external_source(source) -> bool:
        return isinstance(source, DataObj)

    # ------------------------------------------------------------------ execution

    @QtCore.Slot()
    def _process_next(self) -> None:
        reconstructor_manager = getattr(self, "_reconstructorManager", None)
        direct_running = getattr(reconstructor_manager, "isReconstructionRunning", None)
        if (
            self._closing
            or self.queue.paused
            or self.queue.current is not None
            or self._offlineThread is not None
            or self._live.is_running
            or (callable(direct_running) and direct_running())
        ):
            return

        waiting = self.queue.waiting
        if not waiting:
            return

        # Probe a streaming source while it is still WAITING. A newly-created
        # Session/Loaded recording may not be readable yet; do not churn the
        # visible queue through RUNNING -> WAITING on every retry.
        candidate = waiting[0]
        candidate_source = self._resolve_job_source(candidate)
        candidate_reconstructor = get_registry().get_reconstructor(
            candidate.reconstructor_id, raise_on_missing=False
        )
        prepared_live_job = None
        live_resolution = None
        if (
            candidate_source is not None
            and candidate_reconstructor is not None
            and self._uses_streaming_runner(
                candidate, candidate_source, candidate_reconstructor
            )
        ):
            job_path = self._streaming_job_path(candidate_source)
            if not job_path:
                return
            live_resolution = prepare_entry_job(job_path, candidate_reconstructor)
            if live_resolution.state == LiveJobState.PENDING:
                return
            prepared_live_job = live_resolution.prepared

        job = self.queue.take_next()
        if job is None:
            return
        self._skipRequested = False
        self._liveFinalResult = None
        self._currentProgress = {
            "fraction": 0.0,
            "status": "Starting…",
        }
        self._currentPlannedTimepoints = None
        self._emit_changed()
        self._emit_progress_changed()

        source = self._resolve_job_source(job)
        reconstructor = get_registry().get_reconstructor(
            job.reconstructor_id, raise_on_missing=False
        )
        if source is None:
            self._finish_current(error="Queued source no longer exists")
            return
        if reconstructor is None:
            self._finish_current(error=f"Reconstructor {job.reconstructor_id!r} is unavailable")
            return

        if self._uses_streaming_runner(job, source, reconstructor):
            if live_resolution is not None and live_resolution.state == LiveJobState.FAILED:
                reason = live_resolution.reason or "Streaming source access failed"
                self._logger.error("Failing queued dataset %s: %s", job.label, reason)
                self._finish_current(
                    error=reason,
                    display_error=self._display_error_for_source_failure(reason),
                )
                return
            if live_resolution is not None and live_resolution.state == LiveJobState.UNSUPPORTED:
                reason = live_resolution.reason or "Streaming source is unsupported"
                self._logger.warning("Skipping queued dataset %s: %s", job.label, reason)
                self._finish_current(error=reason)
                return
            if self._start_live(job, source, reconstructor, prepared=prepared_live_job):
                return
            self._finish_current(error="Streaming source is not compatible with this dataset")
            return

        self._start_offline(job, source, reconstructor)

    @staticmethod
    def _source_kind(source) -> str:
        if isinstance(source, DataObj):
            return (
                "time-lapse"
                if str(getattr(source, "sourceKind", "image") or "image")
                == TIME_LAPSE_SOURCE_KIND
                else "scan"
            )
        return str(getattr(source, "kind", "") or "")

    @staticmethod
    def _planned_timepoints(source):
        if isinstance(source, DataObj):
            index = getattr(source, "sourceMetadata", None)
            return getattr(index, "planned_count", None) if index is not None else None
        return getattr(source, "planned_timepoints", None)

    def _streaming_job_path(self, source) -> str | None:
        if isinstance(source, DataObj):
            return str(
                getattr(source, "sourceOriginalPath", "")
                or getattr(source, "dataPath", "")
                or ""
            ) or None
        path = str(getattr(source, "primary_path", "") or "")
        if not path:
            return None
        session = getattr(self._sessionController, "session", None)
        raw_root = getattr(session, "raw_root", Path(path).parent)
        return watcher_entry_for_source(raw_root, path)

    @staticmethod
    def _uses_streaming_runner(job, source, reconstructor) -> bool:
        """Keep every logical time-lapse as one processing unit when possible."""
        if source is None or reconstructor is None:
            return False
        if not getattr(reconstructor, "supports_streaming", False):
            return False
        return (
            job.origin == ProcessingOrigin.AUTO
            or ProcessingQueueController._source_kind(source) == "time-lapse"
        )

    def _start_live(self, job, source, reconstructor, *, prepared=None) -> bool:
        self._currentPlannedTimepoints = self._planned_timepoints(source)
        if not getattr(reconstructor, "supports_streaming", False):
            return False
        if prepared is None:
            job_path = self._streaming_job_path(source)
            if not job_path:
                return False
            resolution = prepare_entry_job(job_path, reconstructor)
            if resolution.state != LiveJobState.READY:
                return False
            prepared = resolution.prepared

        source_type = prepared.source_type
        self._logger.info(
            "Processing dataset %s: kind=%s origin=%s source=%s runner=streaming "
            "job=%s seed=%s raw=%s/%s timepoints=%s",
            job.label,
            self._source_kind(source),
            getattr(job.origin, "value", str(job.origin)),
            getattr(job, "source", "session"),
            prepared.job_path,
            prepared.seed_path,
            getattr(source_type, "format_id", "?"),
            getattr(source_type, "layout", "?"),
            self._planned_timepoints(source),
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

    def _start_offline(self, job, source, reconstructor) -> None:
        self._logger.info(
            "Processing dataset %s: kind=%s origin=%s runner=offline timepoints=%s",
            job.label,
            self._source_kind(source),
            getattr(job.origin, "value", str(job.origin)),
            self._planned_timepoints(source),
        )
        if reconstructor.id == "monalisa-legacy":
            self._finish_current(
                error="MoNaLISA Legacy is not yet supported by the shared processing queue"
            )
            return

        data_objs = self._data_objs_for_source(source)
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
        worker.progress.connect(self._on_offline_progress)
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

    def _data_objs_for_source(self, source) -> list[DataObj]:
        if isinstance(source, DataObj):
            if str(getattr(source, "sourceKind", "image") or "image") != "image":
                return []
            path = getattr(source, "dataPath", None)
            dataset_name = getattr(source, "datasetName", None)
            if path:
                if dataset_name in (None, ""):
                    try:
                        names = list(DataObj.getDatasetNames(str(path)))
                    except Exception:
                        return []
                    if len(names) != 1:
                        return []
                    dataset_name = names[0]
                # Use a fresh lazy DataObj so a queued batch cannot unload or
                # mutate the catalog/current object the operator is inspecting.
                return [DataObj(Path(str(path)).name, str(dataset_name), path=str(path))]
            # Shared queued processing is intentionally disk-backed so worker
            # cleanup can never unload/mutate the catalog or Current-data object.
            return []

        refs = list(getattr(source, "source_refs", ()) or ())
        if self._source_kind(source) != "time-lapse":
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
        if job is None or str(getattr(job, "source", "session")) != "session":
            return
        claimer = getattr(self._sessionController, "claimResultForDataset", None)
        if callable(claimer):
            claimer(result, job.dataset_id)

    @QtCore.Slot(object)
    def _on_offline_progress(self, progress) -> None:
        if self.queue.current is None:
            return
        message = str(progress.message or progress.phase.replace("_", " ").capitalize())
        # ReconstructionWorker may prefix internal source-unit position.  The
        # footer already shows the logical queue-batch Item x/y, so keep the
        # phase/status concise there.
        if message.startswith("Item ") and ": " in message:
            message = message.split(": ", 1)[1]
        self._currentProgress = {
            "fraction": max(0.0, min(1.0, float(progress.fraction))),
            "status": message,
        }
        self._emit_progress_changed()

    @QtCore.Slot(int)
    def _on_live_timepoint_progress(self, completed: int) -> None:
        if self.queue.current is None or not self._live.is_running:
            return
        total = int(self._currentPlannedTimepoints or 0)
        completed = max(0, int(completed))
        if total > 0:
            fraction = max(0.0, min(1.0, completed / total))
            status = f"Timepoint {min(completed, total)}/{total}"
        else:
            fraction = None
            status = f"Timepoint {completed}"
        self._currentProgress = {"fraction": fraction, "status": status}
        self._emit_progress_changed()

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

    @staticmethod
    def _display_error_for_source_failure(reason: str) -> str:
        """Return a short operator-facing message for source preflight errors."""
        text = str(reason or "")
        if (
            "Cannot open a new raw HDF5 reader" in text
            and "live in-process handle" in text
        ):
            return (
                "Reconstruction failed: a timepoint from this timelapse is already loaded. "
                "Unload it and try again."
            )
        return f"Reconstruction failed: {text}" if text else "Reconstruction failed."

    def _finish_current(
        self, *, error: str = "", display_error: str = "", skipped: bool = False
    ) -> None:
        job = self.queue.current
        if job is None:
            return
        if skipped:
            self.queue.skip_current()
            self._lastTerminalStatus = {}
        elif error:
            detail = str(error or "")
            self.queue.fail_current(detail)
            self._lastTerminalStatus = {
                "kind": "error",
                "label": str(getattr(job, "label", "") or "Data"),
                "status": str(display_error or f"Reconstruction failed: {detail}"),
                "detail": detail,
            }
        else:
            self.queue.complete_current()
            self._lastTerminalStatus = {}
        self._skipRequested = False
        self._liveFinalResult = None
        self._currentProgress = {}
        self._currentPlannedTimepoints = None
        self._emit_changed()
        self._emit_progress_changed()
        if not self._closing and not self.queue.paused:
            QtCore.QTimer.singleShot(0, self._process_next)

    def _prune_external_sources(self) -> None:
        active_ids = {
            str(job.dataset_id)
            for job in (
                ([self.queue.current] if self.queue.current is not None else [])
                + list(self.queue.waiting)
            )
            if str(getattr(job, "source", "session") or "session") == "loaded"
        }
        for source_id in tuple(self._externalSources):
            if source_id not in active_ids:
                self._externalSources.pop(source_id, None)

    def _emit_progress_changed(self) -> None:
        snapshot = self.queueSnapshot()
        self.sigProgressChanged.emit({
            "current": snapshot.get("current"),
            "current_progress": dict(snapshot.get("current_progress") or {}),
        })

    def _emit_changed(self) -> None:
        self._prune_external_sources()
        snapshot = self.queueSnapshot()
        snapshot["changed_dataset_ids"] = self.queue.consume_changed_dataset_ids()
        self.sigQueueChanged.emit(snapshot)

    def closeEvent(self):
        self._closing = True
        self._retryTimer.stop()
        self.autoPolicy.enabled = False
        self.queue.clear_waiting()
        self._externalSources.clear()
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
