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
  * output / cache dirs in :data:.
"""

import os
from dataclasses import dataclass

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger


#: Suffixes that name a dataset. Used to tell a store from a container: a
#: directory carrying one of these *is* the data, rather than a folder of it.
_STORE_SUFFIX = (
    ".zarr",
    ".h5",
    ".hdf5",
)

#: Of those, the ones stored as a directory. A plain *file* carrying such a
#: suffix is malformed rather than a dataset, so the watcher passes it over.
_DIR_STORE_SUFFIX = (
    ".zarr",
)


class DirectoryWatcher(QtCore.QObject):
    """Emits ``sigEntryFound`` for each new immediate entry of a root folder."""

    sigEntryFound = QtCore.Signal(str)  # absolute path of a newly-seen entry

    def __init__(
        self,
        root_path: str,
        *,
        poll_interval_ms: int = 1000,
        parent=None,
    ) -> None:
        """
        Args:
            root_path: Folder to watch for new entries.
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
        """Return newly-seen entry paths since the last scan, sorted.

        Immediate entries only. Every sub-directory is reported -- whether
        it is a container or a ``.zarr`` store is :class:`JobQueue`'s call,
        not this one's. Files are reported only when their suffix names a
        single-*file* dataset, so a stray ``.txt`` and a ``.zarr`` that is
        somehow a plain file are both passed over.

        No name is excluded by convention. A folder holding no dataset --
        an output directory, a cache -- simply never resolves into a job,
        and one that does is checked against the selected reconstructor
        before anything starts. A name list could never be exhaustive, so
        the real filter is the one that reads what is actually there.

        Updates the seen-set; emits nothing. Called by the timer slot and
        directly by tests. Returns ``[]`` (and logs a warning) if the root
        is unreadable.
        """
        try:
            # "with" closes the directory handle on block exit, guarding against
            # locking the root directory after the scan.
            with os.scandir(self._root_path) as root_dir:
                current_entries = set()
                for entry in root_dir:
                    try:
                        name = entry.name.lower()
                        is_dataset_file = (
                            name.endswith(_STORE_SUFFIX)
                            and not name.endswith(_DIR_STORE_SUFFIX)
                        )
                        if entry.is_dir() or is_dataset_file:
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
        """Timer slot: run :meth:`scan_once`, emit ``sigEntryFound`` per hit."""
        for path in self.scan_once():
            self.sigEntryFound.emit(path)


@dataclass(frozen=True)
class DiscoveredJob:
    """A root entry resolved to a startable job.

    Attributes:
        seed_path: The dataset to open first. For a container that is its
            timepoint-0 store; for a single dataset it is the entry itself.
        job_path: The root entry this was resolved from. Names the job, and
            is what goes back into the queue if it cannot start yet.
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


def _pick_seed(names: list[str]) -> str:
    """The timepoint-0 store among sibling per-timepoint stores.

    Prefers the ``"scan"``-marker ordering (:func:`_seed_key`), falling back to
    a plain lexical minimum when no sibling carries the marker. The fallback is
    not cosmetic: ``_seed_key`` *raises* on an unmarked name, so one oddly-named
    store would otherwise take down the controller's whole heartbeat rather than
    just its own folder.
    """
    marked = [n for n in names if "scan" in n]
    if marked:
        return sorted(marked, key=_seed_key)[0]
    return sorted(names)[0]


class JobQueue:
    """Pending root entries, resolved to jobs on demand.

    Fed by ``DirectoryWatcher.sigEntryFound``. Pure and synchronous: no Qt
    and no store opening -- only ``os.listdir`` on pending containers, so
    a half-written store is never touched here. *What* a resolved job turns
    out to be is settled later, by probing it. The controller calls
    :meth:`next_ready` on its heartbeat and starts one job at a time.
    """

    def __init__(self) -> None:
        self._pending_jobs: list[str] = []

    def add(self, entry: str) -> None:
        """Register a root entry as pending.

        Idempotent -- an entry already pending is ignored.
        """
        if entry not in self._pending_jobs:
            self._pending_jobs.append(entry)

    def next_ready(self) -> DiscoveredJob | None:
        """Return the first pending entry that is now startable, or ``None``.

        An entry resolves in one of two ways:

        * a **container** -- a directory whose own name carries no store suffix
          -- resolves once it holds at least one store, seeded by the
          timepoint-0 one (see :func:`_pick_seed`);
        * anything else is itself the dataset: a single file, or a directory
          that *is* a store because its name says so (``measurement.zarr``).

        Entries are walked in insertion order and the first that resolves is
        removed from the queue. One that does not -- a folder a recording has
        created but not yet written into, or one that cannot be listed -- stays
        pending for a later call, because that is the ordinary state of a
        recording that has only just started.
        """
        for job_path in list(self._pending_jobs):
            seed_path = self._resolve_seed(job_path)
            if seed_path is None:
                continue
            self._pending_jobs.remove(job_path)
            return DiscoveredJob(seed_path, job_path)

        return None

    @staticmethod
    def _resolve_seed(job_path: str) -> str | None:
        """The dataset to open first for ``job_path``, or ``None`` if not yet.

        ``None`` is the "still waiting" answer and never an error, so the
        caller leaves such an entry pending rather than dropping it.
        """
        if not os.path.exists(job_path):
            # Reported by the watcher but not there when asked. Without this
            # the else-branch below would hand it out as a single dataset,
            # since 'not a directory' and 'not there at all' look alike.
            return None

        is_container = (
            os.path.isdir(job_path)
            and not job_path.lower().endswith(_STORE_SUFFIX)
        )
        if not is_container:
            return job_path

        try:
            names = os.listdir(job_path)
        except OSError:
            return None

        stores = [n for n in names if n.lower().endswith(_STORE_SUFFIX)]
        if not stores:
            # Created but not yet written into. Ordinary, not a failure.
            return None
        return os.path.join(job_path, _pick_seed(stores))


    def reset(self) -> None:
        """Drop all pending entries (the hard-reset button).

        Checks if the list of pending jobs is empty or not. If the list
        is not empty, then the pending jobs will be cleared.
        """
        if self._pending_jobs:
            self._pending_jobs.clear()

    def __len__(self) -> int:
        """Number of pending entries."""
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
