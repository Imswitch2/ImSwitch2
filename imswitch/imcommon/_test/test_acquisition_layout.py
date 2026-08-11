from __future__ import annotations

import json

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    MAX_INLINE_LAYOUT_BYTES,
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLayoutError,
    AcquisitionLoop,
    AcquisitionPartition,
    RecordedEventSpan,
    TraversalRule,
    canonicalize_recorded_event_spans,
    decode_acquisition_layout,
    encode_acquisition_layout,
    iter_recorded_coordinates,
    producer_event_coordinates,
    recorded_frame_coordinates,
    unfold_frame_axis,
    validate_acquisition_layout,
)


def _frame_layout(
    *,
    spans: tuple[RecordedEventSpan, ...] | None = None,
    traversal: tuple[TraversalRule, ...] = (),
    partitions: tuple[AcquisitionPartition, ...] = (),
) -> AcquisitionLayout:
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="WidefieldCamera",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 18),
            AcquisitionLoop(
                "linestep",
                "condition",
                2,
                labels=("A", "B"),
            ),
            AcquisitionLoop("scan_x", "scan_x", 18),
        ),
        traversal=traversal,
        recorded_event_spans=spans,
        partitions=partitions,
        modality="monalisa",
        scan_source="ScanControllerAdvanced",
    )


def test_b_only_line_steps_canonicalize_to_one_periodic_span() -> None:
    unfactored = tuple(RecordedEventSpan(start=18 + row * 36, count=18) for row in range(18))

    assert canonicalize_recorded_event_spans(unfactored, producer_event_count=648) == (
        RecordedEventSpan(
            start=18,
            count=18,
            stride=1,
            period=36,
            repeats=18,
        ),
    )


def test_noncanonical_json_decodes_equal_to_periodic_layout() -> None:
    periodic = _frame_layout(spans=(RecordedEventSpan(18, 18, period=36, repeats=18),))
    value = json.loads(encode_acquisition_layout(periodic))
    value["recorded_event_spans"] = [
        {
            "start": 18 + row * 36,
            "count": 18,
            "stride": 1,
            "period": None,
            "repeats": 1,
        }
        for row in range(18)
    ]

    decoded = decode_acquisition_layout(json.dumps(value, separators=(",", ":"), sort_keys=True))

    assert decoded == periodic
    assert hash(decoded) == hash(periodic)
    assert encode_acquisition_layout(decoded) == encode_acquisition_layout(periodic)


def test_explicit_all_events_selection_has_unique_none_representation() -> None:
    assert (
        canonicalize_recorded_event_spans(
            (
                RecordedEventSpan(0, 324),
                RecordedEventSpan(324, 324),
            ),
            producer_event_count=648,
        )
        is None
    )
    assert _frame_layout(spans=(RecordedEventSpan(0, 648),)).recorded_event_spans is None


def test_singleton_periodic_span_becomes_one_arithmetic_run() -> None:
    assert canonicalize_recorded_event_spans(
        (RecordedEventSpan(2, 1, period=3, repeats=4),),
        producer_event_count=20,
    ) == (RecordedEventSpan(2, 4, stride=3),)


def test_canonical_runs_are_maximal_across_input_span_boundaries() -> None:
    # Expanded order: 0, 5, 7, 8, 10, 11, 13. The input periodic blocks are
    # not the maximal arithmetic runs, so decoding must repartition them.
    assert canonicalize_recorded_event_spans(
        (
            RecordedEventSpan(0, 1),
            RecordedEventSpan(5, 2, stride=2, period=3, repeats=3),
        ),
        producer_event_count=20,
    ) == (
        RecordedEventSpan(0, 2, stride=5),
        RecordedEventSpan(7, 2, stride=1, period=3, repeats=2),
        RecordedEventSpan(13, 1, stride=1),
    )


