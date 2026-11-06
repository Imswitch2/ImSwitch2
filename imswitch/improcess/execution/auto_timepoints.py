"""Marker-confirmed finite timepoint scheduling for growing logical acquisitions.

This is intentionally independent of StreamingSession: completed timepoints can
be processed with ordinary Reconstructor.process(), including Full/Robust modes.
No file-size/quiet-time inference and no concurrent HDF5/SWMR reader are used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re

from .readiness import (
    ReadinessState, evaluate_evidence, inspect_physical_reference,
    references_for_scope,
)


@dataclass(frozen=True)
class CompletedTimepointBatch:
    """Contiguous ready logical T indices, beginning at the requested cursor."""

    indices: tuple[int, ...] = ()
    reason: str = ""
    references: tuple[object, ...] = ()


@dataclass
class AutoTimepointState:
    """Job-scoped state retained while the finite worker is parked between T's."""

    next_t: int
    stop_t: int
    publisher: object
    reuse_overrides: dict = field(default_factory=dict)
    calibration_resolved: bool = False


def _native_bool(value):
    if type(value) is bool:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            result = item()
            return result if type(result) is bool else None
        except (ValueError, TypeError):
            pass
    return None


def _native_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _safe_early_hdf5(ref, evidence, all_refs) -> bool:
    """Only post-close, explicitly multi-file HDF5 timepoints are eligible.

    A SWMR stream_complete marker alone may be set before close/reopen for
    metadata finalization. A positive completion outcome is written afterwards.
    A single-file lapse can be reopened by the producer for future scanN groups,
    so it is never eligible even when an individual scan group has finished.
    """
    if _native_bool(evidence.attrs.get("recording:single_lapse_file")) is not False:
        return False
    if _native_text(evidence.attrs.get("recording:completion_outcome")) != "complete":
        return False
    if evidence.markers.stream_complete is not True:
        return False
    return not any(
        other.logical_t != ref.logical_t
        and Path(other.path) == Path(ref.path)
        for other in all_refs
    )


def _current_references(source, job):
    """Refresh only this admitted lapse's physical membership, without a watcher.

    Multi-file members are enumerated by the SAME filename token parser used by
    the Session index, but each candidate is admitted only after its own
    metadata proves its logical T and detector membership. No candidate is
    opened during enumeration. This lets already-admitted jobs continue even
    if the Session Monitor is switched off.

    For Zarr scanN groups inside one store, use the existing lapse index; its
    reads are Zarr metadata only. No active HDF5 group discovery is attempted.
    """
    refs, _ = references_for_scope(
        source, job.timepoint_scope, job.timepoint_total, is_lapse=True)
    path = getattr(source, "primary_path", None)
    if not path or not hasattr(source, "source_refs"):
        return refs
    anchor = Path(path)
    known = {ref.logical_t: ref for ref in refs}
    # Existing multi-file index parser is the authority on scan/time tokens.
    from imswitch.improcess.model.lapse_naming import _split_multi_file_name
    anchor_ref = next((ref for ref in refs if Path(ref.path) == anchor), None)
    anchor_t = int(anchor_ref.logical_t if anchor_ref is not None else 0)
    tokenized = _split_multi_file_name(
        anchor.name, anchor_t, int(job.timepoint_total))
    if tokenized is not None:
        prefix, token, suffix = tokenized
        width = len(str(int(job.timepoint_total)))
        pattern = re.compile(re.escape(prefix) + "_" + re.escape(token)
                             + r"(\d{" + str(width) + r"})" + re.escape(suffix) + r"$")
        try:
            with os.scandir(anchor.parent) as entries:
                paths = [(int(match.group(1)), str(Path(entry.path)))
                         for entry in entries
                         if (match := pattern.fullmatch(entry.name)) is not None
                         and (entry.is_file() or entry.is_dir())]
        except OSError:
            return refs
        # Use the selected detector's existing dataset identity where known.
        # The per-file metadata gate separately rejects ambiguous detector data.
        detector = next((ref.dataset for ref in refs if ref.dataset),
                        getattr(source, "primary_dataset", None))
        for t, sibling in paths:
            if t not in known:
                known[t] = type(refs[0])(sibling, detector, t) if refs else None
        return [ref for _, ref in sorted(known.items()) if ref is not None]
    if anchor.suffix.lower() == ".zarr":
        try:
            from imswitch.improcess.model.lapse_source import discover_time_lapse
            index = discover_time_lapse(anchor, getattr(source, "primary_dataset", None))
            if int(index.planned_count) != int(job.timepoint_total):
                return refs
            for item in index.channel(index.anchor_detector).candidates:
                t = int(item.ordinal) - int(index.anchor.ordinal) + int(index.anchor_index)
                known[t] = type(refs[0])(str(item.path), item.dataset, t) if refs else None
        except Exception:
            return refs
    return [ref for _, ref in sorted(known.items()) if ref is not None]


