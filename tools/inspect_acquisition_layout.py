#!/usr/bin/env python
"""Print how ImSwitch interprets a recording's acquisition metadata.

Run this on a file straight off the rig to see what the acquisition layout
says before opening it in ImProcess. It reads only metadata -- no pixels are
loaded -- so it is safe on large recordings and fast on slow storage.

    python tools/inspect_acquisition_layout.py <path> [detector]

The frame table is the point: it maps stored frame indices to the coordinates
a reconstructor will use. If the first rows do not match what the hardware
actually did, the layout is wrong and every reconstruction from that file will
be confidently wrong in the same way.
"""

from __future__ import annotations

import argparse
import sys


def _describe(resolved) -> None:
    layout = resolved.layout
    print(f"  source      : {resolved.source}")
    print(f"  confidence  : {resolved.confidence}")
    print(f"  provenance  : {layout.provenance}")
    print(f"  usable      : {resolved.is_usable}   (geometry may be taken from it)")
    print(f"  authoritative: {resolved.is_authoritative}   (may refuse a reconstruction)")
    print(f"  payload     : {layout.payload_kind}")
    print(f"  detector    : {layout.detector}")
    print(f"  modality    : {layout.modality}    scan source: {layout.scan_source}")
    print(f"  storage axes: {list(layout.storage_axes)}")

    print("  event loops (outermost first):")
    for loop in layout.event_loops:
        pitch = f"{loop.step} {loop.unit}" if loop.step else "-"
        extra = []
        if loop.device:
            extra.append(f"device={loop.device}")
        if loop.direction is not None:
            extra.append(f"direction={loop.direction:+d}")
        if loop.labels:
            extra.append(f"labels={list(loop.labels)}")
        suffix = ("  " + "  ".join(extra)) if extra else ""
        print(f"    {loop.kind:<12} count={loop.count:<6} pitch={pitch:<12}{suffix}")

    if layout.traversal:
        print("  traversal:")
        for rule in layout.traversal:
            parity = f" parity over {list(rule.parity_loops)}" if rule.parity_loops else ""
            print(f"    {rule.loop_id}: {rule.order}{parity}")

    if layout.recorded_event_spans is not None:
        print(f"  detector gating: {len(layout.recorded_event_spans)} recorded span(s)")
        for span in layout.recorded_event_spans[:4]:
            print(f"    start={span.start} count={span.count} stride={span.stride} "
                  f"period={span.period} repeats={span.repeats}")

    for partition in layout.partitions:
        print(f"  partition   : {partition.kind} index={partition.index} "
              f"of {partition.planned_count}  storage={partition.storage}")

    if resolved.issues:
        print("  issues:")
        for issue in resolved.issues:
            print(f"    [{issue.severity}] {issue.code}: {issue.message}")
    else:
        print("  issues      : none")


def _frame_table(layout, limit: int) -> None:
    """The check that matters: stored frame index -> logical coordinates."""
    from imswitch.imcommon.model.acquisition_layout import (
        PAYLOAD_DETECTOR_FRAME_STREAM,
        iter_recorded_coordinates,
    )

    if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        print("\n  (assembled payload: axes map directly to the array, no frame table)")
        return
    print(f"\n  first {limit} stored frames:")
    for index, coordinates in enumerate(iter_recorded_coordinates(layout)):
        if index >= limit:
            break
        pretty = "  ".join(f"{name}={value}" for name, value in coordinates.items())
        print(f"    frame {index:<5} -> {pretty}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="HDF5, Zarr or TIFF recording")
    parser.add_argument("detector", nargs="?", default=None,
                        help="dataset/detector name (default: the only one)")
    parser.add_argument("--frames", type=int, default=12,
                        help="how many stored frames to map (default 12)")
    args = parser.parse_args(argv)

    from imswitch.improcess.model import DataObj

    data_obj = DataObj(args.path, args.detector, path=args.path)
    print(f"\n{args.path}")
    try:
        resolved = data_obj.acquisition_layout
    except Exception as error:
        print(f"  RESOLUTION FAILED: {type(error).__name__}: {error}")
        for issue in getattr(error, "issues", ()):
            print(f"    [{issue.severity}] {issue.code}: {issue.message}")
        return 1

    _describe(resolved)
    try:
        _frame_table(resolved.layout, args.frames)
    except Exception as error:
        print(f"  frame table unavailable: {type(error).__name__}: {error}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
