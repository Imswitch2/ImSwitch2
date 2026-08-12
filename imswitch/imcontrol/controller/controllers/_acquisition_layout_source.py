"""Producer-side acquisition layout protocol and pure layout builders."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLayoutError,
    AcquisitionLoop,
    AcquisitionPartition,
    RecordedEventSpan,
    TraversalRule,
    validate_acquisition_layout,
)


@runtime_checkable
class AcquisitionLayoutSource(Protocol):
    """Optional capability implemented by scan controllers with exact layouts."""

    def getAcquisitionLayouts(
        self, detectorNames: Sequence[str]
    ) -> Mapping[str, AcquisitionLayout]:
        """Return one producer-authored layout for every requested detector."""
        ...


def _validated(layout: AcquisitionLayout) -> AcquisitionLayout:
    errors = tuple(
        issue
        for issue in validate_acquisition_layout(layout)
        if issue.severity == "error"
    )
    if errors:
        raise AcquisitionLayoutError(
            f"Producer created an invalid acquisition layout for {layout.detector!r}",
            errors,
        )
    return layout


def planned_frame_count(layout: AcquisitionLayout) -> int | None:
    """Return the planned writer-frame count for a frame-stream layout."""
    if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return None
    if layout.recorded_event_spans is not None:
        return sum(
            span.count * span.repeats for span in layout.recorded_event_spans
        )
    return math.prod(loop.count for loop in layout.event_loops)


def validate_detector_edge_counts(
    layouts: Mapping[str, AcquisitionLayout],
    ttl_signals: Mapping[str, Sequence[Any]],
) -> None:
    """Cross-check planned frame selections against final detector TTL edges."""
    for detector, layout in layouts.items():
        expected = planned_frame_count(layout)
        signal = ttl_signals.get(detector)
        if expected is None or signal is None:
            continue
        actual = 0
        previous = False
        for value in signal:
            current = bool(value)
            actual += int(current and not previous)
            previous = current
        if actual != expected:
            raise ValueError(
                f"Advanced detector {detector!r} layout selects {expected} "
                f"frame(s), but the final TTL signal has {actual} rising edge(s)"
            )


def scan_driven_detector_names(controller: Any, detector_names: Sequence[str]) -> tuple[str, ...]:
    """Return requested detectors whose manager emits one assembled scan."""
    managers = getattr(getattr(controller, "_master", None), "detectorsManager", None)
    result = []
    for raw_name in detector_names:
        name = str(raw_name)
        try:
            manager = managers[name]
        except Exception:
            continue
        if bool(getattr(manager, "isScanDriven", False)):
            result.append(name)
    return tuple(result)


def scan_devices(controller: Any, scan_info: Mapping[str, Any]) -> dict[str, str]:
    """Map each ScanInfo physical axis to the positioner that drove it.

    The layout records this as provenance: ``kind`` says an axis is the fast
    one, ``device`` says which stage moved. It is the one thing the legacy
    ``ScanStage:target_device`` attribute carried that the layout could not.
    """
    axes = tuple(scan_info.get("img_axes_phys", ()))
    named = tuple(scan_info.get("axis_names", ()))
    parameter_devices = tuple(
        getattr(controller, "_analogParameterDict", {}).get(
            "target_device", ()
        )
    )
    positioners = getattr(getattr(controller, "_setupInfo", None), "positioners", {})
    devices = {}
    for index, axis in enumerate(axes):
        candidates = []
        if index < len(named):
            candidates.append(named[index])
        if index < len(parameter_devices):
            candidates.append(parameter_devices[index])
        device = next(
            (candidate for candidate in candidates if candidate in positioners),
            None,
        )
        if device is not None:
            devices[_kind(axis, index)] = str(device)
    return devices


def scan_directions(controller: Any, scan_info: Mapping[str, Any]) -> dict[str, int]:
    """Map ScanInfo physical axes to configured positioner directions."""
    positioners = getattr(getattr(controller, "_setupInfo", None), "positioners", {})
    directions = {}
    for kind, device in scan_devices(controller, scan_info).items():
        try:
            positive = bool(positioners[device].isPositiveDirection)
        except Exception:
            continue
        directions[kind] = 1 if positive else -1
    return directions


def build_controller_point_scan_layouts(
    controller: Any, detector_names: Sequence[str]
) -> dict[str, AcquisitionLayout]:
    """Generate signals and adapt an ordinary point-scan controller."""
    controller.getParameters()
    result = controller._master.scanManager.makeFullScan(
        controller._analogParameterDict,
        controller._digitalParameterDict,
    )
    if not result or len(result) != 2 or result[1] is None:
        raise RuntimeError("Scan signal generation did not produce ScanInfoContract metadata")
    _, scan_info = result
    return build_point_scan_layouts(
        scan_info,
        detector_names,
        scan_source=type(controller).__name__,
        pulse_counts=controller.getNumCamTTL(),
        scan_driven_detectors=scan_driven_detector_names(controller, detector_names),
        directions=scan_directions(controller, scan_info),
        devices=scan_devices(controller, scan_info),
    )


def _kind(axis: Any, index: int) -> str:
    label = str(axis or "").strip().lower()
    if label in {"x", "scan_x"}:
        return "scan_x"
    if label in {"y", "scan_y"}:
        return "scan_y"
    if label in {"z", "scan_z"}:
        return "scan_z"
    return f"scan_axis_{index}"


def _physical_loops(
    scan_info: Mapping[str, Any],
    *,
    directions: Mapping[str, int] | None = None,
    storage: bool = False,
    devices: Mapping[str, str] | None = None,
) -> tuple[AcquisitionLoop, ...]:
    dims = tuple(int(value) for value in scan_info.get("img_dims", ()))
    axes = tuple(scan_info.get("img_axes_phys", ()))
    steps = tuple(scan_info.get("pixel_sizes", ()))
    if not dims or len(axes) != len(dims) or any(value <= 0 for value in dims):
        raise ValueError("ScanInfoContract must provide positive physical axes and dimensions")

    controller_order = []
    seen = set()
    for index, (axis, count) in enumerate(zip(axes, dims)):
        kind = _kind(axis, index)
        if kind in seen:
            raise ValueError(f"ScanInfoContract repeats semantic axis {kind!r}")
        seen.add(kind)
        step = None
        if index < len(steps):
            try:
                step = abs(float(steps[index]))
            except (TypeError, ValueError):
                step = None
        direction = (directions or {}).get(kind)
        controller_order.append(
            AcquisitionLoop(
                id=kind,
                kind=kind,
                count=count,
                step=step,
                unit="um" if step is not None else None,
                direction=direction,
                storage_axis=kind if storage else None,
                device=(devices or {}).get(kind),
            )
        )
    # ScanInfoContract is fast-to-slow (X/Y/Z); event chronology is the
    # corresponding outer-to-inner order (Z/Y/X).
    return tuple(reversed(controller_order))


def _traversal(loops: Sequence[AcquisitionLoop]) -> tuple[TraversalRule, ...]:
    return tuple(
        TraversalRule(
            loop.id,
            "reverse" if loop.direction == -1 else "forward",
        )
        for loop in loops
    )


def _assembled_layout(
    *,
    detector: str,
    loops: Sequence[AcquisitionLoop],
    storage_axes: Sequence[str],
    scan_source: str,
    modality: str | None = None,
) -> AcquisitionLayout:
    mapped = tuple(replace(loop, storage_axis=loop.kind) for loop in loops)
    return _validated(
        AcquisitionLayout(
            schema=ACQUISITION_LAYOUT_SCHEMA,
            payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
            detector=detector,
            storage_axes=tuple(storage_axes),
            event_loops=mapped,
            traversal=_traversal(mapped),
            modality=modality,
            scan_source=scan_source,
            provenance="recorded",
        )
    )


def build_point_scan_layouts(
    scan_info: Mapping[str, Any],
    detector_names: Sequence[str],
    *,
    scan_source: str,
    pulse_counts: Mapping[str, int] | None = None,
    scan_driven_detectors: Sequence[str] = (),
    directions: Mapping[str, int] | None = None,
    devices: Mapping[str, str] | None = None,
    modality: str | None = None,
) -> dict[str, AcquisitionLayout]:
    """Build detector-local layouts for MoNaLISA and ordinary point scans."""
    physical = _physical_loops(scan_info, directions=directions, devices=devices)
    scan_driven = set(scan_driven_detectors)
    pulse_counts = pulse_counts or {}
    layouts = {}
    for raw_detector in detector_names:
        detector = str(raw_detector)
        if detector in scan_driven:
            layouts[detector] = _assembled_layout(
                detector=detector,
                loops=physical,
                storage_axes=("frame", *(loop.kind for loop in physical)),
                scan_source=scan_source,
                modality=modality,
            )
            continue
        repeats = max(1, int(pulse_counts.get(detector, 1)))
        loops = physical
        if repeats > 1:
            loops = (*loops, AcquisitionLoop("repeat", "repeat", repeats))
        layouts[detector] = _validated(
            AcquisitionLayout(
                schema=ACQUISITION_LAYOUT_SCHEMA,
                payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
                detector=detector,
                storage_axes=("frame", "detector_y", "detector_x"),
                event_loops=loops,
                traversal=_traversal(loops),
                modality=modality,
                scan_source=scan_source,
                provenance="recorded",
            )
        )
    return layouts


def _condition_loop(count: int, *, storage: bool = False) -> AcquisitionLoop:
    return AcquisitionLoop(
        "condition",
        "condition",
        count,
        labels=tuple(f"condition_{index}" for index in range(count)),
        storage_axis="condition" if storage else None,
    )


def _insert_condition(
    physical: Sequence[AcquisitionLoop], condition: AcquisitionLoop
) -> tuple[AcquisitionLoop, ...]:
    loops = list(physical)
    x_index = next(
        (index for index, loop in enumerate(loops) if loop.kind == "scan_x"),
        len(loops),
    )
    loops.insert(x_index, condition)
    return tuple(loops)


def _advanced_spans(
    *,
    physical: Sequence[AcquisitionLoop],
    condition_count: int,
    mask: Sequence[bool],
    pulse_counts: Sequence[int],
    repeat_count: int,
) -> tuple[RecordedEventSpan, ...]:
    x_count = next(
        (loop.count for loop in physical if loop.kind == "scan_x"),
        1,
    )
    y_count = next(
        (loop.count for loop in physical if loop.kind == "scan_y"),
        1,
    )
    row_count = math.prod(
        loop.count for loop in physical if loop.kind != "scan_x"
    )
    if len(mask) not in {condition_count, condition_count * y_count}:
        raise ValueError(
            "Advanced detector mask must be per-condition or per-expanded-Y-line"
        )

    spans = []
    for row in range(row_count):
        y_index = row % y_count
        for condition in range(condition_count):
            enabled = (
                bool(mask[condition])
                if len(mask) == condition_count
                else bool(mask[y_index * condition_count + condition])
            )
            selected_repeats = pulse_counts[condition] if enabled else 0
            if selected_repeats <= 0:
                continue
            start = (
                (row * condition_count + condition) * x_count * repeat_count
            )
            if selected_repeats == repeat_count:
                spans.append(
                    RecordedEventSpan(start=start, count=x_count * repeat_count)
                )
            else:
                spans.append(
                    RecordedEventSpan(
                        start=start,
                        count=selected_repeats,
                        period=repeat_count,
                        repeats=x_count,
                    )
                )
    return tuple(spans)


def build_advanced_scan_layouts(
    scan_info: Mapping[str, Any],
    detector_names: Sequence[str],
    *,
    scan_source: str,
    detector_masks: Mapping[str, Sequence[bool]],
    pulse_counts_by_condition: Mapping[str, Sequence[int]],
    scan_driven_detectors: Sequence[str] = (),
    directions: Mapping[str, int] | None = None,
    devices: Mapping[str, str] | None = None,
    modality: str | None = None,
) -> dict[str, AcquisitionLayout]:
    """Build exact Advanced Scan layouts from line programs and pulse counts."""
    physical = _physical_loops(scan_info, directions=directions, devices=devices)
    condition_count = max(1, int(scan_info.get("n_linesteps", 1)))
    scan_driven = set(scan_driven_detectors)
    layouts = {}
    for raw_detector in detector_names:
        detector = str(raw_detector)
        if detector in scan_driven:
            loops = physical
            storage_axes = ["frame"]
            if condition_count > 1:
                loops = _insert_condition(
                    physical, _condition_loop(condition_count, storage=True)
                )
                storage_axes.append("condition")
            storage_axes.extend(loop.kind for loop in physical)
            layouts[detector] = _assembled_layout(
                detector=detector,
                loops=loops,
                storage_axes=storage_axes,
                scan_source=scan_source,
                modality=modality,
            )
            continue

        mask = detector_masks.get(detector)
        counts = pulse_counts_by_condition.get(detector)
        if mask is None or counts is None:
            if condition_count > 1:
                # Falling back to a plain X/Y layout would drop the line-step
                # dimension silently: a 648-frame 18x18x2 scan gets described
                # as 324 events, and the recording is then either refused with
                # a message about pulse counts or read as half a scan. A
                # producer that cannot describe a detector has to say so
                # rather than approximate it.
                raise ValueError(
                    f"This scan has {condition_count} line steps, but detector "
                    f"{detector!r} has no line-step enable mask or pulse count, "
                    f"so the order of its frames cannot be described. Enable "
                    f"the detector's TTL for this scan, or record it with a "
                    f"single-line-step scan."
                )
            layouts.update(
                build_point_scan_layouts(
                    scan_info,
                    (detector,),
                    scan_source=scan_source,
                    directions=directions,
                    devices=devices,
                    modality=modality,
                )
            )
            continue
        counts = tuple(int(value) for value in counts)
        if len(counts) != condition_count or any(value < 0 for value in counts):
            raise ValueError(
                f"Advanced pulse counts for {detector!r} must match n_linesteps"
            )
        repeat_count = max(counts, default=0)
        if repeat_count <= 0 or not any(bool(value) for value in mask):
            raise ValueError(
                f"Advanced detector {detector!r} has no recorded pulse in this scan"
            )
        loops = physical
        if condition_count > 1:
            loops = _insert_condition(loops, _condition_loop(condition_count))
        if repeat_count > 1:
            loops = (*loops, AcquisitionLoop("repeat", "repeat", repeat_count))
        spans = _advanced_spans(
            physical=physical,
            condition_count=condition_count,
            mask=tuple(bool(value) for value in mask),
            pulse_counts=counts,
            repeat_count=repeat_count,
        )
        layouts[detector] = _validated(
            AcquisitionLayout(
                schema=ACQUISITION_LAYOUT_SCHEMA,
                payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
                detector=detector,
                storage_axes=("frame", "detector_y", "detector_x"),
                event_loops=loops,
                traversal=_traversal(loops),
                recorded_event_spans=spans,
                modality=modality,
                scan_source=scan_source,
                provenance="recorded",
            )
        )
    return layouts


def build_triggerscope_raster_layouts(
    detector_names: Sequence[str],
    *,
    dimensions: Sequence[int],
    step_sizes: Sequence[float],
    scan_source: str,
    scan_driven_detectors: Sequence[str] = (),
    directions: Mapping[str, int] | None = None,
) -> dict[str, AcquisitionLayout]:
    dims = tuple(max(1, int(value)) for value in dimensions[:2])
    if len(dims) != 2:
        raise ValueError("TriggerScope raster requires X and Y dimensions")
    info = {
        "img_dims": dims,
        "img_axes_phys": ("x", "y"),
        "pixel_sizes": tuple(step_sizes[:2]),
    }
    return build_point_scan_layouts(
        info,
        detector_names,
        scan_source=scan_source,
        scan_driven_detectors=scan_driven_detectors,
        directions=directions,
    )


def build_triggerscope_resolft_layouts(
    detector_names: Sequence[str],
    *,
    scan_parameters: Mapping[str, Any],
    scan_source: str,
    scan_driven_detectors: Sequence[str] = (),
) -> dict[str, AcquisitionLayout]:
    """Build the established firmware order: time outer, plane innermost."""

    def count(key: str) -> int:
        try:
            return max(1, int(float(scan_parameters.get(key, 1))))
        except (TypeError, ValueError):
            return 1

    def step(key: str) -> float | None:
        try:
            return abs(float(scan_parameters[key]))
        except (KeyError, TypeError, ValueError):
            return None

    loops = (
        AcquisitionLoop("time", "time", count("timeLapsePoints")),
        AcquisitionLoop(
            "cycle",
            "cycle",
            count("cycleSteps"),
            step=step("cycleStepSizeUm"),
            unit="um" if step("cycleStepSizeUm") is not None else None,
        ),
        AcquisitionLoop(
            "plane",
            "plane",
            count("roSteps"),
            step=step("roStepSizeUm"),
            unit="um" if step("roStepSizeUm") is not None else None,
        ),
    )
    scan_driven = set(scan_driven_detectors)
    layouts = {}
    for raw_detector in detector_names:
        detector = str(raw_detector)
        if detector in scan_driven:
            layouts[detector] = _assembled_layout(
                detector=detector,
                loops=loops,
                storage_axes=("frame", "time", "cycle", "plane"),
                scan_source=scan_source,
                modality="snouty",
            )
        else:
            layouts[detector] = _validated(
                AcquisitionLayout(
                    schema=ACQUISITION_LAYOUT_SCHEMA,
                    payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
                    detector=detector,
                    storage_axes=("frame", "detector_y", "detector_x"),
                    event_loops=loops,
                    traversal=_traversal(loops),
                    modality="snouty",
                    scan_source=scan_source,
                    provenance="recorded",
                )
            )
    return layouts


def with_time_partition(
    layout: AcquisitionLayout,
    *,
    index: int,
    planned_count: int | None,
    single_file: bool,
) -> AcquisitionLayout:
    """Scope a reusable per-scan layout to one external lapse partition."""
    partitions = tuple(
        partition for partition in layout.partitions if partition.kind != "time"
    ) + (
        AcquisitionPartition(
            "time",
            index=int(index),
            planned_count=(
                int(planned_count) if planned_count is not None else None
            ),
            storage=(
                "one-group-per-item" if single_file else "one-file-per-item"
            ),
        ),
    )
    return _validated(replace(layout, partitions=partitions))
