from __future__ import annotations

import os
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.controller.basecontrollers import ImProcessWidgetController
from imswitch.improcess.controller.session_save_worker import SessionSaveWorker
from imswitch.improcess.live.discovery import DirectoryWatcher
from imswitch.improcess.live.discovery_retry import UnresolvedDiscoveryRetry
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.lapse_source import (
    TIME_LAPSE_SOURCE_KIND,
    discover_time_lapse,
)
from imswitch.improcess.model.processing_session import (
    ProcessingSession,
    result_role_for_result,
)
from imswitch.improcess.model.provenance import graph_of, output_node
from imswitch.improcess.model.saved_result import load_saved_result, read_saved_result_metadata
from imswitch.improcess.model.session_result_presentation import reconstruction_presentation
from imswitch.improcess.model.timepoint_scope import logical_time_lapse_identity
from imswitch.improcess.model.session_result_store import FORMAT_SUFFIX, SessionResultStore


class _AcquisitionProbeWorker(QtCore.QObject):
    """Run one coalesced, metadata-only Session acquisition probe off-GUI."""

    sigFinished = QtCore.Signal(object)

    def __init__(self, requests):
        super().__init__()
        self._requests = tuple(requests)

    @QtCore.Slot()
    def run(self) -> None:
        from imswitch.improcess.execution.auto_timepoints import _current_references
        from imswitch.improcess.execution.readiness import probe_scope
        from imswitch.improcess.model.timepoint_scope import TimepointScope

        results = []
        for request in self._requests:
            dataset_id = str(request["dataset_id"])
            is_lapse = bool(request["is_lapse"])
            source = SimpleNamespace(
                sourceMetadata=None,
                source_refs=list(request["source_refs"]),
                primary_path=request["primary_path"],
                primary_dataset=request["primary_dataset"],
                dataPath=request["primary_path"],
                datasetName=request["primary_dataset"],
            )
            job = SimpleNamespace(
                timepoint_scope=TimepointScope.all(),
                timepoint_total=request["timepoint_total"],
            )
            try:
                if is_lapse:
                    current_refs = list(_current_references(source, job))
                    original_paths = {
                        str(Path(ref.path).resolve())
                        for ref in source.source_refs
                    }
                    single_file_hdf5 = (
                        original_paths
                        and len(original_paths) == 1
                        and Path(next(iter(original_paths))).suffix.lower()
                        in {".h5", ".hdf5"}
                    )
                    if not single_file_hdf5:
                        source.source_refs = [
                            SimpleNamespace(
                                path=ref.path,
                                dataset=ref.dataset,
                                timepoint_index=ref.logical_t,
                            )
                            for ref in current_refs
                        ]
                previous = request.get("progress")
                inspected = {}
                if previous is not None:
                    completed = set(previous.completed_timepoints)
                    inspected = {
                        (ref.path, ref.dataset, ref.logical_t): True
                        for ref in previous.expected_references
                        if ref.logical_t in completed
                    }
                readiness = probe_scope(
                    source,
                    job,
                    is_lapse=is_lapse,
                    inspected=inspected,
                    activity_observed=bool(request.get("activity_observed", False)),
                )
                progress = readiness.progress
                retry = bool(
                    progress is None
                    and "safe source access" in str(readiness.reason).lower()
                )
                if progress is not None:
                    retry = retry or "safe source access" in str(progress.reason).lower()
                results.append((dataset_id, readiness, tuple(source.source_refs), retry, ""))
            except Exception as exc:
                results.append((dataset_id, None, tuple(source.source_refs), True, str(exc)))
        self.sigFinished.emit(results)


def _same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return str(a) == str(b)


