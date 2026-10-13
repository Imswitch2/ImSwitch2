"""DatasetResolver (DR): turn an entry into a dataset of shape (T, *S, Y, X).

An entry is packaged one of three ways. A *container* is one HDF5 file or one
Zarr store, and a *data array* is an array with at least three dimensions --
lower-dimensional arrays in a container are bookkeeping, not data.

* **P1** -- one container, one data array.
* **P2** -- one container, several data arrays, one per timepoint.
* **P3** -- a plain folder of containers, one per timepoint.

In P2 and P3 the timepoint index is read from the name (``scan3``,
``_time07``), and that index -- not how many pieces there are -- is what makes
an entry P2. A lapse still being recorded may hold only its first piece.

Every entry resolves to one of three outcomes: a :class:`Dataset`, *not raw
data* (dropped), or *pending* -- not decidable yet. The DR keeps pending
entries and tries them again on an interval.
"""

import enum
import os
import re
from dataclasses import dataclass

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger


@dataclass(frozen=True)
class Dataset:
    """Raw data, as the shape (T, *S, Y, X).

    Attributes:
        entry: The root entry this was resolved from.
        packaging: ``"P1"``, ``"P2"`` or ``"P3"``.
        format: ``"zarr"`` or ``"hdf5"``.
        pieces: ``(container, array)`` pairs found so far, in timepoint
            order. One for P1; for P2 / P3, later pieces arrive while
            recording and are followed downstream.
        T: Number of timepoints, or ``None`` when not known.
        S: ``((name, size), ...)`` -- the axes swept within one timepoint.
            Empty when a timepoint is a single frame.
        frame_shape: ``(Y, X)``.
        dtype: Pixel type.
    """

    entry: str
    packaging: str
    format: str
    pieces: tuple[tuple[str, str], ...]
    T: int | None
    S: tuple[tuple[str, int], ...]
    frame_shape: tuple[int, int]
    dtype: np.dtype

    @property
    def frames_per_timepoint(self) -> int:
        return int(np.prod([size for _, size in self.S], dtype=np.int64))


class Status(enum.Enum):
    RESOLVED = "resolved"
    NOT_RAW = "not raw data"
    PENDING = "pending"


@dataclass(frozen=True)
class Resolution:
    status: Status
    dataset: Dataset | None = None
    reason: str = ""


_FORMATS = {".zarr": "zarr", ".h5": "hdf5", ".hdf5": "hdf5"}

#: A timepoint marker followed by its index: ``scan3``, ``time07``,
#: ``scan__00__``. The digits are part of the match, so ``timelapse`` and
#: ``timestamp`` carry no index.
_INDEX = re.compile(r"(?i)(?:scan|time)_*(\d+)")


@dataclass(frozen=True)
class _Array:
    path: str
    shape: tuple[int, ...]
    dtype: np.dtype
    attrs: dict


def resolve(entry: str) -> Resolution:
    """Decide what ``entry`` is. Pure: no state, nothing kept open."""
    if not os.path.exists(entry):
        return Resolution(Status.NOT_RAW, reason="no longer exists")

    fmt = _format_of(entry)
    if fmt is not None:
        return _resolve_container(entry, fmt)
    if os.path.isdir(entry):
        return _resolve_folder(entry)
    return Resolution(Status.NOT_RAW, reason="not a container or a folder")


# --- the three packagings ---------------------------------------------------

def _resolve_container(entry: str, fmt: str) -> Resolution:
    arrays = _read_arrays(entry, fmt)
    if not arrays:
        # Unreadable, or its data array not created yet.
        return Resolution(Status.PENDING)

    indexed = [a for a in arrays if _index(a.path) is not None]
    if indexed:
        if len({_index(a.path) for a in indexed}) < len(indexed):
            return Resolution(
                Status.NOT_RAW,
                reason="several data arrays per timepoint "
                       "(several detectors are not supported yet)",
            )
        return _from_pieces(entry, "P2", fmt, [(entry, a) for a in indexed])

    if len(arrays) > 1:
        return Resolution(
            Status.NOT_RAW,
            reason=f"{len(arrays)} data arrays without a timepoint index "
                   f"(several detectors are not supported yet)",
        )

    array = arrays[0]
    S = _stack_sizes(array)
    if S is None:
        return Resolution(Status.PENDING)

    # With frames_per_stack the array is a run of stacks, so T is however
    # many it plans to hold -- unknown if it does not say. Without it, the
    # whole finished array is one timepoint: T = 1, S = (N,).
    T = 1
    if _positive_int(array.attrs.get("recording:frames_per_stack")) is not None:
        frames = _positive_int(array.attrs.get("recording:expected_frames"))
        T = frames // S[0] if frames is not None else None
    return _resolved(entry, "P1", fmt, [(entry, array)], T, S, array)


def _resolve_folder(entry: str) -> Resolution:
    try:
        names = os.listdir(entry)
    except OSError:
        return Resolution(Status.PENDING)

    containers = [n for n in names if _format_of(n) is not None]
    if not containers:
        # Created, not written into yet -- or not raw data at all. Only
        # waiting can tell, and waiting costs a folder listing.
        return Resolution(Status.PENDING)

    indexed = [n for n in containers if _index(n) is not None]
    names = sorted(indexed, key=_index) if indexed else sorted(containers)

    first = os.path.join(entry, names[0])
    arrays = _read_arrays(first, _format_of(first))
    if not arrays:
        return Resolution(Status.PENDING)
    if len(arrays) > 1:
        return Resolution(
            Status.NOT_RAW,
            reason=f"{names[0]} holds {len(arrays)} data arrays "
                   f"(several detectors are not supported yet)",
        )

    pieces = [(first, arrays[0])]
    return _from_pieces(entry, "P3", _format_of(first), pieces)


