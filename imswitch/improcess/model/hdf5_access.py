"""Shared read-only HDF5 access for raw ImProcess acquisition data.

Raw recordings may be inspected simultaneously by Current Data / Working Set,
metadata readers, offline lapse readers, and the live/Auto pipeline.  h5py keeps
one process-global open state per physical HDF5 file, so mixing an ordinary
read handle with an SWMR read handle for the same path can fail with an
incompatible-open-flags error.

Keep every *raw acquisition* reader on the same SWMR-capable read policy.  This
module is intentionally not used for result files or writers.
"""

from __future__ import annotations

import os
from os import PathLike
from typing import Any

import h5py


class RawHdf5AccessConflict(OSError):
    """A raw HDF5 file is already open in this process incompatibly.

    Unlike a newly-created file that simply is not readable yet, this state does
    not resolve while the incompatible in-process handle remains open.  Queue
    preflight can therefore report it as a real failure instead of retrying the
    job forever.
    """


_SWMR_MISMATCH_MARKER = "swmr read access flag not the same for file that is already open"


def _canonical_path(path: str | PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fsdecode(path))))


def raw_hdf5_open_handle_count(path: str | PathLike[str]) -> int:
    """Count live HDF5 file handles for ``path`` in this Python process."""
    target = _canonical_path(path)
    count = 0
    for file_id in h5py.h5f.get_obj_ids(h5py.h5f.OBJ_ALL, h5py.h5f.OBJ_FILE):
        try:
            name = h5py.h5f.get_name(file_id)
        except Exception:
            continue
        if not name:
            continue
        try:
            candidate = _canonical_path(name)
        except (TypeError, ValueError, OSError):
            continue
        if candidate == target:
            count += 1
    return count


def assert_raw_hdf5_new_reader_available(path: str | PathLike[str]) -> None:
    """Reject creation of a second raw HDF5 reader for the same path.

    A loaded ``DataObj`` is allowed to keep and use its own HDF5 handle.  The
    unsafe state under investigation is specifically creation of an
    independent reader (for example a Session timelapse stream) while another
    handle to the same physical file is still alive.
    """
    count = raw_hdf5_open_handle_count(path)
    if count:
        plural = "handle" if count == 1 else "handles"
        raise RawHdf5AccessConflict(
            f"Cannot open a new raw HDF5 reader for {path}: "
            f"the file already has {count} live in-process {plural}. "
            "Unload the existing data source or restart ImProcess before "
            "starting timelapse/session reconstruction."
        )


def open_raw_hdf5(path: str | PathLike[str], **kwargs: Any) -> h5py.File:
    """Open a raw acquisition HDF5 file with the shared read-only policy.

    Callers may pass ordinary read-time h5py options, but may not override the
    access mode, ``libver`` or ``swmr`` policy.  Static/legacy HDF5 files are
    still readable in SWMR reader mode; no file contents are modified.
    """
    forbidden = {"mode", "libver", "swmr"}.intersection(kwargs)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise TypeError(f"open_raw_hdf5() owns option(s): {names}")

    try:
        return h5py.File(str(path), "r", libver="latest", swmr=True, **kwargs)
    except OSError as exc:
        # h5py/HDF5 rejects a SWMR reader when this same physical file is
        # already held by a non-SWMR reader in the process.  This is the exact
        # collision that used to make Auto look merely 'not readable yet'.
        if _SWMR_MISMATCH_MARKER in str(exc).lower():
            raise RawHdf5AccessConflict(
                f"Raw HDF5 access conflict for {path}: {exc}"
            ) from exc
        raise


__all__ = [
    "RawHdf5AccessConflict",
    "assert_raw_hdf5_new_reader_available",
    "open_raw_hdf5",
    "raw_hdf5_open_handle_count",
]
