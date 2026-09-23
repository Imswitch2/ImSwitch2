"""Discovery layer for live reconstruction: root watcher + job queue.

``DirectoryWatcher`` polls a chosen root folder (``QTimer`` on the owning /
GUI thread) and emits each newly-seen immediate sub-directory -- a "timelapse
folder". The scan is a cheap ``os.scandir`` + set diff and opens no store.

``JobQueue`` collects those folders and, on demand, resolves the first one that
now holds a ``.zarr`` timepoint store into a :class:`DiscoveredJob` (seed =
the timepoint-0 store). It is pure and synchronous -- no Qt, no store opening.

Neither unit decides whether a store is *safe to open* -- that is the stream
worker's bounded-retry-open problem. Neither follows the later timepoint files
inside a folder -- that is ``LiveSource.poll()``.

Design choices (override if the pipeline needs otherwise):

  * **one level deep only** -- ``root/<timelapse>/<timepoint>.zarr``;
  * the first :meth:`DirectoryWatcher.start` after construction emits every
    sub-directory already present (the seen-set is empty); a later
    ``stop`` / ``start`` keeps the seen-set, so nothing is re-emitted --
    only :meth:`DirectoryWatcher.reset` (the future hard-reset button) forces
    a full re-emit;
  * ``JobQueue`` picks the seed store from the name alone -- the timepoint
    index is read from the substring starting at ``"scan"`` (see
    :func:`_seed_key`);
  * output / cache dirs in :data:`_EXCLUDED_DIRS` are skipped by the watcher.
"""

import os
from dataclasses import dataclass

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger


# Immediate sub-directory names that are never timelapse folders.
# NOTE: add new directories here that should NOT be treated as timelapses.

# TODO: This structure is probably unnecessary, since we will filter these
#   out before we insert the directories in the JobQueue

_EXCLUDED_DIRS = (
    "rec",
    "deskew",
    "Mini_Recon_Results",
    "__pycache__",
)

# Timepoint store suffix (only .zarr for now)
_STORE_SUFFIX = (
    ".zarr",
    ".h5",
    ".hdf5",
)


