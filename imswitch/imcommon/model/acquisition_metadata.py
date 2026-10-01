"""Format-neutral normalization for acquisition metadata and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .acquisition_layout import LayoutIssue


WRITER_STATE_WRITING = "writing"
WRITER_STATE_FINALIZED = "finalized"
WRITER_STATE_UNKNOWN = "unknown"
VALID_COMPLETION_OUTCOMES = frozenset({"complete", "stopped_early"})


@dataclass(frozen=True)
class RecordingLifecycleMarkers:
    """Native liveness markers extracted by one storage-format reader.

    HDF5 readers populate ``frames_committed`` and ``stream_complete`` from
    sibling datasets. Zarr readers populate the first two values from detector
    array attributes. The shared normalizer intentionally knows neither API.
    """

    writing: bool | None = None
    frames_committed: int | None = None
    stream_complete: bool | None = None
    live_writer_attached: bool = False


@dataclass(frozen=True)
class RecordingLifecycle:
    """One storage-independent interpretation of recording completion."""

    writer_state: str
    completion_outcome: str | None = None
    planned_frames: int | None = None
    actual_frames: int | None = None
    planned_partitions: int | None = None
    actual_partitions: int | None = None
    frames_committed: int | None = None
    discarded_frames: int | None = None
    issues: tuple[LayoutIssue, ...] = ()


def _native_scalar(value: Any) -> Any:
    """Convert NumPy-like scalar wrappers without importing a format library."""
    if isinstance(value, (str, bytes, bytearray, bool, int, float)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return value


def _text(value: Any) -> str | None:
    value = _native_scalar(value)
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        return value
    return str(value)


def flatten_acquisition_metadata(
    metadata: Mapping[str, Any],
    *,
    separator: str = ":",
) -> dict[str, Any]:
    """Flatten nested metadata mappings while preserving already-flat keys.

    This function traverses mappings only. HDF5/Zarr group and side-dataset
    extraction stays in their reader adapters, which then pass plain mappings
    and :class:`RecordingLifecycleMarkers` into this module.
    """
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    if not isinstance(separator, str) or not separator:
        raise ValueError("separator must be a non-empty string")

    flattened: dict[str, Any] = {}

    def visit(value: Mapping[str, Any], prefix: tuple[str, ...]) -> None:
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            path = prefix + (key,)
            if isinstance(raw_value, Mapping):
                visit(raw_value, path)
            else:
                flattened[separator.join(path)] = _native_scalar(raw_value)

    visit(metadata, ())
    return flattened


def _read_count(
    attrs: Mapping[str, Any],
    key: str,
    issues: list[LayoutIssue],
    *,
    positive: bool,
) -> int | None:
    value = _native_scalar(attrs.get(key))
    if value is None:
        return None
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        issues.append(
            LayoutIssue(
                "error",
                "INVALID_RECORDING_COUNT",
                f"{key} must be an integer >= {minimum}",
                key,
            )
        )
        return None
    return value


def _validate_marker_bool(
    value: Any,
    field_name: str,
    issues: list[LayoutIssue],
) -> bool | None:
    value = _native_scalar(value)
    if value is None:
        return None
    if type(value) is not bool:
        issues.append(
            LayoutIssue(
                "error",
                "INVALID_LIFECYCLE_MARKER",
                f"{field_name} must be boolean when present",
                field_name,
            )
        )
        return None
    return value


def normalize_recording_lifecycle(
    attrs: Mapping[str, Any],
    markers: RecordingLifecycleMarkers,
) -> RecordingLifecycle:
    """Normalize native writer markers and final recording outcome metadata.

    Marker precedence is shared by every reader. In particular,
    ``stream_complete=True`` wins over a stale HDF5 ``writing=True`` attribute,
    while ``writing=True`` is considered live only when the reader is attached
    to the current writer.
    """
    if not isinstance(attrs, Mapping):
        raise TypeError("attrs must be a mapping")
    if not isinstance(markers, RecordingLifecycleMarkers):
        raise TypeError("markers must be RecordingLifecycleMarkers")
    attrs = flatten_acquisition_metadata(attrs)
    issues: list[LayoutIssue] = []

    writing = _validate_marker_bool(markers.writing, "writing", issues)
    stream_complete = _validate_marker_bool(markers.stream_complete, "stream_complete", issues)
    live_writer_attached = _validate_marker_bool(
        markers.live_writer_attached, "live_writer_attached", issues
    )
    live_writer_attached = bool(live_writer_attached)

    if stream_complete is True:
        writer_state = WRITER_STATE_FINALIZED
    elif writing is False:
        writer_state = WRITER_STATE_FINALIZED
        if stream_complete is False:
            issues.append(
                LayoutIssue(
                    "warning",
                    "LIFECYCLE_MARKER_CONFLICT",
                    "writing=false marks the recording finalized, but stream_complete is still false",
                    "stream_complete",
                )
            )
    elif live_writer_attached and (writing is True or stream_complete is False):
        writer_state = WRITER_STATE_WRITING
    else:
        writer_state = WRITER_STATE_UNKNOWN
        if writing is True:
            issues.append(
                LayoutIssue(
                    "warning",
                    "STALE_WRITING_MARKER",
                    "writing=true without a current live-writer attachment has unknown completeness",
                    "writing",
                )
            )
        elif stream_complete is False:
            issues.append(
                LayoutIssue(
                    "warning",
                    "INCOMPLETE_STREAM_MARKER",
                    "stream_complete=false without a current live writer has unknown completeness",
                    "stream_complete",
                )
            )
        elif writing is None and stream_complete is None:
            issues.append(
                LayoutIssue(
                    "warning",
                    "MISSING_LIFECYCLE_MARKERS",
                    "Recording lifecycle markers are absent; completeness must remain inferred",
                )
            )

    frames_committed = _native_scalar(markers.frames_committed)
    if frames_committed is not None and (type(frames_committed) is not int or frames_committed < 0):
        issues.append(
            LayoutIssue(
                "error",
                "INVALID_FRAMES_COMMITTED",
                "frames_committed must be a non-negative integer",
                "recording:frames_committed",
            )
        )
        frames_committed = None

    planned_frames = _read_count(attrs, "recording:planned_frames", issues, positive=True)
    actual_frames = _read_count(attrs, "recording:actual_frames", issues, positive=False)
    planned_partitions = _read_count(attrs, "recording:planned_partitions", issues, positive=True)
    actual_partitions = _read_count(attrs, "recording:actual_partitions", issues, positive=False)
    if (
        actual_frames is None
        and writer_state == WRITER_STATE_FINALIZED
        and frames_committed is not None
    ):
        actual_frames = frames_committed

    outcome = _text(attrs.get("recording:completion_outcome"))
    if outcome is not None:
        outcome = outcome.strip()
        if not outcome:
            outcome = None
    if outcome is not None:
        if outcome not in VALID_COMPLETION_OUTCOMES:
            issues.append(
                LayoutIssue(
                    "error",
                    "INVALID_COMPLETION_OUTCOME",
                    f"Unknown recording completion outcome {outcome!r}",
                    "recording:completion_outcome",
                )
            )
        if writer_state != WRITER_STATE_FINALIZED:
            issues.append(
                LayoutIssue(
                    "error",
                    "OUTCOME_BEFORE_FINALIZATION",
                    "A completion outcome is valid only for a finalized recording",
                    "recording:completion_outcome",
                )
            )

    count_pairs = (
        ("frames", planned_frames, actual_frames),
        ("partitions", planned_partitions, actual_partitions),
    )
    if outcome == "complete":
        for name, planned, actual in count_pairs:
            if planned is not None and actual is not None and actual != planned:
                issues.append(
                    LayoutIssue(
                        "error",
                        "COMPLETE_COUNT_MISMATCH",
                        f"Completed recording has {actual} actual {name}, expected {planned}",
                        f"recording:actual_{name}",
                    )
                )
    elif outcome == "stopped_early":
        known_shortfall = False
        known_pair = False
        for name, planned, actual in count_pairs:
            if planned is None or actual is None:
                continue
            known_pair = True
            if actual < planned:
                known_shortfall = True
            elif actual > planned:
                issues.append(
                    LayoutIssue(
                        "error",
                        "EARLY_STOP_COUNT_MISMATCH",
                        f"stopped_early has {actual} actual {name}, exceeding the plan of {planned}",
                        f"recording:actual_{name}",
                    )
                )
        if not known_shortfall and (planned_frames is not None or planned_partitions is not None):
            if known_pair and not any(
                issue.code == "EARLY_STOP_COUNT_MISMATCH" for issue in issues
            ):
                issues.append(
                    LayoutIssue(
                        "error",
                        "EARLY_STOP_COUNT_MISMATCH",
                        "stopped_early does not contain a known shortfall from the acquisition plan",
                        "recording:completion_outcome",
                    )
                )
            elif not known_pair:
                issues.append(
                    LayoutIssue(
                        "warning",
                        "EARLY_STOP_SHORTFALL_UNKNOWN",
                        "stopped_early was recorded, but no planned/actual pair proves the shortfall",
                        "recording:completion_outcome",
                    )
                )

    if (
        frames_committed is not None
        and actual_frames is not None
        and frames_committed > actual_frames
    ):
        issues.append(
            LayoutIssue(
                "error",
                "COMMITTED_FRAMES_EXCEED_ACTUAL",
                f"frames_committed={frames_committed} exceeds actual_frames={actual_frames}",
                "recording:frames_committed",
            )
        )

    # Frames the detector produced beyond the plan and the writer dropped.
    # A recording can meet its planned count exactly and still have discarded
    # a surplus -- a camera that ran free, or was pulsed more often than the
    # scan declared -- and the outcome vocabulary has no word for that, so it
    # read as "complete". The count is on the file; this is where a reader
    # finds out, because nothing consulted it before.
    discarded_frames = _read_count(
        attrs, "recording:discarded_frames", issues, positive=False
    )
    if discarded_frames:
        issues.append(
            LayoutIssue(
                "warning",
                "DISCARDED_SURPLUS_FRAMES",
                f"{discarded_frames} frame(s) arrived beyond the "
                f"{planned_frames if planned_frames is not None else 'planned'} "
                f"this recording planned and were not written. The detector "
                f"produced more than the scan accounted for, so the frames "
                f"that were kept may not be the ones the layout describes.",
                "recording:discarded_frames",
            )
        )

    return RecordingLifecycle(
        writer_state=writer_state,
        completion_outcome=outcome,
        planned_frames=planned_frames,
        actual_frames=actual_frames,
        planned_partitions=planned_partitions,
        actual_partitions=actual_partitions,
        frames_committed=frames_committed,
        discarded_frames=discarded_frames,
        issues=tuple(issues),
    )
