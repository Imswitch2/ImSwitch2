"""Operation terminal for an interrupted disk-backed HDF5 or Zarr lapse.

A finalized scan partition describes its *timepoint*, not whether the whole
operation will ever produce the missing future timepoints.  This
small sidecar is written only after early STOP or abort, after every writer is
closed.  It is not a readiness credential: each referenced scan must still
pass the existing HDF5 finalization checks before processing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

TERMINAL_SUFFIX = ".lapse-stopped.json"


def terminal_path(anchor: str | Path) -> Path:
    anchor = Path(anchor)
    if anchor.suffix.lower() not in {".h5", ".hdf5", ".zarr"}:
        raise ValueError("Lapse terminal requires an HDF5 or Zarr public path")
    return Path(str(anchor) + TERMINAL_SUFFIX)


def write_lapse_terminal(anchor: str | Path, *, completed: int, planned: int,
                         outcome: str = "stopped_early") -> Path:
    """Publish an operation terminal, without touching scientific data.

    Publish only after the writer has closed and cleaned an aborted partition.
    The marker does not certify any individual timepoint as reconstructable.
    """
    path = terminal_path(anchor)
    completed, planned = int(completed), int(planned)
    if outcome not in {"stopped_early", "aborted"} or not (0 <= completed < planned):
        raise ValueError("Invalid operation terminal or completed count")
    if not (Path(anchor).is_file() or Path(anchor).is_dir()):
        raise FileNotFoundError(f"Cannot mark unpublished acquisition: {anchor}")
    record = {
        "version": 1, "outcome": outcome,
        "anchor": Path(anchor).name,
        "completed_timepoints": completed,
        "planned_timepoints": planned,
    }
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_lapse_terminal(anchor: str | Path, *,
                        expected_total: int | None = None) -> dict | None:
    """Read a valid terminal claim; consumers must verify physical T separately."""
    path = terminal_path(anchor)
    try:
        with path.open(encoding="utf-8") as stream:
            info = json.load(stream)
        if (not isinstance(info, dict) or info.get("version") != 1
                or info.get("outcome") not in {"stopped_early", "aborted"}
                or info.get("anchor") != Path(anchor).name):
            return None
        total = info.get("planned_timepoints")
        complete = info.get("completed_timepoints")
        if type(total) is not int or type(complete) is not int:
            return None
        if not 0 <= complete < total:
            return None
        if expected_total is not None and int(expected_total) != total:
            return None
        if not (Path(anchor).is_file() or Path(anchor).is_dir()):
            return None
        return info
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_early_stop(anchor: str | Path, *, completed: int, planned: int) -> Path:
    """Backward-compatible early-STOP writer (also accepts Zarr anchors)."""
    if int(completed) <= 0:
        raise ValueError("Early-stop terminal requires at least one complete timepoint")
    return write_lapse_terminal(anchor, completed=completed, planned=planned)


def read_early_stop(anchor: str | Path, *, expected_total: int | None = None) -> int | None:
    """Backward-compatible stopped prefix accessor (physical checks still required)."""
    record = read_lapse_terminal(anchor, expected_total=expected_total)
    return (int(record["completed_timepoints"]) if record is not None
            and record["outcome"] == "stopped_early" else None)
