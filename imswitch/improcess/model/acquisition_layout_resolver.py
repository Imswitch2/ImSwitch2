"""Resolve explicit and legacy acquisition metadata into one semantic layout.

The serialized contract lives in :mod:`imswitch.imcommon.model.acquisition_layout`.
This module owns ImProcess-only precedence, legacy assumptions, OME projection,
and optional fingerprinted user overrides. It never mutates the source file.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from imswitch.imcommon.model.SharedAttributes import JSON_ATTR_PREFIX
from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLayoutError,
    AcquisitionLoop,
    AcquisitionPartition,
    LayoutIssue,
    RecordedEventSpan,
    TraversalRule,
    decode_acquisition_layout,
    encode_acquisition_layout,
    validate_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_metadata import flatten_acquisition_metadata


OVERRIDE_SCHEMA = "imswitch.acquisition-layout-override/1"
CONFIDENCE_LEVELS = ("low", "medium", "high", "certain")
DEFAULT_CONFIDENCE = {
    "recorded": "certain",
    "user-override": "certain",
    "legacy-adapter": "high",
    "ome-ngff": "medium",
    "shape-inference": "medium",
    "generic-fallback": "low",
}


#: Provenance values that mean the layout states what happened, rather than
#: reconstructing it after the fact.
DECLARED_PROVENANCE = frozenset({"recorded", "user-override"})


@dataclass(frozen=True)
class ResolvedAcquisitionLayout:
    """A layout together with its interpretation source and diagnostics."""

    layout: AcquisitionLayout
    source: str
    confidence: str
    issues: tuple[LayoutIssue, ...] = ()
    replaced_layout: AcquisitionLayout | None = None

    @property
    def is_usable(self) -> bool:
        """True when geometry may be taken from this layout.

        A legacy adapter's axes are inference, but they are the *same*
        metadata every plugin used to parse for itself, read once and read
        consistently. Preferring them over a plugin's private re-parse is the
        whole point of the resolver.
        """
        return self.confidence != "low"

    @property
    def is_authoritative(self) -> bool:
        """True when this layout may refuse a reconstruction.

        Only a producer-authored or user-declared layout states fact. An
        inference -- however well reported -- must not veto a reconstruction
        that used to work: a plugin that cannot proceed on an inferred layout
        falls back to its own older path instead of failing the user's data.

        This is deliberately stricter than :attr:`is_usable`. Conflating the
        two is what let one reconstructor honour a legacy layout while another
        ignored it, and let MoNaLISA disagree with itself between its standard
        and fast-Gauss paths.
        """
        return self.layout.provenance in DECLARED_PROVENANCE


class AcquisitionLayoutResolutionError(AcquisitionLayoutError):
    """Resolution must stop rather than silently choosing a fallback."""


def _issue(
    severity: str,
    code: str,
    message: str,
    field: str | None = None,
    loop_id: str | None = None,
) -> LayoutIssue:
    return LayoutIssue(severity, code, message, field, loop_id)


def _native(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except (UnicodeDecodeError, AttributeError):
            return value
    if isinstance(value, str):
        # Structured attributes -- a per-detector line-step mask, a per-device
        # timing map -- are stored as JSON behind a sentinel, because HDF5
        # attributes hold scalars and flat arrays. Only SharedAttributes
        # decoded it, so every reader that goes to the file directly saw the
        # raw string: the legacy adapter's per-detector gating retry could
        # never find a mask, and an old line-step recording whose detector was
        # gated onto part of the scan failed to resolve at all.
        if value.startswith(JSON_ATTR_PREFIX):
            try:
                return json.loads(value[len(JSON_ATTR_PREFIX):])
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
        return value
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except (TypeError, ValueError):
            pass
    return value


def _normalized_attrs(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(attrs, Mapping):
        return {}

    def flatten(value: Mapping[str, Any]) -> dict[str, Any]:
        flat: dict[str, Any] = {}
        nested: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            # Already-flat acquisition attributes may intentionally contain a
            # mapping value (notably ScanTTL line-step masks). Preserve it.
            if ":" in key or not isinstance(raw_value, Mapping):
                flat[key] = _native(raw_value)
            else:
                nested[key] = raw_value
        if nested:
            flat.update(flatten_acquisition_metadata(nested))
        return flat

    normalized: dict[str, Any] = {}
    nested = attrs.get("ImswitchData")
    if isinstance(nested, Mapping):
        normalized.update(flatten(nested))
    normalized.update(flatten({key: value for key, value in attrs.items() if key != "ImswitchData"}))
    return {key: _native(value) for key, value in normalized.items()}


def _text(value: Any) -> str | None:
    value = _native(value)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    text = str(value).strip()
    return text or None


def _positive_int(value: Any) -> int | None:
    value = _native(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _boolean(value: Any) -> bool | None:
    value = _native(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _number(value: Any) -> float | None:
    value = _native(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sequence(value: Any) -> tuple[Any, ...]:
    value = _native(value)
    if isinstance(value, (list, tuple)):
        return tuple(_native(item) for item in value)
    return ()


def _shape_tuple(shape: Sequence[int] | None) -> tuple[int, ...] | None:
    if shape is None:
        return None
    try:
        result = tuple(int(value) for value in shape)
    except (TypeError, ValueError):
        return None
    return result if all(value >= 0 for value in result) else None


def _default_storage_axes(shape: tuple[int, ...]) -> tuple[str, ...]:
    if len(shape) == 0:
        return ()
    if len(shape) == 1:
        return ("frame",)
    if len(shape) == 2:
        return ("detector_y", "detector_x")
    middle = tuple(f"dim_{index}" for index in range(1, len(shape) - 2))
    return ("frame", *middle, "detector_y", "detector_x")


def _errors(issues: Sequence[LayoutIssue]) -> tuple[LayoutIssue, ...]:
    return tuple(issue for issue in issues if issue.severity == "error")


def _confidence(provenance: str, requested: str | None = None) -> str:
    default = DEFAULT_CONFIDENCE[provenance]
    if requested is None:
        return default
    if requested not in CONFIDENCE_LEVELS:
        raise ValueError(f"Unknown confidence {requested!r}")
    return CONFIDENCE_LEVELS[
        min(CONFIDENCE_LEVELS.index(default), CONFIDENCE_LEVELS.index(requested))
    ]


def _validated_result(
    layout: AcquisitionLayout,
    *,
    source: str,
    shape: tuple[int, ...] | None,
    issues: Sequence[LayoutIssue] = (),
    confidence: str | None = None,
    replaced_layout: AcquisitionLayout | None = None,
    allow_frame_mismatch: bool = False,
) -> ResolvedAcquisitionLayout:
    validation = validate_acquisition_layout(layout, shape=shape)
    if allow_frame_mismatch:
        validation = tuple(
            replace(issue, severity="warning")
            if issue.code in {"FRAME_COUNT_MISMATCH", "LOOP_AXIS_COUNT_MISMATCH"}
            else issue
            for issue in validation
        )
    all_issues = tuple(dict.fromkeys((*issues, *validation)))
    errors = _errors(all_issues)
    if errors:
        raise AcquisitionLayoutResolutionError(
            f"Acquisition layout from {source} is invalid",
            errors,
        )
    return ResolvedAcquisitionLayout(
        layout=layout,
        source=source,
        confidence=_confidence(layout.provenance, confidence),
        issues=all_issues,
        replaced_layout=replaced_layout,
    )


def _decode_explicit(
    attrs: Mapping[str, Any],
    shape: tuple[int, ...] | None,
    *,
    allow_frame_mismatch: bool,
) -> tuple[AcquisitionLayout | None, tuple[LayoutIssue, ...]]:
    encoded = attrs.get("AcquisitionLayout:json")
    declared = _text(attrs.get("AcquisitionLayout:schema"))
    if encoded is None and declared is None:
        return None, ()
    if encoded is None:
        return None, (
            _issue(
                "error",
                "MISSING_EXPLICIT_LAYOUT_JSON",
                "AcquisitionLayout:schema is present without AcquisitionLayout:json",
                "AcquisitionLayout:json",
            ),
        )
    decode_issues: list[LayoutIssue] = []
    try:
        layout = decode_acquisition_layout(encoded, decode_issues)
    except Exception as exc:
        nested_issues = tuple(getattr(exc, "issues", ()))
        if any(issue.code == "UNSUPPORTED_SCHEMA_VERSION" for issue in nested_issues):
            # The file is not broken, this reader just cannot interpret it.
            # Failing here would make a recording from a newer ImSwitch
            # impossible even to look at, when only its semantics are unknown
            # and its pixels are perfectly readable. Downgrade to a warning so
            # resolution falls through to the unknown-semantics fallback.
            return None, tuple(
                replace(issue, severity="warning")
                if issue.code == "UNSUPPORTED_SCHEMA_VERSION"
                else issue
                for issue in nested_issues
            )
        return None, nested_issues or (
            _issue(
                "error",
                "INVALID_EXPLICIT_LAYOUT",
                str(exc),
                "AcquisitionLayout:json",
            ),
        )
    # Anything the decoder had to skip is part of this file's story.
    issues: list[LayoutIssue] = list(decode_issues)
    if declared is not None and declared != layout.schema:
        issues.append(
            _issue(
                "error",
                "EXPLICIT_SCHEMA_MISMATCH",
                "AcquisitionLayout:schema disagrees with the encoded layout",
                "AcquisitionLayout:schema",
            )
        )
    issues.extend(validate_acquisition_layout(layout, shape=shape))
    if allow_frame_mismatch:
        issues = [
            replace(issue, severity="warning")
            if issue.code in {"FRAME_COUNT_MISMATCH", "LOOP_AXIS_COUNT_MISMATCH"}
            else issue
            for issue in issues
        ]
    return layout, tuple(issues)


def _projection_issues(
    layout: AcquisitionLayout,
    axis_labels: Sequence[str] | None,
) -> tuple[LayoutIssue, ...]:
    if axis_labels is None or len(axis_labels) != len(layout.storage_axes):
        return ()
    loop_kinds = {loop.kind for loop in layout.event_loops}
    issues = []
    for index, raw in enumerate(axis_labels):
        label = str(raw).strip().lower()
        canonical = layout.storage_axes[index]
        if label in {"t", "time"} and "time" not in loop_kinds:
            issues.append(
                _issue(
                    "warning",
                    "LOSSY_OME_TIME_PROJECTION",
                    f"Container axis {raw!r} is only an interoperability projection; "
                    f"the acquisition layout records storage role {canonical!r}",
                    "axis_labels",
                )
            )
    return tuple(issues)


def source_fingerprint(path: str | Path) -> str:
    """Return a cheap identity fingerprint without reading image payload bytes."""
    source = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    digest.update(str(source).encode("utf-8"))
    if not source.exists():
        raise FileNotFoundError(source)

    def add_stat(item: Path, relative: str) -> None:
        stat = item.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))

    if source.is_file():
        add_stat(source, source.name)
    else:
        add_stat(source, ".")
        metadata_names = {"zarr.json", ".zattrs", ".zgroup", "tiles.json"}
        for item in sorted(
            (candidate for candidate in source.rglob("*") if candidate.name in metadata_names),
            key=lambda candidate: candidate.as_posix(),
        ):
            if item.is_file():
                add_stat(item, item.relative_to(source).as_posix())
    return digest.hexdigest()


def layout_override_sidecar_path(source_path: str | Path) -> Path:
    return Path(f"{Path(source_path)}.imswitch-layout.json")


def _canonical_dataset_path(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().strip("/")
    return normalized or None


def persist_layout_override(
    source_path: str | Path,
    layout: AcquisitionLayout,
    *,
    detector: str,
    dataset_path: str | None,
    fingerprint: str | None = None,
) -> Path:
    """Persist an explicit override beside a source, never inside the source."""
    override = replace(layout, provenance="user-override")
    encoded = encode_acquisition_layout(override)
    sidecar = layout_override_sidecar_path(source_path)
    payload = {
        "schema": OVERRIDE_SCHEMA,
        "source_fingerprint": fingerprint or source_fingerprint(source_path),
        "detector": str(detector),
        "dataset_path": _canonical_dataset_path(dataset_path),
        "layout_json": encoded,
    }
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sidecar


def _load_sidecar_override(
    source_path: str | Path | None,
    *,
    detector: str,
    dataset_path: str | None,
    fingerprint: str | None,
) -> tuple[AcquisitionLayout | None, tuple[LayoutIssue, ...]]:
    if source_path is None:
        return None, ()
    sidecar = layout_override_sidecar_path(source_path)
    if not sidecar.is_file():
        return None, ()
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, (
            _issue("warning", "INVALID_LAYOUT_OVERRIDE", str(exc), str(sidecar)),
        )
    if payload.get("schema") != OVERRIDE_SCHEMA:
        return None, (
            _issue(
                "warning",
                "UNSUPPORTED_LAYOUT_OVERRIDE",
                "Layout override sidecar has an unsupported schema",
                str(sidecar),
            ),
        )
    current = fingerprint
    if current is None:
        try:
            current = source_fingerprint(source_path)
        except OSError as exc:
            return None, (
                _issue("warning", "OVERRIDE_FINGERPRINT_UNAVAILABLE", str(exc), str(sidecar)),
            )
    if payload.get("source_fingerprint") != current:
        return None, (
            _issue(
                "warning",
                "OVERRIDE_FINGERPRINT_MISMATCH",
                "The layout override does not match the current source and was ignored",
                str(sidecar),
            ),
        )
    if (
        str(payload.get("detector")) != str(detector)
        or _canonical_dataset_path(payload.get("dataset_path"))
        != _canonical_dataset_path(dataset_path)
    ):
        return None, (
            _issue(
                "warning",
                "OVERRIDE_DATASET_MISMATCH",
                "The layout override targets a different detector or dataset and was ignored",
                str(sidecar),
            ),
        )
    try:
        layout = decode_acquisition_layout(payload["layout_json"])
    except Exception as exc:
        return None, (
            _issue("warning", "INVALID_LAYOUT_OVERRIDE", str(exc), str(sidecar)),
        )
    return replace(layout, provenance="user-override"), ()


def _axis_kind(device: Any, index: int) -> str:
    name = str(device or "").lower()
    if "x" in name:
        return "scan_x"
    if "y" in name:
        return "scan_y"
    if "z" in name:
        return "scan_z"
    return ("scan_x", "scan_y", "scan_z")[min(index, 2)]


def _scan_geometry_candidates(
    attrs: Mapping[str, Any],
    *,
    observed_events: int | None,
    multiplier: int,
) -> tuple[tuple[AcquisitionLoop, ...], tuple[LayoutIssue, ...]]:
    devices = _sequence(attrs.get("ScanStage:target_device"))
    lengths = _sequence(attrs.get("ScanStage:axis_length"))
    steps = _sequence(attrs.get("ScanStage:axis_step_size"))
    starts = _sequence(attrs.get("ScanStage:axis_startpos"))
    directions = _sequence(attrs.get("ScanStage:positive_direction"))
    units = _sequence(attrs.get("ScanStage:axis_step_size_unit"))
    scalar_unit = _text(attrs.get("ScanStage:axis_step_size_unit")) if not units else None
    axis_count = min(len(lengths), len(steps))
    if axis_count == 0:
        raise AcquisitionLayoutResolutionError(
            "Legacy scan metadata lacks axis lengths or steps",
            (
                _issue(
                    "error",
                    "MISSING_LEGACY_SCAN_GEOMETRY",
                    "ScanStage axis_length and axis_step_size are required",
                    "ScanStage",
                ),
            ),
        )

    # The single-axis scan fix records each scanned dim's physical stage axis
    # (write-only provenance). When present it is a statement, not a guess, so
    # it outranks inferring the axis from a substring of the device's name --
    # which calls a 'GalvoX'-named Z-drive scan_x and 'NDMover1' whatever its
    # position happens to be.
    prov_devices = _sequence(attrs.get("ScanStage:scan_axis_devices"))
    prov_physical = _sequence(attrs.get("ScanStage:scan_axis_physical"))

    def _kind_for(index: int) -> str:
        device = devices[index] if index < len(devices) else None
        name = _text(device)
        if name and prov_devices and prov_physical:
            for prov_device, physical in zip(prov_devices, prov_physical):
                if _text(prov_device) == name:
                    mapped = {"x": "scan_x", "y": "scan_y", "z": "scan_z"}.get(
                        (_text(physical) or "").lower()
                    )
                    if mapped:
                        return mapped
                    break
        return _axis_kind(device, index)

    nx = _positive_int(attrs.get("ScanTTL:Nx"))
    ny = _positive_int(attrs.get("ScanTTL:Ny"))
    candidate_counts: list[tuple[int, ...]] = []
    size_counts = []
    endpoint_counts = []
    for index in range(axis_count):
        length = _number(lengths[index])
        step = _number(steps[index])
        if length is None or step in (None, 0):
            continue
        size_count = max(1, int(round(abs(length / step))))
        if index == 0 and nx is not None:
            size_count = nx
        elif index == 1 and ny is not None:
            size_count = ny
        size_counts.append(size_count)
        endpoint_count = size_count
        if index < len(starts):
            start = _number(starts[index])
            if start is not None:
                endpoint_count = max(1, int(round(abs((length - start) / step))) + 1)
                if index == 0 and nx is not None:
                    endpoint_count = nx
                elif index == 1 and ny is not None:
                    endpoint_count = ny
        endpoint_counts.append(endpoint_count)
    if not size_counts:
        raise AcquisitionLayoutResolutionError(
            "Legacy scan metadata contains no active axes",
            (_issue("error", "INVALID_LEGACY_SCAN_GEOMETRY", "No active scan axes were found"),),
        )
    candidate_counts.append(tuple(size_counts))
    if tuple(endpoint_counts) != tuple(size_counts):
        candidate_counts.append(tuple(endpoint_counts))

    matching = []
    for counts in candidate_counts:
        event_count = math.prod(counts) * multiplier
        if observed_events is None or event_count == observed_events:
            matching.append(counts)
    matching = list(dict.fromkeys(matching))
    convention_issues: list[LayoutIssue] = []
    if observed_events is None and len(matching) > 1:
        # Without a frame count to arbitrate, the size and endpoint
        # conventions cannot be told apart -- and every scan controller writes
        # ``axis_startpos``, which is what makes them differ, so this mode
        # raised on every real file. That made its only caller unreachable:
        # the detector-gating retry inside the Advanced adapter, which exists
        # precisely to explain a line-step file whose frame count the plain
        # geometry cannot. Take the size convention, which is what the
        # designers compute from, and say that it was a choice.
        matching = matching[:1]
        convention_issues.append(
            _issue(
                "warning",
                "LEGACY_SCAN_CONVENTION_ASSUMED",
                "Legacy size and endpoint conventions disagree and no frame "
                "count was available to choose between them; the size "
                "convention was assumed",
                "ScanStage:axis_startpos",
            )
        )
    if len(matching) != 1:
        raise AcquisitionLayoutResolutionError(
            "Legacy scan geometry is ambiguous",
            (
                _issue(
                    "error",
                    "AMBIGUOUS_LEGACY_SCAN_GEOMETRY",
                    "Legacy size/endpoint conventions do not yield one frame-count match",
                    "ScanStage",
                ),
            ),
        )

    counts = matching[0]
    assumptions = [*convention_issues, 
        _issue(
            "warning",
            "LEGACY_SCAN_GEOMETRY_ASSUMPTION",
            "Scan dimensions were adapted from legacy ScanStage/ScanTTL metadata",
            "ScanStage",
        )
    ]
    # The size and endpoint conventions can disagree about whether a trailing
    # axis moved at all, and picking whichever matches the frame count keeps
    # the file readable. But an axis the size convention calls inactive is not
    # evidence of a scan axis: the same extra frames could equally be
    # timepoints or repeats, and Z and time are not interchangeable. Report the
    # choice instead of presenting it as recorded geometry.
    for index, count in enumerate(counts):
        if count <= 1 or index >= len(size_counts) or size_counts[index] != 1:
            continue
        kind = _kind_for(index)
        assumptions.append(
            _issue(
                "warning",
                "AMBIGUOUS_LEGACY_TRAILING_AXIS",
                f"Legacy metadata gives axis {index} a single position, but "
                f"{count} were needed to explain the frame count. It was "
                f"adapted as {kind!r}; the same frames could be timepoints or "
                f"repeats.",
                "ScanStage:axis_length",
                kind,
            )
        )

    loops = []
    for index, count in enumerate(counts):
        kind = _kind_for(index)
        direction = None
        if index < len(directions):
            direction = 1 if bool(directions[index]) else -1
        step = abs(float(steps[index])) if _number(steps[index]) is not None else None
        unit = _text(units[index]) if index < len(units) else scalar_unit
        device = _text(devices[index]) if index < len(devices) else None
        loops.append(
            AcquisitionLoop(
                id=kind,
                kind=kind,
                count=count,
                step=step,
                unit=unit,
                direction=direction,
                device=device,
            )
        )

    loops, collision_issues = _resolve_axis_kind_collisions(loops)
    return tuple(loops), (*assumptions, *collision_issues)


def _resolve_axis_kind_collisions(
    loops: Sequence[AcquisitionLoop],
) -> tuple[list[AcquisitionLoop], tuple[LayoutIssue, ...]]:
    """Settle two legacy axes whose device names guess the same physical axis.

    ``ScanStage:target_device`` lists every positioner on the rig, not only the
    ones a scan moved, and the kind is guessed from a substring of the device
    name. Two stages therefore collide on real, shipped configurations --
    ``ND-PiezoZ`` beside ``PiezoZ``, or ``Mock Kinesis XY`` beside ``Mock X``.
    Both loops were then given the same id and the whole layout was rejected as
    invalid, which took the file's *pixels* down with it: every caller of
    ``acquisition_layout`` saw an exception rather than a decline.

    An axis at a single position did not move, so it is not part of the
    geometry and is dropped -- that is the collision as it actually occurs,
    since the spare stage is the one standing still. A collision between axes
    that both moved is a guess this adapter cannot make, and it says so rather
    than picking one.
    """
    by_kind: dict[str, list[AcquisitionLoop]] = {}
    for loop in loops:
        by_kind.setdefault(loop.kind, []).append(loop)
    if all(len(group) == 1 for group in by_kind.values()):
        return list(loops), ()

    dropped: list[AcquisitionLoop] = []
    for kind, group in by_kind.items():
        if len(group) == 1:
            continue
        moved = [loop for loop in group if loop.count > 1]
        if len(moved) > 1:
            names = ", ".join(
                repr(loop.device or loop.kind) for loop in moved
            )
            raise AcquisitionLayoutResolutionError(
                "Legacy scan axes cannot be told apart",
                (
                    _issue(
                        "error",
                        "AMBIGUOUS_LEGACY_AXIS_DEVICES",
                        f"Devices {names} both scan {kind!r} by name, and both "
                        f"moved, so which physical axis each drove cannot be "
                        f"recovered from this file",
                        "ScanStage:target_device",
                        kind,
                    ),
                ),
            )
        keep = moved[0] if moved else group[0]
        dropped.extend(loop for loop in group if loop is not keep)

    if not dropped:
        return list(loops), ()
    issues = tuple(
        _issue(
            "warning",
            "LEGACY_INACTIVE_AXIS_DROPPED",
            f"Device {loop.device or loop.kind!r} was listed as a scan axis at "
            f"a single position and shares its guessed axis {loop.kind!r} with "
            f"another device; it did not move, so it was left out of the "
            f"geometry",
            "ScanStage:target_device",
            loop.kind,
        )
        for loop in dropped
    )
    return [loop for loop in loops if loop not in dropped], issues


def _legacy_confidence(issues: Sequence[LayoutIssue]) -> str | None:
    """Lower the adapter's confidence when an axis was a coin flip."""
    if any(issue.code == "AMBIGUOUS_LEGACY_TRAILING_AXIS" for issue in issues):
        return "medium"
    return None


