"""Versioned acquisition-layout metadata shared by acquisition and processing.

The layout describes the chronological producer event lattice independently of
the array axes used by HDF5, Zarr, TIFF, or an in-memory source.  This module is
deliberately format-neutral so producers, readers, and reconstructors all use
the same serialization, validation, and coordinate arithmetic.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from functools import reduce
from operator import mul
from typing import Any, Iterable, Iterator, Mapping, Sequence


ACQUISITION_LAYOUT_SCHEMA = "imswitch.acquisition-layout/1"
MAX_INLINE_LAYOUT_BYTES = 1_048_576

PAYLOAD_DETECTOR_FRAME_STREAM = "detector-frame-stream"
PAYLOAD_ASSEMBLED_IMAGE = "assembled-image"
PAYLOAD_RECONSTRUCTED_IMAGE = "reconstructed-image"
REGISTERED_PAYLOAD_KINDS = frozenset(
    {
        PAYLOAD_DETECTOR_FRAME_STREAM,
        PAYLOAD_ASSEMBLED_IMAGE,
        PAYLOAD_RECONSTRUCTED_IMAGE,
    }
)

REGISTERED_LOOP_KINDS = frozenset(
    {
        "scan_x",
        "scan_y",
        "scan_z",
        "condition",
        "time",
        "cycle",
        "plane",
        "repeat",
        "tile",
        "position",
        "tcspc_bin",
    }
)

#: Whether each registered loop kind advances the producer's scan position --
#: i.e. multiplies ``getNumScanPositions()`` at the recording gate -- or
#: repeats within one position. Pinned for EVERY registered kind and enforced
#: by a test, because the previous ``{condition, repeat}`` set was "the kinds
#: seen so far": any new kind silently counted as a position. ``time`` is
#: positional because firmware-run time lapses (RESOLFT) report their
#: timepoints inside ``getNumScanPositions()``; a controller-run lapse carries
#: time as a partition, not a loop. ``tcspc_bin`` is a payload axis of a
#: time-resolved detector, never a stage move.
LOOP_KIND_ADVANCES_POSITION: Mapping[str, bool] = {
    "scan_x": True,
    "scan_y": True,
    "scan_z": True,
    "cycle": True,
    "plane": True,
    "time": True,
    "tile": True,
    "position": True,
    "condition": False,
    "repeat": False,
    "tcspc_bin": False,
}
#: Loop kinds that repeat within one scan position instead of advancing it.
NON_POSITIONAL_LOOP_KINDS = frozenset(
    kind for kind, advances in LOOP_KIND_ADVANCES_POSITION.items() if not advances
)

VALID_PROVENANCE = frozenset(
    {
        "recorded",
        "user-override",
        "legacy-adapter",
        "ome-ngff",
        "shape-inference",
        "generic-fallback",
    }
)
VALID_TRAVERSAL_ORDERS = frozenset({"forward", "reverse", "serpentine"})
VALID_COPY_POLICIES = frozenset({"forbid", "allow"})


@dataclass(frozen=True)
class LayoutIssue:
    """One stable, structured validation or interpretation diagnostic."""

    severity: str
    code: str
    message: str
    field: str | None = None
    loop_id: str | None = None


class AcquisitionLayoutError(ValueError):
    """An operation could not safely proceed with an acquisition layout."""

    def __init__(self, message: str, issues: Iterable[LayoutIssue] = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


@dataclass(frozen=True)
class AcquisitionLoop:
    """One producer loop, ordered outermost to innermost in its layout."""

    id: str
    kind: str
    count: int
    step: float | None = None
    unit: str | None = None
    direction: int | None = None
    labels: tuple[str, ...] = ()
    storage_axis: str | None = None
    device: str | None = None
    """The device that drove this loop, e.g. the positioner behind ``scan_x``.

    Semantics live in :attr:`kind`; this is provenance. Without it a layout
    cannot say *which* stage moved, which is the one thing the legacy
    ``ScanStage:target_device`` attribute carried that the layout did not.
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", tuple(self.labels))


@dataclass(frozen=True)
class TraversalRule:
    """Map chronological counters for one loop to logical coordinates."""

    loop_id: str
    order: str
    parity_loops: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parity_loops", tuple(self.parity_loops))


@dataclass(frozen=True)
class RecordedEventSpan:
    """Compact stored-frame selection from the producer event lattice."""

    start: int
    count: int
    stride: int = 1
    period: int | None = None
    repeats: int = 1


@dataclass(frozen=True)
class AcquisitionPartition:
    """A split across files, groups, arrays, positions, tiles, or timepoints."""

    kind: str
    index: int | None = None
    planned_count: int | None = None
    storage: str | None = None


@dataclass(frozen=True)
class AcquisitionLayout:
    """Canonical acquisition semantics for one detector and one partition."""

    schema: str
    payload_kind: str
    detector: str
    storage_axes: tuple[str, ...]
    event_loops: tuple[AcquisitionLoop, ...]
    traversal: tuple[TraversalRule, ...] = ()
    recorded_event_spans: tuple[RecordedEventSpan, ...] | None = None
    partitions: tuple[AcquisitionPartition, ...] = ()
    modality: str | None = None
    scan_source: str | None = None
    provenance: str = "recorded"

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_axes", tuple(self.storage_axes))
        object.__setattr__(self, "event_loops", tuple(self.event_loops))
        object.__setattr__(self, "traversal", tuple(self.traversal))
        object.__setattr__(self, "partitions", tuple(self.partitions))
        if self.recorded_event_spans is None:
            return
        spans = tuple(self.recorded_event_spans)
        object.__setattr__(self, "recorded_event_spans", spans)
        try:
            canonical = canonicalize_recorded_event_spans(
                spans, producer_event_count=_producer_event_count(self)
            )
        except (TypeError, ValueError):
            # Invalid objects remain constructible so callers can obtain the
            # complete structured issue list from validate_acquisition_layout.
            return
        object.__setattr__(self, "recorded_event_spans", canonical)


@dataclass(frozen=True)
class UnfoldedArray:
    """An array whose frame axis was replaced by semantic event-loop axes."""

    data: Any
    storage_axes: tuple[str, ...]
    loop_coordinates: Mapping[str, tuple[int, ...]] = field(
        default_factory=dict, compare=False, repr=False
    )


@dataclass(frozen=True)
class _RunGroup:
    """Internal compressed sequence of equal arithmetic runs."""

    start: int
    count: int
    stride: int
    period: int | None = None
    repeats: int = 1

    @property
    def last_start(self) -> int:
        if self.repeats == 1:
            return self.start
        assert self.period is not None
        return self.start + (self.repeats - 1) * self.period


def _is_int(value: Any) -> bool:
    return type(value) is int


def _producer_event_count(layout: AcquisitionLayout) -> int:
    counts = [loop.count for loop in layout.event_loops]
    if not all(_is_int(count) and count > 0 for count in counts):
        raise ValueError("producer event count requires positive integer loop counts")
    return reduce(mul, counts, 1)


def _validate_span_for_expansion(span: RecordedEventSpan) -> None:
    for name in ("start", "count", "stride", "repeats"):
        if not _is_int(getattr(span, name)):
            raise TypeError(f"span {name} must be an integer")
    if span.count <= 0 or span.stride <= 0 or span.repeats <= 0:
        raise ValueError("span count, stride, and repeats must be positive")
    if span.repeats > 1:
        if not _is_int(span.period) or span.period <= 0:
            raise ValueError("span period must be positive when repeats is greater than one")
    elif span.period is not None:
        raise ValueError("span period must be omitted when repeats is one")


