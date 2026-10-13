"""DatasetResolver (DR): turn an entry into a dataset of shape (T, *S, Y, X).

A *container* is one HDF5 file or Zarr store; a *data array* is one with at
least two dimensions (lower ones are bookkeeping). An entry is packaged as:

* **P1** -- one container, one data array;
* **P2** -- one container, one data array per timepoint;
* **P3** -- a folder, one container per timepoint.

P2 and P3 are told apart from P1 by the timepoint index in the name
(``scan3``, ``_time07``), not by counting: a lapse still being recorded may
hold only its first piece.
"""

import os
import re
from dataclasses import dataclass

import h5py
import numpy as np
import zarr
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger


@dataclass(frozen=True)
class EntryInfo:
    """What an entry holds, as (T, *S, Y, X).

    ``pieces`` are the ``(container, array)`` pairs found so far, in timepoint
    order. ``T`` is ``None`` when unknown. ``S`` is ``((name, size), ...)``,
    empty when a timepoint is a single frame.
    """

    entry: str
    packaging: str          # "P1" | "P2" | "P3"
    format: str             # "zarr" | "hdf5"
    pieces: tuple[tuple[str, str], ...]
    timelapse: bool
    T: int | None
    S: tuple[tuple[str, int], ...]
    frame_shape: tuple[int, int]
    dtype: np.dtype

    @property
    def frames_per_timepoint(self) -> int:
        return int(np.prod([size for _, size in self.S], dtype=np.int64))