def _scan_traversal(loops: Sequence[AcquisitionLoop]) -> tuple[TraversalRule, ...]:
    """Chronology only. Legacy scans stepped every axis monotonically.

    ``ScanStage:positive_direction`` lands on the loop's ``direction``; it is a
    physical sign, applied once by ``physical_orientation_flips`` in imcommon,
    never a ``reverse`` traversal (see the producer's ``_traversal``).
    """
    return tuple(TraversalRule(loop.id, "forward") for loop in loops)


def _detector_linestep_mask(
    attrs: Mapping[str, Any], detector: str, condition_count: int, row_count: int
) -> tuple[bool, ...] | None:
    masks = attrs.get("ScanTTL:linestep_enable")
    if not isinstance(masks, Mapping):
        return None
    detector_lower = detector.lower()
    for key, value in masks.items():
        key_lower = str(key).lower()
        if detector_lower != key_lower and detector_lower not in key_lower:
            continue
        mask = tuple(bool(item) for item in _sequence(value))
        if len(mask) in {condition_count, condition_count * row_count}:
            return mask
        raise AcquisitionLayoutResolutionError(
            "Legacy detector line-step mask has an unsupported length",
            (
                _issue(
                    "error",
                    "AMBIGUOUS_LEGACY_DETECTOR_GATING",
                    "Detector line-step mask must be per-condition or per-expanded-line",
                    "ScanTTL:linestep_enable",
                ),
            ),
        )
    return None


