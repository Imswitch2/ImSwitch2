"""Shared serial processing queue for Session and ad-hoc Loaded data.

Manual Session batches, Session auto-processing, and Quick Load batches feed the
same lazy queue. Sources remain lightweight until execution; Session ownership
and autosave semantics are retained only for Session-backed jobs.
"""

from __future__ import annotations

import copy
import time
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
    annotate_result_timepoint_scope,
    coerce_timepoint_scope,
)
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement,
    AutoProcessPolicy,
    ProcessingJob,
    ProcessingOrigin,
    ProcessingQueue,
)
from imswitch.improcess.reconstructors.registry import get_registry
from imswitch.improcess.execution.contracts import Incompatible
from imswitch.improcess.execution.queue_finite import (
    finite_incremental_eligible, make_finite_source,
)
from imswitch.improcess.execution.runner import QtIncrementalRunner
from imswitch.improcess.execution.policy import (
    ExecutionPath, choose_execution, streaming_capability, supports_streaming_settings,
)
from imswitch.improcess.execution.auto_timepoints import (
    AutoTimepointState, CompletedTimepointBatch, probe_completed_prefix,
)
from imswitch.improcess.execution.readiness import (
    Readiness, ReadinessState, probe_scope,
)
from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
from imswitch.improcess.execution.result_publication import (
    PublicationHooks, ResultPublication,
)


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
        self._processWhileAcquiring = False  # session-runtime only
        # Runtime-only ad-hoc sources queued from Quick Load. The queue model
        # keeps only a stable logical id; this registry keeps the lightweight
        # DataObj reference needed to materialize the source lazily at execution.
        self._externalSources: dict[str, DataObj] = {}

        self._offlineThread = None
        self._offlineWorker = None
        self._offlineDataObjs: list[DataObj] = []
        self._offlineResultScopes: list[TimepointScope | None] = []
        # Worker terminal signals arrive before QThread.finished. Keep the queue
        # slot occupied until the thread itself is gone so a following job can
        # never overwrite the only strong references to a still-running QThread.
        self._offlinePendingOutcome: tuple[str, str] | None = None
        self._offlineAtomicLapse = False
        self._offlinePendingResult = None
        self._offlineReconstructor = None
        self._offlineProgressive = False
        self._offlinePublisher = None
        self._offlinePublishedCount = 0
        # Readiness is throttled and finalized physical partitions are cached
        # per queued job. A pending acquisition never occupies the run slot.
        self._readinessDue: dict[str, float] = {}
        self._readinessCached: dict[str, Readiness] = {}
        self._readinessInspected: dict[str, dict] = {}
        # Resumable finite Auto: publisher and first-T calibration survive
        # between bounded worker batches without occupying the serial queue.
        self._autoTimepointStates: dict[str, AutoTimepointState] = {}
        self._autoTimepointDue: dict[str, float] = {}
        self._autoTimepointCached: dict[str, CompletedTimepointBatch] = {}
        # Early HDF5 acceptance has stricter post-close requirements than
        # whole-acquisition readiness: never share positive-evidence caches.
        self._autoTimepointInspected: dict[str, dict] = {}
        self._offlineAutoBatch = False
        self._offlineBatchIndices = ()
        # M1 executor remains available for genuine growing-source Follow
        # work. Finite manual/Auto jobs now use offline per-timepoint execution.
        self._incrementalRunner = None
        self._incrementalReconstructor = None
        self._incrementalResult = None
        self._incrementalError = ""
        self._incrementalMissingRanges = ()
        self._incrementalPublishedResult = None
        self._incrementalCompletedTimepoints = 0
        self._resultPublisher = None
        self._closing = False
        self._live = LiveReconstructionController(self._commChannel)
        self._live.sigFinalResult.connect(self._on_live_final_result)
        self._live.sigFinished.connect(self._on_live_finished)
        self._live.sigTimepointProgress.connect(self._on_live_timepoint_progress)
        self._liveFinalResult = None
        self._currentProgress = {}
        self._currentPlannedTimepoints = None
        self._currentSourceTimepoints = None
        self._lastTerminalStatus = {}
        self._skipRequested = False

        # Retry an auto job whose newly-created store is not readable yet.
        self._retryTimer = QtCore.QTimer(self)
        self._retryTimer.setInterval(500)
        self._retryTimer.timeout.connect(self._process_next)
        self._retryTimer.start()

        if self._sessionController is not None:
            arrival_signal = getattr(
                self._sessionController, "sigAutoAcquisitionsAdmitted", None
            )
            if arrival_signal is not None:
                arrival_signal.connect(self.enqueueAutoArrivals)

    # ------------------------------------------------------------------ public API

    def queueSnapshot(self):
        snapshot = self.queue.view_snapshot(waiting_limit=100)
        snapshot["auto_enabled"] = bool(self.autoPolicy.enabled)
        snapshot["process_while_acquiring"] = bool(self._processWhileAcquiring)
        snapshot["current_progress"] = dict(self._currentProgress or {})
        snapshot["last_terminal_status"] = dict(self._lastTerminalStatus or {})
        return snapshot

    def queueDatasetStates(self) -> dict[str, str]:
        return self.queue.dataset_states()

    def setAutoEnabled(self, enabled: bool) -> None:
        """Arm future arrivals; default to Live when first enabled and capable.

        The operator may subsequently turn Live off explicitly. Repeated
        availability/Auto synchronizations must not override that choice;
        toggling Auto off and on again restores the capability-based default.
        """
        was_enabled = bool(self.autoPolicy.enabled)
        self.autoPolicy.enabled = bool(enabled)
        if not enabled:
            self._processWhileAcquiring = False
        elif not was_enabled:
            reconstructor = getattr(self._main, '_activeReconstructor', None)
            capable = False
            if reconstructor is not None:
                try:
                    params = self._snapshot_params(reconstructor)
                    capable = streaming_capability(reconstructor, params).supported
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
            self._processWhileAcquiring = bool(capable)
        self._emit_changed()

    def setProcessWhileAcquiring(self, enabled: bool) -> None:
        """Control admission policy only, never mutate already submitted jobs."""
        capable = False
        if enabled and self.autoPolicy.enabled:
            reconstructor = getattr(self._main, "_activeReconstructor", None)
            if reconstructor is not None:
                params = self._snapshot_params(reconstructor)
                capable = streaming_capability(reconstructor, params).supported
        self._processWhileAcquiring = bool(capable)
        self._emit_changed()

    def captureAutoRecipe(self) -> dict | None:
        """Freeze a new arrival's reconstructor and parameters at first sight."""
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        if (reconstructor is None or not self.autoPolicy.enabled
                or str(getattr(reconstructor, "result_role", "derived")) == "inspection"):
            return None
        params = self._snapshot_params(reconstructor)
        capable = streaming_capability(reconstructor, params).supported
        return {
            "reconstructor_id": str(reconstructor.id),
            "params": params,
            "execution_requirement": (
                ExecutionRequirement.FOLLOW.value if self._processWhileAcquiring and capable
                else ExecutionRequirement.FINITE.value
            ),
        }

    def enqueueAutoArrivals(self, dataset_ids, recipe: dict) -> int:
        """Submit only entries explicitly admitted by the Session observer.

        Arrival tokens are granted when Watching+Auto were enabled, and may
        resolve later even after Auto is switched off. Never consult the
        current UI params or the current Auto toggle when consuming a token.
        """
        session = getattr(self._sessionController, "session", None)
        if session is None or not isinstance(recipe, dict):
            return 0
        reconstructor_id = str(recipe.get("reconstructor_id", ""))
        reconstructor = get_registry().get_reconstructor(
            reconstructor_id, raise_on_missing=False,
        )
        if (reconstructor is None or
                str(getattr(reconstructor, "result_role", "derived")) == "inspection"):
            return 0
        jobs = []
        for dataset_id in dict.fromkeys(str(item) for item in dataset_ids):
            dataset = session.dataset_by_id(dataset_id)
            if (dataset is None or getattr(dataset, "raw_deleted", False)
                    or self.queue.contains_dataset(dataset_id)
                    or self._sessionController.isDatasetProcessed(dataset_id)):
                continue
            job = self._job_for_dataset(
                dataset, reconstructor, recipe.get("params", {}),
                origin=ProcessingOrigin.AUTO,
                timepoint_scope=TimepointScope.all(),
            )
            job.execution_requirement = ExecutionRequirement(
                recipe.get("execution_requirement", ExecutionRequirement.FINITE.value)
            )
            jobs.append(job)
        added = self.queue.enqueue_many(jobs, position="end")
        if added:
            self._emit_changed()
            self._process_next()
        return added

    def enqueueAutoEligible(self, dataset_ids=None) -> int:
        """Retired historical-backfill entry point (M3B)."""
        return 0

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
                timepoint_total=(
                    int(getattr(getattr(data_obj, "sourceMetadata", None), "planned_count", 0) or 0)
                    if source_kind == TIME_LAPSE_SOURCE_KIND else None
                ),
                execution_requirement=ExecutionRequirement.FINITE,
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
        if self._incrementalRunner is not None:
            self._incrementalRunner.pause(True)
        self._emit_changed()

    def resume(self) -> None:
        self.queue.resume()
        if self._live.is_running and self._live.is_paused:
            self._live.resume()
        if self._incrementalRunner is not None:
            self._incrementalRunner.pause(False)
        self._emit_changed()
        self._process_next()

    def canCloseSession(self) -> bool:
        """Only a running Session-owned job blocks closing the Session."""
        current = self.queue.current
        return current is None or str(getattr(current, "source", "session")) != "session"

    def sessionClosed(self) -> None:
        """Discard Session-owned waiting work without disturbing Loaded jobs."""
        self.autoPolicy.enabled = False
        self._processWhileAcquiring = False
        session_job_ids = [
            job.id for job in self.queue.waiting
            if str(getattr(job, "source", "session")) == "session"
        ]
        if session_job_ids:
            self.queue.remove_waiting(session_job_ids)
            for job_id in session_job_ids:
                self._cancel_waiting_auto_timepoints(job_id)
                self._clear_readiness(job_id)
        self._emit_changed()
        self._process_next()

    def skipCurrent(self) -> None:
        """Skip the current job at the safest boundary the active runner offers."""
        if self.queue.current is None:
            return
        self._skipRequested = True
        if self._incrementalRunner is not None:
            self._incrementalRunner.cancel()
            return
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
        for job in removed:
            self._cancel_waiting_auto_timepoints(job.id)
            self._clear_readiness(job.id)
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
        # Legacy signal is catalog reconciliation, NOT an Auto arrival.
        return

    def _snapshot_params(self, reconstructor) -> dict:
        getter = getattr(self._reconstructorManager, "snapshotProcessingParams", None)
        if callable(getter):
            params = copy.deepcopy(getter(reconstructor))
            # UI Auto localization is a transient value for the *currently*
            # viewed data, not a reusable calibration for arbitrary queue jobs.
            # Each independent acquisition must resolve its own Auto value.
            from imswitch.improcess.execution.localization_scope import (
                fresh_acquisition_params,
            )
            return fresh_acquisition_params(reconstructor, params)
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
            timepoint_total=(
                int(getattr(dataset, "planned_timepoints", 0) or 0)
                if getattr(dataset, "kind", "") == "time-lapse" else None
            ),
            execution_requirement=(
                ExecutionRequirement.FINITE
                if origin != ProcessingOrigin.AUTO
                else ExecutionRequirement.POLICY
            ),
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

    def _clear_readiness(self, job_id) -> None:
        job_id = str(job_id)
        self._readinessDue.pop(job_id, None)
        self._readinessCached.pop(job_id, None)
        self._readinessInspected.pop(job_id, None)
        self._autoTimepointDue.pop(job_id, None)
        self._autoTimepointCached.pop(job_id, None)
        self._autoTimepointInspected.pop(job_id, None)

    def _cancel_waiting_auto_timepoints(self, job_id) -> None:
        state = self._autoTimepointStates.pop(str(job_id), None)
        if state is not None:
            # Finished scientific timepoints remain visible as a partial result.
            try:
                state.publisher.finish("cancelled")
            except Exception:
                self._logger.exception("Could not finalize removed progressive Auto job")

    def _probe_auto_timepoints(self, job, source) -> CompletedTimepointBatch:
        job_id = str(job.id)
        now = time.monotonic()
        if now < self._autoTimepointDue.get(job_id, 0.0):
            return self._autoTimepointCached.get(
                job_id, CompletedTimepointBatch(reason="Waiting for next timepoint"))
        self._autoTimepointDue[job_id] = now + 2.0
        state = self._autoTimepointStates.get(job_id)
        try:
            start, _ = job.timepoint_scope.bounds(job.timepoint_total)
            batch = probe_completed_prefix(
                source, job, next_t=(state.next_t if state else start),
                inspected=self._autoTimepointInspected.setdefault(job_id, {}),
            )
        except (TypeError, ValueError, IndexError) as exc:
            batch = CompletedTimepointBatch(reason=f"Timepoint readiness unavailable: {exc}")
        self._autoTimepointCached[job_id] = batch
        return batch

    def _probe_auto_readiness(self, job, source) -> Readiness:
        """Throttle metadata-only probes; never use file stability as a signal."""
        job_id = str(job.id)
        now = time.monotonic()
        if now < self._readinessDue.get(job_id, 0.):
            return self._readinessCached.get(job_id, Readiness(
                ReadinessState.UNKNOWN, "Waiting for source metadata"))
        self._readinessDue[job_id] = now + 2.0
        try:
            status = probe_scope(
                source, job, is_lapse=self._source_kind(source) == "time-lapse",
                inspected=self._readinessInspected.setdefault(job_id, {}),
            )
        except (ValueError, TypeError, IndexError) as exc:
            status = Readiness(ReadinessState.UNKNOWN, f"Readiness unavailable: {exc}")
        self._readinessCached[job_id] = status
        from imswitch.improcess.execution.readiness import references_for_scope

        is_lapse = self._source_kind(source) == "time-lapse"
        refs, bounds = references_for_scope(
            source,
            job.timepoint_scope,
            job.timepoint_total,
            is_lapse=is_lapse,
        )

        print(
            "\n[AUTO READINESS]",
            "\n  dataset:", job.label,
            "\n  kind:", self._source_kind(source),
            "\n  planned T:", job.timepoint_total,
            "\n  references:", [(r.logical_t, r.dataset) for r in refs],
            "\n  state:", status.state,
            "\n  reason:", status.reason,
            "\n  completed:", (
                status.progress.completed_timepoints
                if status.progress else None
            ),
            flush=True,
        )
        return status

    @QtCore.Slot()
    def _process_next(self) -> None:
        reconstructor_manager = getattr(self, "_reconstructorManager", None)
        direct_running = getattr(reconstructor_manager, "isReconstructionRunning", None)
        if (
            self._closing
            or self.queue.paused
            or self.queue.current is not None
            or self._offlineThread is not None
            or self._incrementalRunner is not None
            or self._live.is_running
            or (callable(direct_running) and direct_running())
        ):
            return

        waiting = self.queue.waiting
        if not waiting:
            return

        # Search in submission order, skipping acquisitions that still lack
        # trustworthy finalization. Pending Auto jobs do not monopolize the
        # execution slot and are never treated as finished by a quiet period.
        selected = None
        selected_source = None
        selected_reconstructor = None
        selected_choice = None
        prepared_live_job = None
        selected_failure = ""
        selected_auto_batch = ()
        selected_auto_refs = ()
        for position, candidate in enumerate(waiting):
            source = self._resolve_job_source(candidate)
            reconstructor = get_registry().get_reconstructor(
                candidate.reconstructor_id, raise_on_missing=False,
            )
            if source is None or reconstructor is None:
                selected = position  # run and report the missing dependency
                break

            growing = False
            auto_batch = ()
            if candidate.origin == ProcessingOrigin.AUTO:
                # Finite Auto can process individually completed timepoints
                # without needing a StreamingSession or an active raw reader.
                # FOLLOW still prefers frame streaming; if its reader is
                # unsafe, sealed timepoints may run through finite process().
                can_run_early = (
                    self._source_kind(source) == "time-lapse"
                    and bool(getattr(reconstructor, "supports_progressive_finite", False))
                    and candidate.timepoint_total is not None
                )
                early_started = str(candidate.id) in self._autoTimepointStates
                if can_run_early and (candidate.execution_requirement != ExecutionRequirement.FOLLOW
                                      or early_started):
                    prefix = self._probe_auto_timepoints(candidate, source)
                    if prefix.indices:
                        auto_batch = prefix.indices
                        choice = choose_execution(candidate, "time-lapse", reconstructor)
                        selected = position
                        selected_choice = choice
                        selected_source = source
                        selected_reconstructor = reconstructor
                        selected_auto_batch = auto_batch
                        selected_auto_refs = prefix.references
                        selected_failure = ""
                        break
                    # If this is a shared HDF5 file, retain the existing
                    # whole-acquisition readiness path. A resumed job must
                    # never fall back to reprocessing its completed prefix.
                    if early_started:
                        status = prefix.reason or "Waiting for next completed timepoint"
                        if candidate.readiness_status != status:
                            candidate.readiness_status = status
                            self._emit_changed()
                        continue
                readiness = self._probe_auto_readiness(candidate, source)
                if readiness.state != ReadinessState.READY:
                    growing = readiness.state == ReadinessState.ACQUIRING
                    choice = choose_execution(
                        candidate, self._source_kind(source), reconstructor,
                        growing=growing, live_reader=readiness.live_readable,
                    )
                    if (choice.path != ExecutionPath.INCREMENTAL_FOLLOW
                            and can_run_early
                            and candidate.execution_requirement == ExecutionRequirement.FOLLOW):
                        # The requested frame-following engine is unsafe for
                        # this source (notably active HDF5). A sealed physical
                        # timepoint can still run through finite process().
                        prefix = self._probe_auto_timepoints(candidate, source)
                        if prefix.indices:
                            selected = position
                            selected_choice = choose_execution(
                                candidate, "time-lapse", reconstructor)
                            selected_source = source
                            selected_reconstructor = reconstructor
                            selected_auto_batch = prefix.indices
                            selected_auto_refs = prefix.references
                            selected_failure = ""
                            break
                    if choice.path != ExecutionPath.INCREMENTAL_FOLLOW:
                        status = (choice.reason if choice.reason and growing
                                  else readiness.reason or choice.reason or "Waiting for acquisition")
                        if candidate.readiness_status != status:
                            candidate.readiness_status = status
                            self._emit_changed()
                        continue
                else:
                    selected_auto_refs = readiness.verified_references
                    choice = choose_execution(
                        candidate, self._source_kind(source), reconstructor,
                    )
            else:
                choice = choose_execution(
                    candidate, self._source_kind(source), reconstructor,
                )

            if choice.path in (ExecutionPath.INCREMENTAL_FINITE,
                               ExecutionPath.INCREMENTAL_FOLLOW):
                path = self._streaming_job_path(source)
                if not path:
                    if candidate.origin == ProcessingOrigin.AUTO:
                        status = "Waiting for completion — streaming source path unavailable"
                        if candidate.readiness_status != status:
                            candidate.readiness_status = status
                            self._emit_changed()
                        continue
                    choice = choose_execution(candidate, "scan", reconstructor)
                else:
                    resolution = prepare_entry_job(path, reconstructor)
                    if resolution.state == LiveJobState.PENDING:
                        if candidate.origin == ProcessingOrigin.AUTO:
                            status = ("Waiting for committed frames — "
                                      + str(resolution.reason or "stream not initialized"))
                            if candidate.readiness_status != status:
                                candidate.readiness_status = status
                                self._emit_changed()
                            continue
                        # Explicit finite/manual jobs can still use the
                        # conventional file reader where compatible.
                        choice = choose_execution(candidate, "scan", reconstructor)
                    elif resolution.state == LiveJobState.READY:
                        prepared_live_job = resolution.prepared
                    elif resolution.state == LiveJobState.UNSUPPORTED:
                        if candidate.origin == ProcessingOrigin.AUTO:
                            status = ("Waiting for completion — streaming source unsupported: "
                                      + str(resolution.reason or "no compatible reader"))
                            if candidate.readiness_status != status:
                                candidate.readiness_status = status
                                self._emit_changed()
                            continue
                        choice = choose_execution(candidate, "scan", reconstructor)
                    elif resolution.state == LiveJobState.FAILED:
                        # Preserve explicit HDF5 conflict diagnostics.
                        selected = position
                        selected_choice = choice
                        selected_source = source
                        selected_reconstructor = reconstructor
                        selected_failure = resolution.reason
                        break
            selected = position
            selected_choice = choice
            selected_source = source
            selected_reconstructor = reconstructor
            selected_failure = ""
            break

        if selected is None:
            return
        job = self.queue.take_ready(selected)
        if job is None:
            return
        self._clear_readiness(job.id)
        self._skipRequested = False
        self._liveFinalResult = None
        self._currentProgress = {
            "fraction": 0.0,
            "status": "Starting…",
        }
        self._currentPlannedTimepoints = None
        self._currentSourceTimepoints = None
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

        if selected_failure:
            self._finish_current(
                error=selected_failure,
                display_error=self._display_error_for_source_failure(selected_failure),
            )
            return
        # A finalized shared HDF5 lapse must use the ordinary finite
        # reader with the exact references verified by readiness.
        shared_hdf5_complete = (
            job.origin == ProcessingOrigin.AUTO
            and len(selected_auto_refs) > 1
            and len({Path(ref.path) for ref in selected_auto_refs}) == 1
            and Path(selected_auto_refs[0].path).suffix.lower()
                in ('.h5', '.hdf5')
        )
        if (not shared_hdf5_complete and selected_choice is not None
                and selected_choice.path in (
                    ExecutionPath.INCREMENTAL_FINITE,
                    ExecutionPath.INCREMENTAL_FOLLOW,
                )):
            if self._start_finite_incremental(
                job, source, reconstructor, prepared=prepared_live_job,
            ):
                return
            # An unsupported physical layout can use the conventional finite
            # reader; do not silently switch a growing follow job to offline.
            if selected_choice.path == ExecutionPath.INCREMENTAL_FOLLOW:
                self._finish_current(error="Growing acquisition has no compatible reader")
                return
        self._start_offline(
            job, source, reconstructor,
            ready_timepoints=selected_auto_batch or None,
            ready_references=selected_auto_refs or None,
        )

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

    @staticmethod
    def _timepoint_origin(source) -> int:
        if isinstance(source, DataObj):
            index = getattr(source, "sourceMetadata", None)
            if index is not None:
                try:
                    return max(0, int(index.anchor.ordinal) - int(index.anchor_index))
                except Exception:
                    return 0
            return 0
        return max(0, int(getattr(source, "timepoint_origin", 0) or 0))

    def _streaming_job_path(self, source) -> str | None:
        if isinstance(source, DataObj):
            if self._source_kind(source) == "time-lapse":
                index = getattr(source, "sourceMetadata", None)
                if index is not None:
                    try:
                        channel = index.channel(index.anchor_detector)
                        target = next(
                            (item for item in channel.candidates
                             if int(item.ordinal) - int(index.anchor.ordinal) + int(index.anchor_index) == 0),
                            None,
                        )
                        if target is not None:
                            return str(target.path)
                    except Exception:
                        pass
            return str(
                getattr(source, "dataPath", "")
                or getattr(source, "sourceOriginalPath", "")
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
        """Compatibility helper; origin and lapse shape alone never mean live."""
        return bool(
            source is not None and reconstructor is not None
            and ExecutionRequirement(getattr(job, "execution_requirement", "policy"))
                == ExecutionRequirement.FOLLOW
            and supports_streaming_settings(reconstructor, job.params)
        )

    @staticmethod
    def _uses_finite_incremental(job, source, reconstructor) -> bool:
        return finite_incremental_eligible(
            job, source, reconstructor,
            source_kind=ProcessingQueueController._source_kind(source),
        )

    def _start_finite_incremental(self, job, source, reconstructor, *, prepared) -> bool:
        """Run finite acquisition incrementally, publishing snapshots in place.

        A preview is a real, retained viewer result. The Session finalization
        event is separate and emitted after the worker threads have stopped,
        including after Skip when timepoints were already published.
        """
        if prepared is None:
            return False
        scope = coerce_timepoint_scope(job.timepoint_scope)
        total = self._planned_timepoints(source)
        try:
            # Strictly no detector-default guessing for multi-detector files.
            disk_source, detector = make_finite_source(
                prepared, source, scope, total,
                origin=self._timepoint_origin(source),
            )
        except Incompatible as exc:
            # Never fall back to a reader that silently chooses the first
            # detector of an ambiguous recording.
            self._finish_current(error=str(exc))
            return True
        except (ValueError, TypeError, NotImplementedError) as exc:
            self._logger.warning(
                "Finite incremental source unavailable for %s: %s; using legacy path",
                job.label, exc,
            )
            return False
        except Exception as exc:
            self._logger.exception("Finite source inspection failed")
            self._finish_current(error=f"Finite source inspection failed: {exc}")
            return True
        count = scope.count(total)
        self._currentSourceTimepoints = total
        self._currentPlannedTimepoints = count or total
        self._incrementalResult = None
        self._incrementalError = ""
        self._incrementalMissingRanges = ()
        self._incrementalPublishedResult = None
        self._incrementalCompletedTimepoints = 0
        self._resultPublisher = self._new_result_publisher(
            job, scope=scope, total=total, relation="timepoint",
        )
        budget = self._reconstructorManager._default_memory_budget_bytes()
        runner = QtIncrementalRunner(
            reconstructor, disk_source, copy.deepcopy(job.params),
            name=job.label, path=prepared.seed_path,
            dataset_name="time-lapse",
            max_buffer_bytes=max(1, min(256 * 1024 * 1024, int(budget) // 3)),
            enable_preview=True,
        )
        self._incrementalRunner = runner
        self._incrementalReconstructor = reconstructor
        runner.sigPreview.connect(self._on_incremental_preview)
        runner.sigTimepoint.connect(self._on_incremental_timepoint)
        runner.sigCoverage.connect(self._on_incremental_coverage)
        runner.sigResult.connect(self._on_incremental_result)
        runner.sigFailed.connect(self._on_incremental_failed)
        runner.sigFinished.connect(self._on_incremental_finished)
        # The existing live viewer keeps one entry per job. Session ownership
        # is claimed on the first snapshot; finalization/autosave is deferred
        # until successful completion or cancellation of published work.
        try:
            runner.start()
        except Exception as exc:
            self._incrementalRunner = None
            self._incrementalReconstructor = None
            self._resultPublisher = None
            self._logger.exception("Incremental executor startup failed")
            self._finish_current(error=f"Incremental executor startup failed: {exc}")
        return True

    def _new_result_publisher(self, job, *, scope=None, total=None, relation="single"):
        """One job-scoped semantic publisher, reused by both worker families.

        Qt signals, result ownership and saving remain owned by Session and the
        viewer. Scientific result types are not rewritten in this milestone.
        """
        if relation == "timepoint":
            plan = ResultAssemblyPlan.for_timepoints(
                str(job.id), str(job.dataset_id), scope=scope, total=total,
            )
        else:
            plan = ResultAssemblyPlan(
                str(job.id), str(job.dataset_id), relation="single",
                scope=coerce_timepoint_scope(scope), total_timepoints=total,
            )
        channel = self._commChannel
        return ResultPublication(plan, PublicationHooks(
            claim=self._claim_current_result,
            annotate_scope=lambda result, selected, source_total: (
                annotate_result_timepoint_scope(result, selected, total=source_total)
                if relation == "timepoint" or scope is not None else None
            ),
            preview=channel.sigLiveResultUpdated.emit,
            plane=channel.sigLiveTimepointUpdated.emit,
            provisional_plane=channel.sigLiveTimepointProvisional.emit,
            discard_provisional=channel.sigDiscardLiveProvisional.emit,
            timepoint_done=channel.sigLiveTimepointDone.emit,
            finalized=lambda result, name: channel.sigResultFinalized.emit(result, name),
            current_changed=channel.sigCurrentResultChanged.emit,
            offline_complete=lambda result, name, reconstructor: (
                self._reconstructorManager.publishQueuedResult(
                    result, name, reconstructor=reconstructor,
                )
            ),
        ))

    @QtCore.Slot(object, object)
    def _on_incremental_preview(self, result, plane) -> None:
        """Hand off an owned snapshot or plane to the common publisher."""
        if self._incrementalRunner is None or self.queue.current is None:
            return
        publisher = self._resultPublisher
        if publisher is None:
            return
        if result is not None:
            if ((getattr(result, "metadata", None) or {}).get("monalisa_streaming")
                    or {}).get("provisional_preview"):
                publisher.provisional_snapshot(result)
                return
            publisher.snapshot(result)
            self._incrementalPublishedResult = publisher.published
        elif plane is not None:
            if isinstance(plane, tuple) and len(plane) == 3 and plane[0] == "provisional":
                _, index, pixels = plane
                publisher.provisional_plane(index, pixels)
            else:
                index, pixels = plane
                publisher.plane(index, pixels)

    @QtCore.Slot(int)
    def _on_incremental_timepoint(self, completed: int) -> None:
        if self._incrementalRunner is None or self.queue.current is None:
            return
        total = int(self._currentPlannedTimepoints or 0)
        completed = max(0, int(completed))
        self._incrementalCompletedTimepoints = max(
            self._incrementalCompletedTimepoints, completed,
        )
        if self._resultPublisher is not None:
            self._resultPublisher.timepoint_completed(self._incrementalCompletedTimepoints)
        self._currentProgress = {
            "fraction": min(1., completed / total) if total else None,
            "status": f"Timepoint {completed}/{total}" if total else f"Timepoint {completed}",
        }
        self._emit_progress_changed()

    @QtCore.Slot(object)
    def _on_incremental_coverage(self, missing_ranges) -> None:
        self._incrementalMissingRanges = tuple(missing_ranges or ())
        if self._resultPublisher is not None:
            self._resultPublisher.missing_frames(missing_ranges)

    @QtCore.Slot(object)
    def _on_incremental_result(self, result) -> None:
        self._incrementalResult = result

    @QtCore.Slot(str)
    def _on_incremental_failed(self, message: str) -> None:
        self._incrementalError = str(message or "Incremental processing failed")
        self._logger.error("Incremental reconstruction failed: %s", self._incrementalError)

    @QtCore.Slot(bool)
    def _on_incremental_finished(self, success: bool) -> None:
        """Stop after both threads finish, retaining published work on Cancel."""
        if self._incrementalRunner is None:
            return
        self._incrementalRunner = None
        result = self._incrementalResult
        error = self._incrementalError
        publisher = self._resultPublisher
        self._resultPublisher = None
        self._incrementalReconstructor = None
        self._incrementalResult = None
        self._incrementalError = ""
        self._incrementalPublishedResult = None
        if self._skipRequested:
            if publisher is not None:
                publisher.finish("cancelled")
            self._finish_current(skipped=True)
            return
        if not success or result is None:
            if publisher is not None:
                publisher.finish("failed")
            self._finish_current(error=error or "Incremental processing stopped without a result")
            return
        try:
            if publisher is None:
                raise RuntimeError("Incremental result publisher is unavailable")
            publisher.finish("complete", result)
        except Exception as exc:
            self._logger.exception("Incremental result publication failed")
            self._finish_current(error=f"Result publication failed: {exc}")
            return
        self._finish_current()

    def _start_live(self, job, source, reconstructor, *, prepared=None) -> bool:
        total_timepoints = self._planned_timepoints(source)
        scope = coerce_timepoint_scope(getattr(job, "timepoint_scope", None))
        try:
            scoped_count = scope.count(total_timepoints)
        except (IndexError, TypeError, ValueError) as exc:
            self._finish_current(error=str(exc))
            return True
        self._currentSourceTimepoints = total_timepoints
        self._currentPlannedTimepoints = scoped_count or total_timepoints
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
            "job=%s seed=%s raw=%s/%s timepoints=%s scope=%s",
            job.label,
            self._source_kind(source),
            getattr(job.origin, "value", str(job.origin)),
            getattr(job, "source", "session"),
            prepared.job_path,
            prepared.seed_path,
            getattr(source_type, "format_id", "?"),
            getattr(source_type, "layout", "?"),
            total_timepoints,
            scope.label(total_timepoints),
        )
        try:
            started = start_prepared_live_job(
                self._live,
                prepared,
                reconstructor,
                dict(job.params),
                name=job.label,
                timepoint_scope=scope,
                total_timepoints=total_timepoints,
                timepoint_origin=self._timepoint_origin(source),
            )
        except Exception as exc:
            self._logger.exception("Could not start queued live reconstruction")
            self._finish_current(error=str(exc))
            return True
        if not started:
            self._finish_current(error="Live reconstruction did not start")
        return True

    def _start_offline(self, job, source, reconstructor, *,
                       ready_timepoints=None, ready_references=None) -> None:
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

        self._currentSourceTimepoints = self._planned_timepoints(source)
        scope = coerce_timepoint_scope(getattr(job, "timepoint_scope", None))
        scoped_count = scope.count(self._currentSourceTimepoints)
        self._currentPlannedTimepoints = scoped_count or self._currentSourceTimepoints
        auto_batch = ready_timepoints is not None
        allowed_t = set(ready_timepoints) if auto_batch else None
        data_objs = self._data_objs_for_source(
            source, timepoint_scope=scope, allowed_timepoints=allowed_t,
            verified_references=ready_references,
        )
        if not data_objs:
            self._finish_current(error="No readable source units found")
            return

        # A finite lapse is one logical result, including when individual
        # completed timepoints are published progressively. Never renumber
        # missing logical positions or infer the time axis from image shape.
        atomic_lapse = self._source_kind(source) == "time-lapse"
        if atomic_lapse:
            try:
                start_t, stop_t = scope.bounds(self._currentSourceTimepoints)
            except (IndexError, TypeError, ValueError) as exc:
                self._release_data_objs(data_objs)
                self._finish_current(error=f"Invalid timelapse scope: {exc}")
                return
            observed = [getattr(obj, "_improcess_timepoint_scope", None)
                        for obj in data_objs]
            indices = [int(tp.first) for tp in observed if tp is not None and tp.bounded]
            if len(indices) != len(data_objs) or len(indices) != len(set(indices)):
                self._release_data_objs(data_objs)
                self._finish_current(error="Timelapse has ambiguous or duplicate timepoint identity")
                return
            required_indices = (allowed_t if auto_batch else
                                set(range(start_t, stop_t)) if stop_t is not None else set())
            if stop_t is not None and set(indices) != required_indices:
                missing = sorted(set(range(start_t, stop_t)) - set(indices))
                self._release_data_objs(data_objs)
                self._finish_current(error=(
                    f"Selected timelapse has missing T{missing[0] + 1}; "
                    "select a completed range for finite processing"
                    if missing else "Timelapse timepoint selection does not match its scope"
                ))
                return
            data_objs = [obj for _, obj in sorted(zip(indices, data_objs))]
            if (len(data_objs) > 1
                    and not getattr(reconstructor, "supports_consolidation", False)
                    and not getattr(reconstructor, "supports_progressive_finite", False)):
                self._release_data_objs(data_objs)
                self._finish_current(error=(
                    f"{getattr(reconstructor, 'name', reconstructor.id)} cannot combine "
                    "multiple timepoints into one logical result"
                ))
                return

        budget = self._reconstructorManager._default_memory_budget_bytes()
        worker_jobs = []
        for data_obj in data_objs:
            params = copy.deepcopy(job.params)
            state = self._autoTimepointStates.get(str(job.id)) if auto_batch else None
            if state is not None:
                params.update(copy.deepcopy(state.reuse_overrides))
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

        # Progressive finite processing is a separate capability from
        # frame-streaming. Other plugins may opt in once their per-item result
        # and axis semantics have been validated.
        progressive = bool(auto_batch or (
            atomic_lapse and len(worker_jobs) > 1 and scoped_count is not None
            and getattr(reconstructor, "supports_progressive_finite", False)
        ))
        if auto_batch and str(job.id) not in self._autoTimepointStates:
            if not progressive or not atomic_lapse:
                self._release_data_objs(data_objs)
                self._finish_current(error="Invalid progressive Auto job")
                return
            _, stop_t = scope.bounds(self._currentSourceTimepoints)
            self._autoTimepointStates[str(job.id)] = AutoTimepointState(
                next_t=min(allowed_t), stop_t=stop_t,
                publisher=self._new_result_publisher(
                    job, scope=scope, total=self._currentSourceTimepoints,
                    relation="timepoint",
                ),
            )
        thread = QtCore.QThread()
        self._offlinePendingOutcome = None
        self._offlineAtomicLapse = atomic_lapse
        self._offlinePendingResult = None
        self._offlineReconstructor = reconstructor
        self._offlineProgressive = progressive
        self._offlineAutoBatch = auto_batch
        self._offlineBatchIndices = tuple(sorted(allowed_t)) if auto_batch else ()
        state = self._autoTimepointStates.get(str(job.id)) if auto_batch else None
        self._offlinePublishedCount = (
            state.next_t - scope.bounds(self._currentSourceTimepoints)[0]
            if state is not None else 0
        )
        self._offlinePublisher = (
            state.publisher if state is not None else
            self._new_result_publisher(
                job, scope=scope, total=self._currentSourceTimepoints,
                relation="timepoint",
            ) if progressive else None
        )
        worker = ReconstructionWorker(
            reconstructor,
            worker_jobs,
            consolidate=atomic_lapse and not progressive and len(worker_jobs) > 1,
            retain_results=atomic_lapse and not progressive,
            release_after_each=True,
            reuse_timepoint_calibration=atomic_lapse,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_offline_progress)
        # Bound @Slot: the publication handler must run on the GUI thread,
        # never in ReconstructionWorker's Qt thread.
        worker.resultReady.connect(self._on_offline_result_ready)
        worker.finished.connect(self._on_offline_finished)
        worker.failed.connect(self._on_offline_failed)
        worker.cancelled.connect(self._on_offline_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(lambda: self._offline_thread_finished(thread))
        thread.finished.connect(thread.deleteLater)

        self._offlineDataObjs = data_objs
        self._offlineResultScopes = [
            getattr(data_obj, "_improcess_timepoint_scope", None) for data_obj in data_objs
        ]
        self._offlineThread = thread
        self._offlineWorker = worker
        thread.start()

    def _data_objs_for_source(self, source, *, timepoint_scope=None,
                              allowed_timepoints=None, verified_references=None) -> list[DataObj]:
        scope = coerce_timepoint_scope(timepoint_scope)
        total = self._planned_timepoints(source)
        try:
            start, stop = scope.bounds(total)
        except (IndexError, TypeError, ValueError):
            return []

        if verified_references is not None:
            # Read exactly the same sealed partitions that readiness checked.
            # They may not yet be present in the passive Session catalog (for
            # example if Monitor was switched off after Auto admission).
            out = []
            for ref in verified_references:
                if ((allowed_timepoints is not None
                     and ref.logical_t not in allowed_timepoints)
                        or ref.logical_t < start
                        or (stop is not None and ref.logical_t >= stop)):
                    continue
                dataset_name = ref.dataset
                if not dataset_name:
                    try:
                        names = list(DataObj.getDatasetNames(str(ref.path)))
                    except Exception:
                        continue
                    if len(names) != 1:
                        continue
                    dataset_name = names[0]
                obj = DataObj(Path(ref.path).name, str(dataset_name), path=str(ref.path))
                obj._improcess_timepoint_scope = TimepointScope.single(ref.logical_t)
                out.append(obj)
            return out

        if isinstance(source, DataObj):
            source_kind = str(getattr(source, "sourceKind", "image") or "image")
            if source_kind == TIME_LAPSE_SOURCE_KIND:
                index = getattr(source, "sourceMetadata", None)
                if index is None:
                    return []
                try:
                    channel = index.channel(index.anchor_detector)
                except Exception:
                    return []
                out = []
                for item in channel.candidates:
                    logical_index = (
                        int(item.ordinal) - int(index.anchor.ordinal) + int(index.anchor_index)
                    )
                    if (logical_index < start or (stop is not None and logical_index >= stop)
                            or (allowed_timepoints is not None
                                and logical_index not in allowed_timepoints)):
                        continue
                    dataset_name = item.dataset
                    if dataset_name in (None, ""):
                        try:
                            names = list(DataObj.getDatasetNames(str(item.path)))
                        except Exception:
                            continue
                        if len(names) != 1:
                            continue
                        dataset_name = names[0]
                    data_obj = DataObj(
                        Path(str(item.path)).name, str(dataset_name), path=str(item.path)
                    )
                    data_obj._improcess_timepoint_scope = TimepointScope.single(logical_index)
                    out.append(data_obj)
                return out
            if source_kind != "image":
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
                return [DataObj(Path(str(path)).name, str(dataset_name), path=str(path))]
            return []

        refs = list(getattr(source, "source_refs", ()) or ())
        is_lapse = self._source_kind(source) == "time-lapse"
        if not is_lapse:
            refs = refs[:1]
        out = []
        for fallback_index, ref in enumerate(refs):
            if is_lapse:
                logical_index = getattr(ref, "timepoint_index", None)
                if logical_index is None:
                    logical_index = fallback_index
                logical_index = int(logical_index)
                if (logical_index < start or (stop is not None and logical_index >= stop)
                        or (allowed_timepoints is not None
                            and logical_index not in allowed_timepoints)):
                    continue
            dataset_name = ref.dataset
            if dataset_name in (None, ""):
                try:
                    names = list(DataObj.getDatasetNames(str(ref.path)))
                except Exception:
                    continue
                if len(names) != 1:
                    continue
                dataset_name = names[0]
            data_obj = DataObj(Path(ref.path).name, str(dataset_name), path=str(ref.path))
            if is_lapse:
                data_obj._improcess_timepoint_scope = TimepointScope.single(logical_index)
            out.append(data_obj)
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
        fraction = max(0.0, min(1.0, float(progress.fraction)))
        if self._offlineAutoBatch and self._offlineBatchIndices:
            job = self.queue.current
            if job is not None:
                start, stop = job.timepoint_scope.bounds(job.timepoint_total)
                if stop is not None and stop > start:
                    first = self._offlineBatchIndices[0]
                    fraction = min(1.0, (first - start
                        + fraction * len(self._offlineBatchIndices)) / (stop - start))
        self._currentProgress = {
            "fraction": fraction,
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

    def _publish_offline_completed(self, result, reconstructor, *, scope=None):
        """Conventional final-only fallback for non-progressive worker results."""
        job = self.queue.current
        if job is None:
            return
        relation = "timepoint" if self._offlineAtomicLapse else "single"
        publisher = self._new_result_publisher(
            job, scope=scope, total=self._currentSourceTimepoints,
            relation=relation,
        )
        publisher.offline_final(result, reconstructor)

    @QtCore.Slot(object, object)
    def _on_offline_result_ready(self, result, run) -> None:
        if self._offlineThread is None or self.queue.current is None:
            return
        scope = self._offlineResultScopes.pop(0) if self._offlineResultScopes else None
        if self._offlineAtomicLapse and not self._offlineProgressive:
            return  # Legacy atomic/consolidated fallback.
        if not self._offlineProgressive:
            self._publish_offline_completed(result, self._offlineReconstructor, scope=scope)
            return
        if self._offlinePendingOutcome is not None:
            return
        try:
            publisher = self._offlinePublisher
            if publisher is None or scope is None or not scope.bounded:
                raise ValueError("Finite output has no exact timepoint identity")
            ordinal = self._offlinePublishedCount
            expected = publisher.plan.coordinate_for_ordinal(ordinal)
            if int(scope.first) != int(expected):
                raise ValueError(f"Expected source T{expected}, got T{scope.first}")
            publisher.append_completed(result, ordinal, run=run)
            self._offlinePublishedCount += 1
            job = self.queue.current
            state = self._autoTimepointStates.get(str(job.id)) if job is not None else None
            if state is not None and self._offlineAutoBatch:
                # The persistent cursor advances only after publication of
                # an actual scientific result, never merely after file reads.
                state.next_t = int(scope.first) + 1
                if not state.calibration_resolved:
                    capture = getattr(self._offlineReconstructor,
                                      "finite_timepoint_reuse_overrides", None)
                    if callable(capture):
                        state.reuse_overrides = dict(capture(result, job.params) or {})
                    state.calibration_resolved = True
        except Exception as exc:
            self._logger.exception("Offline progressive publication failed")
            self._offlinePendingOutcome = ("failed", f"Progressive result assembly failed: {exc}")
            if self._offlineWorker is not None:
                self._offlineWorker.request_cancel()

    @QtCore.Slot(object)
    def _on_offline_finished(self, outcome) -> None:
        if self._skipRequested:
            self._offlinePendingOutcome = ("skipped", "")
            return
        if self._offlinePendingOutcome is not None:
            return
        if getattr(self, "_offlineProgressive", False):
            self._offlinePendingOutcome = ("completed", "")
            return
        if self._offlineAtomicLapse:
            merged = getattr(outcome, "merged", None)
            results = getattr(outcome, "results", ())
            self._offlinePendingResult = (
                merged if merged is not None
                else (results[0] if len(results) == 1 else None)
            )
            error = getattr(outcome, "consolidation_error", None)
            if error or self._offlinePendingResult is None:
                self._offlinePendingOutcome = (
                    "failed", str(error or "Finite timelapse did not yield one combined result"),
                )
                return
        self._offlinePendingOutcome = ("completed", "")

    @QtCore.Slot(object)
    def _on_offline_failed(self, failure: ReconstructionWorkerFailure) -> None:
        self._logger.error(
            "Offline reconstruction failed: %s\n%s",
            str(failure.message or ""), str(failure.traceback or ""),
        )
        if self._offlinePendingOutcome is None:
            self._offlinePendingOutcome = ("failed", str(failure.message or ""))

    @QtCore.Slot(str)
    def _on_offline_cancelled(self, message: str) -> None:
        if self._skipRequested:
            self._offlinePendingOutcome = ("skipped", "")
        elif self._offlinePendingOutcome is None:
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
        self._offlineResultScopes = []
        self._offlineThread = None
        self._offlineWorker = None
        self._offlinePendingOutcome = None
        atomic_lapse = self._offlineAtomicLapse
        progressive = getattr(self, "_offlineProgressive", False)
        auto_batch = self._offlineAutoBatch
        expected_batch = self._offlineBatchIndices
        self._offlineAutoBatch = False
        self._offlineBatchIndices = ()
        job = self.queue.current
        stage = (self._autoTimepointStates.get(str(job.id))
                 if auto_batch and job is not None else None)
        publisher = getattr(self, "_offlinePublisher", None)
        self._offlineProgressive = False
        self._offlinePublisher = None
        self._offlinePublishedCount = 0
        result = self._offlinePendingResult
        reconstructor = self._offlineReconstructor
        self._offlineAtomicLapse = False
        self._offlinePendingResult = None
        self._offlineReconstructor = None

        state, message = outcome
        if (stage is not None and state == "completed" and expected_batch
                and stage.next_t != expected_batch[-1] + 1):
            state, message = "failed", "Finite worker omitted a completed timepoint"
        if (stage is not None and state == "completed" and not self._skipRequested
                and stage.next_t < stage.stop_t):
            # The worker and all HDF5 readers are gone. Release the serial
            # slot while keeping this job, publisher and calibration alive.
            self.queue.defer_current(position="end")
            job.readiness_status = f"Waiting for completed T{stage.next_t + 1}"
            self._readinessDue.pop(str(job.id), None)
            self._autoTimepointDue.pop(str(job.id), None)
            self._autoTimepointCached.pop(str(job.id), None)
            self._autoTimepointInspected.pop(str(job.id), None)
            self._currentProgress = {}
            self._skipRequested = False
            self._emit_changed()
            self._emit_progress_changed()
            if not self._closing and not self.queue.paused:
                QtCore.QTimer.singleShot(0, self._process_next)
            return
        if stage is not None:
            self._autoTimepointStates.pop(str(job.id), None)
        if progressive:
            try:
                if publisher is None:
                    raise RuntimeError("Progressive publisher lost during thread teardown")
                if state == "completed" and not self._skipRequested:
                    if publisher.published is None:
                        raise RuntimeError("Offline processing produced no output")
                    publisher.finish("complete")
                else:
                    publisher.finish("cancelled" if state == "skipped" or self._skipRequested else "failed")
            except Exception as exc:
                self._logger.exception("Offline progressive result finalization failed")
                try:
                    if publisher is not None:
                        publisher.finish("failed")  # Retain completed output even on finalization error.
                except Exception:
                    self._logger.exception("Could not finalize retained partial result")
                state, message = "failed", f"Progressive result finalization failed: {exc}"
        if state == "skipped" or self._skipRequested:
            self._finish_current(skipped=True)
        elif state == "failed":
            self._finish_current(error=message)
        elif progressive:
            # Progressive output was published in place above. Do not route it
            # through the obsolete atomic/consolidated completion branch.
            self._finish_current()
        elif atomic_lapse:
            if result is None:
                self._finish_current(error="Timelapse completed without a consolidated result")
                return
            job = self.queue.current
            if job is None:
                return
            try:
                self._publish_offline_completed(
                    result, reconstructor, scope=job.timepoint_scope,
                )
            except Exception as exc:
                self._logger.exception("Offline timelapse publication failed")
                self._finish_current(error=f"Result publication failed: {exc}")
                return
            self._finish_current()
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
        # sigFinalResult is emitted before sigResultFinalized, so snapshot scope
        # provenance and claim exact Session ownership while the queue still holds
        # the current job.
        job = self.queue.current
        if job is not None and self._source_kind(self._resolve_job_source(job)) == "time-lapse":
            source = self._resolve_job_source(job)
            annotate_result_timepoint_scope(
                result,
                getattr(job, "timepoint_scope", None),
                total=self._planned_timepoints(source),
            )
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
        self._clear_readiness(job.id)
        pending_stage = self._autoTimepointStates.pop(str(job.id), None)
        if pending_stage is not None:
            try:
                pending_stage.publisher.finish(
                    "cancelled" if skipped else "failed" if error else "complete"
                )
            except Exception:
                self._logger.exception("Could not finalize interrupted Auto result")
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
        self._currentSourceTimepoints = None
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
        self._readinessDue.clear()
        self._readinessCached.clear()
        self._readinessInspected.clear()
        self._autoTimepointInspected.clear()
        self._autoTimepointDue.clear()
        self._autoTimepointCached.clear()
        self.autoPolicy.enabled = False
        self.queue.clear_waiting()
        for job_id in tuple(self._autoTimepointStates):
            if self.queue.current is None or str(self.queue.current.id) != job_id:
                self._cancel_waiting_auto_timepoints(job_id)
        self._externalSources.clear()
        self.queue.pause()
        if self._live.is_running:
            self._live.stop(graceful=False)
        if self._offlineWorker is not None:
            self._offlineWorker.request_cancel()
        if self._incrementalRunner is not None:
            self._incrementalRunner.cancel()
        # Do not unload DataObjs from the GUI thread while the worker may still
        # be reading them. _offline_thread_finished owns that cleanup.
        return self.shutdownComplete()

    def shutdownComplete(self):
        return (
            self._offlineThread is None
            and self._incrementalRunner is None
            and not self._live.is_running
        )


__all__ = ["ProcessingQueueController"]