def _from_pieces(entry, packaging, fmt, pieces) -> Resolution:
    """P2 / P3: one piece per timepoint, so a piece's leading axes are S."""
    pieces = sorted(pieces, key=lambda p: _index(_piece_name(p)) or 0)
    first = pieces[0][1]
    S = _stack_sizes(first)
    if S is None:
        return Resolution(Status.PENDING)
    T = _positive_int(first.attrs.get("recording:num_timepoints"))
    return _resolved(entry, packaging, fmt, pieces, T, S, first)


def _resolved(entry, packaging, fmt, pieces, T, S, array) -> Resolution:
    dataset = Dataset(
        entry=entry,
        packaging=packaging,
        format=fmt,
        pieces=tuple(
            (container, a.path) for container, a in pieces
        ),
        T=T,
        S=tuple((f"s{i}", size) for i, size in enumerate(S)),
        frame_shape=(int(array.shape[-2]), int(array.shape[-1])),
        dtype=np.dtype(array.dtype),
    )
    return Resolution(Status.RESOLVED, dataset=dataset)


# --- helpers ----------------------------------------------------------------

def _stack_sizes(array: _Array) -> tuple[int, ...] | None:
    """The S sizes of one timepoint, or ``None`` if not knowable yet.

    ``recording:frames_per_stack`` wins: it is known before a single frame is
    written. Without it, S is the array's own leading axes -- which are only
    final once the array is finished, since a recording grows its leading
    axis frame by frame.
    """
    lead = array.shape[:-2]
    per_stack = _positive_int(array.attrs.get("recording:frames_per_stack"))
    if per_stack is not None and len(lead) == 1:
        return (per_stack,)
    if _still_writing(array.attrs):
        return None
    if not lead or 0 in lead:
        return None
    return tuple(int(n) for n in lead)


def _still_writing(attrs: dict) -> bool:
    value = attrs.get("writing")
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value) if value is not None else False


def _read_arrays(container: str, fmt: str) -> list[_Array] | None:
    """Every data array in ``container``, or ``None`` if it cannot be read."""
    try:
        if fmt == "hdf5":
            return _hdf5_arrays(container)
        return _zarr_arrays(container)
    except Exception:
        # Still being created, held by the writer, half-written metadata.
        return None


def _hdf5_arrays(path: str) -> list[_Array]:
    import h5py

    found = []

    def visit(name, node):
        if isinstance(node, h5py.Dataset) and node.ndim >= 3:
            found.append(_Array(name, tuple(node.shape), node.dtype, dict(node.attrs)))

    try:
        f = h5py.File(path, "r", libver="latest", swmr=True)
    except OSError:
        f = h5py.File(path, "r")
    with f:
        f.visititems(visit)
    return found


def _zarr_arrays(path: str) -> list[_Array]:
    import zarr

    root = zarr.open(path, mode="r")
    found = []

    def walk(node, prefix):
        if isinstance(node, zarr.Array):
            if node.ndim >= 3:
                found.append(_Array(prefix, tuple(node.shape), node.dtype, dict(node.attrs)))
            return
        for name, child in node.members():
            walk(child, f"{prefix}/{name}" if prefix else name)

    walk(root, "")
    return found


def _format_of(path: str) -> str | None:
    return _FORMATS.get(os.path.splitext(path.rstrip("/\\"))[1].lower())


def _index(name: str) -> int | None:
    """The timepoint index in a container name or an array path."""
    match = _INDEX.search(name)
    return int(match.group(1)) if match else None


def _piece_name(piece) -> str:
    container, array = piece
    # P2 carries the index in the array path, P3 in the container name.
    return array.path if _index(array.path) is not None else os.path.basename(container)


def _positive_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    try:
        number = int(np.asarray(value).item())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class DatasetResolver(QtCore.QObject):
    """Resolves entries into datasets, retrying the undecided ones.

    Feed it entries with :meth:`add`. Each is resolved at once; one that is
    pending is kept and tried again every interval while running. A resolved
    entry is emitted on ``sigResolved``; one that is not raw data is logged
    and dropped. Either way it is forgotten: the DR remembers only what is
    pending, and feeding each entry once is the DO's job.
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
        """Resolve ``entry`` now; keep it for retrying if it is pending."""
        if entry in self._pending:
            return
        if not self._try(entry):
            self._pending.append(entry)

    @QtCore.Slot()
    def retry(self) -> None:
        """Try every pending entry again, in the order they arrived."""
        for entry in list(self._pending):
            if self._try(entry):
                self._pending.remove(entry)

    def _try(self, entry: str) -> bool:
        """Resolve once. ``True`` if the entry is settled either way."""
        result = resolve(entry)
        if result.status is Status.PENDING:
            return False
        if result.status is Status.NOT_RAW:
            self._logger.info(
                f"Not raw data: {os.path.basename(entry)} ({result.reason})"
            )
            return True
        self.sigResolved.emit(result.dataset)
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