def _linestep_spans(
    *,
    mask: tuple[bool, ...],
    condition_count: int,
    x_count: int,
    row_count: int,
) -> tuple[RecordedEventSpan, ...]:
    spans: list[RecordedEventSpan] = []
    for row in range(row_count):
        row_mask = mask if len(mask) == condition_count else mask[
            row * condition_count : (row + 1) * condition_count
        ]
        condition = 0
        while condition < condition_count:
            if not row_mask[condition]:
                condition += 1
                continue
            start_condition = condition
            while condition < condition_count and row_mask[condition]:
                condition += 1
            spans.append(
                RecordedEventSpan(
                    start=(row * condition_count + start_condition) * x_count,
                    count=(condition - start_condition) * x_count,
                )
            )
    return tuple(spans)


def adapt_advanced_scan_metadata(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    """Adapt legacy Advanced Scan metadata, including line-step chronology."""
    normalized = _normalized_attrs(attrs)
    condition_count = _positive_int(normalized.get("ScanTTL:n_linesteps"))
    if condition_count is None:
        return None
    source_shape = _shape_tuple(shape)
    if source_shape is None or len(source_shape) < 3:
        return None
    # The writer stores a scan-driven detector's assembled image as
    # ``(T, C, Y, X)`` with a singleton leading frame, while a reader handed
    # only ``(C, Y, X)`` describes the same acquisition. Recognising just the
    # second shape is why the two-line-step APD recordings this very adapter
    # exists for fell through to the frame-stream path and were then reported
    # as ambiguous geometry.
    assembled_shape = source_shape
    frame_wrapper = ()
    if (
        len(source_shape) >= 4
        and source_shape[0] == 1
        and source_shape[1] == condition_count
    ):
        assembled_shape = source_shape[1:]
        frame_wrapper = ("frame",)
    if assembled_shape[0] == condition_count:
        source_shape = assembled_shape
        physical, assumptions = _scan_geometry_candidates(
            normalized,
            observed_events=math.prod(source_shape),
            multiplier=condition_count,
        )
        # The stored axes after the condition are the scan's own, slowest
        # first. Naming only Y and X described a 3D scan with one axis too
        # few, and an explicit layout that disagrees with its container is
        # refused outright -- so a perfectly good (condition, Z, Y, X)
        # point-detector recording could not be resolved at all.
        spatial = _assembled_spatial_loops(physical, assembled_shape[1:])
        if spatial is not None:
            condition = AcquisitionLoop(
                "condition",
                "condition",
                condition_count,
                labels=tuple(f"condition_{index}" for index in range(condition_count)),
                storage_axis="condition",
            )
            assembled_loops = (
                condition,
                *(replace(loop, storage_axis=loop.kind) for loop in spatial),
            )
            layout = AcquisitionLayout(
                schema=ACQUISITION_LAYOUT_SCHEMA,
                payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
                detector=detector,
                storage_axes=(
                    *frame_wrapper,
                    "condition",
                    *(loop.kind for loop in spatial),
                ),
                event_loops=assembled_loops,
                traversal=_scan_traversal(assembled_loops),
                modality=_text(
                    normalized.get("modality")
                    or normalized.get("ReconstructionModality")
                ),
                scan_source="advanced-scan-legacy",
                provenance="legacy-adapter",
            )
            return _validated_result(
                layout,
                source="advanced-scan-legacy-assembled",
                shape=(*(1,) * len(frame_wrapper), *source_shape),
                issues=assumptions,
                confidence=_legacy_confidence(assumptions),
            )
    observed = source_shape[0]

    # Geometry is first resolved against all producer events. If a detector
    # mask is present, retry with its selected fraction below.
    try:
        physical, assumptions = _scan_geometry_candidates(
            normalized,
            observed_events=observed,
            multiplier=condition_count,
        )
        mask = None
    except AcquisitionLayoutResolutionError:
        physical, assumptions = _scan_geometry_candidates(
            normalized,
            observed_events=None,
            multiplier=condition_count,
        )
        by_kind = {loop.kind: loop for loop in physical}
        x_count = by_kind.get("scan_x", physical[0]).count
        row_count = math.prod(loop.count for loop in physical if loop.kind != "scan_x")
        mask = _detector_linestep_mask(normalized, detector, condition_count, row_count)
        if mask is None:
            raise
        selected_per_pattern = sum(mask)
        if len(mask) == condition_count:
            selected = selected_per_pattern * row_count * x_count
        else:
            selected = selected_per_pattern * x_count
        if selected != observed:
            raise AcquisitionLayoutResolutionError(
                "Legacy detector gating does not match the stored frame count",
                (
                    _issue(
                        "error",
                        "LEGACY_DETECTOR_GATING_COUNT_MISMATCH",
                        f"Detector mask selects {selected} frames but the source contains {observed}",
                        "ScanTTL:linestep_enable",
                    ),
                ),
            )

    by_kind = {loop.kind: loop for loop in physical}
    x_loop = by_kind.get("scan_x", physical[0])
    outer = [loop for loop in physical if loop.id != x_loop.id]
    condition = AcquisitionLoop(
        "condition",
        "condition",
        condition_count,
        labels=tuple(f"condition_{index}" for index in range(condition_count)),
    )
    loops = (*reversed(outer), condition, x_loop)
    # The physical adapter above follows controller order X/Y/Z; chronology is
    # Z/Y/condition/X (outermost to innermost).
    outer_row_count = math.prod(loop.count for loop in loops if loop.id not in {"condition", x_loop.id})
    mask = mask or _detector_linestep_mask(
        normalized, detector, condition_count, outer_row_count
    )
    spans = None
    if mask is not None and not all(mask):
        spans = _linestep_spans(
            mask=mask,
            condition_count=condition_count,
            x_count=x_loop.count,
            row_count=outer_row_count,
        )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector=detector,
        storage_axes=_default_storage_axes(source_shape),
        event_loops=tuple(loops),
        traversal=_scan_traversal(loops),
        recorded_event_spans=spans,
        modality=_text(normalized.get("modality") or normalized.get("ReconstructionModality")),
        scan_source="advanced-scan-legacy",
        provenance="legacy-adapter",
    )
    extra_issues = list(assumptions)
    masks = normalized.get("ScanTTL:linestep_enable")
    if isinstance(masks, Mapping) and mask is None:
        extra_issues.append(
            _issue(
                "warning",
                "LEGACY_LINESTEP_ROLE_ASSUMED",
                "Line-step illumination masks were present, but no detector-specific gate was identified",
                "ScanTTL:linestep_enable",
            )
        )
    return _validated_result(
        layout,
        source="advanced-scan-legacy",
        shape=source_shape,
        issues=extra_issues,
        confidence="medium" if extra_issues else "high",
    )