def _span_to_run_group(span: RecordedEventSpan) -> _RunGroup:
    """Apply maximal arithmetic-run splitting without expanding run items."""
    _validate_span_for_expansion(span)
    if span.repeats == 1:
        return _RunGroup(
            span.start,
            span.count,
            1 if span.count == 1 else span.stride,
        )

    assert span.period is not None
    if span.count == 1:
        return _RunGroup(span.start, span.repeats, span.period)
    if span.period == span.count * span.stride:
        return _RunGroup(span.start, span.count * span.repeats, span.stride)
    return _RunGroup(
        span.start,
        span.count,
        span.stride,
        period=span.period,
        repeats=span.repeats,
    )


def _pop_last_run(groups: list[_RunGroup]) -> _RunGroup:
    group = groups.pop()
    if group.repeats == 1:
        return group
    assert group.period is not None
    groups.append(replace(group, repeats=group.repeats - 1))
    return _RunGroup(
        group.last_start,
        group.count,
        1 if group.count == 1 else group.stride,
    )


def _pop_first_run(group: _RunGroup) -> tuple[_RunGroup, _RunGroup | None]:
    first = _RunGroup(
        group.start,
        group.count,
        1 if group.count == 1 else group.stride,
    )
    if group.repeats == 1:
        return first, None
    assert group.period is not None
    remainder = replace(
        group,
        start=group.start + group.period,
        repeats=group.repeats - 1,
        period=group.period if group.repeats - 1 > 1 else None,
    )
    return first, remainder


def _append_maximal_run_group(groups: list[_RunGroup], group: _RunGroup) -> tuple[_RunGroup, ...]:
    """Merge an arithmetic continuation at the boundary of two run groups."""
    if not groups:
        groups.append(group)
        return ()

    previous = groups[-1]
    previous_last_start = previous.last_start
    previous_last = previous_last_start + (previous.count - 1) * previous.stride
    boundary_stride = group.start - previous_last
    if boundary_stride <= 0:
        groups.append(group)
        return ()
    if previous.count > 1 and boundary_stride != previous.stride:
        groups.append(group)
        return ()

    previous_run = _pop_last_run(groups)
    first_run, remainder = _pop_first_run(group)
    if first_run.count == 1 or first_run.stride == boundary_stride:
        absorbed_count = first_run.count
        first_remainder = None
    else:
        # The boundary establishes a different stride. The first event joins
        # the preceding run; the rest starts a new arithmetic run.
        absorbed_count = 1
        first_remainder = _RunGroup(
            first_run.start + first_run.stride,
            first_run.count - 1,
            1 if first_run.count - 1 == 1 else first_run.stride,
        )
    groups.append(
        _RunGroup(
            previous_run.start,
            previous_run.count + absorbed_count,
            boundary_stride if previous_run.count == 1 else previous_run.stride,
        )
    )
    return tuple(value for value in (first_remainder, remainder) if value is not None)


def _factor_run_groups(groups: Sequence[_RunGroup]) -> list[_RunGroup]:
    """Factor consecutive equal runs using the first positive start period."""
    pending = deque(groups)
    factored: list[_RunGroup] = []
    while pending:
        group = pending.popleft()
        if not factored:
            factored.append(group)
            continue

        previous = factored[-1]
        if previous.count != group.count or previous.stride != group.stride:
            factored.append(group)
            continue

        expected_period = previous.period
        if expected_period is None:
            expected_period = group.start - previous.last_start
            if expected_period <= 0:
                factored.append(group)
                continue
        expected_start = previous.last_start + expected_period
        if group.start != expected_start:
            factored.append(group)
            continue

        absorb = group.repeats if group.repeats == 1 or group.period == expected_period else 1
        factored[-1] = _RunGroup(
            previous.start,
            previous.count,
            previous.stride,
            period=expected_period,
            repeats=previous.repeats + absorb,
        )
        if absorb < group.repeats:
            assert group.period is not None
            remainder_repeats = group.repeats - absorb
            pending.appendleft(
                replace(
                    group,
                    start=group.start + absorb * group.period,
                    repeats=remainder_repeats,
                    period=group.period if remainder_repeats > 1 else None,
                )
            )
    return factored


def canonicalize_recorded_event_spans(
    spans: Iterable[RecordedEventSpan] | None,
    *,
    producer_event_count: int,
) -> tuple[RecordedEventSpan, ...] | None:
    """Return the unique compact span representation in stored-frame order.

    The canonicalizer works on compressed arithmetic runs, so a periodic span
    with a very large ``count`` or ``repeats`` does not need to be expanded.
    """
    if not _is_int(producer_event_count) or producer_event_count <= 0:
        raise ValueError("producer_event_count must be a positive integer")
    if spans is None:
        return None

    pending: deque[_RunGroup] = deque()
    for span in spans:
        if not isinstance(span, RecordedEventSpan):
            raise TypeError("spans must contain RecordedEventSpan values")
        pending.append(_span_to_run_group(span))

    maximal_groups: list[_RunGroup] = []
    while pending:
        remainder = _append_maximal_run_group(maximal_groups, pending.popleft())
        pending.extendleft(reversed(remainder))

    if not maximal_groups:
        return tuple()

    factored = _factor_run_groups(maximal_groups)
    canonical = tuple(
        RecordedEventSpan(
            start=group.start,
            count=group.count,
            stride=group.stride,
            period=group.period if group.repeats > 1 else None,
            repeats=group.repeats,
        )
        for group in factored
    )
    if canonical == (RecordedEventSpan(start=0, count=producer_event_count, stride=1),):
        return None
    return canonical


def _layout_to_dict(layout: AcquisitionLayout) -> dict[str, Any]:
    result = asdict(layout)
    # asdict retains tuples, which json supports, but normalizing them to lists
    # makes the public representation explicit and stable for other languages.
    result["storage_axes"] = list(layout.storage_axes)
    result["event_loops"] = [asdict(loop) for loop in layout.event_loops]
    result["traversal"] = [asdict(rule) for rule in layout.traversal]
    result["partitions"] = [asdict(partition) for partition in layout.partitions]
    if layout.recorded_event_spans is None:
        result["recorded_event_spans"] = None
    else:
        result["recorded_event_spans"] = [asdict(span) for span in layout.recorded_event_spans]
    for loop in result["event_loops"]:
        loop["labels"] = list(loop["labels"])
    for rule in result["traversal"]:
        rule["parity_loops"] = list(rule["parity_loops"])
    return result


