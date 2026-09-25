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
    LayoutIssue,
    RecordedEventSpan,
    TraversalRule,
    canonicalize_recorded_event_spans,
    decode_acquisition_layout,
    encode_acquisition_layout,
    iter_recorded_coordinates,
    producer_event_coordinates,
    recorded_frame_coordinates,
    scan_position_count,
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


def test_cross_span_overlap_never_expands_selected_events() -> None:
    """Validation cost follows the run count, not the selected-frame count.

    Expanding every selected ordinal into a set cost seconds and hundreds of
    megabytes on large point-scan selections. A billion selected frames is far
    beyond what any expansion could complete, so reaching the assertions at all
    proves the arithmetic sweep is in use.
    """
    event_count = 4_000_000_000
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="APD",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("event", "time", event_count),),
        recorded_event_spans=(
            RecordedEventSpan(0, 1_000_000_000, stride=2),
            RecordedEventSpan(1_999_999_999, 999_999_999, stride=2),
        ),
    )

    assert layout.recorded_event_spans is not None
    assert len(layout.recorded_event_spans) == 2
    assert not validate_acquisition_layout(layout)


@pytest.mark.parametrize(
    "spans, overlaps",
    [
        # Interleaved parities never meet despite covering the same range.
        ((RecordedEventSpan(0, 50, stride=2), RecordedEventSpan(1, 50, stride=2)), False),
        # Same progression twice is a complete overlap.
        ((RecordedEventSpan(0, 10, stride=10), RecordedEventSpan(0, 10, stride=10)), True),
        # Co-prime strides meet at one shared multiple inside both ranges.
        ((RecordedEventSpan(0, 20, stride=4), RecordedEventSpan(2, 20, stride=6)), True),
        # Co-prime strides whose shared value falls outside the shorter range.
        ((RecordedEventSpan(0, 2, stride=4), RecordedEventSpan(2, 2, stride=6)), False),
        # A periodic mask against a run that lands only in an unselected gap.
        ((RecordedEventSpan(0, 2, stride=1, period=10, repeats=5), RecordedEventSpan(5, 4, stride=1)), False),
        # The same periodic mask against a run that reaches a selected block.
        ((RecordedEventSpan(0, 2, stride=1, period=10, repeats=5), RecordedEventSpan(5, 6, stride=1)), True),
    ],
)
def test_cross_span_overlap_is_exact_for_interleaved_progressions(
    spans: tuple[RecordedEventSpan, ...], overlaps: bool
) -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="APD",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("event", "time", 1000),),
        recorded_event_spans=spans,
    )
    codes = {issue.code for issue in validate_acquisition_layout(layout)}

    assert ("DUPLICATE_PRODUCER_EVENT" in codes) is overlaps


def test_recorded_coordinates_validate_once_per_layout(monkeypatch) -> None:
    """Per-frame coordinate lookups must not re-validate the whole layout.

    Validation walks the layout and serializes it to JSON for the size budget,
    which made a per-frame lookup an order of magnitude more expensive than the
    iterator it is meant to be interchangeable with.
    """
    from imswitch.imcommon.model import acquisition_layout as module

    calls = 0
    original = module.validate_acquisition_layout

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "validate_acquisition_layout", counting)
    layout = _frame_layout()

    coordinates = [module.recorded_frame_coordinates(layout, index) for index in range(50)]

    assert calls == 1
    assert coordinates[0] == {"scan_y": 0, "linestep": 0, "scan_x": 0}
    assert coordinates[18] == {"scan_y": 0, "linestep": 1, "scan_x": 0}


def test_validation_memo_does_not_leak_into_the_serialized_contract() -> None:
    layout = _frame_layout()
    recorded_frame_coordinates(layout, 0)

    assert layout == _frame_layout()
    assert json.loads(encode_acquisition_layout(layout)) == json.loads(
        encode_acquisition_layout(_frame_layout())
    )


def test_detector_frame_payload_validates_authoritative_selected_count() -> None:
    layout = _frame_layout(spans=(RecordedEventSpan(18, 18, period=36, repeats=18),))

    assert not validate_acquisition_layout(layout, shape=(324, 8, 8))
    assert "FRAME_COUNT_MISMATCH" in {
        issue.code for issue in validate_acquisition_layout(layout, shape=(325, 8, 8))
    }