def _adapt_frame_scan(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
    source: str,
    confidence: str | None = None,
) -> ResolvedAcquisitionLayout:
    normalized = _normalized_attrs(attrs)
    source_shape = _shape_tuple(shape)
    if source_shape is None or len(source_shape) < 3:
        raise AcquisitionLayoutResolutionError(
            "Legacy scan frame stream has no frame axis",
            (_issue("error", "LEGACY_SCAN_RANK_MISMATCH", "Expected a frame-stacked source"),),
        )
    physical, assumptions = _scan_geometry_candidates(
        normalized,
        observed_events=source_shape[0],
        multiplier=1,
    )
    loops = tuple(reversed(physical))
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector=detector,
        storage_axes=_default_storage_axes(source_shape),
        event_loops=loops,
        traversal=_scan_traversal(loops),
        modality=_text(normalized.get("modality") or normalized.get("ReconstructionModality")),
        scan_source=source,
        provenance="legacy-adapter",
    )
    requested = _legacy_confidence(assumptions)
    if confidence is not None:
        # _confidence() only ever lowers, so the stricter of the two wins.
        requested = confidence if requested is None else min(
            (requested, confidence), key=CONFIDENCE_LEVELS.index
        )
    return _validated_result(
        layout,
        source=source,
        shape=source_shape,
        issues=assumptions,
        confidence=requested,
    )