class ProcessingSessionController(ImProcessWidgetController):
    """Persistent Session UI/controller and owner of Session filesystem watching."""

    sigCatalogChanged = QtCore.Signal()
    sigDatasetsAdded = QtCore.Signal(object)
    sigAutoAcquisitionsAdmitted = QtCore.Signal(object, object)

    def __init__(
        self,
        *args,
        mainController=None,
        fileIOController=None,
        reconstructionController=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._fileIO = fileIOController
        self._reconstruction = reconstructionController
        self._session: ProcessingSession | None = None
        # A Session always opens in passive Review. This runtime-only mode is
        # deliberately absent from ProcessingSession's persisted manifest.
        self._sessionMode = "review"
        self._selectedDatasetId: str | None = None
        self._selectedReconKey: str | None = None
        self._currentDatasetId: str | None = None
        self._loadingDatasetId: str | None = None
        self._followActiveReconstruction = True
        self._syncingFromActiveReconstruction = False
        self._autoSave = True
        self._saveFormat = "tiff"
        self._resultStore: SessionResultStore | None = None
        self._processingQueueController = None
        self._queueProgressByDatasetId: dict[str, dict] = {}
        # Loaded-result ownership is indexed once when Results-panel membership
        # changes. Full Session renders can then use O(1) dataset lookups instead
        # of rescanning every loaded result for every dataset.
        self._loadedResultsByDatasetId: dict[str, list[tuple[str, object]]] = {}
        self._loadedResultOwners: dict[int, str] = {}
        self._loadedBySavedPath: dict[str, object] = {}
        # Auto-save is serial but asynchronous. Large TIFF/HDF5/Zarr writes must
        # never block the GUI thread.
        self._saveRequests = deque()
        self._saveThread = None
        self._saveWorker = None
        self._saveActiveRequest = None
        # Session owns filesystem observation.  Live processing may temporarily
        # subscribe to this watcher too, but toggling live processing must not
        # stop Session catalog synchronization.
        self._directoryWatcher: DirectoryWatcher | None = None
        self._watchPendingPaths: set[str] = set()
        # Acquisition visibility is a lightweight runtime cache.  It is
        # invalidated by the existing Session watcher and is never persisted
        # into the Session manifest.
        self._acquisitionProgress: dict[str, object] = {}
        self._acquisitionProbePending: set[str] = set()
        self._acquisitionProbeActivity: set[str] = set()
        self._acquisitionProbeInFlight: set[str] = set()
        self._acquisitionProbeThread = None
        self._acquisitionProbeWorker = None
        self._acquisitionProbeGeneration = 0
        self._watchAcquisitionRetryPaths: set[str] = set()
        # Unrecognized entries and metadata probes that hit a transient access
        # conflict share the existing bounded retry scheduler.  Ordinary
        # completed datasets are never added here.
        self._watchUnresolvedPaths: set[str] = set()
        self._watchRetrySchedule = UnresolvedDiscoveryRetry()
        self._watchRetryTimer = QtCore.QTimer(self)
        self._watchRetryTimer.setInterval(3000)
        self._watchRetryTimer.timeout.connect(self._retryUnresolvedPaths)
        self._watchNeedsFullReconcile = False
        # Arrival tokens are granted only when Watching + Auto were enabled
        # when the *root entry first appeared*. They survive later Auto/mode
        # changes until that entry resolves, without retroactive admission.
        self._watchAutoArrivals: dict[str, dict] = {}
        # One transient calibration update per job, after its first scientific
        # timepoint. Provisional live frames are not resolved calibrations.
        self._autoCalibrationSeenJobs: set[str] = set()
        self._watchLapseChildren: dict[str, frozenset[str]] = {}
        self._watchReconcileTimer = QtCore.QTimer(self)
        self._watchReconcileTimer.setSingleShot(True)
        self._watchReconcileTimer.setInterval(150)
        self._watchReconcileTimer.timeout.connect(self._flushWatcherReconcile)
        self._addQueue: list[tuple[object, object]] | None = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        # sigResultsChanged also fires for selection-only changes. Track the
        # actual loaded set so Session only rebuilds its catalog when membership
        # changes; active-result navigation is handled by sigCurrentResultChanged.
        self._loadedResultSignature = self._loaded_result_signature()

        self._widget.sigStartSessionRequested.connect(self.startSession)
        self._widget.sigSettingsRequested.connect(self.editSessionSettings)
        self._widget.sigRefresh.connect(self.refresh)
        self._widget.sigSessionModeChanged.connect(self.setSessionMode)
        self._widget.sigDatasetSelectionChanged.connect(self.datasetSelected)
        self._widget.sigSavedSelectionChanged.connect(self.reconstructionSelected)
        self._widget.sigLoadRawRequested.connect(self.loadSelectedRaw)
        self._widget.sigOpenViewerRequested.connect(self.openSelectedInViewer)
        self._widget.sigLoadSelectedAsMultiDataRequested.connect(self.loadSelectedAsMultiData)
        self._widget.sigProcessSelectedRequested.connect(self.processSelectedDatasets)
        self._widget.sigAutoProcessingChanged.connect(self.setAutoProcessingEnabled)
        self._widget.sigPauseProcessingRequested.connect(self.pauseProcessingQueue)
        self._widget.sigResumeProcessingRequested.connect(self.resumeProcessingQueue)
        self._widget.sigSkipCurrentProcessingRequested.connect(self.skipCurrentProcessing)
        self._widget.sigRevealRawRequested.connect(self.revealSelectedRaw)
        self._widget.sigCopyRawPathRequested.connect(self.copySelectedRawPath)
        self._widget.sigDeleteRawRequested.connect(self.deleteSelectedRaw)
        self._widget.sigFavoriteDatasetRequested.connect(self.setDatasetFavorite)
        self._widget.sigSetSelectedFavoriteRequested.connect(self.setSelectedDatasetsFavorite)
        self._widget.sigSetSelectedAutoIgnoredRequested.connect(
            self.setSelectedDatasetsAutoIgnored
        )
        self._widget.sigReconstructionWorkspaceToggleRequested.connect(
            self.toggleReconstructionWorkspace
        )
        self._widget.sigDiscardReconstructionRequested.connect(self.discardReconstruction)
        self._widget.sigDiscardSelectedRequested.connect(self.discardSelected)
        self._widget.sigReuseCalibrationRequested.connect(self.reuseReconstructionCalibration)
        self._widget.sigAddSelectedReconstructionsRequested.connect(
            self.addSelectedDatasetReconstructions
        )
        self._widget.sigCloseSelectedDatasetReconstructionsRequested.connect(
            self.closeSelectedDatasetReconstructions
        )
        self._widget.sigDeleteSelectedDatasetReconstructionsRequested.connect(
            self.deleteSelectedDatasetReconstructions
        )
        self._widget.sigFollowActiveReconstructionChanged.connect(
            self.setFollowActiveReconstruction
        )
        self._widget.sigCancelAddingRequested.connect(self.cancelAddingReconstructions)
        self._widget.sigCloseSessionReconstructionsRequested.connect(self.closeSessionReconstructions)
        self._widget.sigCloseNonFavoriteReconstructionsRequested.connect(
            self.closeNonFavoriteReconstructions
        )
        self._widget.sigCleanupNonFavoritesRequested.connect(self.cleanupNonFavorites)
        self._widget.sigCloseSessionRequested.connect(self.closeSession)
        self._widget.sigEmptyTrashRequested.connect(self.emptySessionTrash)
        self._widget.sigRevealRawFolderRequested.connect(self.revealRawFolder)
        self._widget.sigRevealResultsFolderRequested.connect(self.revealResultsFolder)

        self._commChannel.sigResultFinalized.connect(self._resultFinalized)
        self._commChannel.sigLiveResultUpdated.connect(self._autoCalibrationPreviewPublished)
        self._commChannel.sigResultsChanged.connect(self._loadedResultsChanged)
        if hasattr(self._commChannel, "sigResultSaved"):
            self._commChannel.sigResultSaved.connect(self._resultSaved)
        self._commChannel.sigCurrentDataChanged.connect(self._currentDataChanged)
        self._commChannel.sigCurrentResultChanged.connect(self._currentResultChanged)

    @property
    def session(self):
        return self._session

    @property
    def directoryWatcher(self) -> DirectoryWatcher | None:
        """Observer owned by the Session, available only in Follow mode."""
        return self._directoryWatcher

    @property
    def sessionMode(self) -> str:
        """Non-persisted directory-observation policy (review / follow)."""
        return self._sessionMode

    def _queue_controller(self):
        return self._processingQueueController or getattr(
            self._main, "processingQueueController", None
        )

    def _disableSessionAuto(self) -> None:
        # Disarming Auto affects only *future* arrivals. Never remove queued
        # or running work, and never call the historical catalog backfill API.
        queue = self._queue_controller()
        if queue is not None:
            queue.setAutoEnabled(False)
            queue.setProcessWhileAcquiring(False)
        self._sync_auto_availability()

    def _sync_streaming_capability(self) -> None:
        from imswitch.improcess.execution.policy import streaming_capability

        reconstructor = getattr(self._main, "_activeReconstructor", None)
        params = {}
        error = ""
        if reconstructor is not None:
            try:
                getter = getattr(self._main._widget, "getReconstructionParams", None)
                if not callable(getter):
                    raise ValueError("Reconstruction parameter widget unavailable")
                params = dict(getter() or {})
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                error = f"Cannot inspect selected settings: {exc}"
        capability = (
            streaming_capability(reconstructor, params)
            if not error else None
        )
        supported = bool(capability and capability.supported)
        reason = error or (capability.reason if capability else "No selected reconstructor")
        if not supported:
            queue = self._queue_controller()
            if queue is not None and getattr(queue, "_processWhileAcquiring", False):
                queue.setProcessWhileAcquiring(False)
        name = str(getattr(reconstructor, "name", "—") or "—")
        if str(getattr(reconstructor, "id", "")) == "monalisa":
            name = "MoNaLISA"
            mode = str(params.get("preset", "") or "")
            if mode:
                name += " · " + ("Robust" if mode == "Full" else mode)
        view = getattr(self._main, "_widget", None)
        setter = getattr(view, "setSessionStreamingCapability", None)
        if callable(setter):
            setter(name, supported, reason)

    def _sync_auto_availability(self):
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        available = bool(
            self._session is not None and self._sessionMode == "follow"
            and reconstructor is not None
            and str(getattr(reconstructor, "result_role", "derived")) != "inspection"
        )
        view = getattr(self._main, "_widget", None)
        if view is not None and hasattr(view, "setSessionAutoProcessingAvailable"):
            view.setSessionAutoProcessingAvailable(available)
        elif hasattr(self._widget, "setAutoProcessingAvailable"):
            self._widget.setAutoProcessingAvailable(available)
        self._sync_streaming_capability()

    def setSessionMode(self, mode: str) -> None:
        """Switch only filesystem observation; existing processing jobs live on.

        Entering Follow seeds a new observer from a snapshot of all current
        root entries. Old or still-unreadable entries are not new arrivals.
        """
        mode = str(mode).strip().lower()
        if mode not in {"review", "follow"}:
            raise ValueError(f"Unknown Session mode: {mode!r}")
        if self._session is None:
            mode = "review"
        if mode == self._sessionMode:
            self._widget.setSessionMode(mode)
            if mode == "follow" and self._directoryWatcher is not None:
                self._widget.setWatchingActive(True)
            return
        self._stopDirectoryWatcher()
        self._sessionMode = mode
        self._widget.setSessionMode(mode)
        self._disableSessionAuto()
        if mode == "follow" and self._session is not None:
            self._startDirectoryWatcher(self._session)

    @staticmethod
    def _session_origin_id(session) -> str | None:
        if session is None:
            return None
        try:
            return str(session.manifest_path.resolve())
        except Exception:
            return str(session.manifest_path)

    def _tag_session_origin(self, result, session=None) -> None:
        if result is None:
            return
        origin = self._session_origin_id(session or self._session)
        if origin:
            result._session_origin = origin

    def claimResultForDataset(self, result, dataset_id: str):
        """Attach exact transient Session ownership to a queued result.

        The processing queue knows which logical dataset created a result. That
        relationship is stronger than trying to infer ownership again from raw
        provenance paths (especially for partitioned time lapses). Provenance is
        still recorded for durable reopen/replay; this claim drives the current
        Session's result list and autosave lifecycle.
        """
        if self._session is None or result is None:
            return None
        dataset = self._session.dataset_by_id(str(dataset_id))
        if dataset is None:
            return None
        result._session_dataset_id = str(dataset.id)
        self._tag_session_origin(result, self._session)
        return dataset

    def _results_associated_with_session(self, session=None) -> list[object]:
        session = session or self._session
        if session is None:
            return []
        matches = []
        seen = set()
        for _name, result in self._commChannel.getAllResults():
            if result is None or id(result) in seen or not self._is_session_reconstruction(result):
                continue
            if session.dataset_for_result(result) is None:
                continue
            seen.add(id(result))
            matches.append(result)
        return matches

    def _reset_add_queue(self) -> None:
        self._addQueue = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(False)

    def _deactivate_session(self, *, status: str | None = None) -> None:
        self._stopDirectoryWatcher()
        self._acquisitionProbeGeneration = getattr(
            self, "_acquisitionProbeGeneration", 0
        ) + 1
        getattr(self, "_acquisitionProgress", {}).clear()
        getattr(self, "_acquisitionProbePending", set()).clear()
        getattr(self, "_acquisitionProbeActivity", set()).clear()
        getattr(self, "_acquisitionProbeInFlight", set()).clear()
        self._sessionMode = "review"
        self._widget.setSessionMode("review")
        self._watchAutoArrivals.clear()
        self._watchLapseChildren.clear()
        self._reset_add_queue()
        self._session = None
        self._resultStore = None
        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._currentDatasetId = None
        self._loadingDatasetId = None
        self._loadedResultSignature = self._loaded_result_signature()
        self._loadedResultsByDatasetId = {}
        self._loadedResultOwners = {}
        self._loadedBySavedPath = {}
        self._widget.setSessionActive(False)
        self._widget.setDatasets([])
        self._widget.setSelectedDataset("", "")
        self._widget.setSavedReconstructions([])
        if status:
            self._widget.setStatus(status)

    def _request_configuration(self) -> dict | None:
        session = self._session
        raw = str(session.raw_root) if session is not None else ""
        results = str(session.results_root) if session is not None else ""
        return self._widget.requestSessionConfiguration(
            raw_root=raw,
            results_root=results,
            auto_save=self._autoSave,
            save_format=self._saveFormat,
            active_raw_root=raw,
        )

    def startSession(self) -> None:
        config = self._request_configuration()
        if config is None:
            return
        self._apply_configuration(config)

    def editSessionSettings(self) -> None:
        config = self._request_configuration()
        if config is None:
            return
        self._apply_configuration(config)

    def _apply_configuration(self, config: dict) -> None:
        raw_text = str(config.get("raw_root") or "").strip()
        if not raw_text:
            return
        raw = Path(raw_text).expanduser()
        results_text = str(config.get("results_root") or "").strip()
        results = Path(results_text).expanduser() if results_text else raw / "results"
        self._autoSave = bool(config.get("auto_save", True))
        fmt = str(config.get("save_format") or "tiff")
        self._saveFormat = fmt if fmt in FORMAT_SUFFIX else "tiff"
        if self._resultStore is not None:
            self._resultStore.save_format = self._saveFormat

        restore_mode = str(config.get("restore_mode") or "none")
        if (
            self._session is not None
            and _same_path(self._session.raw_root, raw)
            and _same_path(self._session.results_root, results)
        ):
            self._widget.setStatus("Session settings updated")
            return
        if not self.openSession(raw, results):
            return
        if restore_mode == "all":
            self._start_add_reconstructions(list(self._session.datasets))
        elif restore_mode == "favorites":
            self._start_add_reconstructions(
                [dataset for dataset in self._session.datasets if dataset.favorite]
            )

    def _prepare_session(self, raw_root, results_root) -> ProcessingSession | None:
        raw = Path(raw_root).expanduser()
        if not raw.is_dir():
            self._widget.setStatus(f"Raw-data folder does not exist: {raw}")
            return None
        try:
            session = ProcessingSession(raw, results_root)
            session.refresh()
        except Exception as exc:
            self._logger.exception("Could not open processing session")
            self._widget.setStatus(f"Could not open session: {exc}")
            return None
        return session

    def _activate_session(self, session: ProcessingSession) -> None:
        # Every Session opens passive. Watching/Auto remain deliberate, runtime-
        # only choices, and are not saved in the Session manifest.
        self._stopDirectoryWatcher()
        self._acquisitionProbeGeneration = getattr(
            self, "_acquisitionProbeGeneration", 0
        ) + 1
        getattr(self, "_acquisitionProgress", {}).clear()
        getattr(self, "_acquisitionProbePending", set()).clear()
        getattr(self, "_acquisitionProbeActivity", set()).clear()
        getattr(self, "_acquisitionProbeInFlight", set()).clear()
        self._sessionMode = "review"
        self._watchAutoArrivals.clear()
        self._autoCalibrationSeenJobs.clear()
        self._watchLapseChildren.clear()
        self._widget.setSessionMode("review")
        self._disableSessionAuto()
        self._session = session
        self._resultStore = SessionResultStore(session, save_format=self._saveFormat)
        self._loadedResultSignature = self._loaded_result_signature()
        self._rebuild_loaded_result_index()
        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._widget.setSessionActive(True, session.raw_root.name)
        self._widget.setStatus(
            f"Session active · {len(session.datasets)} raw dataset(s) · {session.results_root}"
        )
        self._sync_current_dataset(getattr(self._main, "_currentDataObj", None))
        self._render()
        queue_probe = getattr(self, "_queue_acquisition_probes", None)
        if callable(queue_probe):
            queue_probe(str(dataset.id) for dataset in session.datasets)
            self._start_acquisition_probe()
        self.sigCatalogChanged.emit()
        if self._followActiveReconstruction and self._reconstruction is not None:
            self._currentResultChanged(self._reconstruction.getActiveResult())
        self._sync_auto_availability()

    # ------------------------------------------------------ Session directory watch

    def _startDirectoryWatcher(self, session: ProcessingSession) -> None:
        if self._sessionMode != "follow" or session is not self._session:
            return
        self._stopDirectoryWatcher()
        watcher = DirectoryWatcher(str(session.raw_root), parent=self)
        # Establish the baseline *before* reconciling: an entry already in
        # Review must never become a new Follow arrival just because it was
        # previously unreadable. A fresh arrival during reconcile will still
        # be discovered by start()'s subsequent snapshot.
        initial_entries = watcher.scan_once()
        before_ids = {str(dataset.id) for dataset in session.datasets}
        try:
            session.reconcile_all()
        except Exception as exc:
            self._logger.exception("Could not reconcile Session while enabling Follow")
            watcher.deleteLater()
            self._widget.setStatus(f"Could not start Follow: {exc}")
            self._sessionMode = "review"
            self._widget.setSessionMode("review")
            self._disableSessionAuto()
            return
        added_ids = tuple(
            str(dataset.id) for dataset in session.datasets
            if str(dataset.id) not in before_ids
        )
        if added_ids:
            self._render()
            self.sigCatalogChanged.emit()
            # Existing entries are catalogued but are not Auto arrivals.
            self.sigDatasetsAdded.emit(added_ids)
        # Entries that are not recognizable yet need a slow retry even if
        # their root mtime remains unchanged while nested Zarr data appears.
        self._watchUnresolvedPaths = {
            str(path)
            for path in initial_entries
            if not self._pathBelongsToResults(path)
            and not self._sessionKnowsRootEntry(path)
        }
        for path in self._watchUnresolvedPaths:
            self._watchRetrySchedule.track(path)
        self._admit_resolved_arrivals(initial_entries)
        watcher.sigEntryFound.connect(self._sessionPathFound)
        watcher.sigEntryChanged.connect(self._sessionPathChanged)
        watcher.sigEntryRemoved.connect(self._sessionPathRemoved)
        self._directoryWatcher = watcher
        try:
            watcher.start()
            if not bool(getattr(watcher, "running", True)):
                raise RuntimeError("Directory observer did not start")
        except Exception as exc:
            self._logger.exception("Could not start Session directory observer")
            self._stopDirectoryWatcher()
            self._sessionMode = "review"
            self._widget.setSessionMode("review")
            self._disableSessionAuto()
            self._widget.setStatus(f"Could not start watching: {exc}")
            return
        self._sync_auto_availability()
        # Monitor is a one-click start: arm Auto after the observer has
        # successfully started and its pre-existing entries are baselined.
        # The shared queue already defaults Live on when streaming is capable.
        queue_getter = getattr(self, "_queue_controller", None)
        queue = queue_getter() if callable(queue_getter) else None
        reconstructor = getattr(getattr(self, "_main", None), "_activeReconstructor", None)
        if (queue is not None and reconstructor is not None
                and str(getattr(reconstructor, "result_role", "derived")) != "inspection"):
            queue.setAutoEnabled(True)
        update_details = getattr(self, "_update_watcher_detail_paths", None)
        if callable(update_details):
            update_details()
        queue_probe = getattr(self, "_queue_acquisition_probes", None)
        if callable(queue_probe):
            queue_probe(str(dataset.id) for dataset in session.datasets)
            self._start_acquisition_probe()
        self._syncWatchRetryTimer()
        if hasattr(self._widget, "setWatchingActive"):
            self._widget.setWatchingActive(True)

    def _syncWatchRetryTimer(self) -> None:
        # No periodic work at all when the unresolved set is empty or Monitor
        # is off. The regular DirectoryWatcher has its own independent timer.
        active = bool(
            self._sessionMode == "follow"
            and self._directoryWatcher is not None
            and self._watchRetrySchedule
        )
        if active and not self._watchRetryTimer.isActive():
            self._watchRetryTimer.start()
        elif not active:
            self._watchRetryTimer.stop()

    @staticmethod
    def _source_paths(dataset) -> tuple[str, ...]:
        paths = [getattr(dataset, "primary_path", None)]
        paths.extend(
            getattr(ref, "path", None)
            for ref in getattr(dataset, "source_refs", ())
        )
        return tuple(dict.fromkeys(
            str(path) for path in paths if path
        ))

    @classmethod
    def _dataset_uses_metadata_probe(cls, dataset) -> bool:
        return any(
            Path(path).suffix.lower() in {".h5", ".hdf5", ".zarr"}
            for path in cls._source_paths(dataset)
        )

    def _dataset_ids_for_watch_path(self, path: str) -> tuple[str, ...]:
        session = self._session
        if session is None:
            return ()
        try:
            watched = Path(path).resolve()
        except OSError:
            watched = Path(path).absolute()
        matched = []
        for dataset in session.datasets:
            for source in self._source_paths(dataset):
                try:
                    source_path = Path(source).resolve()
                    if source_path == watched:
                        matched.append(str(dataset.id))
                        break
                    source_path.relative_to(watched)
                    matched.append(str(dataset.id))
                    break
                except (ValueError, OSError):
                    continue
        return tuple(dict.fromkeys(matched))

    def _acquisition_probe_request(self, dataset) -> dict:
        refs = []
        for ref in getattr(dataset, "source_refs", ()):
            refs.append(SimpleNamespace(
                path=str(getattr(ref, "path", "")),
                dataset=getattr(ref, "dataset", None),
                timepoint_index=getattr(ref, "timepoint_index", None),
            ))
        if not refs and getattr(dataset, "primary_path", None):
            refs.append(SimpleNamespace(
                path=str(dataset.primary_path),
                dataset=getattr(dataset, "primary_dataset", None),
                timepoint_index=0,
            ))
        planned = getattr(dataset, "planned_timepoints", None)
        if getattr(dataset, "kind", "") != "time-lapse":
            planned = 1
        elif planned not in (None, 0):
            planned = int(planned)
        return {
            "dataset_id": str(dataset.id),
            "is_lapse": str(getattr(dataset, "kind", "")) == "time-lapse",
            "primary_path": str(getattr(dataset, "primary_path", "") or ""),
            "primary_dataset": getattr(dataset, "primary_dataset", None),
            "source_refs": tuple(refs),
            "timepoint_total": planned,
            "progress": self._acquisitionProgress.get(str(dataset.id)),
            "activity_observed": str(dataset.id) in self._acquisitionProbeActivity,
        }

    def _queue_acquisition_probes(self, dataset_ids, *, activity_observed: bool = False) -> None:
        session = self._session
        if session is None:
            return
        for dataset_id in dataset_ids or ():
            dataset = session.dataset_by_id(str(dataset_id))
            if dataset is None or not self._dataset_uses_metadata_probe(dataset):
                continue
            progress = self._acquisitionProgress.get(str(dataset.id))
            # Verified complete acquisitions are stable historical entries.
            # They are still rendered from the cached snapshot, but never
            # reopened just because an unrelated watcher entry changed.
            if progress is not None and progress.status in {"complete", "incomplete"}:
                continue
            self._acquisitionProbePending.add(str(dataset.id))
            if activity_observed:
                self._acquisitionProbeActivity.add(str(dataset.id))

    @staticmethod
    def _zarr_watch_metadata_paths(path: str, dataset: str | None) -> tuple[str, ...]:
        root = Path(path)
        if root.suffix.lower() != ".zarr":
            return ()
        bases = [root]
        name = str(dataset or "").strip("/")
        if name:
            bases.append(root.joinpath(*name.split("/")))
            if not name.endswith("/data"):
                bases.append(root.joinpath(*name.split("/"), "data"))
        paths = []
        for base in bases:
            for marker in (".zattrs", ".zgroup", "zarr.json"):
                paths.append(str(base / marker))
        return tuple(dict.fromkeys(paths))

    def _watch_entry_for_source(self, path: str) -> str | None:
        session = self._session
        if session is None:
            return None
        try:
            relative = Path(path).resolve().relative_to(session.raw_root.resolve())
        except (ValueError, OSError):
            return None
        if not relative.parts:
            return None
        return str((session.raw_root / relative.parts[0]).resolve())

    def _update_watcher_detail_paths(self) -> None:
        watcher = self._directoryWatcher
        session = self._session
        setter = getattr(watcher, "set_detail_paths", None) if watcher is not None else None
        if session is None or not callable(setter):
            return
        details: dict[str, set[str]] = {}
        for dataset in session.datasets:
            progress = self._acquisitionProgress.get(str(dataset.id))
            if progress is not None and progress.status in {"complete", "incomplete"}:
                continue
            refs = list(getattr(dataset, "source_refs", ()) or ())
            if progress is not None:
                refs.extend(
                    SimpleNamespace(path=ref.path, dataset=ref.dataset)
                    for ref in progress.expected_references
                )
            if not refs:
                refs = [SimpleNamespace(
                    path=getattr(dataset, "primary_path", ""),
                    dataset=getattr(dataset, "primary_dataset", None),
                )]
            for ref in refs:
                source = str(getattr(ref, "path", "") or "")
                entry = self._watch_entry_for_source(source)
                if not entry:
                    continue
                metadata = self._zarr_watch_metadata_paths(
                    source, getattr(ref, "dataset", None)
                )
                if metadata:
                    details.setdefault(entry, set()).update(metadata)
        setter({key: tuple(value) for key, value in details.items()})

    def _start_acquisition_probe(self) -> None:
        if self._session is None or self._acquisitionProbeThread is not None:
            return
        if not self._acquisitionProbePending:
            return
        ids = tuple(sorted(self._acquisitionProbePending))
        requests = []
        for dataset_id in ids:
            dataset = self._session.dataset_by_id(dataset_id)
            if dataset is None:
                continue
            requests.append(self._acquisition_probe_request(dataset))
        self._acquisitionProbePending.difference_update(ids)
        self._acquisitionProbeActivity.difference_update(ids)
        if not requests:
            return

        generation = self._acquisitionProbeGeneration
        thread = QtCore.QThread()
        worker = _AcquisitionProbeWorker(requests)
        worker.moveToThread(thread)
        worker.sigFinished.connect(
            lambda results, generation=generation: self._acquisition_probe_finished(
                results, generation
            )
        )
        worker.sigFinished.connect(thread.quit)
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda thread=thread, ids=ids: self._acquisition_probe_thread_finished(
                thread, ids
            )
        )
        self._acquisitionProbeThread = thread
        self._acquisitionProbeWorker = worker
        self._acquisitionProbeInFlight = set(ids)
        thread.start()

    def _acquisition_probe_finished(self, results, generation: int) -> None:
        if generation != self._acquisitionProbeGeneration or self._session is None:
            return
        changed = []
        for result in results or ():
            dataset_id, readiness, refs, retry, error = result
            dataset_id = str(dataset_id)
            dataset = self._session.dataset_by_id(dataset_id)
            if dataset is None:
                continue
            previous = self._acquisitionProgress.get(dataset_id)
            if readiness is not None and readiness.progress is not None:
                progress = self._merge_acquisition_progress(
                    previous, readiness.progress
                )
                self._acquisitionProgress[dataset_id] = progress
                if previous != progress:
                    changed.append(dataset_id)
                if progress.status in {"complete", "incomplete"}:
                    for path in self._source_paths(dataset):
                        self._watchAcquisitionRetryPaths.discard(str(path))
                        self._watchRetrySchedule.discard(str(path))
                elif retry:
                    for path in self._source_paths(dataset):
                        self._watchAcquisitionRetryPaths.add(str(path))
                        self._watchRetrySchedule.track(str(path))
                else:
                    for path in self._source_paths(dataset):
                        self._watchAcquisitionRetryPaths.discard(str(path))
                        self._watchRetrySchedule.discard(str(path))
            elif retry:
                paths = self._source_paths(dataset)
                if previous is not None and previous.status in {"complete", "incomplete"}:
                    for path in paths:
                        self._watchAcquisitionRetryPaths.discard(str(path))
                        self._watchRetrySchedule.discard(str(path))
                else:
                    for path in paths:
                        self._watchAcquisitionRetryPaths.add(str(path))
                        self._watchRetrySchedule.track(str(path))
            if error:
                self._logger.warning(
                    "Acquisition metadata probe failed for %s: %s", dataset_id, error
                )
        self._update_watcher_detail_paths()
        self._syncWatchRetryTimer()
        if changed:
            queue_states = self._queue_states()
            for dataset_id in dict.fromkeys(changed):
                self._renderDataset(
                    dataset_id,
                    render_selected=(dataset_id == self._selectedDatasetId),
                    queue_state=queue_states.get(dataset_id),
                )

    def _acquisition_probe_thread_finished(self, thread, ids) -> None:
        if self._acquisitionProbeThread is not thread:
            return
        self._acquisitionProbeThread = None
        self._acquisitionProbeWorker = None
        self._acquisitionProbeInFlight.difference_update(ids)
        self._start_acquisition_probe()

    @staticmethod
    def _merge_acquisition_progress(previous, current):
        """Keep verified acquisition knowledge across transient probe states."""
        if previous is None:
            return current
        if current is None:
            return previous

        # A verified completion or an explicit interrupted outcome is a
        # terminal acquisition decision.  Later access failures must not make
        # the Session row look queued/unknown again.
        if previous.status == "complete":
            return previous
        if previous.status == "incomplete":
            return previous

        completed = tuple(sorted(set(previous.completed_timepoints)
                                 | set(current.completed_timepoints)))
        catalog = tuple(sorted(set(previous.catalog_timepoints)
                               | set(current.catalog_timepoints)))
        expected = current.expected_references or previous.expected_references
        planned = (
            current.planned_timepoints
            if current.planned_timepoints is not None
            else previous.planned_timepoints
        )
        scope = current.scope or previous.scope
        status = current.status
        if previous.status == "acquiring" and status == "unknown":
            status = "acquiring"
        reason = current.reason or previous.reason
        return replace(
            current,
            planned_timepoints=planned,
            scope=scope,
            expected_references=expected,
            catalog_timepoints=catalog,
            completed_timepoints=completed,
            status=status,
            reason=reason,
        )

    @QtCore.Slot()
    def _retryUnresolvedPaths(self) -> None:
        """Retry a bounded batch through existing reconciliation, never arrival."""
        if self._session is None or self._sessionMode != "follow" or self._directoryWatcher is None:
            return
        acquisition_retry_paths = getattr(self, "_watchAcquisitionRetryPaths", set())
        for path in self._watchRetrySchedule.due():
            if path in acquisition_retry_paths:
                if not Path(path).exists():
                    acquisition_retry_paths.discard(path)
                    self._watchRetrySchedule.discard(path)
                    continue
                self._queue_acquisition_probes(self._dataset_ids_for_watch_path(path))
                continue
            if path not in self._watchUnresolvedPaths:
                self._watchRetrySchedule.discard(path)
            elif not Path(path).exists():
                self._sessionPathRemoved(path)
            elif self._sessionKnowsRootEntry(path):
                # A manual/full refresh may already have resolved it.
                self._watchUnresolvedPaths.discard(path)
                self._watchRetrySchedule.discard(path)
                self._admit_resolved_arrivals((path,))
            else:
                self._watchPendingPaths.add(path)
        if (
            self._watchPendingPaths
            or self._watchNeedsFullReconcile
            or getattr(self, "_acquisitionProbePending", set())
        ):
            self._watchReconcileTimer.start()
        self._syncWatchRetryTimer()

    def _stopDirectoryWatcher(self) -> None:
        watcher, self._directoryWatcher = self._directoryWatcher, None
        if watcher is not None:
            watcher.stop()
            watcher.deleteLater()
        self._watchPendingPaths.clear()
        self._watchUnresolvedPaths.clear()
        getattr(self, "_watchAcquisitionRetryPaths", set()).clear()
        self._watchRetrySchedule.clear()
        self._watchRetryTimer.stop()
        self._watchNeedsFullReconcile = False
        self._watchReconcileTimer.stop()
        if hasattr(self._widget, "setWatchingActive"):
            self._widget.setWatchingActive(False)

    def _pathBelongsToResults(self, path: str) -> bool:
        session = self._session
        if session is None:
            return False
        try:
            Path(path).resolve().relative_to(session.results_root)
            return True
        except (ValueError, OSError):
            return False

    def _sessionKnowsRootEntry(self, path: str) -> bool:
        """Whether ``path`` already owns any catalogued raw source.

        This is deliberately a cheap path-membership check; it never opens raw
        data. It lets Session distinguish "new entry still becoming readable"
        from ordinary growth of a time lapse that LiveSource already owns.
        """
        session = self._session
        if session is None:
            return False
        try:
            root_entry = Path(path).resolve()
        except OSError:
            root_entry = Path(path).absolute()
        for dataset in session.datasets:
            refs = [getattr(dataset, "primary_path", None)]
            refs.extend(getattr(ref, "path", None) for ref in dataset.source_refs)
            for source in refs:
                if not source:
                    continue
                try:
                    Path(source).resolve().relative_to(root_entry)
                    return True
                except (ValueError, OSError):
                    continue
        return False

    def _admit_resolved_arrivals(self, paths) -> None:
        session = self._session
        if session is None:
            return
        for path in paths:
            recipe = self._watchAutoArrivals.get(str(path))
            if recipe is None:
                continue
            matched = []
            for dataset in session.datasets:
                # The recognized logical acquisition may include several
                # physical files. Compare identities, never filename prefixes.
                refs = [getattr(dataset, "primary_path", None)]
                refs.extend(getattr(ref, "path", None)
                            for ref in getattr(dataset, "source_refs", ()))
                for source in refs:
                    try:
                        Path(source).resolve().relative_to(Path(path).resolve())
                        matched.append(str(dataset.id))
                        break
                    except (ValueError, OSError, TypeError):
                        continue
            if matched:
                self._watchAutoArrivals.pop(str(path), None)
                distinct = tuple(dict.fromkeys(matched))
                if len(distinct) != 1:
                    self._widget.setStatus(
                        "Auto unsupported: new recording has ambiguous logical sources"
                    )
                    continue
                self.sigAutoAcquisitionsAdmitted.emit(distinct, recipe)

    @staticmethod
    def _lapse_entry_children(path: str) -> frozenset[str]:
        """Cheap listing, only for a pending multi-file lapse root directory."""
        try:
            import os
            with os.scandir(path) as entries:
                return frozenset(e.name for e in entries
                    if e.is_dir() or e.name.lower().endswith((".h5", ".hdf5")))
        except OSError:
            return frozenset()

    @QtCore.Slot(str)
    def _sessionPathFound(self, path: str) -> None:
        if self._sessionMode != "follow" or self._session is None or self._pathBelongsToResults(path):
            return
        path = str(path)
        if self._sessionKnowsRootEntry(path):
            return
        queue = self._queue_controller()
        if queue is not None and getattr(queue.autoPolicy, "enabled", False):
            recipe = queue.captureAutoRecipe()
            if recipe is not None:
                self._watchAutoArrivals[path] = recipe
        self._watchUnresolvedPaths.add(path)
        self._watchRetrySchedule.track(path)
        self._watchPendingPaths.add(path)
        self._watchReconcileTimer.start()
        self._syncWatchRetryTimer()

    @QtCore.Slot(str)
    def _sessionPathChanged(self, path: str) -> None:
        path = str(path)
        if (self._sessionMode != "follow" or self._session is None
                or self._pathBelongsToResults(path)):
            return
        finder = getattr(self, "_dataset_ids_for_watch_path", None)
        known_ids = finder(path) if callable(finder) else ()
        if known_ids:
            # Catalog discovery deliberately remains frozen for known growing
            # sources.  The existing watcher event is nevertheless the exact
            # trigger needed to refresh metadata-only acquisition progress.
            queue_probe = getattr(self, "_queue_acquisition_probes", None)
            if callable(queue_probe):
                queue_probe(known_ids, activity_observed=True)
            self._watchReconcileTimer.start()
        if path not in self._watchUnresolvedPaths:
            # Known growing Zarr/HDF5 entries are NOT reopened on every
            # metadata update. Only inspect directory children for a pending
            # multi-file logical acquisition whose next file might have arrived.
            queue_getter = getattr(self, "_queue_controller", None)
            queue = queue_getter() if callable(queue_getter) else None
            if queue is None or not Path(path).is_dir() or not any(
                str(job.dataset_id) == str(dataset.id)
                for job in getattr(queue.queue, "waiting", ())
                if str(getattr(job.origin, "value", job.origin)) == "auto"
                for dataset in self._session.datasets
                if getattr(dataset, "kind", "") == "time-lapse"
                if any(
                    str(ref.path).startswith(path + os.sep)
                    or str(ref.path) == path
                    for ref in getattr(dataset, "source_refs", ())
                )
            ):
                return
            children = self._lapse_entry_children(path)
            if children == self._watchLapseChildren.get(path):
                return
            self._watchLapseChildren[path] = children
        self._watchPendingPaths.add(path)
        self._watchReconcileTimer.start()

    @QtCore.Slot(str)
    def _sessionPathRemoved(self, path: str) -> None:
        if self._sessionMode != "follow" or self._session is None or self._pathBelongsToResults(path):
            return
        self._watchUnresolvedPaths.discard(str(path))
        self._watchAcquisitionRetryPaths.discard(str(path))
        self._watchRetrySchedule.discard(str(path))
        self._syncWatchRetryTimer()
        self._watchAutoArrivals.pop(str(path), None)
        self._watchLapseChildren.pop(str(path), None)
        # A removed physical member may belong to a logical lapse spanning
        # siblings.  Let the complete reconciler settle that safely.
        self._watchNeedsFullReconcile = True
        self._watchReconcileTimer.start()

    @QtCore.Slot()
    def _flushWatcherReconcile(self) -> None:
        session = self._session
        if session is None or self._sessionMode != "follow":
            return
        before_ids = {str(dataset.id) for dataset in session.datasets}
        changed = False
        pending = sorted(self._watchPendingPaths)
        acquisition_ids = set(getattr(self, "_acquisitionProbePending", set()))
        added_ids = ()
        admitted = set(pending)
        try:
            if self._watchNeedsFullReconcile:
                session.reconcile_all()
                # Do not discard *other* unresolved paths when an unrelated
                # entry disappears: they must retain their retry schedule and
                # original Auto-arrival token until they actually resolve.
                for path in tuple(self._watchUnresolvedPaths):
                    if not Path(path).exists():
                        self._watchUnresolvedPaths.discard(path)
                        self._watchRetrySchedule.discard(path)
                        self._watchAutoArrivals.pop(path, None)
                    elif self._sessionKnowsRootEntry(path):
                        self._watchUnresolvedPaths.discard(path)
                        self._watchRetrySchedule.discard(path)
                        admitted.add(path)
                changed = True
            else:
                for path in pending:
                    reconciled = session.reconcile_path(
                        path, reconcile_saved_results=False
                    )
                    changed = reconciled or changed
                    if reconciled:
                        self._watchUnresolvedPaths.discard(path)
                        self._watchRetrySchedule.discard(path)
                        acquisition_ids.update(self._dataset_ids_for_watch_path(path))
        except Exception:
            self._logger.exception("Could not reconcile Session filesystem update")
            return
        finally:
            self._watchPendingPaths.clear()
            self._watchNeedsFullReconcile = False
            self._syncWatchRetryTimer()
        if changed:
            added_ids = tuple(
                str(dataset.id) for dataset in session.datasets
                if str(dataset.id) not in before_ids
            )
            self._render()
            self.sigCatalogChanged.emit()
            if added_ids:
                self.sigDatasetsAdded.emit(added_ids)
            acquisition_ids.update(added_ids)
        queue_probe = getattr(self, "_queue_acquisition_probes", None)
        if callable(queue_probe):
            queue_probe(
                acquisition_ids,
                activity_observed=bool(pending or added_ids),
            )
            self._update_watcher_detail_paths()
            self._start_acquisition_probe()
        self._admit_resolved_arrivals(sorted(admitted))

    def openSession(self, raw_root, results_root) -> bool:
        # Build/refresh the candidate before touching the active Session. A bad
        # target path therefore cannot close a perfectly usable current Session.
        candidate = self._prepare_session(raw_root, results_root)
        if candidate is None:
            return False

        current = self._session
        changing = bool(
            current is not None
            and (
                not _same_path(current.raw_root, candidate.raw_root)
                or not _same_path(current.results_root, candidate.results_root)
            )
        )
        if changing and not self.closeSession(for_switch=True):
            return False

        self._activate_session(candidate)
        return True

    def closeSession(self, *, for_switch: bool = False) -> bool:
        session = self._session
        if session is None:
            return True

        if self._saveThread is not None or self._saveRequests:
            self._widget.setStatus(
                "A Session result is still being saved; finish saving before closing."
            )
            return False

        queue_controller = getattr(self, "_processingQueueController", None)
        if queue_controller is not None:
            can_close = getattr(queue_controller, "canCloseSession", None)
            if callable(can_close) and not can_close():
                self._widget.setStatus(
                    "A Session processing job is still running; finish or skip it before closing."
                )
                return False

        # Explicitly finishing a Session is the natural point to offer curation.
        # Switching Sessions stays lightweight and never suggests destructive cleanup.
        if not for_switch:
            favorite_count = sum(1 for dataset in session.datasets if dataset.favorite)
            nonfavorite_count = len(session.datasets) - favorite_count
            finish = self._widget.requestSessionFinishChoice(
                favorite_count=favorite_count, nonfavorite_count=nonfavorite_count
            )
            if finish == "cancel":
                return False
            if finish == "reconstructions":
                if not self._cleanup_nonfavorite_datasets(include_raw=False, ask_raw=False):
                    return False
            elif finish == "raw":
                if not self._cleanup_nonfavorite_datasets(include_raw=True, ask_raw=True):
                    return False

        associated = self._results_associated_with_session(session)
        choice = "keep"
        if associated:
            choice = self._widget.requestSessionCloseChoice(
                len(associated), switching=bool(for_switch)
            )
            if choice == "cancel":
                return False
            if choice == "close":
                # Session "Close" is non-destructive just like the per-result
                # Close action. Persist any reconstruction that only exists in
                # memory before removing its right-panel object.
                for result in associated:
                    if getattr(result, "_session_saved_path", None):
                        continue
                    dataset = session.dataset_for_result(result)
                    if dataset is None:
                        continue
                    try:
                        receipt = self._save_result(
                            result, dataset, str(getattr(result, "name", "reconstruction"))
                        )
                    except Exception as exc:
                        self._logger.exception(
                            "Could not preserve reconstruction before closing Session"
                        )
                        self._widget.setStatus(
                            f"Could not close Session; preserving a reconstruction failed: {exc}"
                        )
                        return False
                    self._registerSavedReceipt(
                        session,
                        dataset,
                        result,
                        receipt,
                        str(getattr(result, "name", "reconstruction")),
                    )
                for result in list(associated):
                    self._remove_loaded_result(result)

        name = session.raw_root.name
        if queue_controller is not None:
            session_closed = getattr(queue_controller, "sessionClosed", None)
            if callable(session_closed):
                session_closed()
        self._deactivate_session(
            status=None if for_switch else f"Session closed · {name}"
        )
        return True

    def refresh(self) -> None:
        if self._session is None:
            self.startSession()
            return
        before_ids = {str(dataset.id) for dataset in self._session.datasets}
        try:
            self._session.reconcile_all()
        except Exception as exc:
            self._logger.exception("Could not refresh processing session")
            self._widget.setStatus(f"Refresh failed: {exc}")
            return
        added_ids = tuple(
            str(dataset.id) for dataset in self._session.datasets
            if str(dataset.id) not in before_ids
        )
        self._render()
        self.sigCatalogChanged.emit()
        if added_ids:
            self.sigDatasetsAdded.emit(added_ids)

    # ------------------------------------------------------------------ UI

    def _rebuild_loaded_result_index(self) -> set[str]:
        """Rebuild loaded-result ownership once and return affected dataset ids."""
        previous_owners = dict(self._loadedResultOwners)
        by_dataset: dict[str, list[tuple[str, object]]] = {}
        owners: dict[int, str] = {}
        by_saved_path: dict[str, object] = {}
        session = self._session
        if session is not None:
            for display_name, result in self._commChannel.getAllResults():
                if not self._is_session_reconstruction(result):
                    continue
                owner = session.dataset_for_result(result)
                if owner is None:
                    continue
                rid = id(result)
                owners[rid] = str(owner.id)
                by_dataset.setdefault(str(owner.id), []).append(
                    (str(display_name or getattr(result, "name", "reconstruction")), result)
                )
                path = getattr(result, "_session_saved_path", None)
                if path:
                    try:
                        by_saved_path[str(Path(path).resolve())] = result
                    except Exception:
                        by_saved_path[str(path)] = result
        self._loadedResultsByDatasetId = by_dataset
        self._loadedResultOwners = owners
        self._loadedBySavedPath = by_saved_path
        affected: set[str] = set()
        for rid in set(previous_owners) | set(owners):
            before = previous_owners.get(rid)
            after = owners.get(rid)
            if before == after:
                continue
            if before:
                affected.add(before)
            if after:
                affected.add(after)
        return affected

    def _index_loaded_result(self, display_name: str, result) -> str | None:
        if self._session is None or not self._is_session_reconstruction(result):
            return None
        owner = self._session.dataset_for_result(result)
        if owner is None:
            return None
        dataset_id = str(owner.id)
        rid = id(result)
        existing_owner = self._loadedResultOwners.get(rid)
        if existing_owner is not None and existing_owner != dataset_id:
            self._unindex_loaded_result(rid)

        # Incremental workers publish immutable snapshots (a new Python object
        # per completed T), whereas the viewer replaces one entry per queue
        # job. Session can have indexed the first snapshot via a selection
        # signal before the final snapshot arrives. Retire the previous object
        # for *this job only*, rather than displaying it alongside the final
        # (possibly auto-saved) result. Distinct queue jobs remain distinct.
        job_id = getattr(result, "_improcess_queue_job_id", None)
        if job_id is not None:
            obsolete = [
                id(previous)
                for _name, previous in self._loadedResultsByDatasetId.get(dataset_id, ())
                if previous is not result
                and getattr(previous, "_improcess_queue_job_id", None) == job_id
            ]
            for previous_id in obsolete:
                self._unindex_loaded_result(previous_id)

        if self._loadedResultOwners.get(rid) == dataset_id:
            return dataset_id
        self._loadedResultOwners[rid] = dataset_id
        self._loadedResultsByDatasetId.setdefault(dataset_id, []).append(
            (str(display_name or getattr(result, "name", "reconstruction")), result)
        )
        path = getattr(result, "_session_saved_path", None)
        if path:
            try:
                self._loadedBySavedPath[str(Path(path).resolve())] = result
            except Exception:
                self._loadedBySavedPath[str(path)] = result
        return dataset_id

    def _unindex_loaded_result(self, rid: int) -> str | None:
        dataset_id = self._loadedResultOwners.pop(int(rid), None)
        if dataset_id is None:
            return None
        items = self._loadedResultsByDatasetId.get(dataset_id, [])
        removed_results = [result for _name, result in items if id(result) == int(rid)]
        items = [item for item in items if id(item[1]) != int(rid)]
        if items:
            self._loadedResultsByDatasetId[dataset_id] = items
        else:
            self._loadedResultsByDatasetId.pop(dataset_id, None)
        for result in removed_results:
            path = getattr(result, "_session_saved_path", None)
            if not path:
                continue
            try:
                self._loadedBySavedPath.pop(str(Path(path).resolve()), None)
            except Exception:
                self._loadedBySavedPath.pop(str(path), None)
        return dataset_id

    def _loaded_by_saved_path(self) -> dict[str, object]:
        return self._loadedBySavedPath

    @staticmethod
    def _is_session_reconstruction(result) -> bool:
        """Whether a result is a reconstruction artifact Session may own.

        A normal reconstruction is a ``reconstruct`` node.  Reconstructing
        several MultiData members with consolidation produces a final
        ``consolidate`` node whose ancestors are those reconstruction nodes;
        it is also a reconstruction artifact for Session purposes.  Generic
        downstream processing nodes remain excluded.
        """
        if result_role_for_result(result) == "inspection":
            return False
        node = output_node(result)
        return isinstance(node, dict) and node.get("op") in {"reconstruct", "consolidate"}

    def _loaded_reconstructions_for_dataset(self, dataset) -> list[tuple[str, object]]:
        """Session reconstruction objects currently present in the right-hand list."""
        if self._session is None or dataset is None:
            return []
        return list(self._loadedResultsByDatasetId.get(str(dataset.id), ()))

    @staticmethod
    def _loaded_key(result) -> str:
        uid = getattr(result, "result_uid", None)
        return f"loaded:{uid or id(result)}"

    def _reconstruction_entries(self, dataset):
        """Session rows for one raw dataset.

        Saved and currently loaded reconstruction objects are merged here, so
        Session immediately reflects a reconstruction even when auto-save is
        disabled.  Downstream processor results are intentionally excluded.
        """
        loaded_by_saved = self._loaded_by_saved_path()
        loaded_for_dataset = self._loaded_reconstructions_for_dataset(dataset)
        loaded_ids = {id(result) for _name, result in loaded_for_dataset}
        entries = []
        represented_loaded: set[int] = set()

        for saved in dataset.saved_reconstructions:
            resolved = str(Path(saved.path).resolve())
            result = loaded_by_saved.get(resolved)
            if result is not None and id(result) in loaded_ids:
                represented_loaded.add(id(result))
            entries.append({
                "key": f"saved:{resolved}",
                "name": saved.name,
                "tooltip": saved.tooltip or saved.name,
                "label": saved.name,
                "saved": saved,
                "loaded": result,
            })

        for display_name, result in loaded_for_dataset:
            if id(result) in represented_loaded:
                continue
            graph = graph_of(result)
            metadata = getattr(result, "metadata", None) or {}
            assembly = metadata.get("result_assembly") if isinstance(metadata, dict) else None
            session_name, _object_name, tooltip = reconstruction_presentation(
                graph, artifact={"result_assembly": assembly},
                fallback_name=str(display_name or getattr(result, "name", "reconstruction")),
            )
            entries.append({
                "key": self._loaded_key(result),
                "name": session_name,
                "tooltip": tooltip or session_name,
                "label": session_name,
                "saved": None,
                "loaded": result,
            })
        return entries

    @staticmethod
    def _dataset_timestamp(dataset) -> float | None:
        """Best lightweight acquisition timestamp for Session ordering.

        For multi-file time lapses use the newest member so newly arriving
        timepoints keep the logical acquisition near the top in Latest mode.
        No pixel data is opened.
        """
        values = []
        for ref in getattr(dataset, "source_refs", ()) or ():
            try:
                values.append(os.path.getmtime(ref.path))
            except OSError:
                continue
        if not values:
            try:
                values.append(os.path.getmtime(dataset.primary_path))
            except OSError:
                pass
        return max(values) if values else None

    @staticmethod
    def _dataset_search_text(dataset) -> str:
        parts = [
            str(getattr(dataset, "label", "") or ""),
            str(getattr(dataset, "primary_path", "") or ""),
            str(getattr(dataset, "primary_dataset", "") or ""),
        ]
        for ref in getattr(dataset, "source_refs", ()) or ():
            parts.append(str(getattr(ref, "path", "") or ""))
            parts.append(str(getattr(ref, "dataset", "") or ""))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _format_bytes(value) -> str:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return ""
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(max(0, value))
        for unit in units:
            if amount < 1024.0 or unit == units[-1]:
                if unit == "B":
                    return f"{int(amount)} {unit}"
                return f"{amount:.1f} {unit}" if amount < 10 else f"{amount:.0f} {unit}"
            amount /= 1024.0
        return ""

    @staticmethod
    def _dataset_source_tooltip(dataset) -> str:
        """Compact source summary: show shared path once, then datasets/items."""
        refs = list(getattr(dataset, "source_refs", ()) or ())
        if not refs:
            path = str(getattr(dataset, "primary_path", "") or "")
            dataset_name = getattr(dataset, "primary_dataset", None)
            lines = [f"Path: {path}"] if path else []
            if dataset_name not in (None, ""):
                lines.append(f"Dataset: {dataset_name}")
            return "\n".join(lines)

        grouped: dict[str, list[str]] = {}
        for ref in refs:
            path = str(getattr(ref, "path", "") or "")
            name = str(getattr(ref, "dataset", "") or "")
            if not path:
                continue
            grouped.setdefault(path, [])
            if name and name not in grouped[path]:
                grouped[path].append(name)

        if not grouped:
            return ""
        if len(grouped) == 1:
            path, datasets = next(iter(grouped.items()))
            lines = [f"Path: {path}"]
            if datasets:
                lines.append("Datasets:")
                lines.extend(f"  {name}" for name in datasets)
            return "\n".join(lines)

        paths = list(grouped)
        try:
            common = os.path.commonpath(paths)
        except (ValueError, OSError):
            common = ""
        lines = [f"Folder: {common}"] if common else []
        lines.append("Sources:")
        for path, datasets in grouped.items():
            display_path = path
            if common:
                try:
                    display_path = os.path.relpath(path, common)
                except (ValueError, OSError):
                    pass
            if datasets:
                lines.extend(f"  {display_path} :: {name}" for name in datasets)
            else:
                lines.append(f"  {display_path}")
        return "\n".join(lines)

    @classmethod
    def _dataset_info(cls, dataset) -> tuple[str, str]:
        bits = []
        tooltip = []
        dimension_tag = str(getattr(dataset, "dimension_tag", "") or "")
        spatial_label = str(getattr(dataset, "spatial_display_label", "") or "")
        if spatial_label:
            bits.append(spatial_label)
        if dimension_tag:
            tooltip.append(f"Dimensions: {dimension_tag}")
        if dataset.kind == "time-lapse" and dataset.planned_timepoints:
            bits.append(f"T={dataset.planned_timepoints}")
            tooltip.append(f"Timepoints: {dataset.planned_timepoints}")
        decoded = cls._format_bytes(getattr(dataset, "decoded_bytes", None))
        if decoded:
            bits.append(decoded)
            tooltip.append(f"Estimated decoded size: {decoded}")
        if getattr(dataset, "raw_deleted", False):
            tooltip.append("Raw acquisition has been removed; saved reconstructions are retained")
        return " · ".join(bits), "\n".join(tooltip)

    def _sync_current_dataset(self, data_obj) -> None:
        self._currentDatasetId = None
        if self._session is None or data_obj is None:
            return
        path = getattr(data_obj, "dataPath", None)
        dataset_name = getattr(data_obj, "datasetName", None)
        for dataset in self._session.datasets:
            if dataset.matches_source(path, dataset_name):
                self._currentDatasetId = dataset.id
                return

    def _currentDataChanged(self, data_obj=None) -> None:
        self._sync_current_dataset(data_obj)
        if self._session is not None:
            self._render()

    def setFollowActiveReconstruction(self, enabled: bool) -> None:
        self._followActiveReconstruction = bool(enabled)
        self._widget.setFollowActiveReconstruction(self._followActiveReconstruction)
        if not self._followActiveReconstruction or self._session is None:
            return
        current = self._reconstruction.getActiveResult() if self._reconstruction is not None else None
        self._currentResultChanged(current)

    def _currentResultChanged(self, result) -> None:
        """Follow the active right-panel result back to its Session source."""
        if not self._followActiveReconstruction or self._session is None or result is None:
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return

        # Keep the exact reconstruction identity as well as the raw-dataset link.
        # This makes an already-expanded Reconstructions section follow the active
        # object on the right, including the ambiguous multi-reconstruction case.
        entry = next(
            (item for item in self._reconstruction_entries(dataset) if item["loaded"] is result),
            None,
        )
        selected_key = str(entry["key"]) if entry is not None else None

        # Deliberately replace Session selection, matching an ordinary user click.
        # Guard the resulting currentItemChanged signal so right -> left following
        # does not immediately trigger the left -> right navigation path again.
        self._syncingFromActiveReconstruction = True
        try:
            self._widget.selectDataset(dataset.id)
        finally:
            self._syncingFromActiveReconstruction = False

        # A filter/search may hide the source row. Keep controller context aligned
        # without changing the user's filter unexpectedly.  In either case restore
        # the exact reconstruction selection after datasetSelected() has run.
        self._selectedDatasetId = dataset.id
        self._selectedReconKey = selected_key
        # If the active result changes between two reconstructions of the same
        # raw dataset, the raw row itself does not change.  Clear the previous
        # reconstruction selection so the exact active reconstruction becomes the sole
        # selected row after rerendering.
        self._widget.clearReconstructionSelection()
        self._renderSelected()

    def _dataset_entry(self, dataset, *, queue_state: str | None = None) -> dict:
        recon_entries = self._reconstruction_entries(dataset)
        total_count = len(recon_entries)
        loaded_count = sum(1 for item in recon_entries if item["loaded"] is not None)
        acquisition = getattr(self, "_acquisitionProgress", {}).get(str(dataset.id))
        acquisition_visible = bool(
            acquisition is not None
            and acquisition.status in {"acquiring", "incomplete"}
        )
        if acquisition_visible:
            status = self._acquisition_progress_label(acquisition)
            # Keep the row's filter/style state truthful even though the
            # acquisition text intentionally outranks queue presentation.
            state = (
                "processing" if queue_state == "running"
                else "queued" if queue_state == "waiting"
                else "raw"
            )
        elif self._loadingDatasetId == dataset.id:
            status = "Loading…"
            state = "loading"
        elif queue_state == "running":
            queue_progress = getattr(self, "_queueProgressByDatasetId", {}).get(
                str(dataset.id), {}
            )
            progress_label = self._queue_progress_label(queue_progress)
            status = "▶ Processing…"
            if progress_label:
                status += f" {progress_label}"
            state = "processing"
        elif queue_state == "waiting":
            status = "… Queued"
            state = "queued"
        elif total_count == 0:
            status = "— No result"
            state = "raw"
        else:
            status = f"✓ Result ({total_count})"
            state = "reconstructed"
        if getattr(dataset, "raw_deleted", False):
            status += " · data deleted"
        info, info_tooltip = self._dataset_info(dataset)
        search = self._dataset_search_text(dataset)
        tooltip = self._dataset_source_tooltip(dataset)
        if info_tooltip:
            tooltip = (tooltip + "\n" if tooltip else "") + info_tooltip
        acquisition_tooltip = self._acquisition_tooltip(acquisition)
        if acquisition_tooltip:
            tooltip = (tooltip + "\n" if tooltip else "") + acquisition_tooltip
        processing_tooltip = self._processing_tooltip(
            dataset, queue_state=queue_state, total_count=total_count
        )
        if processing_tooltip:
            tooltip = (tooltip + "\n" if tooltip else "") + processing_tooltip
        return {
            "id": dataset.id,
            "label": dataset.label,
            "info": info,
            "info_tooltip": info_tooltip,
            "dimension": str(getattr(dataset, "dimension_tag", "") or ""),
            "dimension_label": str(getattr(dataset, "dimension_filter_label", "") or ""),
            "status": status,
            "state": state,
            "timestamp": self._dataset_timestamp(dataset),
            "search": search,
            "tooltip": tooltip,
            "current": dataset.id == self._currentDatasetId,
            "raw_deleted": bool(getattr(dataset, "raw_deleted", False)),
            "favorite": bool(getattr(dataset, "favorite", False)),
            "auto_process_ignored": bool(
                getattr(dataset, "auto_process_ignored", False)
            ),
            "kind": str(getattr(dataset, "kind", "") or ""),
            "planned_timepoints": int(getattr(dataset, "planned_timepoints", 0) or 0),
            "time_lapse_identity": logical_time_lapse_identity(dataset),
            "saved_count": len(dataset.saved_reconstructions),
            "panel_count": loaded_count,
            "result_count": total_count,
            "panel_link": bool(loaded_count),
        }

    @staticmethod
    def _acquisition_progress_label(progress) -> str:
        if progress is None or progress.status not in {"acquiring", "incomplete"}:
            return ""
        planned = int(progress.planned_timepoints or 0)
        prefix = "Acquiring" if progress.status == "acquiring" else "Incomplete"
        if planned > 1:
            return f"{prefix} {progress.completed_count}/{planned}"
        return prefix

    @staticmethod
    def _queue_progress_label(progress: dict | None) -> str:
        text = str((progress or {}).get("status") or "")
        import re
        match = re.search(
            r"(?:Timepoint|Item)\s+(\d+)\s*/\s*(\d+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return ""

    @staticmethod
    def _acquisition_tooltip(progress) -> str:
        if progress is None:
            return ""
        planned = int(progress.planned_timepoints or 0)
        if planned > 1:
            label = f"{progress.completed_count}/{planned} timepoints complete"
        elif progress.status == "acquiring":
            label = "Acquisition in progress"
        elif progress.status == "complete":
            label = "Complete"
        else:
            label = "Completion not confirmed"
        reason = str(progress.reason or "").strip()
        return "Acquisition: " + label + (f"\n  {reason}" if reason else "")

    @staticmethod
    def _processing_tooltip(dataset, *, queue_state: str | None, total_count: int) -> str:
        if queue_state == "running":
            return "Processing: Active reconstruction"
        if queue_state == "waiting":
            return "Processing: Queued"
        if total_count:
            return f"Processing: {total_count} result{'s' if total_count != 1 else ''}"
        return ""

    def _renderDataset(
        self, dataset_id: str, *, render_selected: bool = False, queue_state: str | None = None
    ) -> None:
        session = self._session
        if session is None:
            return
        dataset = session.dataset_by_id(str(dataset_id))
        if dataset is None:
            return
        if queue_state is None:
            queue_state = self._queue_states().get(str(dataset.id))
        entry = self._dataset_entry(dataset, queue_state=queue_state)
        updater = getattr(self._widget, "updateDataset", None)
        if callable(updater):
            updater(entry)
        else:
            self._render()
            return
        if render_selected:
            self._renderSelected()

    def _render(self) -> None:
        session = self._session
        if session is None:
            self._widget.setDatasets([])
            self._renderSelected()
            return
        queue_states = self._queue_states()
        entries = [
            self._dataset_entry(
                dataset, queue_state=queue_states.get(str(dataset.id))
            )
            for dataset in session.datasets
        ]
        self._widget.setDatasets(entries)
        if self._selectedDatasetId and session.dataset_by_id(self._selectedDatasetId) is None:
            self._selectedDatasetId = None
        self._renderSelected()

    def _renderSelected(self) -> None:
        session = self._session
        dataset = session.dataset_by_id(self._selectedDatasetId) if session else None
        if dataset is None:
            self._widget.setSelectedDataset("", "")
            self._widget.setSavedReconstructions([])
            return

        entries = self._reconstruction_entries(dataset)
        loaded_count = sum(1 for item in entries if item["loaded"] is not None)
        saved_count = len(dataset.saved_reconstructions)
        total_count = len(entries)
        summary = f"{total_count} results · {loaded_count} in panel · {saved_count} saved"
        self._widget.setSelectedDataset(dataset.label, summary)

        selected = self._selectedReconKey
        if selected and not any(item["key"] == selected for item in entries):
            selected = None
            self._selectedReconKey = None
        self._widget.setSavedReconstructions(
            [
                {
                    "key": item["key"],
                    "name": item.get("name", item["label"]),
                    "loaded": item["loaded"] is not None,
                    "saved": item["saved"] is not None,
                    "tooltip": item.get("tooltip", item.get("name", "")),
                }
                for item in entries
            ],
            selected,
        )

    def datasetSelected(self, dataset_id) -> None:
        self._selectedDatasetId = str(dataset_id) if dataset_id else None
        self._selectedReconKey = None

        if (
            self._syncingFromActiveReconstruction
            or self._session is None
            or self._reconstruction is None
            or not self._selectedDatasetId
        ):
            self._renderSelected()
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            self._renderSelected()
            return

        loaded = [result for _name, result in self._loaded_reconstructions_for_dataset(dataset)]
        if len(loaded) == 1:
            # The common one-reconstruction case is unambiguous: selecting the
            # raw Session row also selects its reconstruction object on the right.
            entry = next(
                (
                    item
                    for item in self._reconstruction_entries(dataset)
                    if item["loaded"] is loaded[0]
                ),
                None,
            )
            self._selectedReconKey = str(entry["key"]) if entry is not None else None

        self._renderSelected()

        if len(loaded) == 1:
            self._reconstruction.selectResult(loaded[0])
        elif len(loaded) > 1:
            # Do not choose arbitrarily.  The reconstruction list is the explicit
            # ambiguity resolver and already supports selecting the exact object.
            self._widget.setReconstructionsExpanded(True)

    def reconstructionSelected(self, entry_key) -> None:
        self._selectedReconKey = str(entry_key) if entry_key else None
        entry = self._selected_reconstruction_entry()
        if entry is not None and entry["loaded"] is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(entry["loaded"])

    def reuseReconstructionCalibration(self, entry_key, mode: str) -> None:
        entry = self._reconstruction_entry_by_key(str(entry_key or ""))
        if entry is None:
            return
        snapshot = None
        result = entry.get("loaded")
        if result is not None:
            metadata = dict(getattr(result, "metadata", {}) or {})
            snapshot = metadata.get("monalisa_calibration")
            if not isinstance(snapshot, dict):
                snapshot = dict(getattr(result, "params", {}) or {}).get("calibration_snapshot")
        if not isinstance(snapshot, dict) and entry.get("saved") is not None:
            try:
                metadata = read_saved_result_metadata(entry["saved"].path)
                snapshot = metadata.get("monalisa_calibration")
            except Exception as exc:
                self._logger.warning(f"Could not read saved reconstruction calibration: {exc}")

        if not isinstance(snapshot, dict) or not snapshot:
            QtWidgets.QMessageBox.information(
                self._widget, "Reuse MoNaLISA calibration",
                "This reconstruction does not contain a MoNaLISA calibration snapshot.",
            )
            return
        if isinstance(snapshot.get("per_data_item"), list):
            QtWidgets.QMessageBox.information(
                self._widget, "Reuse MoNaLISA calibration",
                "This consolidated reconstruction contains different calibration values "
                "for its data items, so there is no single calibration to reuse.",
            )
            return

        manager = getattr(self._main, "reconstructorManager", None)
        reuse = getattr(manager, "reuseMonalisaCalibration", None)
        if not callable(reuse):
            QtWidgets.QMessageBox.warning(
                self._widget, "Reuse MoNaLISA calibration",
                "The MoNaLISA calibration controls are unavailable.",
            )
            return
        try:
            message = reuse(
                snapshot, mode=str(mode), source_label=str(entry.get("name") or "reconstruction")
            )
        except Exception as exc:
            self._logger.warning(f"Could not reuse MoNaLISA calibration: {exc}")
            QtWidgets.QMessageBox.warning(
                self._widget, "Reuse MoNaLISA calibration", str(exc)
            )
            return
        if message:
            self._widget.setStatus(str(message))

    def _reconstruction_entry_by_key(self, key):
        if self._session is None or not self._selectedDatasetId or not key:
            return None
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return None
        key = str(key)
        return next(
            (item for item in self._reconstruction_entries(dataset) if item["key"] == key),
            None,
        )

    def _selected_reconstruction_entry(self):
        return self._reconstruction_entry_by_key(self._selectedReconKey)

    def _selected_saved(self):
        entry = self._selected_reconstruction_entry()
        return entry["saved"] if entry is not None else None

    # --------------------------------------------------------------- actions

    @staticmethod
    def _viewer_plugin_id(dataset) -> str:
        """Return the inspection plugin hidden behind ``Open in viewer``."""
        return (
            "time-lapse"
            if getattr(dataset, "kind", "") == "time-lapse"
            else "view-only"
        )

    def _viewer_data_obj(self, dataset):
        """Build the lazy source consumed by the inspection plugin.

        This deliberately does not make the source Current Data.  Inspection
        must not change the user's selected processing reconstructor or route
        through the processing queue.
        """
        if getattr(dataset, "kind", "") == "time-lapse":
            index = discover_time_lapse(
                dataset.primary_path, dataset.primary_dataset
            )
            return DataObj.fromMetadataSource(
                index.name,
                index.anchor.path,
                TIME_LAPSE_SOURCE_KIND,
                index,
                originalPath=dataset.primary_path,
            )

        resolved = self._multidata_source(
            dataset.primary_path, dataset.primary_dataset
        )
        if resolved is None:
            raise ValueError(
                "Could not determine one image dataset to open in the viewer"
            )
        source_path, source_dataset = resolved
        data_obj = DataObj(
            Path(source_path).name, source_dataset, path=source_path
        )
        # View-only can retain the lazy source directly instead of decoding the
        # complete acquisition. ReconstructionViewController releases this
        # backing object when the inspection result is closed.
        data_obj.checkAndOpenData()
        return data_obj

    def openSelectedInViewer(self) -> None:
        """Open the selected Session dataset as an inspection result.

        Normal datasets use ``view-only``; logical time lapses use
        ``time-lapse``.  The active reconstructor is intentionally untouched.
        """
        if self._session is None or self._main is None:
            return
        dataset = self._current_selected_dataset()
        if dataset is None or getattr(dataset, "raw_deleted", False):
            return

        manager = getattr(self._main, "reconstructorManager", None)
        opener = getattr(manager, "openInspection", None)
        if not callable(opener):
            self._logger.warning("Inspection viewer is unavailable")
            return

        plugin_id = self._viewer_plugin_id(dataset)
        self._widget.setStatus(f"Opening {dataset.label} in viewer…")
        try:
            data_obj = self._viewer_data_obj(dataset)
            outcome = opener(plugin_id, data_obj)
        except Exception as exc:
            self._logger.warning(
                f"Could not open {dataset.label} in viewer: {exc}", exc_info=True
            )
            detail = str(exc).strip()
            self._widget.setStatus(
                f"Could not open {dataset.label}: {detail}"
                if detail
                else f"Could not open {dataset.label} in viewer"
            )
            return
        if outcome == "ready":
            self._widget.setStatus(f"Opened {dataset.label} in viewer")

    def loadSelectedRaw(self) -> None:
        if self._session is None or self._fileIO is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None or getattr(dataset, "raw_deleted", False):
            return
        # Selection stays metadata-only; double click / Load raw is the explicit
        # transition into the familiar FileIO materialisation path.
        self._loadingDatasetId = dataset.id
        self._widget.setStatus(f"Loading {dataset.label}…")
        self._render()
        QtWidgets.QApplication.processEvents()
        try:
            if getattr(dataset, "kind", "") == "time-lapse":
                # Session timelapses are logical datasets just like Quick Load
                # timelapses. Double-clicking opens the lazy lapse navigator
                # directly instead of falling back to the legacy dataset picker
                # / Multi-data routing.
                data_obj = self._viewer_data_obj(dataset)
                outcome = self._fileIO.setLoadedAsCurrent(data_obj)
            else:
                outcome = self._fileIO._loadFromPath(
                    dataset.primary_path, prefer_as_current=True
                )
            if outcome != "current":
                self._widget.setStatus(f"Could not load {dataset.label}")
            else:
                self._widget.setStatus(f"Loaded {dataset.label}")
        finally:
            self._loadingDatasetId = None
            self._render()

    def _selected_dataset_ids(self) -> list[str]:
        ids = list(self._widget.selectedDatasetIds())
        if not ids and self._selectedDatasetId:
            ids = [self._selectedDatasetId]
        return list(dict.fromkeys(str(item) for item in ids if item))

    @staticmethod
    def _multidata_source(
        path: str, dataset_name: str | None
    ) -> tuple[str, str | None] | None:
        """Return a concrete ordinary-image source for MultiData.

        Multi-file lapse discovery can leave the dataset name unset for
        sibling files when each file contains exactly one image dataset.  A
        MultiData ``DataObj`` should still carry that real dataset name so its
        provenance later matches the Session source precisely.  Never guess
        when a container has several choices.
        """
        if dataset_name not in (None, ""):
            return str(path), str(dataset_name)
        try:
            names = list(DataObj.getDatasetNames(str(path)))
        except Exception:
            return None
        if len(names) != 1:
            return None
        return str(path), str(names[0])

    def loadSelectedAsMultiData(self) -> None:
        if self._session is None or self._fileIO is None:
            return
        datasets = [
            self._session.dataset_by_id(dataset_id)
            for dataset_id in self._selected_dataset_ids()
        ]
        datasets = [d for d in datasets if d is not None and not d.raw_deleted]
        added = 0
        skipped = 0
        for dataset in datasets:
            if dataset.kind == "time-lapse":
                sources = [
                    (ref.path, ref.dataset)
                    for ref in dataset.source_refs
                ]
            else:
                sources = [(dataset.primary_path, dataset.primary_dataset)]

            for source_path, source_dataset in sources:
                resolved = self._multidata_source(source_path, source_dataset)
                if resolved is None:
                    skipped += 1
                    self._logger.warning(
                        "Could not add Session source to MultiData without "
                        f"guessing a dataset: {source_path}::{source_dataset}"
                    )
                    continue
                resolved_path, resolved_dataset = resolved
                self._fileIO.multiDataFrameController.makeAndAddDataObj(
                    Path(resolved_path).name,
                    resolved_dataset,
                    path=resolved_path,
                )
                added += 1
        if added:
            self._fileIO._widget.raiseMultiDataDock()
        message = f"Added {added} dataset{'s' if added != 1 else ''} to Multi-data"
        if skipped:
            message += f" · skipped {skipped} ambiguous source{'s' if skipped != 1 else ''}"
        self._widget.setStatus(message)

    def _current_selected_dataset(self):
        if self._session is None:
            return None
        dataset_id = self._widget.currentDatasetId() or self._selectedDatasetId
        return self._session.dataset_by_id(dataset_id)

    @staticmethod
    def _reveal_path(path: str | Path) -> None:
        path = Path(path)
        target = path if path.is_dir() else path.parent
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    def revealSelectedRaw(self) -> None:
        dataset = self._current_selected_dataset()
        if dataset is not None:
            self._reveal_path(dataset.primary_path)

    def copySelectedRawPath(self) -> None:
        dataset = self._current_selected_dataset()
        if dataset is not None:
            self._widget.copyText(dataset.primary_path)
            self._widget.setStatus("Raw-data path copied")

    def setDatasetFavorite(self, dataset_id, favorite: bool) -> None:
        if self._session is None or not dataset_id:
            return
        self._session.set_favorite([str(dataset_id)], bool(favorite))
        self._render()

    def setSelectedDatasetsFavorite(self, favorite: bool) -> None:
        if self._session is None:
            return
        dataset_ids = self._selected_dataset_ids()
        if not dataset_ids:
            return
        self._session.set_favorite(dataset_ids, bool(favorite))
        self._widget.setStatus(
            f"{'Favorited' if favorite else 'Removed favorite from'} "
            f"{len(dataset_ids)} dataset{'s' if len(dataset_ids) != 1 else ''}"
        )
        self._render()

    def setSelectedDatasetsAutoIgnored(self, ignored: bool) -> None:
        """Legacy slot: the old persisted exclusion UI is retired in M3B."""
        return

    def _nonfavorite_datasets(self):
        if self._session is None:
            return []
        return [dataset for dataset in self._session.datasets if not dataset.favorite]

    def _reconstruction_count_for_datasets(self, datasets) -> int:
        return sum(len(self._reconstruction_entries(dataset)) for dataset in datasets)

    def _close_reconstructions_for_datasets(self, datasets) -> bool:
        """Non-destructive Close: persist in-memory-only results before removing them."""
        if self._session is None:
            return True
        results = []
        seen = set()
        for dataset in datasets:
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) in seen:
                    continue
                seen.add(id(result))
                results.append((dataset, result))

        # Preserve every unsaved result before closing any, so one failed save
        # cannot leave the operation half destructive.
        for dataset, result in results:
            if getattr(result, "_session_saved_path", None):
                continue
            try:
                receipt = self._save_result(
                    result, dataset, str(getattr(result, "name", "reconstruction"))
                )
            except Exception as exc:
                self._logger.exception("Could not preserve reconstruction before closing")
                self._widget.setStatus(f"Could not close reconstructions: {exc}")
                return False
            self._registerSavedReceipt(
                self._session,
                dataset,
                result,
                receipt,
                str(getattr(result, "name", "reconstruction")),
            )
        for _dataset, result in results:
            self._remove_loaded_result(result)
        return True

    def _raw_cleanup_plan(self, datasets):
        if self._session is None:
            return [], [], []
        return self._session.raw_cleanup_plan(dataset.id for dataset in datasets)

    def _discard_reconstructions_for_datasets(self, datasets) -> None:
        """Destructive cleanup: remove loaded objects and trash saved artifacts."""
        if self._session is None:
            return
        seen_results = set()
        for dataset in list(datasets):
            for entry in list(self._reconstruction_entries(dataset)):
                result = entry.get("loaded")
                if result is not None and id(result) not in seen_results:
                    seen_results.add(id(result))
                    self._remove_loaded_result(result)
                saved = entry.get("saved")
                if saved is not None:
                    try:
                        self._session.trash_reconstruction(dataset.id, saved.path)
                    except Exception:
                        self._logger.exception("Could not move non-favorite reconstruction to Session trash")
        # A deleted-raw tombstone with no reconstruction left represents
        # nothing after cleanup; remove it from the manifest as well.
        empty_deleted = [
            dataset.id for dataset in datasets
            if dataset.raw_deleted and not dataset.saved_reconstructions
        ]
        if empty_deleted:
            self._session.forget_datasets(empty_deleted)

    def _cleanup_nonfavorite_datasets(self, *, include_raw: bool, ask_raw: bool) -> bool:
        if self._session is None:
            return True
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets to clean up")
            return True

        deletable, raw_paths, skipped = self._raw_cleanup_plan(datasets)
        physical_deletable = [dataset for dataset in deletable if not dataset.raw_deleted]
        if include_raw and ask_raw and raw_paths:
            if not self._widget.requestRawCleanupConfirmation(
                dataset_count=len(physical_deletable),
                raw_file_count=len(raw_paths),
                skipped_count=len(skipped),
            ):
                return False

        # Cleanup intentionally does not preserve unsaved non-favorite results.
        self._discard_reconstructions_for_datasets(datasets)

        if include_raw:
            # Re-resolve IDs after reconstruction cleanup may have removed raw-deleted
            # tombstones from the manifest. Physical paths are from the safe plan above.
            deletable_ids = [dataset.id for dataset in physical_deletable]
            if raw_paths:
                self._session.mark_raw_deleted(deletable_ids)
                try:
                    release = getattr(self._fileIO, "releasePaths", None)
                    if callable(release):
                        release(raw_paths)
                    from send2trash import send2trash
                    for path in raw_paths:
                        source = Path(path)
                        if source.exists():
                            send2trash(str(source))
                except Exception as exc:
                    self._logger.exception("Could not clean up non-favorite raw data")
                    self._widget.setStatus(f"Raw-data cleanup failed: {exc}")
                    self._session.refresh()
                    self._render()
                    return False
            forget_ids = [dataset.id for dataset in deletable]
            if forget_ids:
                self._session.forget_datasets(forget_ids)

        self._session.refresh()
        if include_raw:
            message = "Cleaned up non-favorite reconstructions and raw data"
            if skipped:
                message += (
                    f" · kept {len(skipped)} dataset{'s' if len(skipped) != 1 else ''} "
                    "whose raw files are shared with kept data"
                )
        else:
            message = "Moved non-favorite reconstructions to Session trash"
        self._widget.setStatus(message)
        self._selectedReconKey = None
        self._render()
        return True

    def cleanupNonFavorites(self) -> None:
        if self._session is None:
            return
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets to clean up")
            return
        choice = self._widget.requestNonFavoriteCleanupChoice(
            len(datasets), self._reconstruction_count_for_datasets(datasets)
        )
        if choice == "cancel":
            return
        self._cleanup_nonfavorite_datasets(
            include_raw=(choice == "raw"), ask_raw=(choice == "raw")
        )

    def _affected_datasets_for_raw_delete(self, dataset_ids: list[str]):
        if self._session is None:
            return [], []
        selected = [self._session.dataset_by_id(item) for item in dataset_ids]
        selected = [item for item in selected if item is not None and not item.raw_deleted]
        paths = {
            os.path.normcase(os.path.abspath(ref.path))
            for dataset in selected
            for ref in dataset.source_refs
            if ref.path
        }
        affected = [
            dataset
            for dataset in self._session.datasets
            if not dataset.raw_deleted
            and any(
                os.path.normcase(os.path.abspath(ref.path)) in paths
                for ref in dataset.source_refs
                if ref.path
            )
        ]
        return affected, sorted(paths)

    def deleteSelectedRaw(self) -> None:
        if self._session is None:
            return
        affected, raw_paths = self._affected_datasets_for_raw_delete(
            self._selected_dataset_ids()
        )
        if not affected or not raw_paths:
            return
        reconstruction_entries = {
            dataset.id: list(self._reconstruction_entries(dataset))
            for dataset in affected
        }
        recon_count = sum(len(entries) for entries in reconstruction_entries.values())
        names = ", ".join(dataset.label for dataset in affected[:3])
        if len(affected) > 3:
            names += f" and {len(affected) - 3} more"

        box = QtWidgets.QMessageBox(self._widget)
        box.setWindowTitle("Delete raw data")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(
            f"Move raw data for {len(affected)} dataset{'s' if len(affected) != 1 else ''} to the system Trash?"
        )
        box.setInformativeText(names)
        delete_recons_btn = None
        if recon_count:
            delete_recons_btn = box.addButton(
                f"Delete raw + {recon_count} reconstruction{'s' if recon_count != 1 else ''}",
                QtWidgets.QMessageBox.DestructiveRole,
            )
            keep_recons_btn = box.addButton(
                "Delete raw only, keep reconstructions",
                QtWidgets.QMessageBox.ActionRole,
            )
        else:
            keep_recons_btn = box.addButton(
                "Delete raw data", QtWidgets.QMessageBox.DestructiveRole
            )
        cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return
        delete_recons = bool(delete_recons_btn is not None and clicked is delete_recons_btn)

        # "Keep reconstructions" must also preserve results that currently
        # exist only in the reconstruction-object list. Persist those before
        # the raw source disappears.
        if recon_count and not delete_recons:
            for dataset in affected:
                for entry in reconstruction_entries.get(dataset.id, ()):
                    if entry["saved"] is not None or entry["loaded"] is None:
                        continue
                    try:
                        receipt = self._save_result(
                            entry["loaded"],
                            dataset,
                            str(getattr(entry["loaded"], "name", "reconstruction")),
                        )
                        self._registerSavedReceipt(
                            self._session,
                            dataset,
                            entry["loaded"],
                            receipt,
                            str(getattr(entry["loaded"], "name", "reconstruction")),
                        )
                    except Exception as exc:
                        self._logger.exception("Could not preserve reconstruction before raw deletion")
                        self._widget.setStatus(f"Could not preserve reconstruction: {exc}")
                        return

        affected_ids = [dataset.id for dataset in affected]
        # Persist the tombstone before touching disk. If an OS-trash operation
        # fails halfway, refresh can still retain the logical Session entry.
        self._session.mark_raw_deleted(affected_ids)
        try:
            release = getattr(self._fileIO, "releasePaths", None)
            if callable(release):
                release(raw_paths)
            from send2trash import send2trash
            for path in raw_paths:
                source = Path(path)
                if source.exists():
                    send2trash(str(source))
        except Exception as exc:
            self._logger.exception("Could not delete raw Session data")
            self._widget.setStatus(f"Raw-data deletion failed: {exc}")
            self._session.refresh()
            self._render()
            return

        if delete_recons:
            for dataset in affected:
                for entry in reconstruction_entries.get(dataset.id, ()):
                    if entry["loaded"] is not None:
                        self._remove_loaded_result(entry["loaded"])
                    if entry["saved"] is not None:
                        try:
                            self._session.trash_reconstruction(dataset.id, entry["saved"].path)
                        except Exception:
                            self._logger.exception("Could not trash associated reconstruction")
            self._session.forget_datasets(affected_ids)
        elif not recon_count:
            self._session.forget_datasets(affected_ids)

        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._session.refresh()
        self._widget.setStatus(
            "Raw data moved to system Trash"
            + ("; reconstructions moved to Session trash" if delete_recons else "")
        )
        self._render()

    def deleteSelectedDatasetReconstructions(self) -> None:
        """Delete results associated with the selected raw datasets."""
        if self._session is None:
            return
        datasets = [
            self._session.dataset_by_id(dataset_id)
            for dataset_id in self._selected_dataset_ids()
        ]
        datasets = [dataset for dataset in datasets if dataset is not None]
        if not datasets:
            return
        count = self._reconstruction_count_for_datasets(datasets)
        if count <= 0:
            self._widget.setStatus("Selected datasets have no results to delete")
            return
        if not self._widget.requestDeleteSelectedResultsConfirmation(count):
            return
        self._discard_reconstructions_for_datasets(datasets)
        self._widget.setStatus(
            f"Moved {count} result{'s' if count != 1 else ''} to Session trash"
        )
        self._render()

    def closeSelectedDatasetReconstructions(self) -> None:
        if self._session is None:
            return
        results = []
        seen = set()
        for dataset_id in self._selected_dataset_ids():
            dataset = self._session.dataset_by_id(dataset_id)
            if dataset is None:
                continue
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) in seen:
                    continue
                seen.add(id(result))
                results.append(result)
        for result in results:
            self._remove_loaded_result(result)
        if results:
            self._widget.setStatus(
                f"Closed {len(results)} reconstruction{'s' if len(results) != 1 else ''} from the panel"
            )
        self._render()

    def addSelectedDatasetReconstructions(self) -> None:
        if self._session is None:
            return
        wanted = set(self._selected_dataset_ids())
        datasets = [dataset for dataset in self._session.datasets if dataset.id in wanted]
        self._start_add_reconstructions(datasets)

    def _start_add_reconstructions(self, datasets) -> None:
        if self._session is None:
            return
        if self._addQueue is not None:
            self._widget.setStatus("Already adding reconstructions")
            return

        loaded_by_saved = self._loaded_by_saved_path()
        queue = []
        for dataset in datasets:
            for saved in dataset.saved_reconstructions:
                try:
                    resolved = str(Path(saved.path).resolve())
                except Exception:
                    resolved = str(saved.path)
                if resolved in loaded_by_saved:
                    continue
                queue.append((dataset, saved))

        if not queue:
            self._widget.setStatus("No matching saved reconstructions to add")
            return

        self._addQueue = queue
        self._addTotal = len(queue)
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(True, 0, self._addTotal)
        self._widget.setStatus(
            f"Adding {self._addTotal} reconstruction{'s' if self._addTotal != 1 else ''}…"
        )
        QtCore.QTimer.singleShot(0, self._process_next_add_reconstruction)

    def cancelAddingReconstructions(self) -> None:
        if self._addQueue is not None:
            self._addCancelled = True
            self._widget.setStatus("Cancelling after current reconstruction…")

    def _process_next_add_reconstruction(self) -> None:
        queue = self._addQueue
        if queue is None:
            return
        if self._addCancelled or not queue:
            self._finish_add_reconstructions(cancelled=self._addCancelled)
            return

        _dataset, saved = queue.pop(0)
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception:
            self._logger.exception("Could not restore saved Session reconstruction")
            self._addFailed += 1
        else:
            self._commChannel.sigResultProduced.emit(
                result,
                str(getattr(result, "name", "") or saved.display_name or saved.name),
            )
            self._addLastResult = result
            self._addAdded += 1
        self._addProcessed += 1
        self._widget.setAddingProgress(True, self._addProcessed, self._addTotal)
        QtCore.QTimer.singleShot(0, self._process_next_add_reconstruction)

    def _finish_add_reconstructions(self, *, cancelled: bool) -> None:
        added = self._addAdded
        total = self._addTotal
        processed = self._addProcessed
        failed = self._addFailed
        last_result = self._addLastResult
        self._addQueue = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(False)

        if last_result is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(last_result)

        if cancelled:
            message = f"Cancelled — added {added} of {total} reconstructions"
        else:
            message = f"Added {added} reconstruction{'s' if added != 1 else ''}"
        if failed:
            message += f" · {failed} failed"
        elif cancelled and processed < total:
            message += f" · {total - processed} not opened"
        self._widget.setStatus(message)
        self._render()

    def closeSessionReconstructions(self) -> None:
        if self._session is None:
            return
        seen = set()
        results = []
        for dataset in self._session.datasets:
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) not in seen:
                    seen.add(id(result))
                    results.append(result)
        for result in results:
            self._remove_loaded_result(result)
        self._widget.setStatus(
            f"Closed {len(results)} Session reconstruction{'s' if len(results) != 1 else ''}"
        )
        self._render()

    def closeNonFavoriteReconstructions(self) -> None:
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets")
            return
        before = sum(
            len(self._loaded_reconstructions_for_dataset(dataset)) for dataset in datasets
        )
        if not self._close_reconstructions_for_datasets(datasets):
            return
        self._widget.setStatus(
            f"Closed {before} reconstruction{'s' if before != 1 else ''} from non-favorites"
        )
        self._render()

    def emptySessionTrash(self) -> None:
        if self._session is None or not self._session.trash_root.exists():
            self._widget.setStatus("Session trash is empty")
            return
        answer = QtWidgets.QMessageBox.question(
            self._widget,
            "Empty Session trash",
            "Permanently delete everything in Session trash? This cannot be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self._session.empty_trash()
        except Exception as exc:
            self._logger.exception("Could not empty Session trash")
            self._widget.setStatus(f"Could not empty Session trash: {exc}")
            return
        self._widget.setStatus("Session trash emptied")

    def revealRawFolder(self) -> None:
        if self._session is not None:
            self._reveal_path(self._session.raw_root)

    def revealResultsFolder(self) -> None:
        if self._session is not None:
            self._session.results_root.mkdir(parents=True, exist_ok=True)
            self._reveal_path(self._session.results_root)

    def addSelectedToReconstructions(self) -> None:
        entry = self._selected_reconstruction_entry()
        if entry is None:
            return
        if entry["loaded"] is not None:
            if self._reconstruction is not None:
                self._reconstruction.selectResult(entry["loaded"])
            return
        saved = entry["saved"]
        if saved is None:
            return
        resolved = str(Path(saved.path).resolve())
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception as exc:
            self._logger.exception("Could not restore saved reconstruction")
            self._widget.setStatus(f"Could not add reconstruction: {exc}")
            return
        self._commChannel.sigResultProduced.emit(
            result, str(getattr(result, "name", "") or saved.display_name or saved.name)
        )
        if self._reconstruction is not None:
            self._reconstruction.selectResult(result)
        self._widget.setStatus(f"Added {saved.name} to reconstructions")
        self._renderSelected()

    def toggleReconstructionWorkspace(self, entry_key) -> None:
        entry = self._reconstruction_entry_by_key(entry_key)
        if entry is None:
            return
        self._selectedReconKey = str(entry_key)

        if entry["loaded"] is not None:
            # Close is explicitly non-destructive. If this result only exists
            # in memory, save it to the Session first so removing the
            # ProcessingResult does not destroy the only copy.
            loaded = entry["loaded"]
            if entry["saved"] is None:
                saved = self._ensure_selected_saved()
                if saved is None:
                    return
            self._remove_loaded_result(loaded)
            self._widget.setStatus("Closed reconstruction object; Session result preserved")
            self._render()
            return

        saved = entry["saved"]
        if saved is None:
            return
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception as exc:
            self._logger.exception("Could not restore saved reconstruction")
            self._widget.setStatus(f"Could not add reconstruction: {exc}")
            return
        self._commChannel.sigResultProduced.emit(
            result, str(getattr(result, "name", "") or saved.display_name or saved.name)
        )
        if self._reconstruction is not None:
            self._reconstruction.selectResult(result)
        self._widget.setStatus(f"Added {saved.name} to reconstructions")
        self._render()

    def _ensure_selected_saved(self):
        """Return the selected saved record, saving an in-memory result if needed."""
        entry = self._selected_reconstruction_entry()
        if (
            entry is None
            or self._session is None
            or self._selectedDatasetId is None
        ):
            return None
        if entry["saved"] is not None:
            return entry["saved"]
        result = entry["loaded"]
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if result is None or dataset is None:
            return None
        try:
            receipt = self._save_result(
                result, dataset, str(getattr(result, "name", "reconstruction"))
            )
        except Exception as exc:
            self._logger.exception("Could not save reconstruction into Session")
            self._widget.setStatus(f"Could not keep reconstruction: {exc}")
            return None
        wanted = str(Path(receipt.primary).resolve())
        saved = self._registerSavedReceipt(
            self._session,
            dataset,
            result,
            receipt,
            str(getattr(result, "name", "reconstruction")),
        )
        if saved is not None:
            self._selectedReconKey = f"saved:{wanted}"
        return saved

    def _remove_loaded_result(self, result) -> None:
        if result is None or self._reconstruction is None:
            return
        remover = getattr(self._reconstruction, "removeResult", None)
        if callable(remover):
            # ReconstructionViewController owns result-list resource release, so
            # Session and the generic Remove current/Remove all paths behave the
            # same way for lazy Session-restored results.
            remover(result)

    def discardReconstruction(self, entry_key) -> None:
        key = str(entry_key or "")
        if key:
            self._discard_reconstruction_keys([key])

    def discardSelected(self) -> None:
        keys = list(dict.fromkeys(self._widget.selectedReconstructionKeys()))
        if not keys and self._selectedReconKey:
            keys = [self._selectedReconKey]
        self._discard_reconstruction_keys(keys)

    def _discard_reconstruction_keys(self, keys) -> None:
        if self._session is None or self._selectedDatasetId is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return
        entries = [self._reconstruction_entry_by_key(str(key)) for key in keys if key]
        entries = [entry for entry in entries if entry is not None]
        if not entries:
            return

        discarded = 0
        for entry in entries:
            if entry["loaded"] is not None:
                self._remove_loaded_result(entry["loaded"])
            if entry["saved"] is not None:
                try:
                    self._session.trash_reconstruction(dataset.id, entry["saved"].path)
                except Exception as exc:
                    self._logger.exception("Could not move reconstruction to Session trash")
                    self._widget.setStatus(
                        f"Could not discard {entry['saved'].name}: {exc}"
                    )
                    self._session.refresh()
                    self._render()
                    return
            discarded += 1

        self._selectedReconKey = None
        self._session.refresh()
        self._widget.setStatus(
            f"Discarded {discarded} reconstruction{'s' if discarded != 1 else ''} to Session trash"
        )
        self._render()

    # ------------------------------------------------------------ result sync

    def _loaded_result_signature(self) -> tuple[int, ...]:
        try:
            return tuple(sorted(id(result) for _name, result in self._commChannel.getAllResults()))
        except Exception:
            return ()

    def _loadedResultsChanged(self) -> None:
        try:
            current_items = list(self._commChannel.getAllResults())
        except Exception:
            current_items = []
        signature = tuple(sorted(id(result) for _name, result in current_items))
        if signature == self._loadedResultSignature:
            return
        previous_ids = set(self._loadedResultSignature)
        current_by_id = {id(result): (display_name, result) for display_name, result in current_items}
        current_ids = set(current_by_id)
        self._loadedResultSignature = signature
        if self._session is None:
            return

        affected: set[str] = set()
        for rid in previous_ids - current_ids:
            owner = self._unindex_loaded_result(rid)
            if owner:
                affected.add(owner)
        for rid in current_ids - previous_ids:
            display_name, result = current_by_id[rid]
            owner = self._index_loaded_result(display_name, result)
            if owner:
                affected.add(owner)
        for dataset_id in affected:
            self._renderDataset(
                dataset_id, render_selected=(dataset_id == self._selectedDatasetId)
            )

    def processSelectedDatasets(
        self, position: str = "end", *, timepoint_scope=None
    ) -> None:
        ids = self._selected_dataset_ids()
        if not ids:
            return
        added = self.queueDatasetsForProcessing(
            ids, position=str(position or "end"), timepoint_scope=timepoint_scope
        )
        if added:
            self._widget.setStatus(
                f"Queued {added} dataset{'s' if added != 1 else ''} for reconstruction"
            )
        else:
            self._widget.setStatus("No selected datasets were added to the reconstruction queue")

    def _queue_states(self) -> dict[str, str]:
        controller = self._processingQueueController or getattr(
            self._main, "processingQueueController", None
        )
        getter = getattr(controller, "queueDatasetStates", None) if controller is not None else None
        if callable(getter):
            return getter()
        return {}

    @QtCore.Slot(object)
    def _processingQueueChanged(self, snapshot) -> None:
        self._widget.setProcessingQueue(snapshot)
        if self._session is None:
            return
        changed_ids = tuple((snapshot or {}).get("changed_dataset_ids", ()) or ())
        if not changed_ids:
            return
        queue_states = self._queue_states()
        for dataset_id in changed_ids:
            dataset_id = str(dataset_id)
            self._renderDataset(
                dataset_id,
                render_selected=(dataset_id == self._selectedDatasetId),
                queue_state=queue_states.get(dataset_id),
            )

    @QtCore.Slot(object)
    def _processingQueueProgressChanged(self, snapshot) -> None:
        current = (snapshot or {}).get("current") or {}
        dataset_id = str(getattr(current, "dataset_id", "") or "")
        if isinstance(current, dict):
            dataset_id = str(current.get("dataset_id") or "")
        if not dataset_id:
            self._queueProgressByDatasetId.clear()
            return
        self._queueProgressByDatasetId[dataset_id] = dict(
            (snapshot or {}).get("current_progress") or {}
        )
        if self._session is None:
            return
        self._renderDataset(
            dataset_id,
            render_selected=(dataset_id == self._selectedDatasetId),
            queue_state=self._queue_states().get(dataset_id),
        )

    def isDatasetProcessed(self, dataset_id: str) -> bool:
        """Whether Session has a complete derived result for ``dataset_id``.

        A partial timelapse result (Current T / range) is useful and remains in
        Session, but must not suppress whole-lapse Auto-reconstruction. Legacy
        timelapse results without scope provenance are treated as whole-lapse.
        """
        if self._session is None:
            return False
        dataset = self._session.dataset_by_id(str(dataset_id))
        if dataset is None:
            return False
        if str(getattr(dataset, "kind", "")) != "time-lapse":
            if dataset.saved_reconstructions:
                return True
            return bool(self._loaded_reconstructions_for_dataset(dataset))

        from imswitch.improcess.model.timepoint_scope import (
            coerce_timepoint_scope,
            graph_has_full_timepoint_scope,
        )

        total = getattr(dataset, "planned_timepoints", None)
        for saved in dataset.saved_reconstructions:
            payload = getattr(saved, "timepoint_scope", None)
            if payload is None:
                return True  # pre-scope Session result
            try:
                if coerce_timepoint_scope(payload).is_full(total or payload.get("total")):
                    return True
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        for _name, result in self._loaded_reconstructions_for_dataset(dataset):
            if graph_has_full_timepoint_scope(graph_of(result), total=total, legacy_full=True):
                return True
        return False

    def setProcessingQueueController(self, controller) -> None:
        old = getattr(self, "_processingQueueController", None)
        if old is not None:
            try:
                old.sigQueueChanged.disconnect(self._processingQueueChanged)
            except Exception:
                pass
            try:
                old.sigProgressChanged.disconnect(self._processingQueueProgressChanged)
            except Exception:
                pass
        self._processingQueueController = controller
        self._queueProgressByDatasetId.clear()
        if controller is not None:
            controller.sigQueueChanged.connect(self._processingQueueChanged)
            controller.sigProgressChanged.connect(self._processingQueueProgressChanged)
            self._widget.setProcessingQueue(controller.queueSnapshot())

    def queueDatasetsForProcessing(
        self, dataset_ids, *, position: str = "end", timepoint_scope=None
    ) -> int:
        """Submit Session datasets to the shared lazy processing queue.

        Kept as a controller-level bridge so the future Session widget does not
        need to know where the queue controller lives on the main controller.
        """
        controller = self._processingQueueController or getattr(
            self._main, "processingQueueController", None
        )
        if controller is None:
            return 0
        return controller.enqueueSessionDatasets(
            dataset_ids, position=position, timepoint_scope=timepoint_scope
        )

    def setAutoProcessingEnabled(self, enabled: bool) -> None:
        queue = self._queue_controller()
        enabled = bool(enabled and self._session is not None
                       and self._sessionMode == "follow")
        if queue is not None:
            queue.setAutoEnabled(enabled)
            if not enabled:
                queue.setProcessWhileAcquiring(False)
        self._sync_auto_availability()

    def setProcessWhileAcquiring(self, enabled: bool) -> None:
        from imswitch.improcess.execution.policy import streaming_capability

        queue = self._queue_controller()
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        capable = False
        if enabled and reconstructor is not None:
            try:
                params = self._main._widget.getReconstructionParams()
                capable = streaming_capability(reconstructor, params).supported
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
        if queue is not None:
            queue.setProcessWhileAcquiring(bool(
                enabled and capable and self._session is not None
                and self._sessionMode == "follow"
                and queue.autoPolicy.enabled
            ))

    def pauseProcessingQueue(self) -> None:
        controller = self._processingQueueController or getattr(self._main, "processingQueueController", None)
        if controller is not None:
            controller.pause()

    def resumeProcessingQueue(self) -> None:
        controller = self._processingQueueController or getattr(self._main, "processingQueueController", None)
        if controller is not None:
            controller.resume()

    def skipCurrentProcessing(self) -> None:
        controller = self._processingQueueController or getattr(self._main, "processingQueueController", None)
        if controller is not None:
            controller.skipCurrent()

    def processingQueueSnapshot(self):
        controller = self._processingQueueController or getattr(self._main, "processingQueueController", None)
        return controller.queueSnapshot() if controller is not None else None

    def _queueAutoSave(self, result, dataset, display_name: str = "") -> None:
        if self._session is None or result is None or dataset is None:
            return
        if getattr(result, "_session_save_pending", False):
            return
        result._session_save_pending = True
        self._saveRequests.append((
            self._session,
            dataset,
            result,
            str(display_name or ""),
            str(self._saveFormat),
        ))
        self._startNextAutoSave()

    def _startNextAutoSave(self) -> None:
        if self._saveThread is not None or not self._saveRequests:
            return
        session, dataset, result, display_name, save_format = self._saveRequests.popleft()
        dataset_id = str(dataset.id)
        store = SessionResultStore(session, save_format=save_format)
        thread = QtCore.QThread()
        worker = SessionSaveWorker(store, result, dataset, display_name)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._autoSaveSucceeded)
        worker.failed.connect(self._autoSaveFailed)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(lambda: self._autoSaveThreadFinished(thread))
        thread.finished.connect(thread.deleteLater)
        self._saveActiveRequest = (session, dataset_id, result, display_name)
        self._saveThread = thread
        self._saveWorker = worker
        thread.start()

    @QtCore.Slot(object, str, str, object)
    def _autoSaveSucceeded(self, result, dataset_id: str, display_name: str, receipt) -> None:
        result._session_save_pending = False
        active = self._saveActiveRequest
        session = active[0] if active is not None else None
        if session is None:
            return
        dataset = session.dataset_by_id(dataset_id)
        if dataset is None:
            return
        saved = self._registerSavedReceipt(
            session, dataset, result, receipt, display_name
        )
        if self._session is session and saved is not None:
            self._widget.setStatus(f"Auto-saved {Path(receipt.primary).name}")
            self._renderDataset(
                dataset_id, render_selected=(dataset_id == self._selectedDatasetId)
            )
            self.sigCatalogChanged.emit()

    @QtCore.Slot(object, str, str, str)
    def _autoSaveFailed(
        self, result, dataset_id: str, _display_name: str, message: str
    ) -> None:
        result._session_save_pending = False
        self._logger.error(f"Session auto-save failed: {message}")
        if self._session is not None and self._session.dataset_by_id(dataset_id) is not None:
            self._widget.setStatus(f"Auto-save failed: {message}")

    def _autoSaveThreadFinished(self, thread) -> None:
        if self._saveThread is not thread:
            return
        self._saveThread = None
        self._saveWorker = None
        self._saveActiveRequest = None
        self._startNextAutoSave()

    def _autoCalibrationPreviewPublished(self, result) -> None:
        """Make the first completed Auto calibration lockable during a live T series.

        A provisional first-frame preview cannot establish a valid calibration.
        The shared result publisher marks scientific completion in result_assembly.
        """
        if self._session is None or result is None:
            return
        metadata = dict(getattr(result, "metadata", {}) or {})
        if (metadata.get("monalisa_streaming") or {}).get("provisional_preview"):
            return
        if int((metadata.get("result_assembly") or {}).get("published_steps") or 0) < 1:
            return
        job_id = str(getattr(result, "_improcess_queue_job_id", "") or "")
        if not job_id or job_id in self._autoCalibrationSeenJobs:
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return
        manager = getattr(self._main, "reconstructorManager", None)
        remember = getattr(manager, "rememberMonalisaAutoCalibration", None)
        if callable(remember):
            try:
                if remember(result, source_label=str(getattr(dataset, "name", "") or result.name)):
                    self._autoCalibrationSeenJobs.add(job_id)
            except Exception as exc:
                self._logger.warning("Could not expose streamed Auto calibration: %s", exc)

    def _resultFinalized(self, result, _display_name: str) -> None:
        if self._session is None or result is None:
            return
        # A restored saved result is already persistent. Re-finalizing it must
        # never create a duplicate Session save.
        if getattr(result, "_session_restored", False):
            return
        if not self._is_session_reconstruction(result):
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return

        # The queued reconstructor can resolve Auto localization without ever
        # loading raw data into the Parameters panel. Publish only the resolved
        # snapshot as a transient Lock-current candidate, not as Auto job input.
        manager = getattr(self._main, "reconstructorManager", None)
        remember = getattr(manager, "rememberMonalisaAutoCalibration", None)
        if callable(remember):
            try:
                remember(result, source_label=str(getattr(dataset, "name", "") or _display_name))
            except Exception as exc:
                self._logger.warning("Could not expose resolved Auto calibration: %s", exc)

        # Live viewer updates deliberately do not broadcast sigResultsChanged
        # on every frame. Finalization is the one stable membership event: index
        # the final object explicitly so Session shows it even with autosave off.
        self._index_loaded_result(_display_name, result)

        # Viewer publication has already happened (or the live entry already
        # exists). Update only this Session row on the next GUI turn.
        dataset_id = str(dataset.id)
        QtCore.QTimer.singleShot(
            0,
            lambda did=dataset_id: self._renderDataset(
                did, render_selected=(did == self._selectedDatasetId)
            ),
        )

        if (
            not self._autoSave
            or getattr(result, "_session_saved_path", None)
            or getattr(result, "_session_save_pending", False)
        ):
            return
        self._queueAutoSave(result, dataset, _display_name)

    def _registerSavedReceipt(
        self, session, dataset, result, receipt, display_name: str = ""
    ):
        primary = Path(receipt.primary).resolve()
        result._session_saved_path = str(primary)
        self._tag_session_origin(result, session)
        if self._session is session:
            self._loadedBySavedPath[str(primary)] = result
        return session.register_saved_result(
            dataset.id,
            primary,
            result=result,
            display_name=str(display_name or getattr(result, "name", "reconstruction")),
            save_manifest=True,
        )

    @classmethod
    def _autosave_stem(cls, dataset, result, display_name: str = "") -> str:
        return SessionResultStore.autosave_stem(dataset, result, display_name)

    def _save_result(self, result, dataset, display_name: str = ""):
        if self._session is None:
            raise RuntimeError("No active Processing Session")
        if self._resultStore is None or self._resultStore.session is not self._session:
            self._resultStore = SessionResultStore(
                self._session, save_format=self._saveFormat
            )
        self._resultStore.save_format = self._saveFormat
        return self._resultStore.save(result, dataset, display_name)

    def _resultSaved(self, result, receipt) -> None:
        if self._session is None or result is None or receipt is None:
            return
        if result_role_for_result(result) == "inspection":
            return
        try:
            primary = Path(receipt.primary).resolve()
            primary.relative_to(self._session.results_root)
        except Exception:
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return
        saved = self._registerSavedReceipt(
            self._session,
            dataset,
            result,
            receipt,
            str(getattr(result, "name", "reconstruction")),
        )
        if saved is not None:
            self._renderDataset(
                dataset.id, render_selected=(dataset.id == self._selectedDatasetId)
            )

    def closeEvent(self):
        if self._saveThread is not None or self._saveRequests:
            return False
        self._stopDirectoryWatcher()
        return True


__all__ = ["ProcessingSessionController"]