def test_deterministic_json_round_trip_preserves_open_kinds() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind="org.example/photon-event-stream",
        detector="TCSPC",
        storage_axes=("event",),
        event_loops=(AcquisitionLoop("phase", "org.example/phase", 4),),
        provenance="recorded",
    )

    first = encode_acquisition_layout(layout)
    second = encode_acquisition_layout(decode_acquisition_layout(first))

    assert first == second
    assert decode_acquisition_layout(first).payload_kind == layout.payload_kind
    assert any(
        issue.code == "UNKNOWN_PAYLOAD_KIND" and issue.severity == "warning"
        for issue in validate_acquisition_layout(layout)
    )


def test_raw_and_canonical_metadata_size_limits_are_enforced() -> None:
    with pytest.raises(AcquisitionLayoutError) as raw_error:
        decode_acquisition_layout(b" " * (MAX_INLINE_LAYOUT_BYTES + 1))
    assert raw_error.value.issues[0].code == "LAYOUT_METADATA_TOO_LARGE"

    oversized = _frame_layout()
    oversized = AcquisitionLayout(
        **{
            **oversized.__dict__,
            "detector": "d" * MAX_INLINE_LAYOUT_BYTES,
        }
    )
    with pytest.raises(AcquisitionLayoutError) as canonical_error:
        encode_acquisition_layout(oversized)
    assert any(issue.code == "LAYOUT_METADATA_TOO_LARGE" for issue in canonical_error.value.issues)


def test_duplicate_traversal_targets_are_rejected() -> None:
    layout = _frame_layout(
        traversal=(
            TraversalRule("scan_x", "forward"),
            TraversalRule("scan_x", "reverse"),
        )
    )

    assert "DUPLICATE_TRAVERSAL_TARGET" in {
        issue.code for issue in validate_acquisition_layout(layout)
    }


@pytest.mark.parametrize(
    "spans, expected_code",
    [
        (
            (RecordedEventSpan(0, 3), RecordedEventSpan(2, 2)),
            "DUPLICATE_PRODUCER_EVENT",
        ),
        ((RecordedEventSpan(647, 2),), "SPAN_OUT_OF_RANGE"),
        (
            (RecordedEventSpan(0, 3, stride=2, period=2, repeats=2),),
            "DUPLICATE_PRODUCER_EVENT",
        ),
    ],
)
def test_span_validation_detects_overlap_and_bounds(
    spans: tuple[RecordedEventSpan, ...], expected_code: str
) -> None:
    layout = _frame_layout(spans=spans)

    assert expected_code in {issue.code for issue in validate_acquisition_layout(layout)}


def test_detector_frame_payload_validates_authoritative_selected_count() -> None:
    layout = _frame_layout(spans=(RecordedEventSpan(18, 18, period=36, repeats=18),))

    assert not validate_acquisition_layout(layout, shape=(324, 8, 8))
    assert "FRAME_COUNT_MISMATCH" in {
        issue.code for issue in validate_acquisition_layout(layout, shape=(325, 8, 8))
    }


def test_assembled_payload_maps_loops_directly_to_storage_axes() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
        detector="RasterDetector",
        storage_axes=("condition", "scan_y", "scan_x"),
        event_loops=(
            AcquisitionLoop("condition", "condition", 2, storage_axis="condition"),
            AcquisitionLoop("scan_y", "scan_y", 3, storage_axis="scan_y"),
            AcquisitionLoop("scan_x", "scan_x", 4, storage_axis="scan_x"),
        ),
    )

    assert not validate_acquisition_layout(layout, shape=(2, 3, 4))
    assert "LOOP_AXIS_COUNT_MISMATCH" in {
        issue.code for issue in validate_acquisition_layout(layout, shape=(2, 4, 4))
    }


def test_serpentine_row_major_parity_uses_all_declared_outer_loops() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="camera",
        storage_axes=("frame",),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("condition", "condition", 2),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
        traversal=(
            TraversalRule(
                "scan_x",
                "serpentine",
                parity_loops=("scan_y", "condition"),
            ),
        ),
    )

    # y=0, condition=1 gives flattened parity 1 and reverses x.
    assert producer_event_coordinates(layout, 3) == {
        "scan_y": 0,
        "condition": 1,
        "scan_x": 2,
    }
    # y=1, condition=0 gives flattened parity 2 and keeps x forward.
    assert producer_event_coordinates(layout, 6) == {
        "scan_y": 1,
        "condition": 0,
        "scan_x": 0,
    }


