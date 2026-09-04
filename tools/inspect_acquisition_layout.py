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
import os
import sys

# Bind to the ImSwitch this script ships with, not to whatever is installed.
# Python puts *this file's* directory on sys.path, never the working
# directory, so an editable install elsewhere on the machine wins by default
# -- and then the tool reports how *that* checkout would read the file, which
# is the one thing it must never do. A rig with several checkouts (a release
# install plus the branch under test) is the normal case, not the exotic one.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(_REPO_ROOT, "imswitch")):
    sys.path.insert(0, _REPO_ROOT)


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

    import imswitch
    from imswitch.improcess.model import DataObj

    print(f"\n{args.path}")
    # Which code is doing the interpreting. Two checkouts read the same file
    # differently -- that is the whole point of the layout work -- so an
    # output without this line cannot be attributed to a version.
    print(f"  interpreted by: {os.path.dirname(os.path.abspath(imswitch.__file__))}")
    if not hasattr(DataObj, "acquisition_layout"):
        print("  RESOLUTION FAILED: this ImSwitch predates the acquisition-layout "
              "contract, so it cannot report a layout at all.")
        print("  Run the tool from a checkout that has it, or install that "
              "checkout, and check the path printed above.")
        return 1

    if args.detector is not None:
        names = [args.detector]
    else:
        try:
            names = list(DataObj.getDatasetNames(args.path)) or [None]
        except Exception as error:
            print(f"  CANNOT LIST DATASETS: {type(error).__name__}: {error}")
            return 1
        if len(names) > 1:
            # A lapse recorded as one file holds one dataset per timepoint
            # (`scan0/Camera`, `scan1/Camera`), and each carries its own
            # partition. Reporting only the first would describe a fraction of
            # the recording as if it were the whole of it.
            print(f"  datasets    : {len(names)} -> {', '.join(map(str, names))}")

    failures = 0
    for name in names:
        if len(names) > 1:
            print(f"\n  --- {name} ---")
        try:
            resolved = DataObj(args.path, name, path=args.path).acquisition_layout
        except Exception as error:
            print(f"  RESOLUTION FAILED: {type(error).__name__}: {error}")
            for issue in getattr(error, "issues", ()):
                print(f"    [{issue.severity}] {issue.code}: {issue.message}")
            failures += 1
            continue

        _describe(resolved)
        try:
            _frame_table(resolved.layout, args.frames)
        except Exception as error:
            print(f"  frame table unavailable: {type(error).__name__}: {error}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