@pytest.mark.parametrize(
    "kind, positions",
    [
        # Line steps and repeated pulses stay inside one scan position.
        ("condition", 18 * 18),
        ("repeat", 18 * 18),
        # Registered and generated scan axes both advance the scan.
        ("scan_z", 18 * 18 * 3),
        ("scan_axis_3", 18 * 18 * 3),
        # Loop kinds are an open vocabulary: an unknown kind cannot be
        # classified, so the caller must skip the comparison instead of
        # assuming it advances the scan and rejecting a valid recording.
        ("polarization", None),
    ],
)
def test_scan_position_count_refuses_to_guess_unknown_loop_kinds(
    kind: str, positions: int | None
) -> None:
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="WidefieldCamera",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 18),
            AcquisitionLoop("extra", kind, 3),
            AcquisitionLoop("scan_x", "scan_x", 18),
        ),
    )

    assert scan_position_count(layout) == positions


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


def test_a_field_this_version_does_not_know_is_ignored_not_refused() -> None:
    """A newer ImSwitch's file must stay readable by an older one.

    Refusing an unknown field makes the whole recording inaccessible over one
    thing the reader cannot act on, which is a worse failure than skipping it.
    """
    document = json.loads(encode_acquisition_layout(_frame_layout()))
    document["event_loops"][0]["settling_time_ms"] = 5.0
    document["future_top_level_hint"] = "something"
    issues: list[LayoutIssue] = []

    decoded = decode_acquisition_layout(json.dumps(document), issues)

    assert [loop.count for loop in decoded.event_loops] == [18, 2, 18]
    codes = {issue.code for issue in issues}
    assert codes == {"UNKNOWN_LAYOUT_FIELD"}
    assert all(issue.severity == "warning" for issue in issues)
    # The skip is named, so it cannot pass unnoticed.
    assert any("settling_time_ms" in issue.message for issue in issues)
    assert any("future_top_level_hint" in issue.message for issue in issues)


def test_a_newer_schema_version_is_refused_with_a_clear_reason() -> None:
    """Unknown fields are additive; a version change may redefine known ones.

    Guessing is unsafe there, so this fails -- but it says why, instead of
    complaining about whichever field happens to be new.
    """
    document = json.loads(encode_acquisition_layout(_frame_layout()))
    document["schema"] = "imswitch.acquisition-layout/2"

    with pytest.raises(AcquisitionLayoutError) as error:
        decode_acquisition_layout(json.dumps(document))

    assert error.value.issues[0].code == "UNSUPPORTED_SCHEMA_VERSION"
    assert "written by a newer ImSwitch" in str(error.value)


def test_an_unrelated_schema_is_still_rejected_by_validation() -> None:
    """Only the acquisition-layout family gets the version treatment."""
    document = json.loads(encode_acquisition_layout(_frame_layout()))
    document["schema"] = "something.else/1"

    decoded = decode_acquisition_layout(json.dumps(document))

    assert "UNSUPPORTED_SCHEMA" in {
        issue.code for issue in validate_acquisition_layout(decoded)
    }


# ----------------------------------------------------------------------
# Direction is a physical sign, applied exactly once (audit condition 1)
# ----------------------------------------------------------------------


def _direction_layout(*, direction_y, traversal=()):
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
    )

    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 3, direction=direction_y),
            AcquisitionLoop("scan_x", "scan_x", 2, direction=1),
        ),
        traversal=tuple(traversal),
    )


def test_logical_coordinates_ignore_direction_and_physical_ones_mirror_it():
    """Logical index 0 is the first position visited; physical mirrors a -1 axis."""
    from imswitch.imcommon.model.acquisition_layout import (
        iter_physical_coordinates,
        iter_recorded_coordinates,
        physical_frame_coordinates,
        physical_orientation_flips,
        recorded_frame_coordinates,
    )

    layout = _direction_layout(direction_y=-1)

    assert physical_orientation_flips(layout) == frozenset({"scan_y"})
    logical = [dict(c) for c in iter_recorded_coordinates(layout)]
    physical = [dict(c) for c in iter_physical_coordinates(layout)]
    assert logical[0] == {"scan_y": 0, "scan_x": 0}
    assert physical[0] == {"scan_y": 2, "scan_x": 0}
    assert [c["scan_y"] for c in logical] == [0, 0, 1, 1, 2, 2]
    assert [c["scan_y"] for c in physical] == [2, 2, 1, 1, 0, 0]
    assert [c["scan_x"] for c in physical] == [c["scan_x"] for c in logical]
    assert dict(physical_frame_coordinates(layout, 5)) == {"scan_y": 0, "scan_x": 1}
    assert dict(recorded_frame_coordinates(layout, 5)) == {"scan_y": 2, "scan_x": 1}


