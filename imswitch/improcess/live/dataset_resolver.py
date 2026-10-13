"""DatasetResolver (DR): turn an entry into a dataset of shape (T, *S, Y, X).

A *container* is one HDF5 file or Zarr store; a *data array* is one with at
least three dimensions (lower ones are bookkeeping). An entry is packaged as:

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
class Dataset:
    """Raw data as (T, *S, Y, X).

    ``pieces`` are the ``(container, array)`` pairs found so far, in timepoint
    order. ``T`` is ``None`` when unknown. ``S`` is ``((name, size), ...)``,
    empty when a timepoint is a single frame.
    """

    entry: str
    packaging: str          # "P1" | "P2" | "P3"
    format: str             # "zarr" | "hdf5"
    pieces: tuple[tuple[str, str], ...]
    T: int | None
    S: tuple[tuple[str, int], ...]
    frame_shape: tuple[int, int]
    dtype: np.dtype

    @property
    def frames_per_timepoint(self) -> int:
        return int(np.prod([size for _, size in self.S], dtype=np.int64))


class NotRawData(Exception):
    """The entry is settled, and it is not raw data."""


_FORMATS = {".zarr": "zarr", ".h5": "hdf5", ".hdf5": "hdf5"}
_INDEX = re.compile(r"(?i)(?:scan|time)_*(\d+)")  # digits required: not "timelapse"
_DETECTORS = "several detectors are not supported yet"


def resolve(entry: str) -> Dataset | None:
    """The dataset in ``entry``, or ``None`` while it cannot be decided yet.

    Raises:
        NotRawData: ``entry`` is settled and holds no raw data.
    """
    if not os.path.exists(entry):
        raise NotRawData("no longer exists")

    fmt = _format(entry)
    if fmt is not None:                                  # P1 or P2
        arrays = _arrays(entry, fmt)
        if not arrays:
            return None
        indexed = sorted(
            (a for a in arrays if _index(a[0]) is not None),
            key=lambda a: _index(a[0]),
        )
        if indexed:
            if len({_index(a[0]) for a in indexed}) < len(indexed):
                raise NotRawData(_DETECTORS)
            return _dataset(entry, "P2", fmt, [(entry, a) for a in indexed])
        if len(arrays) > 1:
            raise NotRawData(_DETECTORS)
        return _dataset(entry, "P1", fmt, [(entry, arrays[0])])

    if not os.path.isdir(entry):
        raise NotRawData("not a container or a folder")

    try:                                                 # P3
        names = [n for n in os.listdir(entry) if _format(n) is not None]
    except OSError:
        return None
    if not names:
        return None
    # Indexed containers first, by index (time2 before time10); then by name.
    first = min(names, key=lambda n: (_index(n) is None, _index(n) or 0, n))
    container = os.path.join(entry, first)
    arrays = _arrays(container, _format(first))
    if not arrays:
        return None
    if len(arrays) > 1:
        raise NotRawData(_DETECTORS)
    return _dataset(entry, "P3", _format(first), [(container, arrays[0])])


def _dataset(entry, packaging, fmt, pieces) -> Dataset | None:
    """Build the dataset from its first piece, or ``None`` if S is not known yet.

    ``recording:frames_per_stack`` gives S before a single frame is written.
    Without it S is the piece's leading axes, which are only final once the
    piece is finished -- a recording grows its leading axis frame by frame.
    """
    _, shape, dtype, attrs = pieces[0][1]
    per_stack = _int(attrs.get("recording:frames_per_stack"))

    if per_stack is not None and len(shape) == 3:
        S = (per_stack,)
    elif bool(attrs.get("writing", False)) or 0 in shape[:-2]:
        return None
    else:
        S = tuple(int(n) for n in shape[:-2])

    if packaging != "P1":
        T = _int(attrs.get("recording:num_timepoints"))
    elif per_stack is None:
        T = 1                       # a finished array without metadata: one stack
    else:
        frames = _int(attrs.get("recording:expected_frames"))
        T = frames // per_stack if frames is not None else None

    return Dataset(
        entry=entry,
        packaging=packaging,
        format=fmt,
        pieces=tuple((container, array[0]) for container, array in pieces),
        T=T,
        S=tuple((f"s{i}", size) for i, size in enumerate(S)),
        frame_shape=(int(shape[-2]), int(shape[-1])),
        dtype=np.dtype(dtype),
    )


def _arrays(container: str, fmt: str) -> list[tuple] | None:
    """Data arrays as ``(path, shape, dtype, attrs)``; ``None`` if unreadable."""
    found = []

    def keep(path, node):
        if node.ndim >= 3:
            found.append((path, tuple(node.shape), node.dtype, dict(node.attrs)))

    try:
        if fmt == "hdf5":
            try:
                f = h5py.File(container, "r", libver="latest", swmr=True)
            except OSError:
                f = h5py.File(container, "r")
            with f:
                f.visititems(
                    lambda path, node: keep(path, node)
                    if isinstance(node, h5py.Dataset) else None
                )
        else:
            stack = [("", zarr.open(container, mode="r"))]
            while stack:
                path, node = stack.pop()
                if isinstance(node, zarr.Array):
                    keep(path, node)
                else:
                    stack += [(f"{path}/{n}".lstrip("/"), c) for n, c in node.members()]
    except Exception:
        return None   # still being created, held by the writer, half-written
    return found


def _format(path: str) -> str | None:
    return _FORMATS.get(os.path.splitext(path.rstrip("/\\"))[1].lower())


def _index(name: str) -> int | None:
    match = _INDEX.search(name)
    return int(match.group(1)) if match else None


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class DatasetResolver(QtCore.QObject):
    """Resolves entries into datasets, retrying the undecided ones.

    Each entry from :meth:`add` is resolved at once. A dataset is emitted on
    ``sigResolved``; not raw data is logged and dropped; a pending entry is
    kept and retried every interval while running. The DR remembers only
    what is pending -- feeding each entry once is the DO's job.
    """

    sigResolved = QtCore.Signal(object)  # Dataset

    def __init__(self, interval_ms: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._pending: list[str] = []
        self._logger = initLogger(self, tryInheritParent=False)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.retry)

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
        """Resolve once; ``True`` if the entry is settled either way."""
        try:
            dataset = resolve(entry)
        except NotRawData as e:
            self._logger.info(f"Not raw data: {os.path.basename(entry)} ({e})")
            return True
        if dataset is None:
            return False
        self.sigResolved.emit(dataset)
        return True


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