def _json_text(layout: AcquisitionLayout) -> str:
    return json.dumps(
        _layout_to_dict(layout),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def encode_acquisition_layout(layout: AcquisitionLayout) -> str:
    """Serialize a validated layout as deterministic canonical JSON."""
    if not isinstance(layout, AcquisitionLayout):
        raise TypeError("layout must be an AcquisitionLayout")
    issues = validate_acquisition_layout(layout)
    errors = tuple(issue for issue in issues if issue.severity == "error")
    if errors:
        raise AcquisitionLayoutError("cannot encode an invalid acquisition layout", errors)
    value = _json_text(layout)
    if len(value.encode("utf-8")) > MAX_INLINE_LAYOUT_BYTES:
        issue = LayoutIssue(
            "error",
            "LAYOUT_METADATA_TOO_LARGE",
            f"Canonical acquisition layout exceeds {MAX_INLINE_LAYOUT_BYTES} UTF-8 bytes",
            "AcquisitionLayout:json",
        )
        raise AcquisitionLayoutError(issue.message, (issue,))
    return value


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


SCHEMA_FAMILY, SCHEMA_VERSION = ACQUISITION_LAYOUT_SCHEMA.rsplit("/", 1)


def _report_unknown_fields(
    value: Mapping[str, Any],
    fields: set[str],
    label: str,
    issues: list[LayoutIssue] | None,
) -> None:
    """Ignore fields this version does not know, and say that it did.

    Rejecting them would make a file written by a newer ImSwitch unreadable by
    an older one, which is a worse failure than not understanding one field:
    the whole recording becomes inaccessible. Additive fields are safe to skip
    by the contract's own rule, and the schema version is the boundary for
    anything that is not -- a different version is refused outright rather
    than half-understood.
    """
    extra = sorted(set(value) - fields)
    if not extra:
        return
    if issues is not None:
        issues.append(
            LayoutIssue(
                "warning",
                "UNKNOWN_LAYOUT_FIELD",
                f"{label} carries {', '.join(extra)}, which this version of "
                f"ImSwitch does not understand and ignored",
                label,
            )
        )


def _check_schema_version(schema: Any) -> None:
    """Refuse a layout from a different version of the contract.

    Unknown *fields* within this version are additive and ignorable; a
    different *version* may change what the existing fields mean, so guessing
    is not safe. Failing here gives a clear message instead of a confusing
    complaint about whichever field happens to be new.
    """
    text = schema if isinstance(schema, str) else ""
    if text == ACQUISITION_LAYOUT_SCHEMA or not text.startswith(f"{SCHEMA_FAMILY}/"):
        return
    version = text.rsplit("/", 1)[-1]
    issue = LayoutIssue(
        "error",
        "UNSUPPORTED_SCHEMA_VERSION",
        f"This recording uses acquisition-layout schema version {version!r}; "
        f"this version of ImSwitch understands {SCHEMA_VERSION!r}. It was "
        f"written by a newer ImSwitch.",
        "schema",
    )
    raise AcquisitionLayoutError(issue.message, (issue,))


def _decode_loop(value: Any, issues: list[LayoutIssue] | None = None) -> AcquisitionLoop:
    item = _expect_mapping(value, "event loop")
    fields = {
        "id",
        "kind",
        "count",
        "step",
        "unit",
        "direction",
        "labels",
        "storage_axis",
        "device",
    }
    _report_unknown_fields(item, fields, "event loop", issues)
    return AcquisitionLoop(
        id=item.get("id"),
        kind=item.get("kind"),
        count=item.get("count"),
        step=item.get("step"),
        unit=item.get("unit"),
        direction=item.get("direction"),
        labels=tuple(item.get("labels", ())),
        storage_axis=item.get("storage_axis"),
        device=item.get("device"),
    )


def _decode_traversal(value: Any, issues: list[LayoutIssue] | None = None) -> TraversalRule:
    item = _expect_mapping(value, "traversal rule")
    fields = {"loop_id", "order", "parity_loops"}
    _report_unknown_fields(item, fields, "traversal rule", issues)
    return TraversalRule(
        loop_id=item.get("loop_id"),
        order=item.get("order"),
        parity_loops=tuple(item.get("parity_loops", ())),
    )


def _decode_span(value: Any, issues: list[LayoutIssue] | None = None) -> RecordedEventSpan:
    item = _expect_mapping(value, "recorded event span")
    fields = {"start", "count", "stride", "period", "repeats"}
    _report_unknown_fields(item, fields, "recorded event span", issues)
    return RecordedEventSpan(
        start=item.get("start"),
        count=item.get("count"),
        stride=item.get("stride", 1),
        period=item.get("period"),
        repeats=item.get("repeats", 1),
    )


def _decode_partition(value: Any, issues: list[LayoutIssue] | None = None) -> AcquisitionPartition:
    item = _expect_mapping(value, "partition")
    fields = {"kind", "index", "planned_count", "storage"}
    _report_unknown_fields(item, fields, "partition", issues)
    return AcquisitionPartition(
        kind=item.get("kind"),
        index=item.get("index"),
        planned_count=item.get("planned_count"),
        storage=item.get("storage"),
    )


def decode_acquisition_layout(
    value: str | bytes | bytearray,
    issues: list[LayoutIssue] | None = None,
) -> AcquisitionLayout:
    """Decode, structurally validate, and immediately canonicalize a layout.

    Pass ``issues`` to collect what the decode had to skip. Fields this
    version does not know are ignored and reported there rather than refused:
    a recording written by a newer ImSwitch stays readable, minus whatever it
    says that this version cannot act on. A different *schema version* is
    refused outright, because that may change what the known fields mean.
    """
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise TypeError("encoded acquisition layout must be str or bytes")
    if len(raw) > MAX_INLINE_LAYOUT_BYTES:
        issue = LayoutIssue(
            "error",
            "LAYOUT_METADATA_TOO_LARGE",
            f"Encoded acquisition layout exceeds {MAX_INLINE_LAYOUT_BYTES} UTF-8 bytes",
            "AcquisitionLayout:json",
        )
        raise AcquisitionLayoutError(issue.message, (issue,))

    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("AcquisitionLayout:json is not valid UTF-8 JSON") from exc
    item = _expect_mapping(decoded, "acquisition layout")
    _check_schema_version(item.get("schema"))
    fields = {
        "schema",
        "payload_kind",
        "detector",
        "storage_axes",
        "event_loops",
        "traversal",
        "recorded_event_spans",
        "partitions",
        "modality",
        "scan_source",
        "provenance",
    }
    _report_unknown_fields(item, fields, "acquisition layout", issues)
    spans_value = item.get("recorded_event_spans")
    spans = (
        None
        if spans_value is None
        else tuple(_decode_span(entry, issues) for entry in spans_value)
    )
    layout = AcquisitionLayout(
        schema=item.get("schema"),
        payload_kind=item.get("payload_kind"),
        detector=item.get("detector"),
        storage_axes=tuple(item.get("storage_axes", ())),
        event_loops=tuple(_decode_loop(entry, issues) for entry in item.get("event_loops", ())),
        traversal=tuple(_decode_traversal(entry, issues) for entry in item.get("traversal", ())),
        recorded_event_spans=spans,
        partitions=tuple(_decode_partition(entry, issues) for entry in item.get("partitions", ())),
        modality=item.get("modality"),
        scan_source=item.get("scan_source"),
        provenance=item.get("provenance", "recorded"),
    )
    canonical_raw = _json_text(layout).encode("utf-8")
    if len(canonical_raw) > MAX_INLINE_LAYOUT_BYTES:
        issue = LayoutIssue(
            "error",
            "LAYOUT_METADATA_TOO_LARGE",
            f"Canonical acquisition layout exceeds {MAX_INLINE_LAYOUT_BYTES} UTF-8 bytes",
            "AcquisitionLayout:json",
        )
        raise AcquisitionLayoutError(issue.message, (issue,))
    return layout


def _issue(
    issues: list[LayoutIssue],
    code: str,
    message: str,
    field_name: str | None = None,
    loop_id: str | None = None,
    severity: str = "error",
) -> None:
    issues.append(LayoutIssue(severity, code, message, field_name, loop_id))


def _expanded_span_ordinals(spans: Sequence[RecordedEventSpan]) -> Iterator[int]:
    for span in spans:
        _validate_span_for_expansion(span)
        period = span.period or 0
        for repetition in range(span.repeats):
            repetition_start = span.start + repetition * period
            for item in range(span.count):
                yield repetition_start + item * span.stride


def _span_runs(span: RecordedEventSpan) -> Iterator[tuple[int, int, int]]:
    """Yield ``(start, stride, count)`` for each arithmetic run in a span."""
    period = span.period or 0
    for repetition in range(span.repeats):
        yield span.start + repetition * period, span.stride, span.count


def _span_progressions(span: RecordedEventSpan) -> Iterator[tuple[int, int, int]]:
    """The same events as ``_span_runs``, in the fewer arithmetic progressions.

    A span is a grid: ``start + i*period + j*stride`` over ``repeats`` by
    ``count``. It can be cut into progressions along either edge, and the two
    cuts describe exactly the same events, so taking the shorter one costs
    nothing and bounds the work by ``min(repeats, count)`` rather than by
    ``repeats`` alone.

    That distinction is the difference between validating a layout and hanging
    on one. Two spans of three frames per position over a hundred-thousand
    position scan are ordinary, and cutting them per repetition built six
    hundred thousand runs -- seventy megabytes and a second and a half -- to
    answer a question about six progressions.
    """
    period = span.period or 0
    if span.repeats <= span.count:
        yield from _span_runs(span)
        return
    # Cut the other way: one progression per offset within a run, stepping by
    # the period instead of the stride.
    for offset in range(span.count):
        yield span.start + offset * span.stride, period, span.repeats


def _run_last(run: tuple[int, int, int]) -> int:
    start, stride, count = run
    return start + (count - 1) * stride


def _runs_share_a_value(
    run_a: tuple[int, int, int], run_b: tuple[int, int, int]
) -> bool:
    """Exact constant-time intersection test for two finite arithmetic runs."""
    a_start, a_stride, _ = run_a
    b_start, b_stride, _ = run_b
    low = max(a_start, b_start)
    high = min(_run_last(run_a), _run_last(run_b))
    if low > high:
        return False
    divisor = math.gcd(a_stride, b_stride)
    delta = b_start - a_start
    if delta % divisor:
        return False
    # Solve a_start + i * a_stride == b_start + j * b_stride for the smallest
    # shared value, then walk the combined period into the overlapping range.
    b_steps = b_stride // divisor
    if b_steps > 1:
        inverse = pow((a_stride // divisor) % b_steps, -1, b_steps)
        steps = (delta // divisor) % b_steps * inverse % b_steps
    else:
        steps = 0
    shared = a_start + steps * a_stride
    combined_period = a_stride // divisor * b_stride
    if shared < low:
        shared += -(-(low - shared) // combined_period) * combined_period
    return shared <= high


def _spans_share_an_event(spans: Sequence[RecordedEventSpan]) -> bool:
    """True when two spans select the same producer event.

    This works on compressed arithmetic progressions, so a periodic span is
    never expanded into its individual ordinals, and each span is cut along
    its shorter edge -- see :func:`_span_progressions` -- so the cost follows
    ``min(repeats, count)`` rather than the number of selected frames or the
    number of repetitions. Both of the wider measures reach hundreds of
    megabytes on ordinary point-scan selections.
    """
    runs = sorted(
        (run for span in spans for run in _span_progressions(span)),
        key=lambda run: run[0],
    )
    active: list[tuple[int, int, int]] = []
    for run in runs:
        start = run[0]
        active = [other for other in active if _run_last(other) >= start]
        for other in active:
            if _runs_share_a_value(other, run):
                return True
        active.append(run)
    return False


def _validate_spans(
    layout: AcquisitionLayout,
    producer_event_count: int | None,
    issues: list[LayoutIssue],
) -> int | None:
    spans = layout.recorded_event_spans
    if spans is None:
        return producer_event_count

    structurally_valid = True
    for index, span in enumerate(spans):
        if not isinstance(span, RecordedEventSpan):
            _issue(
                issues, "INVALID_SPAN", f"Span {index} has an invalid type", "recorded_event_spans"
            )
            structurally_valid = False
            continue
        for name in ("start", "count", "stride", "repeats"):
            number = getattr(span, name)
            minimum = 0 if name == "start" else 1
            if not _is_int(number) or number < minimum:
                _issue(
                    issues,
                    "INVALID_SPAN_VALUE",
                    f"Span {index} {name} must be an integer >= {minimum}",
                    f"recorded_event_spans[{index}].{name}",
                )
                structurally_valid = False
        if _is_int(span.repeats) and span.repeats > 1:
            if not _is_int(span.period) or span.period <= 0:
                _issue(
                    issues,
                    "INVALID_SPAN_PERIOD",
                    f"Span {index} period must be positive when repeats is greater than one",
                    f"recorded_event_spans[{index}].period",
                )
                structurally_valid = False
        elif span.period is not None:
            _issue(
                issues,
                "UNNEEDED_SPAN_PERIOD",
                f"Span {index} period must be omitted when repeats is one",
                f"recorded_event_spans[{index}].period",
            )
            structurally_valid = False

    if not structurally_valid or producer_event_count is None:
        return None

    selected_count = sum(span.count * span.repeats for span in spans)
    # A single span is fully covered by the within-span Diophantine test below.
    needs_cross_span_check = len(spans) > 1
    for span_index, span in enumerate(spans):
        last = span.start + (span.repeats - 1) * (span.period or 0) + (span.count - 1) * span.stride
        if span.start < 0 or last >= producer_event_count:
            _issue(
                issues,
                "SPAN_OUT_OF_RANGE",
                f"Span {span_index} addresses events outside 0..{producer_event_count - 1}",
                f"recorded_event_spans[{span_index}]",
            )
        if span.repeats > 1:
            assert span.period is not None
            divisor = math.gcd(span.period, span.stride)
            if span.repeats > span.stride // divisor and span.count > span.period // divisor:
                _issue(
                    issues,
                    "DUPLICATE_PRODUCER_EVENT",
                    f"Span {span_index} selects a producer event more than once",
                    f"recorded_event_spans[{span_index}]",
                )
                needs_cross_span_check = False

    # Only spans that fit inside the producer's own event count are worth
    # comparing, and this is where they are known to. The sweep below builds
    # one run per repetition, so running it first let a layout of a few
    # hundred bytes -- two spans claiming three hundred thousand repetitions
    # of a scan that has six events -- allocate hundreds of megabytes while
    # opening a file, to compare spans that are invalid either way.
    out_of_range = any(
        span.start < 0
        or span.start
        + (span.repeats - 1) * (span.period or 0)
        + (span.count - 1) * span.stride
        >= producer_event_count
        for span in spans
    )
    if needs_cross_span_check and not out_of_range and _spans_share_an_event(spans):
        _issue(
            issues,
            "DUPLICATE_PRODUCER_EVENT",
            "Two recorded event spans select the same producer event",
            "recorded_event_spans",
        )
    return selected_count


def validate_acquisition_layout(
    layout: AcquisitionLayout,
    *,
    shape: Sequence[int] | None = None,
) -> tuple[LayoutIssue, ...]:
    """Return all structural and payload-specific issues for a layout."""
    issues: list[LayoutIssue] = []
    if not isinstance(layout, AcquisitionLayout):
        return (LayoutIssue("error", "INVALID_LAYOUT", "Value is not an AcquisitionLayout"),)

    if layout.schema != ACQUISITION_LAYOUT_SCHEMA:
        _issue(
            issues, "UNSUPPORTED_SCHEMA", f"Expected schema {ACQUISITION_LAYOUT_SCHEMA!r}", "schema"
        )
    for field_name in ("payload_kind", "detector"):
        value = getattr(layout, field_name)
        if not isinstance(value, str) or not value.strip():
            _issue(issues, "INVALID_TEXT", f"{field_name} must be a non-empty string", field_name)
    if layout.provenance not in VALID_PROVENANCE:
        _issue(
            issues,
            "INVALID_PROVENANCE",
            f"Unknown layout provenance {layout.provenance!r}",
            "provenance",
        )

    axes = layout.storage_axes
    if any(not isinstance(axis, str) or not axis.strip() for axis in axes):
        _issue(
            issues, "INVALID_STORAGE_AXIS", "Storage axes must be non-empty strings", "storage_axes"
        )
    if len(set(axes)) != len(axes):
        _issue(
            issues, "DUPLICATE_STORAGE_AXIS", "Storage axis names must be unique", "storage_axes"
        )
    if shape is not None:
        try:
            source_shape = tuple(shape)
        except TypeError:
            source_shape = ()
            _issue(issues, "INVALID_SHAPE", "Source shape must be a sequence", "shape")
        if len(source_shape) != len(axes):
            _issue(
                issues,
                "STORAGE_RANK_MISMATCH",
                f"Layout declares {len(axes)} axes but source rank is {len(source_shape)}",
                "storage_axes",
            )
    else:
        source_shape = None

    loop_ids: list[str] = []
    loop_by_id: dict[str, AcquisitionLoop] = {}
    valid_loop_counts = True
    for index, loop in enumerate(layout.event_loops):
        if not isinstance(loop, AcquisitionLoop):
            _issue(issues, "INVALID_LOOP", f"Loop {index} has an invalid type", "event_loops")
            valid_loop_counts = False
            continue
        if not isinstance(loop.id, str) or not loop.id.strip():
            _issue(
                issues,
                "INVALID_LOOP_ID",
                f"Loop {index} id must be a non-empty string",
                f"event_loops[{index}].id",
            )
        else:
            loop_ids.append(loop.id)
            loop_by_id[loop.id] = loop
        if not isinstance(loop.kind, str) or not loop.kind.strip():
            _issue(
                issues,
                "INVALID_LOOP_KIND",
                f"Loop {loop.id!r} kind must be a non-empty string",
                f"event_loops[{index}].kind",
                loop.id,
            )
        if not _is_int(loop.count) or loop.count <= 0:
            _issue(
                issues,
                "INVALID_LOOP_COUNT",
                f"Loop {loop.id!r} count must be a positive integer",
                f"event_loops[{index}].count",
                loop.id,
            )
            valid_loop_counts = False
        if loop.step is not None and (
            not isinstance(loop.step, (int, float))
            or isinstance(loop.step, bool)
            or not math.isfinite(float(loop.step))
        ):
            _issue(
                issues,
                "INVALID_LOOP_STEP",
                f"Loop {loop.id!r} step must be finite",
                f"event_loops[{index}].step",
                loop.id,
            )
        if loop.device is not None and (
            not isinstance(loop.device, str) or not loop.device.strip()
        ):
            _issue(
                issues,
                "INVALID_LOOP_DEVICE",
                f"Loop {loop.id!r} device must be a non-empty string",
                f"event_loops[{index}].device",
                loop.id,
            )
        if loop.unit is not None and (not isinstance(loop.unit, str) or not loop.unit.strip()):
            _issue(
                issues,
                "INVALID_LOOP_UNIT",
                f"Loop {loop.id!r} unit must be a non-empty string",
                f"event_loops[{index}].unit",
                loop.id,
            )
        if loop.direction not in (None, -1, 1):
            _issue(
                issues,
                "INVALID_LOOP_DIRECTION",
                f"Loop {loop.id!r} direction must be +1 or -1",
                f"event_loops[{index}].direction",
                loop.id,
            )
        elif loop.direction is not None and not (
            isinstance(loop.kind, str) and loop.kind.startswith("scan_")
        ):
            # Direction orients a physical scan axis. On a time, condition or
            # repeat loop it has no meaning, and an image-building consumer
            # would mirror its T or condition axis on it.
            _issue(
                issues,
                "DIRECTION_ON_NON_SPATIAL_LOOP",
                f"Loop {loop.id!r} of kind {loop.kind!r} cannot carry a direction",
                f"event_loops[{index}].direction",
                loop.id,
            )
        if not isinstance(loop.labels, tuple) or any(
            not isinstance(label, str) for label in loop.labels
        ):
            _issue(
                issues,
                "INVALID_LOOP_LABELS",
                f"Loop {loop.id!r} labels must be strings",
                f"event_loops[{index}].labels",
                loop.id,
            )
        elif loop.labels and _is_int(loop.count) and len(loop.labels) != loop.count:
            _issue(
                issues,
                "LOOP_LABEL_COUNT_MISMATCH",
                f"Loop {loop.id!r} has {len(loop.labels)} labels for count {loop.count}",
                f"event_loops[{index}].labels",
                loop.id,
            )
    if len(set(loop_ids)) != len(loop_ids):
        _issue(issues, "DUPLICATE_LOOP_ID", "Event loop IDs must be unique", "event_loops")

    traversal_targets: list[str] = []
    loop_positions = {loop_id: index for index, loop_id in enumerate(loop_ids)}
    for index, rule in enumerate(layout.traversal):
        if not isinstance(rule, TraversalRule):
            _issue(
                issues,
                "INVALID_TRAVERSAL",
                f"Traversal rule {index} has an invalid type",
                "traversal",
            )
            continue
        traversal_targets.append(rule.loop_id)
        if rule.loop_id not in loop_by_id:
            _issue(
                issues,
                "UNKNOWN_TRAVERSAL_LOOP",
                f"Traversal references absent loop {rule.loop_id!r}",
                f"traversal[{index}].loop_id",
                rule.loop_id,
            )
        if rule.order not in VALID_TRAVERSAL_ORDERS:
            _issue(
                issues,
                "INVALID_TRAVERSAL_ORDER",
                f"Unknown traversal order {rule.order!r}",
                f"traversal[{index}].order",
                rule.loop_id,
            )
            continue
        if rule.order != "serpentine" and rule.parity_loops:
            _issue(
                issues,
                "UNEXPECTED_PARITY_LOOPS",
                f"{rule.order!r} traversal cannot declare parity loops",
                f"traversal[{index}].parity_loops",
                rule.loop_id,
            )
        if rule.order == "serpentine":
            if not rule.parity_loops:
                _issue(
                    issues,
                    "MISSING_PARITY_LOOPS",
                    "Serpentine traversal requires parity loops",
                    f"traversal[{index}].parity_loops",
                    rule.loop_id,
                )
            elif rule.loop_id in loop_positions:
                target_position = loop_positions[rule.loop_id]
                parity_length = len(rule.parity_loops)
                expected = (
                    tuple(loop_ids[target_position - parity_length : target_position])
                    if parity_length <= target_position
                    else ()
                )
                if tuple(rule.parity_loops) != expected:
                    _issue(
                        issues,
                        "INVALID_PARITY_LOOPS",
                        f"Serpentine parity loops must be the ordered contiguous suffix immediately outside {rule.loop_id!r}",
                        f"traversal[{index}].parity_loops",
                        rule.loop_id,
                    )
    if len(set(traversal_targets)) != len(traversal_targets):
        _issue(
            issues,
            "DUPLICATE_TRAVERSAL_TARGET",
            "Only one traversal rule may target each loop",
            "traversal",
        )

    for index, partition in enumerate(layout.partitions):
        if not isinstance(partition, AcquisitionPartition):
            _issue(
                issues, "INVALID_PARTITION", f"Partition {index} has an invalid type", "partitions"
            )
            continue
        if not isinstance(partition.kind, str) or not partition.kind.strip():
            _issue(
                issues,
                "INVALID_PARTITION_KIND",
                f"Partition {index} kind must be a non-empty string",
                f"partitions[{index}].kind",
            )
        if partition.index is not None and (not _is_int(partition.index) or partition.index < 0):
            _issue(
                issues,
                "INVALID_PARTITION_INDEX",
                f"Partition {index} index must be a non-negative integer",
                f"partitions[{index}].index",
            )
        if partition.planned_count is not None and (
            not _is_int(partition.planned_count) or partition.planned_count <= 0
        ):
            _issue(
                issues,
                "INVALID_PARTITION_COUNT",
                f"Partition {index} planned_count must be positive when present",
                f"partitions[{index}].planned_count",
            )

    producer_count = None
    if valid_loop_counts:
        producer_count = reduce(mul, (loop.count for loop in layout.event_loops), 1)
    selected_count = _validate_spans(layout, producer_count, issues)

    if layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM:
        if axes.count("frame") != 1:
            _issue(
                issues,
                "FRAME_AXIS_REQUIRED",
                "Detector frame streams require exactly one canonical 'frame' storage axis",
                "storage_axes",
            )
        for loop in layout.event_loops:
            if isinstance(loop, AcquisitionLoop) and loop.storage_axis is not None:
                _issue(
                    issues,
                    "FRAME_LOOP_STORAGE_AXIS",
                    f"Frame-stream loop {loop.id!r} must not map directly to a storage axis",
                    "event_loops",
                    loop.id,
                )
        if (
            source_shape is not None
            and len(source_shape) == len(axes)
            and axes.count("frame") == 1
            and selected_count is not None
        ):
            observed = source_shape[axes.index("frame")]
            if observed != selected_count:
                _issue(
                    issues,
                    "FRAME_COUNT_MISMATCH",
                    f"Layout selects {selected_count} frames but source contains {observed}",
                    "shape",
                )
    elif layout.payload_kind in (PAYLOAD_ASSEMBLED_IMAGE, PAYLOAD_RECONSTRUCTED_IMAGE):
        if layout.recorded_event_spans is not None:
            _issue(
                issues,
                "SPANS_FORBIDDEN",
                f"{layout.payload_kind} payloads cannot declare recorded event spans",
                "recorded_event_spans",
            )
        mapped_axes: list[str] = []
        for loop in layout.event_loops:
            if not isinstance(loop, AcquisitionLoop):
                continue
            if loop.storage_axis is None:
                _issue(
                    issues,
                    "LOOP_STORAGE_AXIS_REQUIRED",
                    f"Loop {loop.id!r} must map to a storage axis",
                    "event_loops",
                    loop.id,
                )
                continue
            mapped_axes.append(loop.storage_axis)
            if loop.storage_axis not in axes:
                _issue(
                    issues,
                    "UNKNOWN_LOOP_STORAGE_AXIS",
                    f"Loop {loop.id!r} maps to absent axis {loop.storage_axis!r}",
                    "event_loops",
                    loop.id,
                )
            elif (
                source_shape is not None and len(source_shape) == len(axes) and _is_int(loop.count)
            ):
                observed = source_shape[axes.index(loop.storage_axis)]
                if observed != loop.count:
                    _issue(
                        issues,
                        "LOOP_AXIS_COUNT_MISMATCH",
                        f"Loop {loop.id!r} count {loop.count} does not match axis {loop.storage_axis!r} length {observed}",
                        "shape",
                        loop.id,
                    )
        if len(set(mapped_axes)) != len(mapped_axes):
            _issue(
                issues,
                "DUPLICATE_LOOP_STORAGE_AXIS",
                "Acquisition loops must map to distinct storage axes",
                "event_loops",
            )
    elif isinstance(layout.payload_kind, str) and layout.payload_kind.strip():
        _issue(
            issues,
            "UNKNOWN_PAYLOAD_KIND",
            f"Payload kind {layout.payload_kind!r} has no registered strict validator",
            "payload_kind",
            severity="warning",
        )

    try:
        encoded_size = len(_json_text(layout).encode("utf-8"))
    except (TypeError, ValueError):
        encoded_size = 0
    if encoded_size > MAX_INLINE_LAYOUT_BYTES:
        _issue(
            issues,
            "LAYOUT_METADATA_TOO_LARGE",
            f"Canonical acquisition layout exceeds {MAX_INLINE_LAYOUT_BYTES} UTF-8 bytes",
            "AcquisitionLayout:json",
        )
    return tuple(issues)


def scan_position_count(layout: AcquisitionLayout) -> int | None:
    """Producer scan positions, or ``None`` when a loop kind is unclassifiable.

    Recording cross-checks this against ``getNumScanPositions()``. Loop kinds
    are an open vocabulary, so a layout using a kind this version does not know
    cannot be split into positional and non-positional loops. Callers must skip
    the comparison in that case instead of assuming the unknown loop advances
    the scan, which would reject a valid recording before a writer is opened.
    """
    positions = 1
    for loop in layout.event_loops:
        if not isinstance(loop, AcquisitionLoop) or not isinstance(loop.kind, str):
            return None
        # ``scan_*`` covers both the registered axes and the ``scan_axis_<n>``
        # names producers generate for axes without a semantic X/Y/Z label.
        if loop.kind not in REGISTERED_LOOP_KINDS and not loop.kind.startswith("scan_"):
            return None
        if loop.kind in NON_POSITIONAL_LOOP_KINDS:
            continue
        if not _is_int(loop.count) or loop.count <= 0:
            return None
        positions *= loop.count
    return positions


def _producer_counters(layout: AcquisitionLayout, event_index: int) -> dict[str, int]:
    event_count = _producer_event_count(layout)
    if not _is_int(event_index) or not 0 <= event_index < event_count:
        raise IndexError(f"producer event index {event_index!r} is outside 0..{event_count - 1}")
    remaining = event_index
    counters: dict[str, int] = {}
    for loop in reversed(layout.event_loops):
        remaining, coordinate = divmod(remaining, loop.count)
        counters[loop.id] = coordinate
    return {loop.id: counters[loop.id] for loop in layout.event_loops}


def _producer_event_coordinates_unchecked(
    layout: AcquisitionLayout, event_index: int
) -> Mapping[str, int]:
    counters = _producer_counters(layout, event_index)
    coordinates = dict(counters)
    loop_by_id = {loop.id: loop for loop in layout.event_loops}
    for rule in layout.traversal:
        if rule.order == "forward":
            continue
        reverse = rule.order == "reverse"
        if rule.order == "serpentine":
            flat = 0
            for parity_loop_id in rule.parity_loops:
                flat = flat * loop_by_id[parity_loop_id].count + counters[parity_loop_id]
            reverse = flat % 2 == 1
        if reverse:
            target = loop_by_id[rule.loop_id]
            coordinates[rule.loop_id] = target.count - 1 - counters[rule.loop_id]
    return {loop.id: coordinates[loop.id] for loop in layout.event_loops}


def _shape_free_errors(layout: AcquisitionLayout) -> tuple[LayoutIssue, ...]:
    """Return shape-independent validation errors, once per layout instance.

    The coordinate helpers are called per stored frame, and validation both
    walks the whole layout and serializes it to JSON to check the inline-size
    budget. Re-running that on every lookup dominated the cost of resolving a
    coordinate, so the verdict is memoized on the (frozen, immutable) instance.
    The cache is not a dataclass field, so equality, hashing, ``asdict`` and
    ``replace`` are unaffected.
    """
    if not isinstance(layout, AcquisitionLayout):
        return tuple(
            issue for issue in validate_acquisition_layout(layout) if issue.severity == "error"
        )
    cached = getattr(layout, "_cached_shape_free_errors", None)
    if cached is not None:
        return cached
    errors = tuple(
        issue for issue in validate_acquisition_layout(layout) if issue.severity == "error"
    )
    object.__setattr__(layout, "_cached_shape_free_errors", errors)
    return errors


def _raise_for_invalid_coordinates(layout: AcquisitionLayout) -> None:
    errors = _shape_free_errors(layout)
    if errors:
        raise AcquisitionLayoutError("cannot resolve coordinates for an invalid layout", errors)


def producer_event_coordinates(layout: AcquisitionLayout, event_index: int) -> Mapping[str, int]:
    """Map one partition-local producer ordinal to logical loop coordinates."""
    _raise_for_invalid_coordinates(layout)
    return _producer_event_coordinates_unchecked(layout, event_index)


def _recorded_event_ordinal(layout: AcquisitionLayout, frame_index: int) -> int:
    if not _is_int(frame_index) or frame_index < 0:
        raise IndexError("recorded frame index must be a non-negative integer")
    if layout.recorded_event_spans is None:
        event_count = _producer_event_count(layout)
        if frame_index >= event_count:
            raise IndexError(f"recorded frame index {frame_index} is outside 0..{event_count - 1}")
        return frame_index
    remaining = frame_index
    for span in layout.recorded_event_spans:
        span_size = span.count * span.repeats
        if remaining >= span_size:
            remaining -= span_size
            continue
        repetition, item = divmod(remaining, span.count)
        return span.start + repetition * (span.period or 0) + item * span.stride
    total = sum(span.count * span.repeats for span in layout.recorded_event_spans)
    raise IndexError(f"recorded frame index {frame_index} is outside 0..{total - 1}")


def recorded_frame_coordinates(layout: AcquisitionLayout, frame_index: int) -> Mapping[str, int]:
    """Map a stored detector frame to logical coordinates without reading pixels."""
    _raise_for_invalid_coordinates(layout)
    return _producer_event_coordinates_unchecked(
        layout, _recorded_event_ordinal(layout, frame_index)
    )


def iter_recorded_coordinates(layout: AcquisitionLayout) -> Iterator[Mapping[str, int]]:
    """Yield logical coordinates for every stored frame in storage order."""
    _raise_for_invalid_coordinates(layout)
    if layout.recorded_event_spans is None:
        ordinals: Iterable[int] = range(_producer_event_count(layout))
    else:
        ordinals = _expanded_span_ordinals(layout.recorded_event_spans)
    for ordinal in ordinals:
        yield _producer_event_coordinates_unchecked(layout, ordinal)


def physical_orientation_flips(layout: AcquisitionLayout) -> frozenset[str]:
    """IDs of the scan-axis loops whose logical index runs against the physical axis.

    ``AcquisitionLoop.direction`` is the orientation of a loop's *logical
    index axis* against the physical axis: with ``direction == -1``, logical
    index 0 sits at the highest physical coordinate and the index decreases
    with position. It is not a traversal order. For a ``forward`` or
    ``serpentine`` loop -- every in-tree producer -- that is the same thing as
    the sign of the stage step, because logical index 0 is the first position
    visited. For a genuine ``reverse`` retrace logical index 0 is the *last*
    position visited, so a producer emitting ``reverse`` must set
    ``direction`` for the index axis, i.e. negate the stage sign. The two
    compose: traversal maps chronology to the index, this helper maps the
    index to physical orientation.

    Only physical scan axes (``scan_*`` kinds) carry an orientation;
    validation refuses ``direction`` on any other kind, so a time, condition
    or repeat loop is never mirrored.

    This is the only place the sign is interpreted. Producers and legacy
    adapters never encode a negative direction as a ``reverse`` traversal,
    and consumers never re-read ``direction`` themselves -- doing both is how
    one recording came to reconstruct as mirror images in two reconstructors.
    """
    return frozenset(
        loop.id
        for loop in layout.event_loops
        if loop.direction == -1 and isinstance(loop.kind, str) and loop.kind.startswith("scan_")
    )


def _physical_coordinates(
    layout: AcquisitionLayout, coordinates: Mapping[str, int]
) -> Mapping[str, int]:
    flips = physical_orientation_flips(layout)
    if not flips:
        return coordinates
    count_by_id = {loop.id: loop.count for loop in layout.event_loops}
    return {
        loop_id: (count_by_id[loop_id] - 1 - value if loop_id in flips else value)
        for loop_id, value in coordinates.items()
    }


def physical_frame_coordinates(layout: AcquisitionLayout, frame_index: int) -> Mapping[str, int]:
    """Map a stored frame to indices that increase with physical position.

    :func:`recorded_frame_coordinates` with :func:`physical_orientation_flips`
    applied. Use this to place a frame in an image; use the logical form to
    reason about chronology.
    """
    return _physical_coordinates(layout, recorded_frame_coordinates(layout, frame_index))


def iter_physical_coordinates(layout: AcquisitionLayout) -> Iterator[Mapping[str, int]]:
    """Yield physically oriented coordinates for every stored frame in storage order."""
    for coordinates in iter_recorded_coordinates(layout):
        yield _physical_coordinates(layout, coordinates)


class UnconsumedLoopError(AcquisitionLayoutError):
    """A consumer met an event loop it neither places nor declared folded.

    Raised instead of dropping the loop: a loop the consumer does not know
    about is a whole dimension of the recording, and folding it silently is
    how a two-pulse scan came out with every second frame overwriting the
    first. The message names the consumer and the loop so the fix is obvious:
    either place the loop, or say what folding it means.
    """


@dataclass(frozen=True)
class LoopSelection:
    """The loops one consumer placed, by role, and the ones it folded.

    ``by_role`` maps each requested role to the loop that fills it, or
    ``None`` for an optional role the layout does not have. ``folded`` holds
    the loops the consumer declared it collapses on purpose, in layout order.
    """

    by_role: Mapping[str, AcquisitionLoop | None]
    folded: tuple[AcquisitionLoop, ...] = ()

    def __getitem__(self, role: str) -> AcquisitionLoop | None:
        return self.by_role[role]

    def count(self, role: str, default: int = 1) -> int:
        """Loop count for ``role``, or ``default`` when the role is absent."""
        loop = self.by_role.get(role)
        return int(loop.count) if loop is not None else default


def select_loops(
    layout: AcquisitionLayout,
    *,
    consumer: str,
    roles: Mapping[str, str | Sequence[str]],
    required: Iterable[str] = (),
    fold: Iterable[str] = (),
) -> LoopSelection | None:
    """Pick the loops a consumer places, and refuse the ones it cannot.

    This is the one way a consumer reads ``event_loops``. ``roles`` maps a
    consumer-side role (``"fast"``, ``"slow"``, ``"depth"``, ``"time"``...)
    to the loop kind -- or kinds, first match wins -- that fills it. A kind
    fills at most one role; a layout with two loops of one kind cannot be
    placed by roles and is refused. ``required`` names the roles without
    which the layout is simply not this consumer's kind of scan: the result
    is then ``None`` and the caller falls back, exactly as before. ``fold``
    names the kinds the consumer collapses deliberately -- SMLM flattening
    ``time`` and ``repeat`` into one blinking trace, say -- and those loops
    are returned in :attr:`LoopSelection.folded` so the consumer can do the
    collapsing explicitly.

    Every other loop raises :class:`UnconsumedLoopError`. That is the whole
    point: a consumer used to select the kinds it knew and let the rest fall
    through its index arithmetic, so a ``repeat`` loop the producer emitted
    for a two-pulse scan produced two slots per position and the second pulse
    overwrote the first, with no message anywhere. Registered kinds are an
    open vocabulary, so the refusal is for loops the *consumer* does not
    handle, not for kinds this module does not know.
    """
    kind_to_role: dict[str, str] = {}
    for role, kinds in roles.items():
        for kind in ((kinds,) if isinstance(kinds, str) else tuple(kinds)):
            if kind in kind_to_role and kind_to_role[kind] != role:
                raise ValueError(
                    f"{consumer}: kind {kind!r} is mapped to both roles "
                    f"{kind_to_role[kind]!r} and {role!r}"
                )
            kind_to_role[kind] = role
    fold_kinds = frozenset(fold)

    by_role: dict[str, AcquisitionLoop | None] = {role: None for role in roles}
    folded: list[AcquisitionLoop] = []
    unconsumed: list[AcquisitionLoop] = []
    for loop in layout.event_loops:
        role = kind_to_role.get(loop.kind)
        if role is not None:
            if by_role[role] is not None:
                raise UnconsumedLoopError(
                    f"{consumer} cannot place loop {loop.id!r}: kind "
                    f"{loop.kind!r} already fills role {role!r} (loop "
                    f"{by_role[role].id!r}). Two loops of one kind need "
                    f"distinct kinds -- see the contract's note on one "
                    f"physical axis driven by two loops."
                )
            by_role[role] = loop
        elif loop.kind in fold_kinds:
            folded.append(loop)
        else:
            unconsumed.append(loop)

    if unconsumed:
        described = ", ".join(
            f"{loop.id!r} (kind {loop.kind!r}, count {loop.count})" for loop in unconsumed
        )
        raise UnconsumedLoopError(
            f"{consumer} does not place event loop(s) {described}, and does "
            f"not declare how to fold them. Folding a loop silently mixes "
            f"unrelated frames into one output; place it, or fold it on "
            f"purpose. Loops this consumer places: "
            f"{sorted(kind_to_role)}; folds: {sorted(fold_kinds)}."
        )
    for role in required:
        if by_role.get(role) is None:
            return None
    return LoopSelection(by_role=by_role, folded=tuple(folded))


def recorded_frame_count(layout: AcquisitionLayout) -> int:
    """Number of stored frames the layout describes: its spans, else every event."""
    _raise_for_invalid_coordinates(layout)
    if layout.recorded_event_spans is None:
        return _producer_event_count(layout)
    return sum(span.count * span.repeats for span in layout.recorded_event_spans)


def recorded_frames_per_time_point(layout: AcquisitionLayout) -> int:
    """Stored frames belonging to one time point, exactly, spans included.

    A "stack" to a live reader is what one time point produces. Multiplying
    ``scan_x`` by ``scan_y`` -- the original definition -- silently dropped
    every other loop inside the time loop (conditions, a Z axis, a ``repeat``)
    and ignored a gated detector's spans, so the reader waited for the wrong
    number of frames. Without a ``time`` loop the whole layout is one stack.

    Counting until the time coordinate first changed was the next mistake: it
    assumed the time loop is outermost, and returned half a stack when time
    was nested inside a spatial axis and nothing at all when the traversal ran
    it backwards.

    Counting correctly is still not enough, because a single number only
    describes the stream if each time point's frames are *consecutive* in it.
    They are exactly when the time loop is the outermost one; nested inside a
    spatial axis the acquisition returns to earlier time points again and
    again, so the first N frames span several of them and no stack size exists
    to be returned. That is refused rather than answered with a number a
    reader would slice the wrong frames with.
    """
    _raise_for_invalid_coordinates(layout)
    time_index = next(
        (
            index
            for index, loop in enumerate(layout.event_loops)
            if loop.kind == "time"
        ),
        None,
    )
    if time_index is None:
        return recorded_frame_count(layout)
    time_loop = layout.event_loops[time_index]
    if time_index != 0:
        outer = ", ".join(
            f"{loop.kind}={loop.count}"
            for loop in layout.event_loops[:time_index]
        )
        raise ValueError(
            f"This acquisition repeats its time points inside {outer}, so the "
            f"frames of one time point are not consecutive in the recorded "
            f"stream and no single stack size describes it. Read it through "
            f"its recorded coordinates instead."
        )

    if layout.recorded_event_spans is None:
        # Time outermost and every event kept: the stack is simply what one
        # pass over the inner loops produces. Deriving it arithmetically
        # matters -- walking the coordinates took seconds on a scan of a few
        # million frames, just to state a number the loop counts already give.
        total = 1
        for loop in layout.event_loops:
            total *= loop.count
        return total // time_loop.count

    per_point: dict[int, int] = {}
    for coordinates in iter_recorded_coordinates(layout):
        value = coordinates[time_loop.id]
        per_point[value] = per_point.get(value, 0) + 1
    if not per_point:
        return 0
    counts = set(per_point.values())
    if len(counts) > 1:
        raise ValueError(
            f"This layout's time points do not hold the same number of "
            f"frames ({sorted(counts)}), so there is no single stack size. "
            f"A gated detector selecting different frames per time point has "
            f"to be read through its recorded coordinates."
        )
    return counts.pop()


def unfold_frame_axis(
    array: Any,
    layout: AcquisitionLayout,
    *,
    copy_policy: str = "forbid",
) -> UnfoldedArray:
    """Replace a frame axis with event-loop axes, gathering only when allowed.

    A sparse selection can be unfolded when its recorded coordinates form a
    Cartesian subset of the producer lattice. Irregular masks remain usable
    through :func:`iter_recorded_coordinates`, but cannot be represented by a
    regular ndarray without inventing a missing-value policy.

    The unfolded loop axes are in *logical* order (index 0 first along the
    loop's index axis), not physical orientation: a ``direction == -1`` axis
    comes out mirrored relative to physical position. A caller assembling an
    image reverses those axes per :func:`physical_orientation_flips`.
    """
    if copy_policy not in VALID_COPY_POLICIES:
        raise ValueError(f"copy_policy must be one of {sorted(VALID_COPY_POLICIES)}")
    shape = tuple(getattr(array, "shape", ()))
    errors = tuple(
        issue
        for issue in validate_acquisition_layout(layout, shape=shape)
        if issue.severity == "error"
    )
    if errors:
        raise AcquisitionLayoutError("cannot unfold an invalid acquisition layout", errors)
    if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        issue = LayoutIssue(
            "error",
            "UNFOLD_REQUIRES_FRAME_STREAM",
            "Only detector-frame-stream payloads have a frame axis to unfold",
            "payload_kind",
        )
        raise AcquisitionLayoutError(issue.message, (issue,))

    coordinates = list(iter_recorded_coordinates(layout))
    loop_coordinates = {
        loop.id: tuple(sorted({coordinate[loop.id] for coordinate in coordinates}))
        for loop in layout.event_loops
    }
    expected_count = reduce(mul, (len(values) for values in loop_coordinates.values()), 1)
    coordinate_tuples = [
        tuple(coordinate[loop.id] for loop in layout.event_loops) for coordinate in coordinates
    ]
    if expected_count != len(coordinate_tuples) or len(set(coordinate_tuples)) != len(
        coordinate_tuples
    ):
        issue = LayoutIssue(
            "error",
            "IRREGULAR_EVENT_SELECTION",
            "Recorded events do not form a rectangular subset; use the coordinate iterator or a chunked gather",
            "recorded_event_spans",
        )
        raise AcquisitionLayoutError(issue.message, (issue,))

    logical_order = [
        tuple(values[index] for values, index in zip(loop_coordinates.values(), indices))
        for indices in _cartesian_indices(
            tuple(len(values) for values in loop_coordinates.values())
        )
    ]
    position_by_coordinate = {
        coordinate: index for index, coordinate in enumerate(coordinate_tuples)
    }
    try:
        permutation = [position_by_coordinate[coordinate] for coordinate in logical_order]
    except KeyError as exc:
        issue = LayoutIssue(
            "error",
            "IRREGULAR_EVENT_SELECTION",
            "Recorded events do not contain every coordinate in their rectangular subset",
            "recorded_event_spans",
        )
        raise AcquisitionLayoutError(issue.message, (issue,)) from exc

    identity = permutation == list(range(len(permutation)))
    reordered = array
    if not identity:
        if copy_policy == "forbid":
            issue = LayoutIssue(
                "error",
                "UNFOLD_REQUIRES_COPY",
                "Traversal or event selection requires a gather; pass copy_policy='allow' or use coordinates",
                "traversal",
            )
            raise AcquisitionLayoutError(issue.message, (issue,))
        frame_axis = layout.storage_axes.index("frame")
        indexer: list[Any] = [slice(None)] * len(shape)
        indexer[frame_axis] = permutation
        reordered = array[tuple(indexer)]

    frame_axis = layout.storage_axes.index("frame")
    loop_shape = tuple(len(loop_coordinates[loop.id]) for loop in layout.event_loops)
    unfolded_shape = shape[:frame_axis] + loop_shape + shape[frame_axis + 1 :]
    try:
        unfolded = reordered.reshape(unfolded_shape)
    except (AttributeError, TypeError, ValueError) as exc:
        issue = LayoutIssue(
            "error",
            "UNFOLD_RESHAPE_FAILED",
            "The source backend could not reshape the frame axis without materialization",
            "storage_axes",
        )
        raise AcquisitionLayoutError(issue.message, (issue,)) from exc
    unfolded_axes = (
        layout.storage_axes[:frame_axis]
        + tuple(loop.id for loop in layout.event_loops)
        + layout.storage_axes[frame_axis + 1 :]
    )
    return UnfoldedArray(unfolded, unfolded_axes, loop_coordinates)


def _cartesian_indices(shape: tuple[int, ...]) -> Iterator[tuple[int, ...]]:
    if not shape:
        yield ()
        return
    total = reduce(mul, shape, 1)
    for flat_index in range(total):
        remaining = flat_index
        coordinates = [0] * len(shape)
        for axis in range(len(shape) - 1, -1, -1):
            remaining, coordinates[axis] = divmod(remaining, shape[axis])
        yield tuple(coordinates)
