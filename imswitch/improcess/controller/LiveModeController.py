"""Controller for live reconstruction mode in the watcher UI.

Watches a chosen root folder for new *timelapse* sub-folders and drives one
``LiveReconstructionController`` run at a time. Discovery is two units from
``improcess/live/discovery.py``:

* ``DirectoryWatcher`` -- root folder -> ``sigEntryFound(entry)`` for each
  new immediate sub-directory;
* ``JobQueue`` -- a folder -> a :class:`DiscoveredJob` (its timepoint-0
  ``.zarr`` store) once one appears.

Store *readiness* ("safe to open now") is **not** decided here -- the stream
worker opens the source with bounded retry. Following the later timepoint files
inside a folder is the ``LiveSource``'s job.
"""

import collections
import os
from pathlib import Path

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live.discovery import DirectoryWatcher, JobQueue
from imswitch.improcess.live.job import (
    LiveJobState,
    prepare_discovered_job,
    start_prepared_live_job,
)
from .basecontrollers import ImProcessWidgetController
from .LiveReconstructionController import LiveReconstructionController


class LiveModeController(ImProcessWidgetController):
    """Live reconstruction mode: watch -> queue -> one run at a time."""

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller
        self._session_controller = None
        self._logger = initLogger(self, tryInheritParent=False)

        # All four collaborators live only while live mode is running; they are
        # built in _start_live and are None until then.
        self._live_rec_ctr = None
        self._directory_watcher = None
        self._owns_directory_watcher = True
        self._watcher_signal_connected = False
        self._job_queue = None
        self._tick_timer = None

        self._currently_processing = False
        self._watched_folder = None
        # Name of the run in flight, so its finished data object can be
        # matched when saving. Cleared once the run ends.
        self._current_job_name = None
        self._current_save_stem = None
        # How many entries each folder name has already produced. Kept for the
        # controller's whole life, not per watched root: the viewer keys a run's
        # data object on its name, so a name reused after a reset would write
        # into the earlier run's object instead of starting its own.
        self._job_name_counts: collections.Counter[str] = collections.Counter()
        # Live processing needs its own notion of which root entries have been
        # queued.  A Session-owned DirectoryWatcher may already have seen every
        # entry before the legacy Live UI is enabled, so watcher discovery state
        # can no longer double as live-processing state.
        self._live_seen_entries: set[str] = set()

        # The widget corresponds to the DirectoryWatcherFrame (view), where
        # the LiveModeController controls this view.
        self._widget.sigLiveChanged.connect(self._on_live_toggled)
        self._widget.sigResetClicked.connect(self._on_reset_clicked)
        self._widget.sigSkipClicked.connect(self._on_skip_clicked)

    def setSessionController(self, controller) -> None:
        """Attach the Session controller used to share its DirectoryWatcher."""
        self._session_controller = controller

    def isLiveReconstructionRunning(self) -> bool:
        """Whether the live reconstruction controller is mid-stack.

        Asked before drop-in reconstructors are reloaded: a live session holds
        the reconstructor it started with, and must not be pulled onto a new
        version half way through a stack.
        """
        return bool(getattr(self._live_rec_ctr, 'is_running', False))

    def _on_live_toggled(self, enabled: bool) -> None:
        """Handle the watcher UI's live toggle."""
        if enabled:
            self._start_live()
        else:
            self._stop_live()

    # ------------------------------------------------------------- lifecycle
    def _start_live(self) -> None:
        """Validate the selection, then start watching + the queue tick."""
        folder_path = self._widget.path
        if not folder_path or not os.path.isdir(folder_path):
            self._logger.error(f"Not a valid folder for live mode: {folder_path!r}")
            self._widget.liveCheck.setChecked(False)
            return

        if self._get_active_reconstructor() is None:
            self._logger.error("No active reconstructor available")
            self._widget.liveCheck.setChecked(False)
            return

        # A new root folder gets fresh live-processing state.  The watcher itself
        # may also need to change while the path stays the same (for example a
        # Session is opened/closed between Live toggles), so compare the desired
        # Session-owned watcher as well as the path.
        shared = self._session_watcher_for(folder_path)
        watcher_changed = (
            folder_path != self._watched_folder
            or (shared is not None and self._directory_watcher is not shared)
            or (shared is None and not getattr(self, "_owns_directory_watcher", True))
        )
        if watcher_changed:
            self._teardown_watcher()
            path_changed = folder_path != self._watched_folder
            self._watched_folder = folder_path
            self._job_queue = JobQueue()
            if path_changed:
                self._live_seen_entries.clear()
            if shared is not None:
                self._directory_watcher = shared
                self._owns_directory_watcher = False
            else:
                self._directory_watcher = DirectoryWatcher(folder_path, parent=self)
                self._owns_directory_watcher = True
        self._currently_processing = False

        self._connect_watcher()

        if self._live_rec_ctr is None:
            self._live_rec_ctr = LiveReconstructionController(self._commChannel)
        try:
            self._live_rec_ctr.sigFinished.disconnect(self._on_job_finished)
        except TypeError:
            pass
        self._live_rec_ctr.sigFinished.connect(self._on_job_finished)

        # Retry pending folders (a folder can appear before its first store).
        self._tick_timer = QtCore.QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._process_next_job)
        self._tick_timer.start()

        if self._owns_directory_watcher:
            self._directory_watcher.start()
        else:
            # Session may have been watching for minutes before Live is enabled.
            # Seed only entries this Live controller has not processed/queued;
            # do not reset the Session watcher's discovery state.
            for entry in self._directory_watcher.snapshot_entries():
                self._on_entry_found(entry)
        self._logger.info(f"Live mode started: watching {folder_path}")

    def _stop_live(self) -> None:
        """Stop watching and the current run; keep the seen-set for a re-toggle."""
        if self._directory_watcher is not None and getattr(self, "_owns_directory_watcher", True):
            self._directory_watcher.stop()
        if not getattr(self, "_owns_directory_watcher", True):
            self._disconnect_watcher()

        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer.deleteLater()
            self._tick_timer = None

        if self._live_rec_ctr is not None:
            self._live_rec_ctr.stop()

        self._currently_processing = False
        self._logger.info("Live mode stopped")

    @QtCore.Slot()
    def _on_skip_clicked(self) -> None:
        """Give up on the rest of this timelapse and move to the next job.

        Live mode keeps running: only the current run ends. Whatever it has
        reconstructed stays in the viewer -- the data object and its timepoints
        belong to the viewer, not the run -- and the timepoint in progress is
        finished first, so the last one in the object is whole rather than cut
        off part-way.

        ``notify_finished`` is what advances the queue. A graceful stop returns
        immediately and tears down asynchronously, so the slot must stay taken
        until ``sigFinished`` confirms the run is gone; freeing it here would let
        the next job start and force-kill the run still draining.
        """
        if not self._currently_processing:
            self._logger.info("Skip ignored: no timelapse is being processed")
            return
        self._logger.info("Skipping the rest of the current timelapse")
        self._live_rec_ctr.stop(notify_finished=True)

    @QtCore.Slot()
    def _on_reset_clicked(self) -> None:
        """Shut live mode down and forget what has been processed.

        A full stop, not just an abort: the run in flight is ended, the watcher
        and queue tick stop, and the live toggle is unchecked so the UI shows
        what actually happened. The watcher's seen-set and the pending queue are
        then cleared, so ticking the box again reconstructs the whole folder
        from the start.

        Whatever the aborted run had already reconstructed stays in the viewer --
        the data object and its timepoints belong to the viewer, not the run. A
        later re-run lands in its own object named ``<folder>.1``, ``<folder>.2``,
        ... (see :meth:`_unique_job_name`), so the two sit side by side.
        """
        self._stop_live()
        # Reflect the shutdown in the UI. Already-unchecked emits nothing, and a
        # re-entrant _stop_live via sigLiveChanged is harmless (it is idempotent).
        self._widget.liveCheck.setChecked(False)

        if self._directory_watcher is not None and getattr(self, "_owns_directory_watcher", True):
            self._directory_watcher.reset()
        # Cleared with an owned watcher too: _on_entry_found drops anything in
        # this set, so the watcher's re-emitted entries would never run again.
        getattr(self, "_live_seen_entries", set()).clear()
        if self._job_queue is not None:
            self._job_queue.reset()
        self._logger.info(
            f"Reset: live mode stopped; {self._watched_folder!r} will be "
            f"reconstructed from the start when re-enabled"
        )

    def _next_run_names(self, base: str) -> tuple[str, str]:
        """Names for one run: the viewer entry, and the stem of its saved file.

        Reset re-runs a folder, so both need a run index -- but they spell it
        differently. The entry keeps the folder name with a ``.1`` / ``.2``
        suffix, matching how the reconstruction list already reads; the file
        puts the index last, after ``_recon``, which reads better as a
        filename::

            run 0:  timelapse_00      ->  timelapse_00_recon.tif
            run 1:  timelapse_00.1    ->  timelapse_00_recon_1.tif

        Computed from the base name and index together rather than derived from
        one another, so neither spelling has to be parsed back apart.
        """
        index = self._job_name_counts[base]
        self._job_name_counts[base] += 1
        name = base if index == 0 else f"{base}.{index}"
        stem = f"{base}_recon" if index == 0 else f"{base}_recon_{index}"
        return name, stem

    def _teardown_watcher(self) -> None:
        self._disconnect_watcher()
        if self._directory_watcher is not None and getattr(self, "_owns_directory_watcher", True):
            self._directory_watcher.stop()
            self._directory_watcher.deleteLater()
        self._directory_watcher = None
        self._owns_directory_watcher = True

    def _root_has_processing_session(self, folder_path: str) -> bool:
        """Session owns saving for its raw root, regardless of Review/Follow."""
        controller = getattr(self, "_session_controller", None)
        session = getattr(controller, "session", None) if controller is not None else None
        if session is None:
            return False
        try:
            return Path(folder_path).resolve() == Path(session.raw_root).resolve()
        except (OSError, TypeError, ValueError):
            return False

    def _session_watcher_for(self, folder_path: str):
        """Legacy Live must own its watcher independently of Session modes.

        Review deliberately tears the Session watcher down. Borrowing that
        QObject would leave legacy Live with a deleted/stopped observer when
        switching Follow -> Review. This legacy path is retired in M4.
        """
        return None

    def _connect_watcher(self) -> None:
        if self._directory_watcher is None or getattr(self, "_watcher_signal_connected", False):
            return
        self._directory_watcher.sigEntryFound.connect(self._on_entry_found)
        self._watcher_signal_connected = True

    def _disconnect_watcher(self) -> None:
        if self._directory_watcher is None or not getattr(self, "_watcher_signal_connected", False):
            return
        try:
            self._directory_watcher.sigEntryFound.disconnect(self._on_entry_found)
        except TypeError:
            pass
        self._watcher_signal_connected = False

    # -------------------------------------------------------------- queueing

    @QtCore.Slot(str)
    def _on_entry_found(self, entry: str) -> None:
        """Queue a newly-discovered root entry and try to start it.

        An entry, not a timelapse folder: the watcher also reports single
        recordings sitting in the root, and what each one turns out to be is
        settled later, by probing it.
        """
        entry = os.path.abspath(entry)
        # Session results may live below the watched raw root. Session ignores
        # that output subtree; legacy Live must do the same when it borrows the
        # Session watcher, otherwise the results folder can remain pending as a
        # non-recording job forever.
        controller = getattr(self, "_session_controller", None)
        session = getattr(controller, "session", None) if controller is not None else None
        if session is not None:
            try:
                Path(entry).resolve().relative_to(Path(session.results_root).resolve())
                return
            except (ValueError, OSError):
                pass
        seen = getattr(self, "_live_seen_entries", None)
        if seen is None:
            seen = self._live_seen_entries = set()
        if entry in seen:
            return
        seen.add(entry)
        self._job_queue.add(entry)
        self._logger.info(f"Discovered entry: {entry}")
        self._process_next_job()

    def _process_next_job(self) -> None:
        """Start the next resolvable job, unless a run is already in flight."""
        if self._currently_processing:
            return

        reconstructor = self._get_active_reconstructor()
        if reconstructor is None:
            return

        # Pick the first job this reconstructor can actually work on. Each
        # candidate is probed for what its raw data *is*, with four outcomes:
        #   * unreadable yet -- a store still being written, a file the
        #     recorder still holds. Put back, because that is the normal
        #     state of a recording that has only just started;
        #   * persistent in-process HDF5 access conflict -- logged and dropped;
        #   * readable but not something this plugin can reconstruct --
        #     skipped with a warning naming the shape it turned out to be;
        #   * startable -- taken.
        # Iterative rather than re-entering: a root of many entries would
        # otherwise recurse once per entry.
        job = None
        prepared = None
        source_type = None
        deferred = []
        while True:
            candidate = self._job_queue.next_ready()
            if candidate is None:
                break

            resolution = prepare_discovered_job(candidate, reconstructor)
            if resolution.state == LiveJobState.PENDING:
                # Defer only what is still there: an entry since deleted
                # would otherwise be re-queued and re-probed forever.
                if os.path.exists(candidate.seed_path):
                    deferred.append(candidate)
                break

            if resolution.state == LiveJobState.FAILED:
                self._logger.error(
                    f"Cannot open {candidate.seed_path}: {resolution.reason}"
                )
            elif resolution.state == LiveJobState.UNSUPPORTED:
                self._logger.warning(
                    f"Skipping {candidate.seed_path}: {resolution.reason}"
                )
            else:
                job = candidate
                prepared = resolution.prepared
                source_type = prepared.source_type
                break

        # Hand back the not-yet-readable ones whatever the outcome: dequeuing
        # is how they were inspected, and dropping one loses its recording.
        for entry in deferred:
            self._job_queue.add(entry.job_path)

        if job is None:
            return

        self._currently_processing = True
        self._logger.info(
            f"Processing {job.job_path} (seed {job.seed_path}, "
            f"{source_type.layout})"
        )

        # The job's name becomes the run's result name, and so the name of the
        # viewer entry it creates -- one data object per job. After a reset the
        # same job runs again, so the name is made unique.
        job_name, save_stem = self._next_run_names(
            os.path.basename(os.path.normpath(job.job_path))
        )
        # Built from what the probe already established, so the store is not
        # reopened to answer a question that has been answered.
        params = self._get_reconstructor_params()
        try:
            self._current_job_name = job_name
            self._current_save_stem = save_stem
            started = start_prepared_live_job(
                self._live_rec_ctr, prepared, reconstructor, params, name=job_name
            )
        except Exception as e:
            self._logger.error(f"Failed to start reconstruction for {job.seed_path}: {e}")
            started = False

        # start() returning False means no sigFinished will arrive -- advance now.
        if not started:
            self._currently_processing = False
            self._process_next_job()

    @QtCore.Slot()
    def _on_job_finished(self) -> None:
        """A run ended -- optionally save it, free the slot, take the next job.

        Every ending arrives here: normal completion, "Skip directory" (which
        asks for ``sigFinished`` precisely so it does), and a run that failed to
        start. Saving is therefore requested in one place rather than per path.
        """
        finished_name, self._current_job_name = self._current_job_name, None
        save_stem, self._current_save_stem = self._current_save_stem, None
        self._request_save(finished_name, save_stem)
        self._currently_processing = False
        self._process_next_job()

    def _request_save(self, job_name: str | None, save_stem: str | None) -> None:
        """Ask the viewer to write this run's data object, if saving is on.

        Output lives beside the watched folder rather than inside it, so a
        re-run does not rediscover its own reconstructions as new timelapses::

            <parent>/<root>/timelapse_00  ->  <parent>/<root>_recon/timelapse_00_recon.tif

        The viewer owns the accumulated buffer, so it does the writing; this
        only decides *where*. A run that produced nothing (failed startup) is
        skipped there, by name, rather than guessed at here.
        """
        if not job_name or not save_stem or not self._watched_folder:
            return
        # A matching Processing Session now owns result persistence through the
        # shared sigResultFinalized -> SessionResultStore lifecycle. Keeping the
        # old <root>_recon save as well would duplicate the same live result and
        # ignore the Session's own auto-save policy. Outside Session, preserve
        # the legacy watcher save behavior during migration.
        if self._root_has_processing_session(self._watched_folder):
            return
        if not getattr(self._widget, "saveCheck", None) or not self._widget.saveCheck.isChecked():
            return

        root = os.path.normpath(self._watched_folder)
        save_dir = os.path.join(
            os.path.dirname(root), f"{os.path.basename(root)}_recon"
        )
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            self._logger.error(f"Cannot create save folder {save_dir!r}: {exc}")
            return

        path = os.path.join(save_dir, f"{save_stem}.tif")
        self._logger.info(f"Saving {job_name} to {path}")
        self._commChannel.sigSaveLiveResult.emit(job_name, path)

    # -------------------------------------------------------------- helpers
    def _get_active_reconstructor(self):
        """Get the active reconstructor from the main view controller."""
        if self._main_controller is None:
            return None
        return getattr(self._main_controller, '_activeReconstructor', None)

    def _get_reconstructor_params(self) -> dict:
        """Get the current reconstructor parameters from the view."""
        widget = getattr(self._main_controller, '_widget', None)
        if widget is None:
            return {}

        getter = getattr(widget, 'getReconstructionParams', None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:
                self._logger.warning(f"Could not read reconstruction params from view: {exc}")

        par_tree = getattr(widget, 'parTree', None)
        for legacy_getter_name in ('get_values', 'get_param_dict'):
            legacy_getter = getattr(par_tree, legacy_getter_name, None)
            if callable(legacy_getter):
                try:
                    return legacy_getter()
                except Exception as exc:
                    self._logger.warning(
                        f"Could not read reconstruction params via {legacy_getter_name}: {exc}"
                    )

        return {}


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
