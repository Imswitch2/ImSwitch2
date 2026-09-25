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
import re
from collections import deque
from typing import Any

import h5py
import zarr

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live.source_factory import make_live_source
from imswitch.improcess.live.discovery import DirectoryWatcher, JobQueue
from imswitch.improcess.live.source_type import probe_source_type
from .basecontrollers import ImProcessWidgetController
from .LiveReconstructionController import LiveReconstructionController

# How deep below the selected folder to look for recording stores. Measurement
# folders are created one level under the watched root; depth 2 covers
# "select the parent folder" without scanning the whole tree.
_DISCOVERY_MAX_DEPTH = 2
# Output subdirectories (reconstructions/logs) that must never be ingested.
_DISCOVERY_EXCLUDE_DIRS = {"rec", "deskew", "Mini_Recon_Results", "__pycache__"}
_LIVE_EXTENSION_SUFFIXES = {
    "zarr": {".zarr"},
    "hdf5": {".hdf5", ".h5", ".hdf"},
    "h5": {".hdf5", ".h5", ".hdf"},
    "hdf": {".hdf5", ".h5", ".hdf"},
}


class LiveModeController(ImProcessWidgetController):
    """Live reconstruction mode: watch -> queue -> one run at a time."""

    def __init__(self, *args, main_controller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main_controller = main_controller
        self._logger = initLogger(self, tryInheritParent=False)

        # All four collaborators live only while live mode is running; they are
        # built in _start_live and are None until then.
        self._live_rec_ctr = None
        self._directory_watcher = None
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

        # The widget corresponds to the DirectoryWatcherFrame (view), where
        # the LiveModeController controls this view.
        self._widget.sigLiveChanged.connect(self._on_live_toggled)
        self._widget.sigResetClicked.connect(self._on_reset_clicked)
        self._widget.sigSkipClicked.connect(self._on_skip_clicked)

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

        # A new root folder gets fresh discovery state; the same folder keeps its
        # seen-set, so toggling live off/on does not reprocess everything.
        if folder_path != self._watched_folder:
            self._teardown_watcher()
            self._watched_folder = folder_path
            self._job_queue = JobQueue()
            self._directory_watcher = DirectoryWatcher(folder_path, parent=self)
            self._directory_watcher.sigEntryFound.connect(self._on_entry_found)
        self._currently_processing = False

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

        self._directory_watcher.start()
        self._logger.info(f"Live mode started: watching {folder_path}")

    def _stop_live(self) -> None:
        """Stop watching and the current run; keep the seen-set for a re-toggle."""
        if self._directory_watcher is not None:
            self._directory_watcher.stop()

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

        if self._directory_watcher is not None:
            self._directory_watcher.reset()
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
        if self._directory_watcher is not None:
            self._directory_watcher.stop()
            self._directory_watcher.deleteLater()
            self._directory_watcher = None

    # -------------------------------------------------------------- queueing

    @QtCore.Slot(str)
    def _on_entry_found(self, entry: str) -> None:
        """Queue a newly-discovered root entry and try to start it.

        An entry, not a timelapse folder: the watcher also reports single
        recordings sitting in the root, and what each one turns out to be is
        settled later, by probing it.
        """
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
        # candidate is probed for what its raw data *is*, and there are three
        # outcomes, only one of which means "not for us":
        #   * unreadable yet -- a store still being written, a file the
        #     recorder still holds. Put back, because that is the normal
        #     state of a recording that has only just started;
        #   * readable but not something this plugin can reconstruct --
        #     skipped with a warning naming the shape it turned out to be;
        #   * startable -- taken.
        # Iterative rather than re-entering: a root of many entries would
        # otherwise recurse once per entry.
        job = None
        source_type = None
        deferred = []
        while True:
            candidate = self._job_queue.next_ready()
            if candidate is None:
                break

            candidate_type = probe_source_type(candidate.seed_path)
            if candidate_type is None:
                # Defer only what is still there: an entry since deleted
                # would otherwise be re-queued and re-probed forever.
                if os.path.exists(candidate.seed_path):
                    deferred.append(candidate)
                break

            # Read like every other plugin capability, so an out-of-tree
            # reconstructor predating this one accepts whatever it is given
            # rather than failing to start at all.
            accepts = getattr(reconstructor, "accepts_raw_source", None)
            if callable(accepts) and not accepts(candidate_type):
                self._logger.warning(
                    f"Skipping {candidate.seed_path}: {reconstructor.name} "
                    f"cannot reconstruct a {candidate_type.format_id} "
                    f"{candidate_type.layout} recording of "
                    f"{candidate_type.frames_per_stack} frame(s) per timepoint"
                )
            else:
                job, source_type = candidate, candidate_type
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
        source = make_live_source(job.seed_path, source_type, detector_name=None)
        params = self._get_reconstructor_params()
        try:
            self._current_job_name = job_name
            self._current_save_stem = save_stem
            started = self._live_rec_ctr.start(
                reconstructor, source, params,
                source_arg=job.seed_path, name=job_name,
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
