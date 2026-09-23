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
    print(f"  usable      : {resolved.is_usable}"
          f"   ({'geometry may be taken from it' if resolved.is_usable else 'geometry must not be taken from it'})")
    print(f"  authoritative: {resolved.is_authoritative}"
          f"   ({'may refuse a reconstruction' if resolved.is_authoritative else 'cannot refuse a reconstruction'})")
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


def _describe_empty_container(path: str) -> None:
    """Report what a container with no dataset says about itself."""
    attrs = {}
    try:
        import h5py

        if h5py.is_hdf5(path):
            with h5py.File(path, "r") as file:
                attrs = dict(file.attrs)
    except Exception:
        attrs = {}
    if not attrs:
        try:
            import zarr

            attrs = dict(zarr.open(path, mode="r").attrs)
        except Exception:
            attrs = {}
    recording = {
        key: value for key, value in attrs.items()
        if isinstance(key, str) and key.startswith("recording:")
    }
    if not recording:
        return
    print("  the container describes itself as:")
    for key in sorted(recording):
        value = recording[key]
        if isinstance(value, bytes):
            value = value.decode()
        print(f"    {key} = {value}")


def _describe_lifecycle(data_obj) -> None:
    """Whether the recording finished, and how much of the plan it holds.

    Without this the frame table below is describing a plan, and a recording
    that stopped early looks exactly like one that completed.
    """
    try:
        lifecycle = data_obj.recording_lifecycle
    except Exception as error:
        print(f"  lifecycle   : unavailable ({type(error).__name__})")
        return
    if lifecycle is None:
        return
    parts = [f"writer={lifecycle.writer_state}"]
    if lifecycle.completion_outcome:
        parts.append(f"outcome={lifecycle.completion_outcome}")
    if lifecycle.actual_frames is not None or lifecycle.planned_frames is not None:
        parts.append(
            f"frames={lifecycle.actual_frames} of {lifecycle.planned_frames}"
        )
    if lifecycle.actual_partitions is not None:
        parts.append(
            f"partitions={lifecycle.actual_partitions} of "
            f"{lifecycle.planned_partitions}"
        )
    print(f"  lifecycle   : {'  '.join(parts)}")
    for issue in lifecycle.issues:
        print(f"    [{issue.severity}] {issue.code}: {issue.message}")


def _stored_frame_count(data_obj, layout) -> int | None:
    """Frames actually in the container, from the array's shape alone."""
    if not layout.storage_axes or layout.storage_axes[0] != "frame":
        return None
    try:
        shape = data_obj.data.shape
    except Exception:
        return None
    return int(shape[0]) if shape else None


def _frame_table(layout, limit: int, stored: int | None = None) -> None:
    """The check that matters: stored frame index -> logical coordinates."""
    from imswitch.imcommon.model.acquisition_layout import (
        PAYLOAD_DETECTOR_FRAME_STREAM,
        iter_recorded_coordinates,
    )

    if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        print("\n  (assembled payload: axes map directly to the array, no frame table)")
        return

    coordinates = list(iter_recorded_coordinates(layout))
    planned = len(coordinates)
    # The layout describes the scan that was planned. When the recording
    # stopped early the file holds fewer frames than that, and printing the
    # remainder under the heading "stored frames" states that data exists which
    # does not -- for the one output a rig session is told to trust.
    held = planned if stored is None else min(stored, planned)

    print(f"\n  first {min(limit, held)} stored frames:")
    for index, coordinate in enumerate(coordinates[:held]):
        if index >= limit:
            break
        pretty = "  ".join(f"{name}={value}" for name, value in coordinate.items())
        print(f"    frame {index:<5} -> {pretty}")

    if stored is not None and stored > planned:
        print(f"    ({stored - planned} frame(s) beyond the {planned} the layout "
              f"describes are stored and unaccounted for)")
    elif held < planned:
        missing = coordinates[held:]
        preview = "; ".join(
            "  ".join(f"{name}={value}" for name, value in coordinate.items())
            for coordinate in missing[:3]
        )
        more = "" if len(missing) <= 3 else f", and {len(missing) - 3} more"
        print(f"    ({len(missing)} planned frame(s) were never recorded: "
              f"{preview}{more})")


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

    try:
        available = list(DataObj.getDatasetNames(args.path))
    except Exception as error:
        print(f"  CANNOT LIST DATASETS: {type(error).__name__}: {error}")
        # A recording stopped before its first frame leaves a container with no
        # image in it. That is a legitimate outcome, not a corrupt file, and
        # the container says which recording it was: report that rather than
        # leaving the operator with an error and nothing else.
        _describe_empty_container(args.path)
        return 1

    if args.detector is not None:
        if available and args.detector not in available:
            # DataObj falls back to the only dataset when the requested name is
            # unknown, so a typo used to be answered with a different
            # detector's layout and a zero exit code -- the reading looks
            # authoritative and describes the wrong data.
            print(f"  NO SUCH DATASET: {args.detector!r}. This file holds: "
                  f"{', '.join(available)}")
            return 1
        names = [args.detector]
    else:
        names = available or [None]
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
        data_obj = DataObj(args.path, name, path=args.path)
        try:
            resolved = data_obj.acquisition_layout
        except Exception as error:
            print(f"  RESOLUTION FAILED: {type(error).__name__}: {error}")
            for issue in getattr(error, "issues", ()):
                print(f"    [{issue.severity}] {issue.code}: {issue.message}")
            failures += 1
            continue

        _describe(resolved)
        _describe_lifecycle(data_obj)
        try:
            _frame_table(
                resolved.layout,
                args.frames,
                _stored_frame_count(data_obj, resolved.layout),
            )
        except Exception as error:
            print(f"  frame table unavailable: {type(error).__name__}: {error}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