def test_a_positive_axis_has_identical_logical_and_physical_coordinates():
    from imswitch.imcommon.model.acquisition_layout import (
        iter_physical_coordinates,
        iter_recorded_coordinates,
        physical_orientation_flips,
    )

    layout = _direction_layout(direction_y=1)

    assert physical_orientation_flips(layout) == frozenset()
    assert list(map(dict, iter_physical_coordinates(layout))) == list(
        map(dict, iter_recorded_coordinates(layout))
    )


def test_a_genuine_reverse_traversal_and_a_negative_direction_compose():
    """Traversal is chronology, direction is sign: a retrace on a -1 axis is both."""
    from imswitch.imcommon.model.acquisition_layout import (
        TraversalRule,
        iter_physical_coordinates,
        iter_recorded_coordinates,
    )

    layout = _direction_layout(
        direction_y=-1, traversal=(TraversalRule("scan_y", "reverse"),)
    )

    # The retrace reverses the logical index ...
    assert [c["scan_y"] for c in iter_recorded_coordinates(layout)] == [2, 2, 1, 1, 0, 0]
    # ... and the negative direction mirrors it again on top.
    assert [c["scan_y"] for c in iter_physical_coordinates(layout)] == [0, 0, 1, 1, 2, 2]


def test_direction_is_refused_on_a_non_spatial_loop():
    """A time/condition/repeat axis has no orientation to mirror."""
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
        physical_orientation_flips,
        validate_acquisition_layout,
    )

    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("condition", "condition", 2, direction=-1),
            AcquisitionLoop("scan_x", "scan_x", 2, direction=-1),
        ),
    )

    codes = {issue.code for issue in validate_acquisition_layout(layout)}
    assert "DIRECTION_ON_NON_SPATIAL_LOOP" in codes
    # And the helper would not mirror it even if validation were bypassed.
    assert physical_orientation_flips(layout) == frozenset({"scan_x"})


# ----------------------------------------------------------------------
# One loop-consumption helper (audit condition 4)
# ----------------------------------------------------------------------


def _consumer_layout(*loops, spans=None):
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
    )

    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
        recorded_event_spans=spans,
    )


def test_select_loops_fills_roles_and_reports_optional_ones_as_none():
    from imswitch.imcommon.model.acquisition_layout import AcquisitionLoop, select_loops

    layout = _consumer_layout(
        AcquisitionLoop("t", "time", 2),
        AcquisitionLoop("y", "scan_y", 3),
        AcquisitionLoop("x", "scan_x", 4),
    )
    selected = select_loops(
        layout,
        consumer="test",
        roles={"fast": "scan_x", "slow": "scan_y", "depth": "scan_z", "time": "time"},
        required=("fast", "slow"),
    )

    assert selected["fast"].id == "x" and selected["slow"].id == "y"
    assert selected["depth"] is None
    assert selected.count("depth") == 1
    assert selected.count("time") == 2
    assert selected.folded == ()


def test_select_loops_returns_none_when_a_required_role_is_absent():
    from imswitch.imcommon.model.acquisition_layout import AcquisitionLoop, select_loops

    layout = _consumer_layout(AcquisitionLoop("t", "time", 6))
    assert (
        select_loops(
            layout,
            consumer="test",
            roles={"fast": "scan_x", "slow": "scan_y", "time": "time"},
            required=("fast", "slow"),
        )
        is None
    )


def test_select_loops_refuses_a_loop_the_consumer_neither_places_nor_folds():
    """The two-pulse regression: a 'repeat' loop used to be dropped silently."""
    from imswitch.imcommon.model.acquisition_layout import (
        AcquisitionLoop,
        UnconsumedLoopError,
        select_loops,
    )

    layout = _consumer_layout(
        AcquisitionLoop("y", "scan_y", 3),
        AcquisitionLoop("x", "scan_x", 4),
        AcquisitionLoop("repeat", "repeat", 2),
    )
    with pytest.raises(UnconsumedLoopError) as raised:
        select_loops(
            layout,
            consumer="MoNaLISA placement",
            roles={"fast": "scan_x", "slow": "scan_y"},
            required=("fast", "slow"),
        )
    message = str(raised.value)
    assert "MoNaLISA placement" in message
    assert "'repeat'" in message and "count 2" in message