def probe_completed_prefix(source, job, next_t: int, *, inspected=None,
                           max_new_files: int = 4) -> CompletedTimepointBatch:
    """Inspect only a bounded, contiguous prefix of finished timepoints.

    SourceRefs identify the logical acquisition; job-local sibling lookup
    discovers later members even after Monitor is disabled.
    An absent T, unsafe HDF5 file, or unconfirmed completion stops the prefix.
    Previously verified completed members may be cached per job.
    """
    total = getattr(job, "timepoint_total", None)
    if total is None:
        return CompletedTimepointBatch(reason="Unknown planned timelapse extent")
    try:
        start, stop = job.timepoint_scope.bounds(total)
    except (TypeError, ValueError, IndexError) as exc:
        return CompletedTimepointBatch(reason=f"Invalid timepoint scope: {exc}")
    if stop is None or not start <= next_t <= stop:
        return CompletedTimepointBatch(reason="Invalid progressive timepoint cursor")
    if next_t == stop:
        return CompletedTimepointBatch()
    refs = _current_references(source, job)
    mapping = {}
    for ref in refs:
        if ref.logical_t in mapping:
            return CompletedTimepointBatch(reason="Ambiguous duplicate logical timepoints")
        mapping[ref.logical_t] = ref
    cache = inspected if inspected is not None else {}
    ready = []
    ready_refs = []
    for t in range(next_t, min(stop, next_t + max(1, int(max_new_files)))):
        ref = mapping.get(t)
        if ref is None:
            return CompletedTimepointBatch(tuple(ready), f"Waiting for T{t + 1} to arrive",
                                           tuple(ready_refs))
        key = (ref.path, ref.dataset, t)
        if key in cache:
            ready.append(t)
            ready_refs.append(ref)
            continue
        try:
            evidence = inspect_physical_reference(ref)
        except Exception as exc:
            return CompletedTimepointBatch(
                tuple(ready), f"Waiting for safe T{t + 1} access: {type(exc).__name__}",
                tuple(ready_refs))
        state = evaluate_evidence(
            evidence, is_lapse=True, expected_total=total, logical_index=t,
        )
        if state.state != ReadinessState.READY:
            return CompletedTimepointBatch(
                tuple(ready), state.reason or f"Waiting for T{t + 1} completion",
                tuple(ready_refs))
        if evidence.format_id == "hdf5" and not _safe_early_hdf5(ref, evidence, refs):
            return CompletedTimepointBatch(
                tuple(ready), "Waiting for safe HDF5 file closure / complete acquisition",
                tuple(ready_refs))
        if evidence.format_id not in ("zarr", "hdf5"):
            return CompletedTimepointBatch(tuple(ready), "Source has no safe early reader",
                                           tuple(ready_refs))
        cache[key] = True
        ready.append(t)
        ready_refs.append(ref)
    return CompletedTimepointBatch(tuple(ready), references=tuple(ready_refs))