def adapt_monalisa_scan_metadata(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    normalized = _normalized_attrs(attrs)
    if "ScanTTL:Nx" not in normalized or "ScanTTL:Ny" not in normalized:
        return None
    if "ScanStage:axis_length" not in normalized:
        return None
    return _adapt_frame_scan(
        normalized,
        shape=shape,
        detector=detector,
        source="monalisa-scan-legacy",
    )


def adapt_scan_stage_metadata(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    """Adapt a recording carrying stage geometry and nothing more specific.

    ImProcess used to interpret these files in the live sources and in
    MoNaLISA's own geometry helpers, each with its own arithmetic and its own
    rounding. Interpreting them here instead is what lets those be deleted.

    Unlike the modality adapters this one declines rather than raising when the
    geometry cannot explain the frame count. Nothing in the file claims to be a
    scan, so an unexplained count means this is not one -- not that the file is
    broken -- and the resolution falls through to the generic fallback exactly
    as it did before this adapter existed.
    """
    normalized = _normalized_attrs(attrs)
    if "ScanStage:axis_length" not in normalized:
        return None
    if "ScanStage:axis_step_size" not in normalized:
        return None

    # Stage extents describe what was *configured*, not necessarily what ran.
    # A timelapse that never moved the stage still carries them, and its frame
    # count can coincide with the configured position product -- which would
    # read a hundred timepoints as a 10x10 raster. When the file says it holds
    # several timepoints, a positional reading that leaves no room for them is
    # a coincidence, not a description.
    timepoints = _positive_int(
        normalized.get("recording:num_timepoints")
    ) or _positive_int(normalized.get("Rec:LapseTime"))
    if timepoints is not None and timepoints > 1:
        return None

    source_shape = _shape_tuple(shape)
    try:
        if source_shape is not None and len(source_shape) == 2:
            # A point detector's assembled image, from a rig that named no
            # modality. Reachable here rather than only from the TriggerScope
            # adapter, which now requires a file that actually says
            # TriggerScope: an assembled raster is a property of the payload,
            # not of one firmware.
            return _adapt_assembled_raster(
                normalized,
                shape=source_shape,
                detector=detector,
                scan_source="scan-stage-legacy",
                source="scan-stage-legacy-assembled",
                confidence="medium",
            )
        return _adapt_frame_scan(
            normalized,
            shape=shape,
            detector=detector,
            source="scan-stage-legacy",
            # The least specific adapter: it matches on stage extents alone,
            # with no statement anywhere that this recording was a scan.
            confidence="medium",
        )
    except AcquisitionLayoutResolutionError:
        return None


def _assembled_spatial_loops(
    physical: Sequence[AcquisitionLoop], dims: Sequence[int]
) -> tuple[AcquisitionLoop, ...] | None:
    """Order the scan's loops onto the stored spatial axes, or decline.

    ``_scan_geometry_candidates`` returns loops fastest first, as the ScanInfo
    dimensions are indexed; an assembled image stores them the other way
    round. A legacy file also lists every configured axis, so a stage that
    stood still appears as a count-1 loop with no stored axis of its own --
    those are dropped from the outside in until what remains matches the
    container.
    """
    ordered = list(reversed(list(physical)))
    dims = [int(size) for size in dims]
    while len(ordered) > len(dims) and ordered and ordered[0].count == 1:
        ordered.pop(0)
    if len(ordered) != len(dims):
        return None
    if [loop.count for loop in ordered] != dims:
        return None
    return tuple(ordered)


def _adapt_assembled_raster(
    normalized: Mapping[str, Any],
    *,
    shape: tuple[int, ...],
    detector: str,
    scan_source: str,
    source: str,
    confidence: str | None = None,
) -> ResolvedAcquisitionLayout:
    """Describe a 2D image a point detector assembled over a stage raster.

    A scan-driven detector stores one image whose axes *are* the scan, so the
    layout is assembled-image with the Y/X loops mapped straight onto the
    array. Nothing about that is specific to one firmware -- it follows from
    the payload being an assembled image over stage geometry -- so both the
    TriggerScope adapter and the plain stage adapter reach it, and each names
    itself.
    """
    physical, assumptions = _scan_geometry_candidates(
        normalized,
        observed_events=math.prod(shape),
        multiplier=1,
    )
    by_kind = {loop.kind: loop for loop in physical}
    x_loop = by_kind.get("scan_x")
    y_loop = by_kind.get("scan_y")
    if x_loop is None or y_loop is None or shape != (y_loop.count, x_loop.count):
        raise AcquisitionLayoutResolutionError(
            "Legacy raster geometry does not match the assembled image",
            (
                _issue(
                    "error",
                    "LEGACY_RASTER_SHAPE_MISMATCH",
                    "Raster Y/X dimensions do not match the stored image",
                    "shape",
                ),
            ),
        )
    loops = (
        replace(y_loop, storage_axis="scan_y"),
        replace(x_loop, storage_axis="scan_x"),
    )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
        detector=detector,
        storage_axes=("scan_y", "scan_x"),
        event_loops=loops,
        traversal=_scan_traversal(loops),
        scan_source=scan_source,
        provenance="legacy-adapter",
    )
    return _validated_result(
        layout,
        source=source,
        shape=shape,
        issues=assumptions,
        confidence=confidence or _legacy_confidence(assumptions),
    )


def adapt_triggerscope_raster_metadata(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    normalized = _normalized_attrs(attrs)
    source_hint = " ".join(
        filter(
            None,
            (
                _text(normalized.get("recording:scan_source")),
                _text(normalized.get("scan_source")),
                _text(normalized.get("controller")),
            ),
        )
    ).lower()
    # Only a file that names TriggerScope is read as TriggerScope. The wider
    # signature this used to accept -- a sequence time, no line steps, no Nx --
    # is what every point-scan and MoNaLISA controller publishes for every
    # recording on the rig, scan or not, so this adapter claimed files from
    # other modalities, named TriggerScope as their scan source, and (running
    # before the honest stage adapter) reported "high" confidence for a guess.
    trigger_signature = "triggerscope" in source_hint
    if not trigger_signature or "ScanStage:axis_length" not in normalized:
        return None

    # Configured stage extents describe what the rig could scan, not what this
    # recording did. A timelapse on a scan rig carries them, and its frame
    # count can coincide with the configured position product; reading that as
    # a raster invents geometry. The same guard the stage adapter already has.
    timepoints = _positive_int(
        normalized.get("recording:num_timepoints")
    ) or _positive_int(normalized.get("Rec:LapseTime"))
    if timepoints is not None and timepoints > 1:
        return None
    source_shape = _shape_tuple(shape)
    if source_shape is not None and len(source_shape) == 2:
        return _adapt_assembled_raster(
            normalized,
            shape=source_shape,
            detector=detector,
            scan_source="triggerscope-raster-legacy",
            source="triggerscope-raster-legacy-assembled",
        )
    return _adapt_frame_scan(
        normalized,
        shape=shape,
        detector=detector,
        source="triggerscope-raster-legacy",
    )


def adapt_snouty_metadata(
    attrs: Mapping[str, Any],
    *,
    shape: Sequence[int],
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    normalized = _normalized_attrs(attrs)
    cycles = _positive_int(normalized.get("MS-RESOLFT_Scan:cycleSteps"))
    planes = _positive_int(normalized.get("MS-RESOLFT_Scan:roSteps"))
    if cycles is None or planes is None:
        return None
    source_shape = _shape_tuple(shape)
    if source_shape is None or len(source_shape) < 3:
        return None
    observed = source_shape[0]
    timepoints = _positive_int(normalized.get("recording:num_timepoints")) or 1
    per_partition = cycles * planes
    partitions: tuple[AcquisitionPartition, ...] = ()
    loops: list[AcquisitionLoop] = []
    if observed == per_partition * timepoints and timepoints > 1:
        loops.append(AcquisitionLoop("time", "time", timepoints))
    elif observed == per_partition:
        if timepoints > 1:
            partitions = (
                AcquisitionPartition(
                    "time",
                    index=max(0, int(normalized.get("recording:lapse_index", 0) or 0)),
                    planned_count=timepoints,
                    storage=(
                        "one-group-per-item"
                        if bool(normalized.get("recording:single_lapse_file"))
                        else "one-file-per-item"
                    ),
                ),
            )
    else:
        raise AcquisitionLayoutResolutionError(
            "SNOUTY cycle/plane metadata does not match the stored frame count",
            (
                _issue(
                    "error",
                    "SNOUTY_FRAME_COUNT_MISMATCH",
                    f"Expected {per_partition} or {per_partition * timepoints} frames, found {observed}",
                    "shape",
                ),
            ),
        )
    # Same loop, same pitch, as the producer: the cycle DAC steps by
    # cycleStepSizeUm and the readout by roStepSizeUm. This adapter used to put
    # the cycle pitch on the plane loop, so a consumer reading loop.step from
    # a legacy file and from a fresh recording got different geometries.
    cycle_step = _number(normalized.get("MS-RESOLFT_Scan:cycleStepSizeUm"))
    plane_step = _number(normalized.get("MS-RESOLFT_Scan:roStepSizeUm"))
    loops.extend(
        (
            AcquisitionLoop(
                "cycle",
                "cycle",
                cycles,
                step=cycle_step,
                unit="um" if cycle_step is not None else None,
            ),
            AcquisitionLoop(
                "plane",
                "plane",
                planes,
                step=plane_step,
                unit="um" if plane_step is not None else None,
            ),
        )
    )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector=detector,
        storage_axes=_default_storage_axes(source_shape),
        event_loops=tuple(loops),
        partitions=partitions,
        modality="snouty",
        scan_source="triggerscope-resolft-legacy",
        provenance="legacy-adapter",
    )
    return _validated_result(
        layout,
        source="snouty-legacy",
        shape=source_shape,
        issues=(
            _issue(
                "warning",
                "LEGACY_SNOUTY_ORDER_ASSUMPTION",
                "Cycle/plane order was adapted from the legacy SNOUTY contract",
                "MS-RESOLFT_Scan",
            ),
        ),
    )


def adapt_tiling_manifest(
    index: Any,
    *,
    detector: str | None = None,
) -> ResolvedAcquisitionLayout:
    """Adapt a parsed tiling manifest without reading any tile pixels."""
    tiles = tuple(getattr(index, "tiles", ()) or ())
    chosen_detector = detector or getattr(index, "alignment_detector", None) or "tile"
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
        detector=str(chosen_detector),
        storage_axes=("tile", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("tile", "tile", max(1, len(tiles)), storage_axis="tile"),
        ),
        partitions=(
            AcquisitionPartition("tile", planned_count=max(1, len(tiles)), storage="one-file-per-item"),
        ),
        modality="tiling",
        scan_source="tiling-manifest",
        provenance="legacy-adapter",
    )
    return _validated_result(
        layout,
        source="tiling-manifest",
        shape=None,
        issues=(
            _issue(
                "warning",
                "TILING_MANIFEST_ADAPTER",
                "Tile order and placement remain authoritative in the tiling manifest",
            ),
        ),
    )


def _ome_layout(
    *,
    axis_labels: Sequence[str] | None,
    shape: tuple[int, ...] | None,
    detector: str,
) -> ResolvedAcquisitionLayout | None:
    if axis_labels is None or shape is None or len(axis_labels) != len(shape):
        return None
    labels = tuple(str(label).strip().lower() for label in axis_labels)
    if not labels or any(not label for label in labels):
        return None
    recognized = {"t", "time", "c", "channel", "z", "y", "x"}
    if not any(label in recognized for label in labels):
        return None
    role_for = {
        "t": "frame",
        "time": "frame",
        "c": "channel",
        "channel": "channel",
        "z": "scan_z",
        "y": "detector_y",
        "x": "detector_x",
    }
    kind_for = {
        "t": "time",
        "time": "time",
        "c": "channel",
        "channel": "channel",
        "z": "scan_z",
    }
    storage_axes = tuple(role_for.get(label, f"dim_{index}") for index, label in enumerate(labels))
    only_time_frames = all(label in {"t", "time", "y", "x"} for label in labels) and any(
        label in {"t", "time"} for label in labels
    )
    loops = []
    for index, label in enumerate(labels):
        if label not in kind_for:
            continue
        loops.append(
            AcquisitionLoop(
                id=kind_for[label],
                kind=kind_for[label],
                count=shape[index],
                storage_axis=None if only_time_frames else storage_axes[index],
            )
        )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM if only_time_frames else PAYLOAD_ASSEMBLED_IMAGE,
        detector=detector,
        storage_axes=storage_axes,
        event_loops=tuple(loops),
        provenance="ome-ngff",
    )
    return _validated_result(
        layout,
        source="ome-ngff",
        shape=shape,
        issues=(
            _issue(
                "warning",
                "OME_AXES_WITHOUT_ACQUISITION_LOOPS",
                "Container axes were used because no acquisition-loop contract was recorded",
                "axis_labels",
            ),
        ),
    )


