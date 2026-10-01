from __future__ import annotations

import ast
import inspect

from imswitch.imcommon.model import acquisition_metadata
from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycleMarkers,
    flatten_acquisition_metadata,
    normalize_recording_lifecycle,
)


def _codes(lifecycle) -> set[str]:
    return {issue.code for issue in lifecycle.issues}


def test_nested_and_flat_metadata_share_one_normalized_key_space() -> None:
    flattened = flatten_acquisition_metadata(
        {
            "recording": {
                "planned_frames": 12,
                "actual_frames": 12,
            },
            "AcquisitionLayout:schema": "imswitch.acquisition-layout/1",
        }
    )

    assert flattened == {
        "recording:planned_frames": 12,
        "recording:actual_frames": 12,
        "AcquisitionLayout:schema": "imswitch.acquisition-layout/1",
    }


def test_hdf5_stream_complete_side_marker_overrides_stale_writing_attribute() -> None:
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:completion_outcome": "complete",
            "recording:planned_frames": 12,
            "recording:actual_frames": 12,
        },
        RecordingLifecycleMarkers(
            writing=True,
            frames_committed=12,
            stream_complete=True,
        ),
    )

    assert lifecycle.writer_state == "finalized"
    assert lifecycle.completion_outcome == "complete"
    assert lifecycle.frames_committed == 12
    assert not lifecycle.issues


def test_zarr_live_marker_requires_runtime_writer_attachment() -> None:
    live = normalize_recording_lifecycle(
        {},
        RecordingLifecycleMarkers(
            writing=True,
            frames_committed=7,
            live_writer_attached=True,
        ),
    )
    stale = normalize_recording_lifecycle(
        {},
        RecordingLifecycleMarkers(writing=True, frames_committed=7),
    )

    assert live.writer_state == "writing"
    assert stale.writer_state == "unknown"
    assert "STALE_WRITING_MARKER" in _codes(stale)


def test_false_writing_marker_finalizes_without_stream_complete() -> None:
    lifecycle = normalize_recording_lifecycle(
        {"recording": {"completion_outcome": "complete"}},
        RecordingLifecycleMarkers(writing=False, frames_committed=4),
    )

    assert lifecycle.writer_state == "finalized"
    assert lifecycle.actual_frames == 4
    assert lifecycle.completion_outcome == "complete"


def test_until_stop_complete_allows_absent_planned_count() -> None:
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:completion_outcome": "complete",
            "recording:actual_frames": 23,
            "recording:actual_partitions": 4,
        },
        RecordingLifecycleMarkers(writing=False, frames_committed=23),
    )

    assert lifecycle.planned_frames is None
    assert lifecycle.planned_partitions is None
    assert not lifecycle.issues


def test_complete_and_early_stop_counts_are_checked() -> None:
    complete = normalize_recording_lifecycle(
        {
            "recording:completion_outcome": "complete",
            "recording:planned_frames": 10,
            "recording:actual_frames": 8,
        },
        RecordingLifecycleMarkers(writing=False, frames_committed=8),
    )
    stopped = normalize_recording_lifecycle(
        {
            "recording:completion_outcome": "stopped_early",
            "recording:planned_frames": 10,
            "recording:actual_frames": 8,
            "recording:planned_partitions": 1,
            "recording:actual_partitions": 1,
        },
        RecordingLifecycleMarkers(writing=False, frames_committed=8),
    )

    assert "COMPLETE_COUNT_MISMATCH" in _codes(complete)
    assert "EARLY_STOP_COUNT_MISMATCH" not in _codes(stopped)


def test_early_stop_requires_at_least_one_known_shortfall() -> None:
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:completion_outcome": "stopped_early",
            "recording:planned_frames": 10,
            "recording:actual_frames": 10,
        },
        RecordingLifecycleMarkers(writing=False, frames_committed=10),
    )

    assert "EARLY_STOP_COUNT_MISMATCH" in _codes(lifecycle)


def test_outcome_is_invalid_until_writer_is_finalized() -> None:
    lifecycle = normalize_recording_lifecycle(
        {"recording:completion_outcome": "complete"},
        RecordingLifecycleMarkers(
            writing=True,
            frames_committed=2,
            live_writer_attached=True,
        ),
    )

    assert lifecycle.writer_state == "writing"
    assert "OUTCOME_BEFORE_FINALIZATION" in _codes(lifecycle)


def test_legacy_missing_markers_remain_unknown_with_a_structured_issue() -> None:
    lifecycle = normalize_recording_lifecycle({}, RecordingLifecycleMarkers())

    assert lifecycle.writer_state == "unknown"
    assert "MISSING_LIFECYCLE_MARKERS" in _codes(lifecycle)


def test_metadata_normalizer_has_no_format_or_application_imports() -> None:
    tree = ast.parse(inspect.getsource(acquisition_metadata))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    forbidden = {"h5py", "zarr", "imswitch.imcontrol", "imswitch.improcess"}
    assert not any(
        name == item or name.startswith(f"{item}.") for name in imported for item in forbidden
    )