def test_select_loops_hands_back_explicitly_folded_loops():
    from imswitch.imcommon.model.acquisition_layout import AcquisitionLoop, select_loops

    layout = _consumer_layout(
        AcquisitionLoop("t", "time", 2),
        AcquisitionLoop("y", "scan_y", 3),
        AcquisitionLoop("x", "scan_x", 4),
        AcquisitionLoop("repeat", "repeat", 2),
    )
    selected = select_loops(
        layout,
        consumer="SMLM",
        roles={"fast": "scan_x", "slow": "scan_y"},
        fold=("time", "repeat"),
    )
    assert [loop.id for loop in selected.folded] == ["t", "repeat"]


def test_select_loops_refuses_two_loops_of_one_kind_and_conflicting_role_maps():
    from imswitch.imcommon.model.acquisition_layout import (
        AcquisitionLoop,
        UnconsumedLoopError,
        select_loops,
    )

    layout = _consumer_layout(
        AcquisitionLoop("coarse", "scan_x", 2),
        AcquisitionLoop("fine", "scan_x", 4),
    )
    with pytest.raises(UnconsumedLoopError, match="already fills role"):
        select_loops(layout, consumer="test", roles={"fast": "scan_x"})
    with pytest.raises(ValueError, match="mapped to both roles"):
        select_loops(layout, consumer="test", roles={"a": "scan_x", "b": ("scan_x",)})


def test_recorded_frame_counts_honour_spans_and_every_inner_loop():
    from imswitch.imcommon.model.acquisition_layout import (
        AcquisitionLoop,
        RecordedEventSpan,
        recorded_frame_count,
        recorded_frames_per_time_point,
    )

    full = _consumer_layout(
        AcquisitionLoop("t", "time", 3),
        AcquisitionLoop("z", "scan_z", 2),
        AcquisitionLoop("y", "scan_y", 3),
        AcquisitionLoop("c", "condition", 2),
        AcquisitionLoop("x", "scan_x", 4),
        AcquisitionLoop("r", "repeat", 2),
    )
    assert recorded_frame_count(full) == 3 * 2 * 3 * 2 * 4 * 2
    # A stack is everything one time point produces -- not scan_x * scan_y.
    assert recorded_frames_per_time_point(full) == 2 * 3 * 2 * 4 * 2

    # A detector gated to condition 1 only: per time point, half the frames.
    per_time = 2 * 3 * 2 * 4 * 2
    gated = _consumer_layout(
        AcquisitionLoop("t", "time", 3),
        AcquisitionLoop("z", "scan_z", 2),
        AcquisitionLoop("y", "scan_y", 3),
        AcquisitionLoop("c", "condition", 2),
        AcquisitionLoop("x", "scan_x", 4),
        AcquisitionLoop("r", "repeat", 2),
        spans=(RecordedEventSpan(start=8, count=8, period=16, repeats=3 * 2 * 3),),
    )
    assert recorded_frame_count(gated) == per_time * 3 // 2
    assert recorded_frames_per_time_point(gated) == per_time // 2

    no_time = _consumer_layout(
        AcquisitionLoop("y", "scan_y", 3), AcquisitionLoop("x", "scan_x", 4)
    )
    assert recorded_frames_per_time_point(no_time) == 12


def test_every_registered_loop_kind_is_pinned_as_positional_or_not():
    """A new kind must say whether it advances the scan; it cannot default."""
    from imswitch.imcommon.model.acquisition_layout import (
        LOOP_KIND_ADVANCES_POSITION,
        NON_POSITIONAL_LOOP_KINDS,
        REGISTERED_LOOP_KINDS,
    )

    assert set(LOOP_KIND_ADVANCES_POSITION) == set(REGISTERED_LOOP_KINDS)
    assert NON_POSITIONAL_LOOP_KINDS == {
        kind for kind, advances in LOOP_KIND_ADVANCES_POSITION.items() if not advances
    }
    # The two the gate has always excluded stay excluded.
    assert {"condition", "repeat"} <= NON_POSITIONAL_LOOP_KINDS