class DirectoryWatcher(QtCore.QObject):
    """Emits ``sigTimelapseFound`` for each new immediate sub-directory of a root."""

    sigTimelapseFound = QtCore.Signal(str)  # absolute path of a newly-seen sub-directory

    def __init__(
        self,
        root_path: str,
        *,
        poll_interval_ms: int = 1000,
        parent=None,
    ) -> None:
        """
        Args:
            root_path: Folder to watch for new timelapse sub-directories.
            poll_interval_ms: Delay between scans once started.
            parent: Optional Qt parent (keeps the watcher on its thread).
        """
        super().__init__(parent)
        self._root_path = root_path
        self._logger = initLogger(self, tryInheritParent=False)
        self._seen_entries: set[str] = set()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Begin polling: one scan now, then every ``poll_interval_ms``.

        Does not clear the seen-set, so only the first start after construction
        emits pre-existing folders.
        """
        self._tick()
        self._timer.start()

    def stop(self) -> None:
        """Stop polling. The seen-set is kept, so a later :meth:`start` does not
        re-emit folders already seen this session.
        """
        self._timer.stop()

    def reset(self) -> None:
        """Forget every seen sub-directory; the next scan re-emits all of them.

        Wired to the future hard-reset button.
        """
        self._seen_entries.clear()

    @property
    def running(self) -> bool:
        """Whether the poll timer is active."""
        return self._timer.isActive()

    def scan_once(self) -> list[str]:
        """Return newly-seen sub-directory paths since the last scan, sorted.

        Immediate sub-directories only; entries in :data:`_EXCLUDED_DIRS` and
        non-directory entries are skipped. Updates the seen-set; emits nothing.
        Called by the timer slot and directly by tests. Returns ``[]`` (and
        logs a warning) if the root is unreadable.
        """
        try:
            # "with" closes the directory handle on block exit, guarding against
            # locking the root directory after the scan.
            with os.scandir(self._root_path) as root_dir:
                current_entries = set()
                for entry in root_dir:
                    # TODO: remove?
                    if entry.name in _EXCLUDED_DIRS:
                        continue
                    try:
                        current_entries.add(os.path.abspath(entry.path))
                    except OSError:
                        continue
        except OSError as e:
            self._logger.warning(f"Cannot scan {self._root_path}: {e}")
            return []

        new_entries_sorted = sorted(current_entries - self._seen_entries)
        self._seen_entries.update(new_entries_sorted)
        return new_entries_sorted

    @QtCore.Slot()
    def _tick(self) -> None:
        """Timer slot: run :meth:`scan_once`, emit ``sigTimelapseFound`` per hit."""
        for path in self.scan_once():
            self.sigTimelapseFound.emit(path)


@dataclass(frozen=True)
class DiscoveredJob:
    """A timelapse folder resolved to a startable job.

    Attributes:
        seed_path: Absolute path to the timepoint-0 store in the folder.
        job_path: Absolute path of the job.
    """

    seed_path: str
    job_path: str


def _seed_key(name: str) -> str:
    """Sort key that puts a timelapse's timepoint-0 store first.

    Returns the store name from the ``"scan"`` marker onward
    (``"exp_scan__00__CAM.zarr"`` -> ``"scan__00__CAM.zarr"``). Sorting sibling
    names by this key is enough to pick the seed: ``scan0`` / ``scan_00`` /
    ``scan__00__`` is always the lexical minimum of a ``scanN`` family, padded
    or not, because the character right after ``"scan"`` is ``'0'`` for
    timepoint 0 and ``'1'``-``'9'`` for every other timepoint. The key is *not*
    a correct total order for unpadded names (``scan10`` sorts before
    ``scan2``) -- only ``sorted(...)[0]`` is meaningful.

    Raises:
        ValueError: ``name`` contains no ``"scan"`` marker.
    """
    i = name.find("scan")
    if i < 0:
        raise ValueError(f"timepoint store name has no 'scan' marker: {name!r}")
    return name[i:]


class JobQueue:
    """Pending timelapse folders, resolved to jobs on demand.

    Fed by ``DirectoryWatcher.sigTimelapseFound``. Pure and synchronous: no Qt,
    no store opening -- only ``os.listdir`` on the pending folders. The
    controller calls :meth:`next_ready` on its heartbeat and starts one job at
    a time.
    """

    def __init__(self) -> None:
        self._pending_jobs: list[str] = []

    def add(self, folder: str) -> None:
        """Register a timelapse folder as pending.

        Idempotent -- a folder already pending is ignored.
        """
        if folder not in self._pending_jobs:
            self._pending_jobs.append(folder)

    def next_ready(self) -> DiscoveredJob | None:
        """Return the first pending folder that now holds a store.

        Lists pending folders in insertion order; the first that contains at
        least one ``.zarr`` entry is removed from the queue and returned as a
        :class:`DiscoveredJob` whose ``seed_path`` is the timepoint-0 store
        (see :func:`_seed_key`). A folder with no store yet -- or one that
        cannot be listed -- stays pending. Returns ``None`` if nothing resolves
        this call.
        """
        for job in list(self._pending_jobs):
            try:
                if os.path.isdir(job):
                    names = os.listdir(job)
                    stores = sorted(
                        (n for n in names if n.lower().endswith(_STORE_SUFFIX)),
                        key=_seed_key,
                    )
                else:
                    stores = [job]
            except OSError:
                continue
            # stores = sorted(
            #     (n for n in names if n.lower().endswith(_STORE_SUFFIX)),
            #     key=_seed_key,
            # )
            if stores:
                self._pending_jobs.remove(job)
                return DiscoveredJob(
                    seed_path=os.path.join(job, stores[0]),
                    job_path=job,
                )
        return None

    def reset(self) -> None:
        """Drop all pending folders (for the future hard-reset button).

        Checks if the list of pending jobs is empty or not. If the list
        is not empty, then the pending jobs will be cleared.
        """
        if self._pending_jobs:
            self._pending_jobs.clear()

    def __len__(self) -> int:
        """Number of pending folders."""
        return len(self._pending_jobs)


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
