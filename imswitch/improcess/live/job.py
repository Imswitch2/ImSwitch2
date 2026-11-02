"""Shared preparation of one logical live-reconstruction job.

The legacy DirectoryWatcher path established an important boundary: a watched
*root entry* is the logical job, while a seed dataset/store is only the first
physical object used to classify and open that job.  Session processing must
preserve the same boundary instead of treating its catalog ``primary_path`` as
an independent live-start protocol.

This module contains the common bridge used by both controllers:

    root entry -> JobQueue seed resolution -> source probe -> compatibility
               -> LiveSource construction

It deliberately does not own queueing or UI policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from imswitch.improcess.model.hdf5_access import RawHdf5AccessConflict

from .discovery import DiscoveredJob, JobQueue
from .source_factory import make_live_source
from .source_type import RawSourceType, probe_source_type


class LiveJobState(str, Enum):
    READY = "ready"
    PENDING = "pending"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparedLiveJob:
    """A logical job resolved exactly as the DirectoryWatcher path resolves it."""

    discovered: DiscoveredJob
    source_type: RawSourceType

    @property
    def job_path(self) -> str:
        return self.discovered.job_path

    @property
    def seed_path(self) -> str:
        return self.discovered.seed_path

    def make_source(self, *, detector_name=None):
        """Build the reader from the already-probed source type."""
        return make_live_source(
            self.seed_path,
            self.source_type,
            detector_name=detector_name,
        )


@dataclass(frozen=True)
class LiveJobResolution:
    state: LiveJobState
    prepared: PreparedLiveJob | None = None
    reason: str = ""


def prepare_discovered_job(discovered: DiscoveredJob, reconstructor) -> LiveJobResolution:
    """Probe/validate a :class:`DiscoveredJob` using the proven live semantics.

    ``PENDING`` means the seed exists conceptually but is not readable enough to
    classify yet, so callers should leave/requeue the logical job. ``FAILED`` is
    a persistent access conflict that cannot resolve while the current process
    keeps its incompatible handle open. ``UNSUPPORTED`` is a readable source
    the selected reconstructor explicitly rejects.
    """
    try:
        source_type = probe_source_type(discovered.seed_path)
    except RawHdf5AccessConflict as exc:
        return LiveJobResolution(
            LiveJobState.FAILED,
            reason=str(exc),
        )
    if source_type is None:
        return LiveJobResolution(
            LiveJobState.PENDING,
            reason=f"{discovered.seed_path} is not readable yet",
        )

    accepts = getattr(reconstructor, "accepts_raw_source", None)
    if callable(accepts) and not accepts(source_type):
        name = getattr(reconstructor, "name", getattr(reconstructor, "id", "reconstructor"))
        return LiveJobResolution(
            LiveJobState.UNSUPPORTED,
            reason=(
                f"{name} cannot reconstruct a {source_type.format_id} "
                f"{source_type.layout} recording of "
                f"{source_type.frames_per_stack} frame(s) per timepoint"
            ),
        )

    return LiveJobResolution(
        LiveJobState.READY,
        prepared=PreparedLiveJob(discovered, source_type),
    )


def prepare_entry_job(job_path: str | Path, reconstructor) -> LiveJobResolution:
    """Resolve a watched root entry then prepare it for live reconstruction."""
    discovered = JobQueue.resolve_entry(str(job_path))
    if discovered is None:
        return LiveJobResolution(
            LiveJobState.PENDING,
            reason=f"{job_path} does not contain a startable dataset yet",
        )
    return prepare_discovered_job(discovered, reconstructor)




def start_prepared_live_job(
    live_controller,
    prepared: PreparedLiveJob,
    reconstructor,
    params: dict,
    *,
    name: str,
) -> bool:
    """Construct the source and start the shared live controller.

    Both legacy DirectoryWatcher processing and Session processing call this
    function, so source construction and the seed passed as ``source_arg``
    cannot drift between the two orchestration paths.
    """
    source = prepared.make_source(detector_name=None)
    return bool(
        live_controller.start(
            reconstructor,
            source,
            params,
            source_arg=prepared.seed_path,
            name=name,
            source_dataset_name=(
                "time-lapse" if prepared.source_type.is_timelapse else None
            ),
        )
    )

def watcher_entry_for_source(root_path: str | Path, source_path: str | Path) -> str:
    """Return the immediate watched entry that owns ``source_path``.

    Session catalog discovery may already have descended one level into a
    recording container and stored that child as ``primary_path``.  The old
    DirectoryWatcher instead hands ``JobQueue`` the immediate root entry.  Map
    back to that entry so Session and watcher processing use identical seed
    resolution.
    """
    root = Path(root_path).expanduser().resolve()
    source = Path(source_path).expanduser().resolve()
    try:
        relative = source.relative_to(root)
    except (ValueError, OSError):
        return str(source)
    if not relative.parts:
        return str(source)
    return str(root / relative.parts[0])
