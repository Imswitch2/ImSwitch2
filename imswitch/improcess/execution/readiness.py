"""Conservative, metadata-only readiness for Session Follow Auto jobs.

Only explicit ImControl per-detector write barriers can make an automatic job
runnable. A quiet file, a readable shape, an expected count, or an old file
without markers is *not* evidence of completion. The caller caches finalized
items and throttles probes; this module owns no queue or directory observer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycleMarkers, normalize_recording_lifecycle,
    WRITER_STATE_FINALIZED, WRITER_STATE_WRITING,
)
from imswitch.improcess.model.timepoint_scope import coerce_timepoint_scope


class ReadinessState(str, Enum):
    READY = "ready"
    ACQUIRING = "acquiring"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True)
class Readiness:
    state: ReadinessState
    reason: str = ""
    live_readable: bool = False


@dataclass(frozen=True)
class PhysicalReference:
    path: str
    dataset: str | None
    logical_t: int


@dataclass(frozen=True)
class PhysicalEvidence:
    """One physical detector partition's native lifecycle markers."""
    format_id: str
    attrs: dict
    markers: RecordingLifecycleMarkers
    allocated_frames: int


def references_for_scope(source, scope, total: int | None, *, is_lapse: bool):
    """Use the Session's logical SourceRefs, not directory-name heuristics."""
    start, stop = coerce_timepoint_scope(scope).bounds(total)
    index = getattr(source, "sourceMetadata", None)
    if index is not None and is_lapse:
        try:
            items = index.channel(index.anchor_detector).candidates
            refs = [PhysicalReference(str(item.path), item.dataset,
                     int(item.ordinal) - int(index.anchor.ordinal) + int(index.anchor_index))
                    for item in items]
        except (AttributeError, KeyError, TypeError, ValueError):
            refs = []
    else:
        raw_refs = list(getattr(source, "source_refs", ()) or ())
        if raw_refs:
            refs = [PhysicalReference(str(ref.path), ref.dataset,
                     int(ref.timepoint_index) if ref.timepoint_index is not None else -1)
                    for ref in raw_refs]
        else:
            path = getattr(source, "dataPath", None) or getattr(source, "primary_path", None)
            dataset = getattr(source, "datasetName", None) or getattr(source, "primary_dataset", None)
            refs = [PhysicalReference(str(path), dataset, 0)] if path else []
    if not is_lapse:
        return ([PhysicalReference(refs[0].path, refs[0].dataset, 0)]
                if refs else []), (0, 1)
    if stop is None:
        return (), (start, stop)
    return [ref for ref in refs if start <= ref.logical_t < stop], (start, stop)


def _find_detector_data(root, dataset):
    """Find an explicitly named detector; refuse alphabetical auto-selection."""
    name = str(dataset or "").strip("/")
    if name in ("time-lapse", "raw"):
        name = ""
    if name:
        try:
            group = root[name]
            # A SourceRef can identify either its detector group or the
            # dataset itself (e.g. scan2/CAM/data).
            if hasattr(group, "shape"):
                return group
            return group["data"] if "data" in group else group
        except (KeyError, TypeError, ValueError):
            return None
    # No detector was supplied. Only accept exactly one candidate at root.
    options = []
    for key in root.keys():
        child = root[key]
        try:
            if "data" in child:
                options.append(child["data"])
        except (TypeError, ValueError):
            pass
    return options[0] if len(options) == 1 else None


def _read_hdf5(path, dataset):
    import h5py
    from imswitch.improcess.model.hdf5_access import (
        assert_raw_hdf5_new_reader_available,
    )
    assert_raw_hdf5_new_reader_available(path)
    # A non-SWMR, locked open should fail while a SWMR writer is active.
    # Do not attach a concurrent SWMR reader just to probe Auto readiness on
    # Windows: doing so has been observed to disrupt the ImControl writer.
    with h5py.File(path, "r", libver="latest", swmr=False, locking=True) as root:
        obj = _find_detector_data(root, dataset)
        if obj is None or not hasattr(obj, "shape"):
            return None
        attrs = dict(obj.attrs)
        parent = obj.parent
        committed = int(parent["frames_committed"][0]) if "frames_committed" in parent else None
        complete = bool(parent["stream_complete"][0]) if "stream_complete" in parent else None
        writing = attrs.get("writing")
        if writing is not None:
            writing = bool(writing)
        return PhysicalEvidence("hdf5", attrs, RecordingLifecycleMarkers(
            writing=writing, frames_committed=committed, stream_complete=complete),
            int(obj.shape[0]))


def _read_zarr(path, dataset):
    import zarr
    root = zarr.open(path, mode="r")
    obj = _find_detector_data(root, dataset)
    if obj is None or not hasattr(obj, "shape"):
        return None
    attrs = dict(obj.attrs)
    committed = attrs.get("recording:frames_committed")
    writing = attrs.get("writing")
    return PhysicalEvidence("zarr", attrs, RecordingLifecycleMarkers(
        writing=writing if isinstance(writing, bool) else None,
        frames_committed=int(committed) if type(committed) is int else None),
        int(obj.shape[0]))


def inspect_physical_reference(ref: PhysicalReference) -> PhysicalEvidence | None:
    """Read headers only. HDF5 never becomes an actively followed reader."""
    suffix = Path(ref.path).suffix.lower()
    if suffix == ".zarr":
        return _read_zarr(ref.path, ref.dataset)
    if suffix in (".hdf5", ".h5"):
        return _read_hdf5(ref.path, ref.dataset)
    return None