def test_serpentine_can_reset_or_carry_across_an_outer_boundary() -> None:
    loops = (
        AcquisitionLoop("scan_z", "scan_z", 2),
        AcquisitionLoop("scan_y", "scan_y", 3),
        AcquisitionLoop("scan_x", "scan_x", 2),
    )
    base = dict(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="camera",
        storage_axes=("frame",),
        event_loops=loops,
    )
    reset = AcquisitionLayout(
        **base,
        traversal=(TraversalRule("scan_x", "serpentine", ("scan_y",)),),
    )
    carry = AcquisitionLayout(
        **base,
        traversal=(TraversalRule("scan_x", "serpentine", ("scan_z", "scan_y")),),
    )

    # First event at z=1, y=0: reset parity is 0; carried parity is 3.
    assert producer_event_coordinates(reset, 6)["scan_x"] == 0
    assert producer_event_coordinates(carry, 6)["scan_x"] == 1


def test_partition_event_ordinals_restart_at_zero() -> None:
    first = _frame_layout(partitions=(AcquisitionPartition("time", 0, None, "one-file-per-item"),))
    second = _frame_layout(partitions=(AcquisitionPartition("time", 1, None, "one-file-per-item"),))

    assert recorded_frame_coordinates(first, 18) == recorded_frame_coordinates(second, 18)
    assert first.event_loops == second.event_loops
    assert first.recorded_event_spans == second.recorded_event_spans


def test_combined_time_is_an_event_loop_not_a_partition_offset() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="camera",
        storage_axes=("frame",),
        event_loops=(
            AcquisitionLoop("time", "time", 2),
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
    )

    assert producer_event_coordinates(layout, 6) == {
        "time": 1,
        "scan_y": 0,
        "scan_x": 0,
    }


def test_recorded_coordinate_iterator_preserves_b_only_storage_order() -> None:
    layout = _frame_layout(spans=(RecordedEventSpan(18, 18, period=36, repeats=18),))
    coordinates = list(iter_recorded_coordinates(layout))

    assert len(coordinates) == 324
    assert coordinates[0] == {"scan_y": 0, "linestep": 1, "scan_x": 0}
    assert coordinates[17] == {"scan_y": 0, "linestep": 1, "scan_x": 17}
    assert coordinates[18] == {"scan_y": 1, "linestep": 1, "scan_x": 0}


def test_unfold_dense_forward_layout_is_a_view() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="camera",
        storage_axes=("frame", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
    )
    source = np.arange(12).reshape(6, 2)

    unfolded = unfold_frame_axis(source, layout)

    assert unfolded.storage_axes == ("scan_y", "scan_x", "detector_x")
    assert unfolded.data.shape == (2, 3, 2)
    assert np.shares_memory(source, unfolded.data)


def test_unfold_serpentine_requires_explicit_copy_permission() -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="camera",
        storage_axes=("frame",),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
        traversal=(TraversalRule("scan_x", "serpentine", ("scan_y",)),),
    )
    source = np.arange(6)

    with pytest.raises(AcquisitionLayoutError) as copy_error:
        unfold_frame_axis(source, layout)
    assert copy_error.value.issues[0].code == "UNFOLD_REQUIRES_COPY"

    unfolded = unfold_frame_axis(source, layout, copy_policy="allow")
    np.testing.assert_array_equal(unfolded.data, [[0, 1, 2], [5, 4, 3]])


def test_sparse_rectangular_condition_subset_can_unfold_without_a_copy() -> None:
    layout = _frame_layout(spans=(RecordedEventSpan(18, 18, period=36, repeats=18),))
    source = np.arange(324 * 2).reshape(324, 1, 2)

    unfolded = unfold_frame_axis(source, layout)

    assert unfolded.data.shape == (18, 1, 18, 1, 2)
    assert unfolded.loop_coordinates["linestep"] == (1,)
    assert np.shares_memory(source, unfolded.data)