class DatasetResolver(QtCore.QObject):
    """Resolves entries into :class:`EntryInfo`, retrying the undecided ones.

    Each entry from :meth:`add` is resolved at once and emitted on
    ``sigResolved``; one that cannot be resolved yet is kept and retried every
    interval while running. An entry that is gone, or is neither a folder nor
    a container, is dropped. The DR remembers only what is pending --
    feeding each entry once is the DO's job.
    """

    sigResolved = QtCore.Signal(object)  # EntryInfo
    FORMATS = (
        ".h5",
        ".hdf5",
        ".zarr",
    )
    #: A timepoint marker and its index. The digits are required, so
    #: ``timelapse`` and ``timestamp`` carry no index.
    INDEX = re.compile(r"(?i)(?:scan|time)_*(\d+)")

    def __init__(self, interval_ms: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._pending: list[str] = []
        self._logger = initLogger(self, tryInheritParent=False)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.retry)

    # --- running -------------------------------------------------------------

    @property
    def pending(self) -> list[str]:
        return list(self._pending)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def add(self, entry: str) -> None:
        if entry not in self._pending and not self._settle(entry):
            self._pending.append(entry)

    @QtCore.Slot()
    def retry(self) -> None:
        for entry in list(self._pending):
            if self._settle(entry):
                self._pending.remove(entry)

    def _settle(self, entry: str) -> bool:
        """Resolve once; ``True`` if the entry is settled either way.

        The name is checked first, because ``resolve`` answers ``None`` for
        "not yet" -- a stray ``notes.txt`` or a deleted entry would otherwise
        be retried forever.
        """
        if not os.path.exists(entry) or not (
            os.path.isdir(entry) or entry.lower().endswith(self.FORMATS)
        ):
            self._logger.info(f"Ignored: {os.path.basename(entry)}")
            return True
        info = self.resolve(entry)
        if info is None:
            return False
        self.sigResolved.emit(info)
        return True

    # --- resolving -----------------------------------------------------------

    def resolve(self, entry: str) -> EntryInfo | None:
        """What ``entry`` holds, or ``None`` while that cannot be decided yet."""
        # 1. Gone.
        if not os.path.exists(entry):
            return None

        # 2. / 3. The container to open: a folder's first, or the entry itself.
        if os.path.isdir(entry) and not entry.lower().endswith(self.FORMATS):
            packaging = "P3"
            try:
                names = [n for n in os.listdir(entry) if n.lower().endswith(self.FORMATS)]
            except OSError:
                return None
            if not names:
                return None
            # By timepoint index (time2 before time10), then by name.
            first = min(names, key=lambda n: (self._index(n) is None, self._index(n) or 0, n))
            container = os.path.join(entry, first)
        else:
            packaging = None                 # P1 or P2: decided by what is inside
            container = entry

        # 2.1 Open it with the handle for its format.
        fmt = "zarr" if container.lower().endswith(".zarr") else "hdf5"
        arrays = (
            self._get_zarr_data_shapes(container) if fmt == "zarr"
            else self._get_hdf5_data_shapes(container)
        )
        if not arrays:
            return None                      # unreadable, or no data array yet

        # Arrays carrying a timepoint index (scan0, scan1, ...) make it P2.
        indexed = sorted(
            (a for a in arrays if self._index(a[0]) is not None),
            key=lambda a: self._index(a[0]),
        )
        if packaging is None:
            packaging = "P2" if indexed else "P1"
        # Several unindexed arrays mean several detectors -- the first for now.
        pieces = indexed if packaging == "P2" else [min(arrays, key=lambda a: a[0])]
        _, shape, dtype, attrs = pieces[0]

        # 2.2 Metadata: S, and the number of timepoints T.
        per_stack = self._int(attrs.get("recording:frames_per_stack"))
        if per_stack is not None and len(shape) == 3:
            S = (per_stack,)
        # 2.3 No metadata: S from the array's own shape -- final only once the
        #     array is finished, since a recording grows it frame by frame.
        elif attrs.get("writing", False) or 0 in shape[:-2]:
            return None
        else:
            S = tuple(int(n) for n in shape[:-2])

        if packaging != "P1":
            T = self._int(attrs.get("recording:num_timepoints"))
        elif per_stack is None:
            T = 1                            # one array, no metadata: one stack
        else:
            frames = self._int(attrs.get("recording:expected_frames"))
            T = frames // per_stack if frames is not None else None

        # 2.4
        return EntryInfo(
            entry=entry,
            packaging=packaging,
            format=fmt,
            pieces=tuple((container, path) for path, *_ in pieces),
            timelapse=T != 1,
            T=T,
            S=tuple((f"s{i}", size) for i, size in enumerate(S)),
            frame_shape=(int(shape[-2]), int(shape[-1])),
            dtype=np.dtype(dtype),
        )

    @staticmethod
    def _get_hdf5_data_shapes(container: str) -> list[tuple] | None:
        """Data arrays as ``(path, shape, dtype, attrs)``; ``None`` if unreadable."""
        found = []
        try:
            f = h5py.File(container, "r", libver="latest", swmr=True)
        except Exception:
            # still being created, held by writer, half-written
            return None
        with f:
            # attrs is copied: a node's attrs are only readable while open.
            f.visititems(
                lambda path, node: (
                    found.append((path, node.shape, node.dtype, dict(node.attrs)))
                    if node.ndim >= 2 else None
                ) if isinstance(node, h5py.Dataset) else None
            )
        return found

    @staticmethod
    def _get_zarr_data_shapes(container: str) -> list[tuple] | None:
        """Data arrays as ``(path, shape, dtype, attrs)``; ``None`` if unreadable."""
        found = []
        try:
            stack = [("", zarr.open(container, mode="r"))]
            while stack:
                path, node = stack.pop()
                if isinstance(node, zarr.Array):
                    if node.ndim >= 2:
                        found.append((path, node.shape, node.dtype, dict(node.attrs)))
                else:
                    stack += [
                        (f"{path}/{n}".lstrip("/"), c) for n, c in node.members()
                    ]
            return found
        except Exception:
            # still being created, held by writer, half-written
            return None

    @classmethod
    def _index(cls, name: str) -> int | None:
        match = cls.INDEX.search(name)
        return int(match.group(1)) if match else None

    @staticmethod
    def _int(value) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None


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