def evaluate_evidence(evidence: PhysicalEvidence | None, *, is_lapse: bool = False,
                      expected_total: int | None = None, logical_index: int = 0) -> Readiness:
    """Require explicit, self-consistent writer information to authorize Auto."""
    if evidence is None:
        return Readiness(ReadinessState.UNKNOWN, "Waiting for readable ImControl metadata")
    markers = evidence.markers
    if markers.frames_committed is None:
        return Readiness(ReadinessState.UNKNOWN, "No committed-frame barrier")
    if evidence.format_id == "hdf5" and markers.stream_complete is None:
        return Readiness(ReadinessState.UNKNOWN, "No HDF5 completion marker")
    if evidence.format_id == "zarr" and markers.writing is None:
        return Readiness(ReadinessState.UNKNOWN, "No Zarr writer-state marker")
    lifecycle = normalize_recording_lifecycle(evidence.attrs, markers)
    if any(issue.severity == "error" for issue in lifecycle.issues):
        return Readiness(ReadinessState.UNKNOWN, "Invalid or contradictory writer metadata")
    if is_lapse:
        try:
            declared = int(evidence.attrs.get("recording:num_timepoints"))
            actual_t = int(evidence.attrs.get("recording:lapse_index"))
        except (ValueError, TypeError):
            return Readiness(ReadinessState.UNKNOWN, "Missing explicit lapse membership")
        if expected_total is None or declared != expected_total or actual_t != logical_index:
            return Readiness(ReadinessState.UNKNOWN, "Ambiguous lapse membership or total")
    if lifecycle.completion_outcome == "stopped_early":
        return Readiness(ReadinessState.UNKNOWN, "Recording stopped early; select finalized T manually")
    if lifecycle.writer_state == WRITER_STATE_FINALIZED:
        if evidence.format_id == "hdf5" and markers.stream_complete is not True:
            return Readiness(ReadinessState.UNKNOWN, "HDF5 completion not confirmed")
        if evidence.allocated_frames < 1:
            return Readiness(ReadinessState.UNKNOWN, "No committed image frames")
        if markers.frames_committed > evidence.allocated_frames:
            return Readiness(ReadinessState.UNKNOWN, "Committed count exceeds stored array")
        if markers.frames_committed < evidence.allocated_frames:
            return Readiness(ReadinessState.UNKNOWN, "Recorded frames not fully committed")
        if lifecycle.planned_frames is not None and markers.frames_committed < lifecycle.planned_frames:
            return Readiness(ReadinessState.UNKNOWN, "Finalized timepoint is incomplete")
        return Readiness(ReadinessState.READY)
    if lifecycle.writer_state == WRITER_STATE_WRITING:
        return Readiness(ReadinessState.ACQUIRING, "Waiting for acquisition to finish",
                         live_readable=(evidence.format_id == "zarr" and markers.frames_committed > 0))
    if (evidence.format_id == "zarr" and markers.writing is True
            and markers.frames_committed > 0):
        return Readiness(ReadinessState.ACQUIRING, "Growing Zarr recording",
                         live_readable=True)
    return Readiness(ReadinessState.UNKNOWN, "Writer completion is unconfirmed")


def probe_scope(source, job, *, is_lapse: bool, inspected=None,
                max_new_files: int = 12) -> Readiness:
    """Bounded probe; `inspected` caches finalized physical refs across ticks."""
    refs, (start, stop) = references_for_scope(
        source, job.timepoint_scope, job.timepoint_total, is_lapse=is_lapse,
    )
    if stop is None:
        return Readiness(ReadinessState.UNKNOWN, "Unknown planned lapse extent")
    mapping = {ref.logical_t: ref for ref in refs if ref.logical_t >= 0}
    missing = [t for t in range(start, stop) if t not in mapping]
    if missing:
        # A live-follow job can start on explicitly committed Zarr T0 even
        # though the later logical timepoints do not exist yet. Finite Auto
        # must keep waiting; no absent final T is ever declared complete.
        first = mapping.get(start)
        evidence = None
        if first is not None:
            try:
                evidence = inspect_physical_reference(first)
                state = evaluate_evidence(evidence, is_lapse=is_lapse,
                    expected_total=job.timepoint_total, logical_index=start)
            except Exception:
                state = Readiness(ReadinessState.UNKNOWN)
            if (evidence is not None and evidence.format_id == "zarr"
                    and state.state in (ReadinessState.READY, ReadinessState.ACQUIRING)
                    and evidence.markers.frames_committed is not None
                    and evidence.markers.frames_committed > 0):
                return Readiness(ReadinessState.ACQUIRING,
                    f"Waiting for logical T{missing[0] + 1} of {stop}", live_readable=True)
        return Readiness(ReadinessState.UNKNOWN,
                         f"Waiting for logical T{missing[0] + 1} of {stop}")
    cache = inspected if inspected is not None else {}
    pending_live = False
    count = 0
    for t in range(start, stop):
        ref = mapping[t]
        key = (ref.path, ref.dataset, t)
        if key in cache:
            continue
        if count >= max_new_files:
            return Readiness(ReadinessState.UNKNOWN, "Checking completed timepoints")
        count += 1
        try:
            evidence = inspect_physical_reference(ref)
        except Exception as exc:
            return Readiness(ReadinessState.UNKNOWN,
                             f"Waiting for safe source access: {type(exc).__name__}")
        state = evaluate_evidence(evidence, is_lapse=is_lapse,
                                  expected_total=job.timepoint_total,
                                  logical_index=t)
        if state.state != ReadinessState.READY:
            if state.live_readable:
                pending_live = True
            return Readiness(state.state, state.reason, live_readable=pending_live)
        cache[key] = True
    return Readiness(ReadinessState.READY)