def _shape_fallback(shape: tuple[int, ...], detector: str) -> ResolvedAcquisitionLayout:
    storage_axes = _default_storage_axes(shape)
    if len(shape) <= 2:
        layout = AcquisitionLayout(
            schema=ACQUISITION_LAYOUT_SCHEMA,
            payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
            detector=detector,
            storage_axes=storage_axes,
            event_loops=(),
            provenance="shape-inference",
        )
        return _validated_result(
            layout,
            source="shape-inference",
            shape=shape,
            issues=(
                _issue(
                    "warning",
                    "SHAPE_ONLY_IMAGE",
                    "Only array rank is known; no acquisition loops were inferred",
                    "shape",
                ),
            ),
        )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector=detector,
        storage_axes=storage_axes,
        event_loops=(AcquisitionLoop("frame", "repeat", max(1, shape[0])),),
        provenance="generic-fallback",
    )
    return _validated_result(
        layout,
        source="generic-fallback",
        shape=shape,
        issues=(
            _issue(
                "warning",
                "GENERIC_FRAME_STREAM",
                "No acquisition semantics were found; the leading axis remains generic Frame",
                "shape",
            ),
        ),
    )


def resolve_acquisition_layout(
    attrs: Mapping[str, Any] | None,
    *,
    shape: Sequence[int],
    detector: str,
    axis_labels: Sequence[str] | None = None,
    axis_metadata_explicit: bool = False,
    user_override: AcquisitionLayout | str | bytes | bytearray | None = None,
    source_path: str | Path | None = None,
    dataset_path: str | None = None,
    fingerprint: str | None = None,
) -> ResolvedAcquisitionLayout:
    """Resolve one image source using the fixed contract precedence."""
    normalized = _normalized_attrs(attrs)
    source_shape = _shape_tuple(shape)
    if source_shape is None:
        raise ValueError("shape must contain non-negative integers")
    detector = str(detector or "unknown")

    allow_frame_mismatch = (
        _text(normalized.get("recording:completion_outcome")) == "stopped_early"
        or _boolean(normalized.get("writing")) is True
    )
    explicit_layout, explicit_issues = _decode_explicit(
        normalized,
        source_shape,
        allow_frame_mismatch=allow_frame_mismatch,
    )
    sidecar_override, sidecar_issues = _load_sidecar_override(
        source_path,
        detector=detector,
        dataset_path=dataset_path,
        fingerprint=fingerprint,
    )
    override_value = user_override if user_override is not None else sidecar_override
    if override_value is not None:
        try:
            override = (
                override_value
                if isinstance(override_value, AcquisitionLayout)
                else decode_acquisition_layout(override_value)
            )
            override = replace(override, provenance="user-override")
        except Exception as exc:
            issues = tuple(getattr(exc, "issues", ())) or (
                _issue("error", "INVALID_USER_OVERRIDE", str(exc), "user_override"),
            )
            raise AcquisitionLayoutResolutionError("User layout override is invalid", issues) from exc
        recovery_issues = list(sidecar_issues)
        recovery_issues.extend(explicit_issues)
        if explicit_layout is not None:
            recovery_issues.append(
                _issue(
                    "warning",
                    "USER_OVERRIDE_REPLACES_LAYOUT",
                    "A validated user override replaces the source acquisition layout",
                    "user_override",
                )
            )
        elif _errors(explicit_issues):
            recovery_issues.append(
                _issue(
                    "warning",
                    "USER_OVERRIDE_RECOVERS_INVALID_LAYOUT",
                    "A validated user override replaces invalid explicit source metadata",
                    "user_override",
                )
            )
        # Recovered explicit errors are retained as provenance diagnostics but
        # demoted so the validated override can proceed.
        recovery_issues = [
            replace(issue, severity="warning") if issue.severity == "error" else issue
            for issue in recovery_issues
        ]
        return _validated_result(
            override,
            source="user-override-sidecar" if user_override is None else "user-override",
            shape=source_shape,
            issues=(*recovery_issues, *_projection_issues(override, axis_labels)),
            replaced_layout=explicit_layout,
            allow_frame_mismatch=allow_frame_mismatch,
        )

    if explicit_layout is not None or _errors(explicit_issues):
        errors = _errors(explicit_issues)
        if errors or explicit_layout is None:
            raise AcquisitionLayoutResolutionError(
                "Explicit acquisition layout is invalid; automatic fallback is forbidden",
                errors or explicit_issues,
            )
        return _validated_result(
            explicit_layout,
            source="explicit-metadata",
            shape=source_shape,
            issues=(*sidecar_issues, *explicit_issues, *_projection_issues(explicit_layout, axis_labels)),
            allow_frame_mismatch=allow_frame_mismatch,
        )

    # A layout this version cannot interpret still has to be reported, whatever
    # the fallback chain settles on.
    sidecar_issues = (*sidecar_issues, *explicit_issues)

    for adapter in (
        adapt_advanced_scan_metadata,
        adapt_snouty_metadata,
        adapt_monalisa_scan_metadata,
        adapt_triggerscope_raster_metadata,
        # Least specific: only stage geometry, no modality claim. Last so a
        # file that any modality adapter recognises keeps that reading.
        adapt_scan_stage_metadata,
    ):
        adapted = adapter(normalized, shape=source_shape, detector=detector)
        if adapted is not None:
            return replace(adapted, issues=(*sidecar_issues, *adapted.issues))

    if axis_metadata_explicit:
        ome = _ome_layout(axis_labels=axis_labels, shape=source_shape, detector=detector)
        if ome is not None:
            return replace(ome, issues=(*sidecar_issues, *ome.issues))

    fallback = _shape_fallback(source_shape, detector)
    return replace(fallback, issues=(*sidecar_issues, *fallback.issues))
